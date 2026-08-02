"""Tests for payload captures filled by a supplier rather than by the anchor.

Three layers, and they are tested separately on purpose.

`hook_manifest` is the **trust boundary**: it decides which capture names may
exist, refuses a payload capture nothing declares, refuses one two things declare,
and kind-checks every value a supplier hands back before it renders. Those tests
are synthetic and tiny, because the rule they pin has nothing to do with Instagram.

`capture_supply` is the **derivation**: one deterministic rule and one agent seam,
behind a preference chain. Its unit tests use hand-written smali so that "exactly
one register is a dispatch chain" is tested as a rule, not as a fact about one APK.

Then the real decodes, which are the only place the rule's *reach* can be
measured. `RealDecodeSupplyTests` pins that it produces `LX/0Dxw;`/`p4` on 439 and
`LX/077N;`/`p3` on 430 — the values the shipped patches use. `Holdout340Tests` is
the half that matters more: the rule was derived from 430 and 439, and those are
NOT two independent confirmations, because both of its keys are consequences of
one architectural rewrite and both fail together below it. So 340 is asked whether
the supplier declines, and asked in a way that cannot pass by being unable to run:
the same supplier, on the same 340 method, with its two keys relaxed one at a
time, is shown to execute every step it has and still decline at the last one.

The real-decode tests skip when `work/` is absent, which is normal — `work/` is
gitignored. `INDEX_340` is built with
``tools/indexer/build_index.py work/340-holdout/decode --out work/index-340``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

from dfinsta_pipeline import capture_supply as cs
from dfinsta_pipeline.capture_supply import (
    AGENT,
    DISPATCH_MINIMUM,
    PROFILE_GUARD,
    STAGE_AMBIGUOUS_DISPATCH_REGISTER,
    STAGE_AMBIGUOUS_SELF_PROFILE_TYPE,
    STAGE_CANDIDATE_UNREADABLE,
    STAGE_DISPATCH_REGISTER_NOT_ANCHORED,
    STAGE_DRAWABLE_ABSENT,
    STAGE_DRAWABLE_NOT_INDEXED,
    STAGE_INCOMPLETE_PROPOSAL,
    STAGE_MALFORMED_VALUE,
    STAGE_MISSING_PARAM,
    STAGE_NO_DISPATCH_REGISTER,
    STAGE_NO_PROPOSAL,
    STAGE_NO_SELF_PROFILE_TYPE,
    STAGE_PRECONDITION_TYPE_ABSENT,
    STAGE_UNASKED_ROLE,
    STAGE_UNKNOWN_SUPPLIER,
    Supplied,
    SupplyRequest,
    decline,
    profile_action_bar_self_guard,
    run_supply_chain,
)
from dfinsta_pipeline.hook_index import HookIndex
from dfinsta_pipeline.hook_manifest import (
    AnchorHit,
    CaptureSupply,
    Hook,
    HostFingerprint,
    ManifestError,
    SuppliedCapture,
    find_anchor_hits,
    load_manifest,
    merge_supplied,
    resolve_in_source,
)
from dfinsta_pipeline.resolve import Outcome, resolve_hook

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "hooks.json"

DECODE_439 = ROOT / "work/439-explore/stock-439"
DECODE_430 = ROOT / "work/430-clean-build-v2/stock-430"
DECODE_340 = ROOT / "work/340-holdout/decode"
INDEX_439 = ROOT / "work/index-439"
INDEX_430 = ROOT / "work/index-430"
INDEX_340 = ROOT / "work/index-340"

PROFILE_ACTION_BAR = "Lcom/instagram/profile/actionbar/ProfileActionBar;"
SELF_DRAWABLE = "instagram_menu_outline_24"

#: The own-profile guard. These lines are now IN `manifest/hooks.json`; they are
#: kept here as the independent statement of what the shipped payload must
#: contain, so `test_the_shipped_payload_carries_exactly_this_guard` fails if the
#: manifest ever drifts from what these tests assume.
GUARD_LINES = (
    "    instance-of <l>, <model>, <selfprofile>",
    "",
    "    if-eqz <l>, :dfinsta_not_self_profile",
    "",
)

PROFILE_GUARD_SUPPLY = CaptureSupply(
    provides=(
        SuppliedCapture("model", "reg", "model_register"),
        SuppliedCapture("selfprofile", "type", "self_profile_type"),
    ),
    suppliers=(PROFILE_GUARD, AGENT),
    params=(("self_drawable", SELF_DRAWABLE), ("requires_type", PROFILE_ACTION_BAR)),
)


def guarded_settings_hook(**overrides: object) -> Hook:
    """The shipped settings hook, exactly as `manifest/hooks.json` declares it.

    This used to SPLICE the guard and the supply group into a shipped hook that
    had neither, standing in for a manifest entry a human had not yet approved.
    That entry is now shipped, so the splice has to go: applied to the current
    manifest it inserted a SECOND `instance-of`/`if-eqz` pair and a duplicate
    `:dfinsta_not_self_profile` label — a payload that would not assemble — and
    every test here went on passing, because they all ask `assertIn`. Reading the
    manifest directly removes the reconstruction, so these tests now exercise the
    bytes that ship.

    `overrides` is kept for the tests that deliberately deform the hook.
    """
    base = {hook.hook_id: hook for hook in load_manifest(MANIFEST)}[
        "install_settings_long_click"
    ]
    if not overrides:
        return base
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


# ----------------------------------------------------------------- tiny fixtures

PROBE_ANCHOR = ("const-string <r:reg>, \"probe\"",)
PROBE_PAYLOAD = (
    "    invoke-static {}, Lcom/dfinstagram/probe;->h_probe_hook()V",
    "    # dfinsta_probe",
    "    const-string <r>, \"probe\"",
)


def simple_hook(
    payload: Sequence[str] = PROBE_PAYLOAD,
    supplied: Sequence[CaptureSupply] = (),
    anchor: Sequence[str] = PROBE_ANCHOR,
) -> Hook:
    return Hook(
        hook_id="probe_hook",
        intent="probe",
        tier="robust",
        strategy="s",
        semantic_deps=(),
        hosts=(HostFingerprint("named", "Lcom/x/Y;"),),
        anchor=tuple(anchor),
        payload=tuple(payload),
        marker="# dfinsta_probe",
        expected_marker_count=1,
        supplied_captures=tuple(supplied),
    )


def one_reg_supply(
    name: str = "extra",
    kind: str = "reg",
    suppliers: Sequence[str] = ("stub",),
    role: str = "",
) -> CaptureSupply:
    return CaptureSupply(
        provides=(SuppliedCapture(name, kind, role),), suppliers=tuple(suppliers)
    )


class ManifestDeclarationTests(unittest.TestCase):
    """Which capture names a manifest may use, and who is allowed to declare them."""

    def test_payload_capture_declared_by_neither_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            simple_hook(payload=(*PROBE_PAYLOAD, "    move-object <ghost>, <r>"))
        self.assertIn("<ghost>", str(caught.exception))
        self.assertIn("no anchor line", str(caught.exception))

    def test_payload_capture_declared_by_a_supplier_is_accepted(self):
        hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
            supplied=(one_reg_supply(),),
        )
        self.assertEqual(hook.supplied_capture_names, ("extra",))

    def test_capture_declared_by_both_anchor_and_supplier_is_refused(self):
        # A merge would need a rule for which wins and there is none, so this is a
        # manifest bug. It is also the only way the two can meet: an anchor line
        # cannot reference a supplied capture without declaring a kind for it.
        with self.assertRaises(ManifestError) as caught:
            simple_hook(supplied=(one_reg_supply(name="r"),))
        message = str(caught.exception)
        self.assertIn("<r>", message)
        self.assertIn("captured by an anchor line AND", message)

    def test_two_supply_groups_may_not_declare_one_capture(self):
        with self.assertRaises(ManifestError) as caught:
            simple_hook(
                payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
                supplied=(one_reg_supply(), one_reg_supply(suppliers=("other",))),
            )
        self.assertIn("two supply groups", str(caught.exception))

    def test_supplied_capture_the_payload_never_uses_is_refused(self):
        # The shape of a dropped safety guard: declare the machinery for an
        # own-profile check, then render a payload without one.
        with self.assertRaises(ManifestError) as caught:
            simple_hook(supplied=(one_reg_supply(),))
        self.assertIn("declared but never used by the payload", str(caught.exception))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            SuppliedCapture("extra", "smali")
        self.assertIn("unknown kind", str(caught.exception))

    def test_unspellable_capture_name_is_refused(self):
        # A name the CAPTURE regex cannot match is a capture the payload can never
        # reference, so it would silently never render.
        for name in ("Extra", "9extra", "ex-tra", ""):
            with self.subTest(name=name), self.assertRaises(ManifestError):
                SuppliedCapture(name, "reg")

    def test_reserved_capture_name_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            SuppliedCapture("init", "reg")
        self.assertIn("reserved", str(caught.exception))

    def test_role_defaults_to_the_capture_name(self):
        self.assertEqual(SuppliedCapture("model", "reg").role, "model")
        self.assertEqual(SuppliedCapture("model", "reg", "model_register").role, "model_register")

    def test_empty_supplier_chain_is_refused(self):
        # With no supplier the hook could never resolve and would report
        # "awaiting" forever instead of escalating.
        with self.assertRaises(ManifestError) as caught:
            CaptureSupply(provides=(SuppliedCapture("x", "reg"),), suppliers=())
        self.assertIn("at least one supplier", str(caught.exception))

    def test_duplicate_supplier_in_a_chain_is_refused(self):
        with self.assertRaises(ManifestError):
            CaptureSupply(
                provides=(SuppliedCapture("x", "reg"),), suppliers=(PROFILE_GUARD, PROFILE_GUARD)
            )

    def test_duplicate_role_in_one_group_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            CaptureSupply(
                provides=(
                    SuppliedCapture("a", "reg", "same"),
                    SuppliedCapture("b", "reg", "same"),
                ),
                suppliers=("stub",),
            )
        self.assertIn("duplicate role", str(caught.exception))

    def test_shipped_manifest_declares_the_supply_group_on_one_hook(self):
        """Was: "nothing declares a supplied capture yet".

        That assertion pinned the state of an earlier slice, which added the
        mechanism and deliberately left the manifest entry to a human. The human
        approved it, so the assertion is inverted rather than deleted: it now pins
        WHICH hook declares a group and WHAT the group says, so an accidental
        second group, a renamed role, or a dropped supplier still fails here.
        """
        hooks = load_manifest(MANIFEST)
        self.assertEqual(len(hooks), 7)
        with_supply = [hook for hook in hooks if hook.supplied_captures]
        self.assertEqual(
            [hook.hook_id for hook in with_supply], ["install_settings_long_click"]
        )
        (hook,) = with_supply
        self.assertEqual(len(hook.supplied_captures), 1)
        supply = hook.supplied_captures[0]
        self.assertEqual(supply.names, ("model", "selfprofile"))
        self.assertEqual(
            [(item.name, item.kind, item.role) for item in supply.provides],
            [("model", "reg", "model_register"), ("selfprofile", "type", "self_profile_type")],
        )
        # deterministic rule first, agent as the fallback — never the reverse
        self.assertEqual(supply.suppliers, (PROFILE_GUARD, AGENT))
        self.assertEqual(
            supply.parameters,
            {"requires_type": PROFILE_ACTION_BAR, "self_drawable": SELF_DRAWABLE},
        )
        # and the flag that would short-circuit the whole supply path is off
        self.assertFalse(hook.requires_proposal)

    def test_the_shipped_payload_carries_exactly_this_guard(self):
        # The guard must appear ONCE. Twice would not assemble (duplicate label),
        # and no `assertIn` anywhere in this file would notice — which is exactly
        # what the old splicing fixture silently produced once the manifest gained
        # the guard of its own.
        hook = guarded_settings_hook()
        payload = list(hook.payload)
        for line in GUARD_LINES:
            if line:
                self.assertEqual(payload.count(line), 1, line)
        self.assertEqual(payload.count("    :dfinsta_not_self_profile"), 1)
        guard_at = payload.index(GUARD_LINES[0])
        label_at = payload.index("    :dfinsta_not_self_profile")
        attach_at = next(
            index for index, line in enumerate(payload) if "setOnLongClickListener" in line
        )
        tail_at = next(
            index for index, line in enumerate(payload) if "setLayoutParams" in line
        )
        # DFInsta lines inside the guard, stock tail outside it and last
        self.assertLess(guard_at, attach_at)
        self.assertLess(attach_at, label_at)
        self.assertLess(label_at, tail_at)
        self.assertTrue(payload[-1].strip().startswith("return-object"))

    def test_guarded_settings_hook_fixture_is_a_valid_hook(self):
        hook = guarded_settings_hook()
        self.assertEqual(hook.supplied_capture_names, ("model", "selfprofile"))
        self.assertFalse(hook.requires_proposal)


class MergeSuppliedTests(unittest.TestCase):
    """The render boundary. Everything here is a refusal, never a repair."""

    def setUp(self):
        self.hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
            supplied=(one_reg_supply(),),
        )

    def test_a_well_formed_value_merges(self):
        merged = merge_supplied(self.hook, {"r": "v1"}, {"extra": "v4"})
        self.assertEqual(merged, {"r": "v1", "extra": "v4"})

    def test_wrong_kind_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            merge_supplied(self.hook, {"r": "v1"}, {"extra": "LX/0Dxw;"})
        self.assertIn("declared 'reg'", str(caught.exception))

    def test_a_value_carrying_extra_smali_is_refused(self):
        # The whole point of typing a supplied capture: one of the two shipped
        # suppliers is an LLM, and `reg` must admit `v0` and nothing else.
        for value in (
            "v0}, LX/Evil;->go()V\n    invoke-static {",
            "v0 # comment",
            "v0, v1",
            "{v0 .. v3}",
            " v0",
            "v0\n",
        ):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                merge_supplied(self.hook, {"r": "v1"}, {"extra": value})

    def test_collision_with_an_anchor_binding_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            merge_supplied(self.hook, {"r": "v1"}, {"r": "v9", "extra": "v4"})
        self.assertIn("collides with the anchor binding", str(caught.exception))

    def test_undeclared_name_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            merge_supplied(self.hook, {"r": "v1"}, {"extra": "v4", "sneaky": "v5"})
        self.assertIn("<sneaky>", str(caught.exception))

    def test_missing_declared_name_is_refused(self):
        with self.assertRaises(ManifestError) as caught:
            merge_supplied(self.hook, {"r": "v1"}, {})
        self.assertIn("['extra']", str(caught.exception))

    def test_supplying_to_a_hook_that_declares_none_is_refused(self):
        with self.assertRaises(ManifestError):
            merge_supplied(simple_hook(), {"r": "v1"}, {"extra": "v4"})


CLASS_WITH_CAPTURE = """\
.class public final Lcom/x/Y;

.method public static a()V
    .locals 2

    const-string v1, "probe"

    return-void
.end method
"""


class ResolveInSourceSupplyTests(unittest.TestCase):
    """Two passes: locate the site, then render it once the blanks are filled."""

    def setUp(self):
        self.hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
            supplied=(one_reg_supply(),),
        )

    def test_first_pass_locates_the_site_and_reports_awaiting(self):
        result = resolve_in_source(self.hook, "Lcom/x/Y;", CLASS_WITH_CAPTURE)
        self.assertFalse(result.resolved)
        self.assertEqual(result.awaiting, ("extra",))
        self.assertEqual(result.occurrences, 1)
        # The anchor's own bindings survive, because the supplier needs them.
        self.assertEqual(result.bindings, {"r": "v1"})
        self.assertEqual(result.anchor, ['const-string v1, "probe"'])
        self.assertEqual(result.payload, [])
        self.assertIn("extra", result.reason)

    def test_second_pass_renders_from_the_merged_bindings(self):
        result = resolve_in_source(
            self.hook, "Lcom/x/Y;", CLASS_WITH_CAPTURE, {"extra": "v4"}
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.awaiting, ())
        self.assertIn("    move-object v4, v1", result.payload)

    def test_an_awaiting_resolution_is_never_appliable(self):
        result = resolve_in_source(self.hook, "Lcom/x/Y;", CLASS_WITH_CAPTURE)
        with self.assertRaises(ManifestError):
            result.as_operation(self.hook)

    def test_a_hook_with_no_supplied_captures_is_unchanged(self):
        result = resolve_in_source(simple_hook(), "Lcom/x/Y;", CLASS_WITH_CAPTURE)
        self.assertTrue(result.resolved)
        self.assertEqual(result.awaiting, ())

    def test_a_missing_anchor_still_beats_awaiting(self):
        # "the site is not here" must not be reported as "two blanks to fill".
        result = resolve_in_source(self.hook, "Lcom/x/Y;", ".class public Lcom/x/Y;\n")
        self.assertEqual(result.awaiting, ())
        self.assertIn("did not match", result.reason)


class SuppliedResultTests(unittest.TestCase):
    """A decline is a value with a stage. It is not an empty success."""

    def test_a_decline_may_not_carry_values(self):
        with self.assertRaises(ManifestError):
            Supplied("s", {"a": "v0"}, "some_stage", "because")

    def test_a_decline_must_name_its_stage(self):
        with self.assertRaises(ManifestError) as caught:
            Supplied("s", {}, "", "because")
        self.assertIn("name the stage", str(caught.exception))

    def test_silence_is_not_success(self):
        with self.assertRaises(ManifestError) as caught:
            Supplied("s")
        self.assertIn("neither values nor a decline", str(caught.exception))

    def test_a_stage_without_a_reason_is_refused(self):
        with self.assertRaises(ManifestError):
            Supplied("s", {}, "some_stage", "")

    def test_ok_is_the_absence_of_a_decline(self):
        self.assertTrue(Supplied("s", {"a": "v0"}).ok)
        self.assertFalse(decline("s", "st", "why").ok)


class ChainTests(unittest.TestCase):
    """Preference order, fall-through, and what the chain refuses to accept."""

    def setUp(self):
        self.supply = CaptureSupply(
            provides=(SuppliedCapture("extra", "reg", "the_reg"),),
            suppliers=("first", "second"),
        )
        self.hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
            supplied=(self.supply,),
        )
        hits = find_anchor_hits(self.hook, CLASS_WITH_CAPTURE)
        self.request = SupplyRequest(
            hook=self.hook,
            supply=self.supply,
            descriptor="Lcom/x/Y;",
            smali_path="smali/com/x/Y.smali",
            smali=CLASS_WITH_CAPTURE,
            hit=hits[0],
            index=None,  # type: ignore[arg-type]
            decode=Path("/nowhere"),
        )

    def run_chain(self, first, second):
        return run_supply_chain(self.request, {"first": first, "second": second})

    def test_the_first_supplier_wins_when_it_answers(self):
        outcome = self.run_chain(
            lambda request: Supplied("first", {"the_reg": "v4"}),
            lambda request: Supplied("second", {"the_reg": "v9"}),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.supplier, "first")
        # Keyed by CAPTURE NAME: roles are a supplier-side vocabulary.
        self.assertEqual(dict(outcome.values), {"extra": "v4"})
        self.assertEqual(len(outcome.attempts), 1)

    def test_a_decline_falls_through_and_is_recorded(self):
        outcome = self.run_chain(
            lambda request: decline("first", "nope", "precondition does not hold"),
            lambda request: Supplied("second", {"the_reg": "v9"}),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.supplier, "second")
        self.assertEqual(dict(outcome.values), {"extra": "v9"})
        # The loser is kept: a gate has to see the rule was TRIED and why it lost.
        self.assertEqual([item.stage for item in outcome.attempts], ["nope", ""])

    def test_every_supplier_declining_names_the_captures(self):
        outcome = self.run_chain(
            lambda request: decline("first", "a", "no"),
            lambda request: decline("second", "b", "also no"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.missing, ("extra",))
        self.assertIn("extra", outcome.reason())
        self.assertIn("first declined (a)", outcome.reason())
        self.assertIn("second declined (b)", outcome.reason())

    def test_a_malformed_value_is_treated_as_a_decline_not_a_win(self):
        outcome = self.run_chain(
            lambda request: Supplied("first", {"the_reg": "v0}, LX/Evil;->go()V"}),
            lambda request: Supplied("second", {"the_reg": "v9"}),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.supplier, "second")
        self.assertEqual(outcome.attempts[0].stage, STAGE_MALFORMED_VALUE)

    def test_an_incomplete_answer_is_treated_as_a_decline(self):
        outcome = self.run_chain(
            lambda request: Supplied("first", {"unrelated": "v4"}),
            lambda request: decline("second", "b", "no"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts[0].stage, "incomplete_answer")

    def test_an_answer_to_a_question_nobody_asked_is_a_decline(self):
        # There is no capture to put the surplus in, and dropping it silently
        # would accept an answer nothing can check.
        outcome = self.run_chain(
            lambda request: Supplied("first", {"the_reg": "v4", "bonus": "v5"}),
            lambda request: decline("second", "b", "no"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts[0].stage, STAGE_UNASKED_ROLE)
        self.assertIn("bonus", outcome.attempts[0].declined)

    def test_an_unregistered_supplier_is_a_decline_naming_itself(self):
        outcome = run_supply_chain(self.request, {})
        self.assertFalse(outcome.ok)
        self.assertEqual(
            [item.stage for item in outcome.attempts],
            [STAGE_UNKNOWN_SUPPLIER, STAGE_UNKNOWN_SUPPLIER],
        )

    def test_the_agent_supplier_declines_without_a_proposal(self):
        outcome = run_supply_chain(self.request, {"first": cs.agent_supplier, "second": cs.agent_supplier})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts[0].stage, STAGE_NO_PROPOSAL)

    def test_the_agent_supplier_answers_from_a_proposal(self):
        request = dataclasses.replace(self.request, proposed={"the_reg": "v7"})
        outcome = run_supply_chain(request, {"first": cs.agent_supplier, "second": cs.agent_supplier})
        self.assertTrue(outcome.ok)
        self.assertEqual(dict(outcome.values), {"extra": "v7"})

    def test_a_partial_proposal_is_declined(self):
        supply = CaptureSupply(
            provides=(
                SuppliedCapture("extra", "reg", "the_reg"),
                SuppliedCapture("other", "type", "the_type"),
            ),
            suppliers=("first",),
        )
        hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>", "    check-cast <r>, <other>"),
            supplied=(supply,),
        )
        request = dataclasses.replace(
            self.request, hook=hook, supply=supply, proposed={"the_reg": "v7"}
        )
        outcome = run_supply_chain(request, {"first": cs.agent_supplier})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.attempts[0].stage, STAGE_INCOMPLETE_PROPOSAL)


# --------------------------------------------------- deterministic supplier, unit

DISPATCH_CLASS = """\
.class public final LX/AAA;

.method public static make(Landroid/content/Context;LX/MODEL;)Landroid/widget/ImageView;
    .locals 4

    instance-of v0, p1, LX/Other;

    if-eqz v0, :cond_0

    instance-of v0, p1, LX/Self;

    instance-of v0, v2, Ljava/util/Collection;

    :cond_0
    const-string v1, "probe"

    return-object v1
.end method
"""


def write_index(
    index_dir: Path,
    decode: Path,
    classes: Mapping[str, str],
    drawables: Mapping[str, str],
    *,
    resource_types: Sequence[str] = ("drawable",),
) -> Path:
    """The three files `tools/indexer/build_index.py` writes, for a few classes."""
    index_dir.mkdir(parents=True, exist_ok=True)
    header = {
        "kind": "dfinsta.index.header",
        "schema_version": 1,
        "generator": "tests/test_capture_supply.py",
        "decode_path": str(Path(decode).resolve()),
        "decode_name": Path(decode).name,
        "content_hash": "sha256:" + "cd" * 32,
        "resource_types_indexed": list(resource_types),
    }
    rows = [
        {
            "kind": "dfinsta.index.class",
            "descriptor": descriptor,
            "path": path,
            "tree": path.split("/", 1)[0],
            "super": "Ljava/lang/Object;",
            "interfaces": [],
            "methods": [],
            "obfuscated": descriptor.startswith("LX/"),
        }
        for descriptor, path in classes.items()
    ]
    (index_dir / "structural.jsonl").write_text(
        "\n".join([json.dumps(header)] + [json.dumps(row) for row in rows]) + "\n",
        encoding="utf-8",
    )
    (index_dir / "api_surface.json").write_text(
        json.dumps(
            {
                "header": header,
                "api_paths": {},
                "resources": {"drawable": dict(drawables)},
                "stable_types": {
                    descriptor: path
                    for descriptor, path in classes.items()
                    if not descriptor.startswith("LX/")
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / "header.json").write_text(json.dumps(header), encoding="utf-8")
    return index_dir


class ProfileGuardUnitTests(unittest.TestCase):
    """The deterministic rule as a rule, on smali small enough to read."""

    ICON = "0x7f080999"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)

    def build(
        self,
        *,
        host: str = DISPATCH_CLASS,
        self_body: str = f".class public LX/Self;\n\n    const v0, {ICON}\n",
        other_body: str = ".class public LX/Other;\n",
        drawables: Mapping[str, str] | None = None,
        classes: Mapping[str, str] | None = None,
        params: Sequence[tuple[str, str]] | None = None,
        resource_types: Sequence[str] = ("drawable",),
        write: Sequence[str] = ("LX/AAA;", "LX/Self;", "LX/Other;", PROFILE_ACTION_BAR),
    ) -> SupplyRequest:
        bodies = {
            "LX/AAA;": host,
            "LX/Self;": self_body,
            "LX/Other;": other_body,
            PROFILE_ACTION_BAR: ".class public Lcom/instagram/profile/actionbar/ProfileActionBar;\n",
        }
        bodies.update(classes or {})
        paths = {
            descriptor: "smali/"
            + descriptor[1:-1].replace("$", "_")
            + ".smali"
            for descriptor in bodies
        }
        decode = self.base / "decode"
        for descriptor, text in bodies.items():
            if descriptor not in write:
                continue
            target = decode / paths[descriptor]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        decode.mkdir(parents=True, exist_ok=True)
        index_dir = write_index(
            self.base / "index",
            decode,
            paths,
            {"self_icon": self.ICON} if drawables is None else drawables,
            resource_types=resource_types,
        )
        supply = CaptureSupply(
            provides=(
                SuppliedCapture("model", "reg", "model_register"),
                SuppliedCapture("selfprofile", "type", "self_profile_type"),
            ),
            suppliers=(PROFILE_GUARD,),
            params=tuple(
                params
                if params is not None
                else (("self_drawable", "self_icon"), ("requires_type", PROFILE_ACTION_BAR))
            ),
        )
        hook = simple_hook(
            anchor=('const-string <r:reg>, "probe"',),
            payload=(*PROBE_PAYLOAD, "    instance-of <r>, <model>, <selfprofile>"),
            supplied=(supply,),
        )
        # `p1` is what the fixture's instance-of chain tests; the anchor binds `v1`,
        # so the request's bindings name both and the "is it anchored" step is a
        # real check rather than a formality.
        hits = find_anchor_hits(hook, host)
        hit = AnchorHit(
            bindings={**hits[0].bindings, "model_arg": "p1"},
            lines=hits[0].lines,
            first_line=hits[0].first_line,
            last_line=hits[0].last_line,
        )
        hook = dataclasses.replace(
            hook, anchor=('const-string <r:reg>, "probe"', "# <model_arg:reg>")
        )
        return SupplyRequest(
            hook=hook,
            supply=supply,
            descriptor="LX/AAA;",
            smali_path=paths["LX/AAA;"],
            smali=host,
            hit=hit,
            index=HookIndex.load(index_dir),
            decode=decode,
        )

    def test_the_rule_produces_the_register_and_the_type(self):
        result = profile_action_bar_self_guard(self.build())
        self.assertTrue(result.ok, result.declined)
        self.assertEqual(
            dict(result.values),
            {"model_register": "p1", "self_profile_type": "LX/Self;"},
        )

    def test_a_missing_precondition_type_declines(self):
        request = self.build(write=("LX/AAA;", "LX/Self;", "LX/Other;"))
        # Written nowhere AND absent from the index: `has()` is the check.
        request = dataclasses.replace(
            request,
            supply=dataclasses.replace(
                request.supply,
                params=(("self_drawable", "self_icon"), ("requires_type", "Lcom/nope/Gone;")),
            ),
        )
        result = profile_action_bar_self_guard(request)
        self.assertEqual(result.stage, STAGE_PRECONDITION_TYPE_ABSENT)
        self.assertEqual(dict(result.values), {})

    def test_a_missing_drawable_declines(self):
        result = profile_action_bar_self_guard(self.build(drawables={"other_icon": self.ICON}))
        self.assertEqual(result.stage, STAGE_DRAWABLE_ABSENT)

    def test_an_index_without_drawables_declines_rather_than_raising(self):
        result = profile_action_bar_self_guard(
            self.build(resource_types=("layout",), drawables={})
        )
        self.assertEqual(result.stage, STAGE_DRAWABLE_NOT_INDEXED)

    def test_a_missing_param_declines(self):
        result = profile_action_bar_self_guard(self.build(params=()))
        self.assertEqual(result.stage, STAGE_MISSING_PARAM)
        self.assertIn("self_drawable", result.declined)

    def test_no_dispatch_chain_declines(self):
        # One instance-of on each register is a type check, not a dispatch chain.
        host = DISPATCH_CLASS.replace("instance-of v0, p1, LX/Self;", "nop")
        result = profile_action_bar_self_guard(self.build(host=host))
        self.assertEqual(result.stage, STAGE_NO_DISPATCH_REGISTER)

    def test_two_dispatch_chains_decline_rather_than_pick_one(self):
        host = DISPATCH_CLASS.replace(
            "instance-of v0, v2, Ljava/util/Collection;",
            "instance-of v0, v2, LX/Self;\n\n    instance-of v0, v2, LX/Other;",
        )
        result = profile_action_bar_self_guard(self.build(host=host))
        self.assertEqual(result.stage, STAGE_AMBIGUOUS_DISPATCH_REGISTER)

    def test_a_dispatch_register_the_anchor_did_not_bind_declines(self):
        host = DISPATCH_CLASS.replace("p1", "p3")
        result = profile_action_bar_self_guard(self.build(host=host))
        self.assertEqual(result.stage, STAGE_DISPATCH_REGISTER_NOT_ANCHORED)

    def test_no_subtype_loading_the_drawable_declines(self):
        result = profile_action_bar_self_guard(
            self.build(self_body=".class public LX/Self;\n")
        )
        self.assertEqual(result.stage, STAGE_NO_SELF_PROFILE_TYPE)

    def test_two_subtypes_loading_the_drawable_decline_rather_than_take_the_first(self):
        result = profile_action_bar_self_guard(
            self.build(other_body=f".class public LX/Other;\n\n    const v0, {self.ICON}\n")
        )
        self.assertEqual(result.stage, STAGE_AMBIGUOUS_SELF_PROFILE_TYPE)
        self.assertIn("LX/Self;", result.declined)
        self.assertIn("LX/Other;", result.declined)

    def test_a_candidate_in_the_index_but_not_on_disk_declines(self):
        # Skipping it would let "exactly one matched" be true only because the
        # second match was never looked at.
        result = profile_action_bar_self_guard(
            self.build(write=("LX/AAA;", "LX/Self;", PROFILE_ACTION_BAR))
        )
        self.assertEqual(result.stage, STAGE_CANDIDATE_UNREADABLE)

    def test_the_id_must_be_a_whole_token(self):
        # `0x7f0809991` is a different resource, not this one.
        result = profile_action_bar_self_guard(
            self.build(self_body=f".class public LX/Self;\n\n    const v0, {self.ICON}1\n")
        )
        self.assertEqual(result.stage, STAGE_NO_SELF_PROFILE_TYPE)

    def test_being_asked_the_wrong_question_raises_rather_than_declining(self):
        # A wiring bug is not a version whose architecture moved, and must not be
        # reported as one.
        request = self.build()
        request = dataclasses.replace(
            request,
            supply=CaptureSupply(
                provides=(SuppliedCapture("x", "reg", "something_else"),),
                suppliers=(PROFILE_GUARD,),
                params=request.supply.params,
            ),
        )
        with self.assertRaises(ManifestError):
            profile_action_bar_self_guard(request)

    def test_dispatch_minimum_is_two(self):
        self.assertEqual(DISPATCH_MINIMUM, 2)


class ResolveStageSupplyTests(unittest.TestCase):
    """The stage's outcome when a supplier answers, and when it will not."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.decode = self.base / "decode"
        path = "smali/com/x/Y.smali"
        target = self.decode / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CLASS_WITH_CAPTURE, encoding="utf-8")
        index_dir = write_index(
            self.base / "index", self.decode, {"Lcom/x/Y;": path}, {}
        )
        self.index = HookIndex.load(index_dir)
        self.supply = CaptureSupply(
            provides=(SuppliedCapture("extra", "reg", "the_reg"),), suppliers=("stub",)
        )
        self.hook = simple_hook(
            payload=(*PROBE_PAYLOAD, "    move-object <extra>, <r>"),
            supplied=(self.supply,),
        )

    def resolve(self, supplier, **kwargs):
        return resolve_hook(
            self.hook, self.index, self.decode, registry={"stub": supplier}, **kwargs
        )

    def test_a_supplier_that_answers_resolves_and_renders(self):
        result = self.resolve(lambda request: Supplied("stub", {"the_reg": "v4"}))
        self.assertIs(result.outcome, Outcome.RESOLVED)
        assert result.resolution is not None
        self.assertIn("    move-object v4, v1", result.resolution.payload)
        self.assertEqual(result.resolution.bindings["extra"], "v4")
        self.assertEqual(result.supplies[0].supplier, "stub")

    def test_a_supplier_that_declines_needs_an_agent_and_names_the_capture(self):
        result = self.resolve(
            lambda request: decline("stub", "precondition", "not this version")
        )
        self.assertIs(result.outcome, Outcome.NEEDS_AGENT)
        self.assertIn("extra", result.reason)
        self.assertIn("not this version", result.reason)
        self.assertIn("precondition", result.reason)
        self.assertEqual(result.descriptor, "Lcom/x/Y;")
        # Nothing appliable came out of it.
        with self.assertRaises(ManifestError):
            result.as_operation(self.hook)

    def test_a_declining_supplier_is_recorded_for_the_gate(self):
        result = self.resolve(lambda request: decline("stub", "precondition", "no"))
        serialised = result.to_dict()
        self.assertEqual(
            serialised["supplies"][0]["attempts"][0]["stage"], "precondition"
        )
        self.assertEqual(serialised["supplies"][0]["values"], {})

    def test_a_supplier_returning_the_wrong_kind_escalates_rather_than_rendering(self):
        result = self.resolve(lambda request: Supplied("stub", {"the_reg": "LX/Nope;"}))
        self.assertIs(result.outcome, Outcome.NEEDS_AGENT)

    def test_requires_proposal_still_short_circuits_a_supplied_hook(self):
        # A hook flagged as a SHAPE keeps escalating until a human clears the
        # flag, even once it has a supplier that would answer.
        hook = dataclasses.replace(self.hook, requires_proposal=True, constraints=("shape",))
        result = resolve_hook(
            hook,
            self.index,
            self.decode,
            registry={"stub": lambda request: Supplied("stub", {"the_reg": "v4"})},
        )
        self.assertIs(result.outcome, Outcome.NEEDS_AGENT)
        self.assertIn("requires_proposal", result.reason)

    def test_the_candidate_report_carries_awaiting(self):
        result = self.resolve(lambda request: decline("stub", "s", "no"))
        self.assertEqual(result.candidates[0].awaiting, ("extra",))
        self.assertTrue(result.candidates[0].sited)
        self.assertFalse(result.candidates[0].resolved)


# ----------------------------------------------------------------- real decodes

HAVE_439 = DECODE_439.is_dir() and INDEX_439.is_dir()
HAVE_430 = DECODE_430.is_dir() and INDEX_430.is_dir()
HAVE_340 = DECODE_340.is_dir() and INDEX_340.is_dir()

#: What the shipped 430 and 439 patches actually guard on. Pinned here so a
#: change to the rule that still "works" but produces a different type fails.
EXPECTED = {
    "439": ("LX/0DnT;", "p4", "LX/0Dxw;", 10),
    "430": ("LX/077K;", "p3", "LX/077N;", 11),
}


@unittest.skipUnless(HAVE_439 and HAVE_430, "work/ decodes are absent (gitignored)")
class RealDecodeSupplyTests(unittest.TestCase):
    """430 and 439: the rule's whole confirmed reach, and not two data points.

    Both versions ship the same post-rewrite ProfileActionBar design, so agreement
    here is one architecture agreeing with itself. It is worth pinning because the
    values are the ones the device-verified patches use — and worth remembering
    that `Holdout340Tests` is what says how far it generalises.
    """

    def case(self, label: str):
        index_dir, decode = (
            (INDEX_439, DECODE_439) if label == "439" else (INDEX_430, DECODE_430)
        )
        host, register, self_type, subtypes = EXPECTED[label]
        index = HookIndex.for_decode(index_dir, decode)
        return index, decode, host, register, self_type, subtypes

    def test_the_supplier_produces_the_shipped_guard_values(self):
        for label in EXPECTED:
            with self.subTest(version=label):
                index, decode, host, register, self_type, subtypes = self.case(label)
                hook = guarded_settings_hook()
                result = resolve_hook(hook, index, decode, proposals=[host])
                self.assertIs(result.outcome, Outcome.RESOLVED, result.reason)
                self.assertEqual(result.descriptor, host)
                self.assertEqual(result.supplies[0].supplier, PROFILE_GUARD)
                self.assertEqual(
                    dict(result.supplies[0].values),
                    {"model": register, "selfprofile": self_type},
                )

    def test_the_rendered_payload_carries_the_own_profile_guard(self):
        for label in EXPECTED:
            with self.subTest(version=label):
                index, decode, host, register, self_type, _ = self.case(label)
                result = resolve_hook(guarded_settings_hook(), index, decode, proposals=[host])
                assert result.resolution is not None
                payload = result.resolution.payload
                self.assertIn(f"    instance-of v0, {register}, {self_type}", payload)
                self.assertIn("    if-eqz v0, :dfinsta_not_self_profile", payload)
                # The stock tail is outside the guard: a stranger's Options must
                # still get its layout params and still be returned.
                self.assertTrue(payload[-1].strip().startswith("return-object"))

    def test_the_model_register_is_not_one_capture_name_across_versions(self):
        # The reason this mechanism exists at all: the argument order swapped, so
        # the model is <b> on 439 and <d> on 430 and no single capture holds it.
        bindings = {}
        for label in EXPECTED:
            index, decode, host, _, _, _ = self.case(label)
            result = resolve_hook(guarded_settings_hook(), index, decode, proposals=[host])
            assert result.resolution is not None
            bindings[label] = result.resolution.bindings
        model_439 = bindings["439"]["model"]
        model_430 = bindings["430"]["model"]
        self.assertEqual(bindings["439"]["b"], model_439)
        self.assertNotEqual(bindings["439"]["d"], model_439)
        self.assertEqual(bindings["430"]["d"], model_430)
        self.assertNotEqual(bindings["430"]["b"], model_430)

    def test_the_drawable_id_is_re_resolved_per_version(self):
        # Names survive a version step, ids do not. The rule must key on the name.
        ids = {
            label: HookIndex.load(INDEX_439 if label == "439" else INDEX_430).resource_id(
                "drawable", SELF_DRAWABLE
            )
            for label in EXPECTED
        }
        self.assertTrue(all(ids.values()), ids)
        self.assertNotEqual(ids["439"], ids["430"])

    def test_exactly_one_subtype_loads_the_drawable(self):
        for label in EXPECTED:
            with self.subTest(version=label):
                index, decode, host, _, self_type, subtypes = self.case(label)
                result = resolve_hook(guarded_settings_hook(), index, decode, proposals=[host])
                evidence = "\n".join(result.supplies[0].attempts[0].evidence)
                self.assertIn(f"1 of {subtypes} subtypes load", evidence)
                self.assertIn(self_type, evidence)

    def test_the_agent_supplier_is_not_reached_when_the_rule_applies(self):
        index, decode, host, _, _, _ = self.case("439")
        result = resolve_hook(guarded_settings_hook(), index, decode, proposals=[host])
        self.assertEqual([item.supplier for item in result.supplies[0].attempts], [PROFILE_GUARD])


@unittest.skipUnless(HAVE_340, "work/340-holdout or work/index-340 is absent")
class Holdout340Tests(unittest.TestCase):
    """The negative half: 340 has neither of the rule's keys, and it says so.

    Both keys fail together below 430 because both are consequences of one
    architectural rewrite — there is no `ProfileActionBar`, no model-subtype
    dispatch chain, and `instagram_menu_outline_24` does not exist (340 ships
    `instagram_menu_pano_outline_24`, a different asset). So the supplier must
    decline, and the point of this class is that it declines *having run*.

    The site the request points at is 340's own action bar:
    `LX/66Y;->configureActionBar(LX/2QW;)V`, which loads the 340 menu drawable and
    does carry a dispatch chain — of Activity types, on the legacy config-object
    design. It is the most favourable material 340 has for this rule, and the rule
    still refuses it.
    """

    HOST_PATH = "smali_classes3/X/66Y.smali"
    HOST = "LX/66Y;"
    PANO = "instagram_menu_pano_outline_24"
    PRESENT_IN_340 = "Lcom/instagram/modal/ModalActivity;"

    @classmethod
    def setUpClass(cls):
        cls.index = HookIndex.for_decode(INDEX_340, DECODE_340)
        cls.body = (DECODE_340 / cls.HOST_PATH).read_text(encoding="utf-8", errors="replace")

    def request(self, drawable: str, requires: str) -> SupplyRequest:
        lines = self.body.splitlines()
        pano_id = self.index.resource_id("drawable", self.PANO)
        assert pano_id is not None
        site = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == f"const v0, {pano_id}"
        )
        supply = CaptureSupply(
            provides=(
                SuppliedCapture("model", "reg", "model_register"),
                SuppliedCapture("selfprofile", "type", "self_profile_type"),
            ),
            suppliers=(PROFILE_GUARD, AGENT),
            params=(("self_drawable", drawable), ("requires_type", requires)),
        )
        hook = guarded_settings_hook(supplied_captures=(supply,))
        # Bindings for a site 340 does not actually have. `v6` is the register the
        # 340 method's own dispatch chain tests, so the "is it anchored" step is
        # given every chance to pass rather than failing for a bookkeeping reason.
        hit = AnchorHit(
            bindings={
                "l": "v0", "a": "v3", "b": "v6", "c": "v5", "d": "p1",
                "view": "v6", "lp": "v2",
                "listener": "LX/0000;", "helper": "LX/0001;", "hm": "A00",
            },
            lines=(lines[site].strip(),),
            first_line=site,
            last_line=site,
        )
        return SupplyRequest(
            hook=hook,
            supply=supply,
            descriptor=self.HOST,
            smali_path=self.HOST_PATH,
            smali=self.body,
            hit=hit,
            index=self.index,
            decode=DECODE_340,
        )

    # -- positive controls: the machinery is live against THIS decode ---------

    def test_control_the_340_index_is_real_and_answers_resource_lookups(self):
        self.assertGreater(self.index.class_count(), 100_000)
        self.assertIsNotNone(self.index.resource_id("drawable", self.PANO))
        self.assertTrue(self.index.has(self.PRESENT_IN_340))

    def test_control_the_site_is_a_real_method_with_instance_of_material(self):
        request = self.request(SELF_DRAWABLE, PROFILE_ACTION_BAR)
        first, last = request.method_lines()
        signature = self.body.splitlines()[first]
        self.assertIn("configureActionBar", signature)
        self.assertGreater(last - first, 1000)
        window = "\n".join(self.body.splitlines()[first : last + 1])
        self.assertGreaterEqual(len(re.findall(r"^\s*instance-of ", window, re.M)), 5)

    def test_control_with_both_keys_relaxed_every_step_runs_and_still_declines(self):
        # THE control. With the selector replaced by a type 340 does have and the
        # drawable by the one 340 does ship, the supplier reaches its LAST step —
        # so the decline below is a finding, not an inability to run.
        result = profile_action_bar_self_guard(self.request(self.PANO, self.PRESENT_IN_340))
        self.assertEqual(result.stage, STAGE_NO_SELF_PROFILE_TYPE)
        evidence = "\n".join(result.evidence)
        self.assertIn("precondition", evidence)
        self.assertIn(self.PANO, evidence)
        self.assertIn("dispatch register v6", evidence)
        self.assertIn("v6 is bound by the anchor", evidence)
        self.assertIn("0 of 3 subtypes load", evidence)

    # -- the finding ---------------------------------------------------------

    def test_the_supplier_declines_on_the_selector(self):
        result = profile_action_bar_self_guard(self.request(SELF_DRAWABLE, PROFILE_ACTION_BAR))
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, STAGE_PRECONDITION_TYPE_ABSENT)
        self.assertEqual(dict(result.values), {})
        self.assertIn(PROFILE_ACTION_BAR, result.declined)

    def test_the_second_key_fails_independently_of_the_first(self):
        # Relaxing only the selector: the drawable is gone too. Two keys, one
        # rewrite — which is why 430 and 439 are not two confirmations.
        result = profile_action_bar_self_guard(self.request(SELF_DRAWABLE, self.PRESENT_IN_340))
        self.assertEqual(result.stage, STAGE_DRAWABLE_ABSENT)
        self.assertEqual(dict(result.values), {})

    def test_340_has_no_profile_action_bar_at_all(self):
        self.assertFalse(self.index.has(PROFILE_ACTION_BAR))
        self.assertIsNone(self.index.resource_id("drawable", SELF_DRAWABLE))

    def test_the_chain_escalates_to_the_agent_and_the_agent_has_nothing(self):
        outcome = run_supply_chain(self.request(SELF_DRAWABLE, PROFILE_ACTION_BAR))
        self.assertFalse(outcome.ok)
        self.assertEqual(
            [item.stage for item in outcome.attempts],
            [STAGE_PRECONDITION_TYPE_ABSENT, STAGE_NO_PROPOSAL],
        )
        self.assertEqual(outcome.missing, ("model", "selfprofile"))
        self.assertIn("model", outcome.reason())
        self.assertIn("selfprofile", outcome.reason())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
