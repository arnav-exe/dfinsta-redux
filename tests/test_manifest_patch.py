"""Tests for the stage between "a fingerprint was proposed" and "the manifest carries it".

The module is a wall of refusals with an editor behind it, so every test here is
written from the question "what would let a wrong fingerprint reach
`manifest/hooks.json`", not from the code's shape. Four hazards, and one class of
tests each:

  1. **One version cannot tell a fingerprint from a coincidence.** Measured, on
     this very project: the systrace literal the 439 proposers cited selects
     exactly one class on 439 and it is the right one, exactly one on 430 and it
     is the WRONG one, and exactly one on 440 and it is right again. So the
     two-version rule is tested by handing `plan` a proposal that skipped it —
     which means building one *around* `generalise.Proposal.__post_init__`, since
     the builder refuses to construct it. That is deliberate and it is the point:
     the builder fails fast, and this stage refuses a hand-built proposal that
     never went through the builder. Removing either check alone changes nothing;
     `hand_built` is what makes the second one falsifiable.

  2. **Absence is never a pass.** `verify` is given decodes for some versions and
     not others, and the missing one is asserted to come back `unchecked`, to be
     `not ok`, and to make `apply` refuse. A test that only checked "no failures"
     would pass on a `verify` that silently skipped every version.

  3. **A dry run must open nothing.** `apply` without `confirm` is asserted to
     leave the file byte-identical AND to leave no temporary files behind, and the
     atomicity tests break the write part-way — a failed rename, a patched
     document that does not load — and assert the original survives intact.

  4. **A reformat destroys the review.** After a patch, the manifest is asserted
     to be byte-for-byte the original with exactly one `hosts` entry text swapped
     for another. Not "the JSON is equivalent": the bytes.

The fixtures are the REAL manifest copied to a temporary directory with one
hook's host fingerprint wound back to the `by_agent` it actually carried before a
human hand-wrote `by_anchor` — so `load_manifest` really validates, the
formatting really round-trips, and the diff is the diff a human would have
reviewed. The decodes are synthetic: two hand-written classes are enough to make
the resolver answer, and a real decode is 181,000 files.

Nothing here writes `manifest/hooks.json`. `tearDownModule` re-reads it and
fails the module if a single byte moved.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

from dfinsta_pipeline.generalise import Proposal, Selection
from dfinsta_pipeline.hook_manifest import HostFingerprint, ManifestError, load_manifest
from dfinsta_pipeline.manifest_patch import (
    MIN_CORROBORATING_VERSIONS,
    NO_VALUE_REASON,
    REFUSE_BLOCKED,
    REFUSE_DOES_NOT_LOAD,
    REFUSE_FORBIDDEN_VALUE,
    REFUSE_NO_FINGERPRINT,
    REFUSE_NO_VALUE_RULE,
    REFUSE_NOT_STRONGER,
    REFUSE_ONE_VERSION,
    REFUSE_PLAN_REFUSED,
    REFUSE_REFORMATS,
    REFUSE_SELECTION_NOT_EXACT,
    REFUSE_SEVERAL_HOSTS,
    REFUSE_STALE,
    REFUSE_UNCONFIRMED,
    REFUSE_UNKNOWN_HOOK,
    REFUSE_UNVERIFIED,
    STRENGTH,
    VALUE_FIELDS,
    VERIFY_CHANGED,
    VERIFY_NO_BASELINE,
    VERIFY_OK,
    VERIFY_UNCHECKED,
    VERIFY_UNRESOLVED,
    DecodeUnderTest,
    Patch,
    PatchError,
    VerifyResult,
    apply,
    forbidden_in_note,
    forbidden_in_value,
    main,
    plan,
    read_proposals,
    render_patch,
    serialise,
    strength_key,
    verify,
)

REPO = Path(__file__).resolve().parents[1]
REAL_MANIFEST = REPO / "manifest" / "hooks.json"
#: Captured at import so `tearDownModule` can prove no test wrote the real file.
REAL_MANIFEST_BYTES = REAL_MANIFEST.read_bytes()

PROPOSALS_439 = REPO / "work" / "generalise-439-proposals.json"
BY_ANCHOR_PROPOSAL = REPO / "work" / "by-anchor-proposal.json"

HOOK = "install_settings_long_click_actionbar"
OTHER_HOOK = "install_settings_long_click"

#: The literals the real 439 generaliser run proposed for this hook, and the
#: hosts it measured them against. Version-stamped expectations, never join keys.
LITERAL = "notifications_entry_point_impression"
CO_LITERAL = "ig4a-instagram-schema"
HOSTS = {"439": "LX/0Di2;", "430": "LX/06X7;"}
CONFIG_CLASS = {"439": "LX/0DiA;", "430": "LX/06XA;"}

NOTE = (
    "MEASURED by dfinsta_pipeline.generalise across 2 decode(s): the host is the one "
    "class carrying 'notifications_entry_point_impression', 'ig4a-instagram-schema' "
    "(439 -> LX/0Di2;; 430 -> LX/06X7;)."
)

INDEX_SCHEMA_VERSION = 1


def tearDownModule() -> None:
    """The repo's own manifest is the one file these tests may never touch."""
    if REAL_MANIFEST.read_bytes() != REAL_MANIFEST_BYTES:
        raise AssertionError(
            f"{REAL_MANIFEST} changed while this module ran. Every test here works on a "
            "tempfile copy; a real write means one of them was pointed at the repo"
        )


# ------------------------------------------------------------------- fixtures


def dump(data: Any) -> str:
    """The manifest's on-disk form, spelled out here rather than imported.

    Deliberately NOT `manifest_patch.serialise`. A fixture that formats itself
    with the module under test agrees with whatever that module happens to do, so
    a mutation that reordered every key would leave the fixture and the output
    matching each other and the byte-identity test would pass on a whole-file
    reformat. This is the independent statement of the format the repo's manifest
    is actually in — `RepoManifestTests` proves it against the real file.
    """
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def manifest_with(directory: Path, hook_id: str, host: Mapping[str, Any] | Sequence[Any]) -> Path:
    """The real manifest, copied, with one hook's `hosts` replaced.

    `host` is one entry or a whole list. Byte-identical to the original apart
    from the entry that was swapped.
    """
    data = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    for entry in data["hooks"]:
        if entry["hook_id"] == hook_id:
            entry["hosts"] = list(host) if isinstance(host, list) else [dict(host)]
            break
    else:  # pragma: no cover - a typo in a fixture
        raise AssertionError(f"{hook_id} is not in the real manifest")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hooks.json"
    path.write_text(dump(data), encoding="utf-8")
    return path


BY_AGENT_HOST = {
    "kind": "by_agent",
    "note": "an agent proposed this host; this is the state the manifest was in before "
    "a human hand-wrote the by_anchor entry",
}
BY_ANCHOR_HOST = {"kind": "by_anchor", "note": "the anchor is the whole fingerprint"}
NAMED_STABLE_HOST = {
    "kind": "named",
    "descriptor": "Lcom/instagram/profile/actionbar/ProfileActionBar;",
    "note": "a name a human wrote",
}
NAMED_OBFUSCATED_HOST = {
    "kind": "named",
    "descriptor": "LX/06X7;",
    "note": "the 430 name, which still exists in 439 on an unrelated class",
}


#: What `generalise.generalise_anchor` writes for a hook whose anchor selects
#: exactly one class per version. Cites the descriptors next to the versions they
#: were measured on, exactly as the shipped manifest's own notes do.
ANCHOR_NOTE = (
    "the anchor is the fingerprint: 439 -> LX/0Di2; (1 of 1 matched), "
    "430 -> LX/06X7; (1 of 1 matched)"
)


def selection(version: str, selected: Sequence[str], literals=(LITERAL, CO_LITERAL)) -> Selection:
    return Selection.measure(version, literals, list(selected), HOSTS[version])


def proposal(
    *,
    hook_id: str = HOOK,
    literal: str = LITERAL,
    co_literals: Sequence[str] = (CO_LITERAL,),
    versions: Sequence[str] = ("439", "430"),
    blocks: Sequence[tuple[str, str]] = (),
    note: str = NOTE,
) -> Proposal:
    """A real `generalise.Proposal`, built through the real constructor."""
    return Proposal(
        hook_id=hook_id,
        kind="by_literal",
        literal=literal,
        co_literals=tuple(co_literals),
        selections=tuple(selection(version, [HOSTS[version]]) for version in versions),
        reason=(
            f"{literal!r} selects exactly the known host in every one of the "
            f"{len(versions)} version(s) measured"
        ),
        blocks=tuple(blocks),
        note=note,
    )


def anchor_proposal(
    *,
    hook_id: str = HOOK,
    versions: Sequence[str] = ("439", "430"),
    note: str = ANCHOR_NOTE,
) -> Proposal:
    """A real `by_anchor` `generalise.Proposal`, built through the real constructor.

    No literal, because the fingerprint is the hook's own anchor — the kind that
    took the agent count from 2 to 0 between Instagram 439 and 440, and the one
    this stage could not express until the producer landed.
    """
    return Proposal(
        hook_id=hook_id,
        kind="by_anchor",
        selections=tuple(
            selection(version, [HOSTS[version]], literals=()) for version in versions
        ),
        reason=(
            "the anchor selects exactly one class on each of "
            f"{', '.join(versions)}, and it is the known host every time"
        ),
        note=note,
    )


def no_fingerprint(hook_id: str = OTHER_HOOK) -> Proposal:
    return Proposal(
        hook_id,
        "",
        reason=(
            "no durable fingerprint found: the hosts share exactly one string constant. "
            "This hook still needs an agent, and saying so is the result"
        ),
    )


def hand_built(source: Proposal, **fields: Any) -> Proposal:
    """A Proposal with fields swapped in WITHOUT re-running generalise's constructor.

    `generalise.Proposal.__post_init__` already refuses a one-version fingerprint
    and a selection that is not exact, so those two states cannot be reached
    through the builder — which is exactly why `manifest_patch.plan` re-checks
    them and exactly why this exists. It manufactures the object a caller
    assembling a Proposal by hand, or a future `generalise` whose own guard was
    relaxed, would hand this stage. Without it, both refusals would be untestable
    and therefore unfalsifiable.
    """
    clone = Proposal.__new__(Proposal)
    for name in Proposal.__dataclass_fields__:
        object.__setattr__(clone, name, fields.get(name, getattr(source, name)))
    return clone


# --------------------------------------------------------------- synthetic decode

HOST_SMALI = """\
.class public final {descriptor}
.super Ljava/lang/Object;

.method public final A00()V
    .registers 6

    const-string v5, "{literal}"

    const-string v5, "{co_literal}"

    iput-object v1, v0, {config}->A00:Landroid/graphics/drawable/Drawable;

    const v2, 0x7f134a0e

    iput v2, v0, {config}->A01:I

    iput-object v3, v0, {config}->A02:Landroid/view/View$OnClickListener;

    iput-object v4, v0, {config}->A03:Landroid/view/View$OnLongClickListener;

    return-void
.end method
"""

#: A class that carries the literals but not the anchor, so a `by_literal` search
#: that stopped at the index would find it and the anchor match still refuses it.
DECOY_SMALI = """\
.class public final {descriptor}
.super Ljava/lang/Object;

.method public final A00()V
    .registers 2

    const-string v1, "{literal}"

    const-string v1, "{co_literal}"

    return-void
.end method
"""


def write_index(
    index_dir: Path,
    decode: Path,
    classes: Mapping[str, str],
    api_paths: Mapping[str, Sequence[str]] | None = None,
) -> Path:
    """The three files `tools/indexer/build_index.py` writes, for a handful of classes."""
    index_dir.mkdir(parents=True, exist_ok=True)
    header = {
        "kind": "dfinsta.index.header",
        "schema_version": INDEX_SCHEMA_VERSION,
        "generator": "tests/test_manifest_patch.py",
        "decode_path": str(Path(decode).resolve()),
        "decode_name": Path(decode).name,
        "content_hash": "sha256:" + "cd" * 32,
        "resource_types_indexed": ["drawable"],
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
                "api_paths": {
                    literal: list(descriptors)
                    for literal, descriptors in (api_paths or {}).items()
                },
                "resources": {"drawable": {"ic_probe": "0x7f080001"}},
                "resource_names_by_id": {"0x7f080001": "drawable/ic_probe"},
                "stable_types": {
                    descriptor: path
                    for descriptor, path in classes.items()
                    if not descriptor.startswith("LX/")
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / "header.json").write_text(json.dumps(header, indent=2) + "\n", encoding="utf-8")
    return index_dir


def build_decode(
    root: Path,
    version: str,
    *,
    host: str | None = None,
    extra_anchored: Mapping[str, str] | None = None,
    literals_on_host: bool = True,
) -> DecodeUnderTest:
    """One tiny decode plus its index: the host class, and whatever else was asked for.

    *literals_on_host* False writes the anchor into the host and the literals into
    a class that does NOT carry the anchor, which is how a `by_literal`
    fingerprint ends up selecting nothing at all.
    """
    host = host or HOSTS[version]
    decode = root / f"decode-{version}"
    classes: dict[str, str] = {}
    api: dict[str, list[str]] = {LITERAL: [], CO_LITERAL: []}

    def emit(descriptor: str, template: str) -> None:
        relative = f"smali_classes6/X/{descriptor[3:-1]}.smali"
        path = decode / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            template.format(
                descriptor=descriptor,
                literal=LITERAL,
                co_literal=CO_LITERAL,
                config=CONFIG_CLASS[version],
            ),
            encoding="utf-8",
        )
        classes[descriptor] = relative

    if literals_on_host:
        emit(host, HOST_SMALI)
        api[LITERAL].append(host)
        api[CO_LITERAL].append(host)
    else:
        # The anchor is here, the literals are not.
        emit(host, HOST_SMALI.replace(f'"{LITERAL}"', '"unrelated/one/"').replace(
            f'"{CO_LITERAL}"', '"unrelated/two/"'
        ))
    for descriptor, template in (extra_anchored or {}).items():
        emit(descriptor, template)
        api[LITERAL].append(descriptor)
        api[CO_LITERAL].append(descriptor)

    index = write_index(root / f"index-{version}", decode, classes, api)
    return DecodeUnderTest(version, decode, index)


def passing(versions: Sequence[str] = ("439", "430"), hook_id: str = HOOK) -> list[VerifyResult]:
    """Verification results a caller could hand `apply`, without running a resolver."""
    return [
        VerifyResult(
            version=version,
            hook_id=hook_id,
            status=VERIFY_OK,
            before="",
            after=HOSTS[version],
            expected=HOSTS[version],
            reason="verified in a fixture",
        )
        for version in versions
    ]


def entry_text(entry: Mapping[str, Any], depth: int = 8) -> str:
    """One `hosts` entry as it appears in the file: serialised and re-indented."""
    pad = " " * depth
    return "\n".join(pad + line for line in dump(entry).rstrip("\n").splitlines())


def leftovers(manifest: Path) -> list[str]:
    """Temp files an interrupted atomic write would have left beside the manifest.

    Named by prefix rather than "everything that is not hooks.json", because the
    directory legitimately holds fixtures; what must never survive is the scratch
    file `_write_atomically` renames from.
    """
    return sorted(
        item.name
        for item in manifest.parent.iterdir()
        if item.name.startswith(manifest.name + ".")
    )


class TempManifestCase(unittest.TestCase):
    """Every test gets its own copy of the manifest in its own directory."""

    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="dfinsta-patch-test-")
        self.addCleanup(self._scratch.cleanup)
        self.root = Path(self._scratch.name)
        self.manifest = manifest_with(self.root, HOOK, BY_AGENT_HOST)
        self.original = self.manifest.read_text(encoding="utf-8")

    def assertUntouched(self) -> None:
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), self.original)
        self.assertEqual(leftovers(self.manifest), [])


# ------------------------------------------------------------------ the refusals


class PlanRefusalTests(TempManifestCase):
    def test_a_proposal_that_found_no_fingerprint_is_refused_and_materialises_nothing(self):
        patch = plan(no_fingerprint(OTHER_HOOK), self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_NO_FINGERPRINT])
        self.assertFalse(patch.writable)
        self.assertIsNone(patch.proposed)
        self.assertIn("still needs an agent", patch.refusals[0].reason)
        self.assertUntouched()

    def test_a_fingerprint_measured_on_one_version_is_refused(self):
        one = hand_built(proposal(), selections=(selection("439", [HOSTS["439"]]),))
        patch = plan(one, self.manifest)
        self.assertIn(REFUSE_ONE_VERSION, [item.code for item in patch.refusals])
        self.assertEqual(patch.versions, ("439",))
        self.assertGreaterEqual(MIN_CORROBORATING_VERSIONS, 2)
        self.assertUntouched()

    def test_a_fingerprint_measured_on_no_version_at_all_is_refused(self):
        none = hand_built(proposal(), selections=())
        patch = plan(none, self.manifest)
        self.assertIn(REFUSE_ONE_VERSION, [item.code for item in patch.refusals])
        self.assertIn("no version at all", patch.refusals[0].reason)

    def test_two_versions_is_enough_and_is_not_refused_for_corroboration(self):
        """The positive control: the two-version rule must be able to pass."""
        patch = plan(proposal(versions=("439", "430")), self.manifest)
        self.assertNotIn(REFUSE_ONE_VERSION, [item.code for item in patch.refusals])
        self.assertEqual(patch.versions, ("439", "430"))

    def test_a_fingerprint_that_selected_the_wrong_class_on_a_version_is_refused(self):
        wrong = hand_built(
            proposal(),
            selections=(
                selection("439", [HOSTS["439"]]),
                # Exactly what the 439 systrace literal did on 430: one class, and
                # it was Lcom/instagram/profile/actionbar/ProfileActionBar;.
                selection("430", ["Lcom/instagram/profile/actionbar/ProfileActionBar;"]),
            ),
        )
        patch = plan(wrong, self.manifest)
        codes = [item.code for item in patch.refusals]
        self.assertIn(REFUSE_SELECTION_NOT_EXACT, codes)
        self.assertIn("One class is not the same as the right class", "".join(
            item.reason for item in patch.refusals
        ))
        self.assertUntouched()

    def test_a_fingerprint_that_selected_several_classes_is_refused(self):
        several = hand_built(
            proposal(),
            selections=(
                selection("439", [HOSTS["439"], "LX/0AqB;"]),
                selection("430", [HOSTS["430"]]),
            ),
        )
        patch = plan(several, self.manifest)
        self.assertIn(REFUSE_SELECTION_NOT_EXACT, [item.code for item in patch.refusals])

    def test_a_fingerprint_that_selected_nothing_is_refused(self):
        empty = hand_built(
            proposal(),
            selections=(selection("439", []), selection("430", [HOSTS["430"]])),
        )
        patch = plan(empty, self.manifest)
        self.assertIn(REFUSE_SELECTION_NOT_EXACT, [item.code for item in patch.refusals])

    def test_a_literal_carrying_an_absolute_path_is_refused_and_never_materialised(self):
        # This one reaches `plan` through the REAL constructor:
        # `generalise.forbidden_reason` does not refuse a path-shaped string, so
        # without this stage's own rule it would be written into the manifest.
        leak = proposal(literal="/home/arnav/AI/dfinsta-redux/work/430-clean-build-v2/")
        patch = plan(leak, self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertIn("absolute path", patch.refusals[0].reason)
        self.assertFalse(patch.writable)
        self.assertIsNone(patch.proposed)
        self.assertUntouched()

    def test_a_literal_carrying_an_obfuscated_descriptor_is_refused(self):
        leak = proposal(literal="host is LX/0Di2; on this version")
        patch = plan(leak, self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertIn("LX/0Di2;", patch.refusals[0].reason)
        self.assertFalse(patch.writable)

    def test_a_literal_carrying_a_resource_id_is_refused(self):
        # `generalise.forbidden_reason` refuses this one too, so the proposal has
        # to be assembled around its constructor. Kept anyway: this stage is the
        # last thing between a value and the manifest, and a guard that only ever
        # ran upstream is a guard nobody can prove still runs.
        leak = hand_built(proposal(), literal="label_0x7f134a0e_default")
        patch = plan(leak, self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertIn("resource id", patch.refusals[0].reason)
        self.assertFalse(patch.writable)

    def test_a_forbidden_co_literal_is_refused_as_well_as_a_forbidden_primary(self):
        leak = proposal(co_literals=("/home/arnav/AI/dfinsta-redux/work/",))
        patch = plan(leak, self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertIn("co_literal", patch.refusals[0].reason)

    def test_the_real_literals_are_the_positive_control_for_the_forbidden_rules(self):
        """A rule that refuses everything would pass every refusal test above."""
        self.assertEqual(forbidden_in_value(LITERAL), "")
        self.assertEqual(forbidden_in_value(CO_LITERAL), "")
        patch = plan(proposal(), self.manifest)
        self.assertNotIn(REFUSE_FORBIDDEN_VALUE, [item.code for item in patch.refusals])

    def test_a_note_may_cite_a_version_stamped_descriptor_but_not_a_path_or_an_id(self):
        # The shipped manifest's own host notes read "439 -> LX/0DnT; (1 of
        # 181,421 classes)". A rule that refused those would refuse every real
        # proposal for a reason the manifest itself disproves.
        self.assertEqual(forbidden_in_note(NOTE), "")
        self.assertIn("absolute path", forbidden_in_note("measured in /home/arnav/work/x/"))
        self.assertIn("resource id", forbidden_in_note("the label is 0x7f134a0e"))
        patch = plan(proposal(note="measured in /home/arnav/AI/dfinsta-redux/work/"), self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertFalse(patch.writable)

    def test_a_by_anchor_host_is_not_churned_into_a_by_literal_one(self):
        manifest = manifest_with(self.root / "anchor", HOOK, BY_ANCHOR_HOST)
        patch = plan(proposal(), manifest)
        codes = [item.code for item in patch.refusals]
        self.assertIn(REFUSE_NOT_STRONGER, codes)
        self.assertIn("by_anchor", patch.refusals[-1].reason)

    def test_a_working_named_host_is_not_churned_into_a_by_literal_one(self):
        manifest = manifest_with(self.root / "named", HOOK, NAMED_STABLE_HOST)
        patch = plan(proposal(), manifest)
        self.assertIn(REFUSE_NOT_STRONGER, [item.code for item in patch.refusals])

    def test_an_obfuscated_named_host_is_weaker_than_a_literal_and_may_be_replaced(self):
        # `named` at LX/06X7; is not a fingerprint at all: the name still exists
        # in the next version on an unrelated class.
        manifest = manifest_with(self.root / "obf", HOOK, NAMED_OBFUSCATED_HOST)
        patch = plan(proposal(), manifest)
        self.assertNotIn(REFUSE_NOT_STRONGER, [item.code for item in patch.refusals])
        self.assertEqual(strength_key(NAMED_OBFUSCATED_HOST), "named_obfuscated")
        self.assertLess(STRENGTH["named_obfuscated"], STRENGTH["by_agent"])

    def test_the_strength_ladder_ranks_by_anchor_above_everything_else(self):
        self.assertEqual(max(STRENGTH, key=lambda key: STRENGTH[key]), "by_anchor")
        self.assertLess(STRENGTH["by_agent"], STRENGTH["by_literal"])
        self.assertLess(STRENGTH["by_literal"], STRENGTH["named"])

    def test_a_proposal_that_would_not_retire_the_agent_is_refused(self):
        blocked = proposal(
            blocks=(
                (
                    "resolve.search_hosts",
                    "the literals are absent from the API-surface index, so the hook would "
                    "resolve to ZERO candidates and escalate anyway",
                ),
            )
        )
        patch = plan(blocked, self.manifest)
        self.assertIn(REFUSE_BLOCKED, [item.code for item in patch.refusals])
        self.assertIn("ZERO candidates", patch.refusals[0].reason)

    def test_a_proposal_for_a_hook_this_manifest_does_not_have_is_refused(self):
        patch = plan(proposal(hook_id="a_hook_that_does_not_exist"), self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_UNKNOWN_HOOK])
        self.assertFalse(patch.writable)

    def test_a_hook_declaring_several_host_fingerprints_is_refused(self):
        manifest = manifest_with(
            self.root / "two", HOOK, [dict(BY_AGENT_HOST), dict(NAMED_STABLE_HOST)]
        )
        patch = plan(proposal(), manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_SEVERAL_HOSTS])
        self.assertFalse(patch.writable)

    def test_a_host_kind_this_stage_cannot_rank_is_refused_rather_than_guessed(self):
        # `plan` reads the manifest as JSON rather than through `load_manifest`,
        # so a kind nothing validated reaches the strength comparison. Refusing is
        # the only safe answer: an unrankable kind cannot be said to be improved.
        manifest = manifest_with(self.root / "odd", HOOK, {"kind": "by_vibes"})
        patch = plan(proposal(), manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_NOT_STRONGER])
        self.assertIn("strength ladder", patch.refusals[0].reason)
        self.assertFalse(patch.writable)
        # And the renderer must survive it: a traceback where a refusal belongs
        # loses the reason the stage stopped.
        self.assertIn("unrankable", "\n".join(render_patch(patch)))

    def test_a_manifest_in_another_format_is_refused_rather_than_reformatted(self):
        reformatted = self.root / "four-space" / "hooks.json"
        reformatted.parent.mkdir()
        reformatted.write_text(
            json.dumps(json.loads(self.original), indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        before = reformatted.read_text(encoding="utf-8")
        patch = plan(proposal(), reformatted)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_REFORMATS])
        self.assertFalse(patch.writable)
        self.assertEqual(reformatted.read_text(encoding="utf-8"), before)

    def test_plan_writes_nothing_at_all(self):
        plan(proposal(), self.manifest)
        plan(no_fingerprint(), self.manifest)
        self.assertUntouched()

    def test_plan_refuses_something_that_is_not_a_proposal(self):
        # A caller contract violation is a failure, not a finding.
        with self.assertRaises(PatchError):
            plan({"hook_id": HOOK, "kind": "by_literal"}, self.manifest)  # type: ignore[arg-type]

    def test_a_patch_that_neither_changes_anything_nor_refuses_cannot_exist(self):
        with self.assertRaises(PatchError) as caught:
            Patch(hook_id=HOOK, manifest_path=self.manifest, current=None, proposed=None)
        self.assertIn("neither a change nor a reason", str(caught.exception))


# ------------------------------------------------------------------- the patch


class PatchContentTests(TempManifestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch = plan(proposal(), self.manifest)

    def test_the_patch_is_not_refused_and_carries_both_fingerprints(self):
        self.assertEqual(self.patch.refusals, ())
        self.assertEqual(self.patch.hook_id, HOOK)
        self.assertEqual(dict(self.patch.current or {}), BY_AGENT_HOST)
        self.assertEqual(
            dict(self.patch.proposed or {}),
            {
                "kind": "by_literal",
                "literal": LITERAL,
                "co_literals": [CO_LITERAL],
                "note": NOTE,
            },
        )

    def test_the_patch_carries_the_exact_json_that_would_change(self):
        self.assertIn(f'"literal": "{LITERAL}"', self.patch.after_entry)
        self.assertIn('"kind": "by_agent"', self.patch.before_entry)
        self.assertEqual(json.loads(self.patch.after_entry), dict(self.patch.proposed or {}))

    def test_the_patch_records_the_versions_and_the_host_each_was_measured_against(self):
        self.assertEqual(self.patch.versions, ("439", "430"))
        self.assertEqual(self.patch.expected, HOSTS)

    def test_the_diff_touches_only_the_one_hosts_entry(self):
        diff = self.patch.diff()
        self.assertTrue(diff)
        changed = [
            line[1:].strip().rstrip(",")
            for line in diff
            if line[:1] in {"+", "-"} and not line.startswith(("---", "+++"))
        ]
        allowed = {
            line.strip().rstrip(",")
            for entry in (self.patch.current, self.patch.proposed)
            for line in dump(entry).splitlines()
        }
        self.assertTrue(changed)
        self.assertLessEqual(set(changed), allowed)

    def test_render_names_the_current_and_proposed_kinds(self):
        lines = "\n".join(render_patch(self.patch))
        self.assertIn("'by_agent'", lines)
        self.assertIn("'by_literal'", lines)
        self.assertIn("PLANNED", lines)


# ------------------------------------------------------------------- applying it


class ApplyTests(TempManifestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch = plan(proposal(), self.manifest)
        self.assertEqual(self.patch.refusals, ())

    def test_apply_without_confirm_writes_nothing(self):
        result = apply(self.patch, self.manifest, verified=passing())
        self.assertFalse(result.written)
        self.assertIn(REFUSE_UNCONFIRMED, [item.code for item in result.refusals])
        self.assertUntouched()

    def test_apply_with_confirm_produces_a_manifest_load_manifest_accepts(self):
        result = apply(self.patch, self.manifest, confirm=True, verified=passing())
        self.assertTrue(result.written)
        self.assertEqual(result.refusals, ())
        hooks = {hook.hook_id: hook for hook in load_manifest(self.manifest)}
        host = hooks[HOOK].hosts[0]
        self.assertEqual(host.kind, "by_literal")
        self.assertEqual(host.literal, LITERAL)
        self.assertEqual(host.co_literals, (CO_LITERAL,))
        self.assertEqual(leftovers(self.manifest), [])

    def test_every_byte_outside_the_patched_entry_is_unchanged(self):
        apply(self.patch, self.manifest, confirm=True, verified=passing())
        after = self.manifest.read_text(encoding="utf-8")
        self.assertNotEqual(after, self.original)
        # Not "the JSON is equivalent": swapping the one entry's text back must
        # reproduce the original file byte for byte.
        rolled_back = after.replace(
            entry_text(self.patch.proposed or {}), entry_text(self.patch.current or {})
        )
        self.assertEqual(rolled_back, self.original)

    def test_a_failed_rename_leaves_the_original_manifest_intact(self):
        with mock.patch(
            "dfinsta_pipeline.manifest_patch.os.replace",
            side_effect=OSError("simulated: the rename died part-way"),
        ):
            with self.assertRaises(OSError):
                apply(self.patch, self.manifest, confirm=True, verified=passing())
        self.assertUntouched()

    def test_a_patched_manifest_that_does_not_load_is_never_written(self):
        broken = json.loads(self.patch.document_after)
        # An empty marker: `str.count("")` returns len+1, so this hook could never
        # resolve again. `load_manifest` refuses it.
        broken["hooks"][0]["marker"] = ""
        patch = dataclasses.replace(self.patch, document_after=dump(broken))
        with self.assertRaises(ManifestError):
            load_manifest_from_text(patch.document_after)
        result = apply(patch, self.manifest, confirm=True, verified=passing())
        self.assertFalse(result.written)
        self.assertEqual([item.code for item in result.refusals], [REFUSE_DOES_NOT_LOAD])
        self.assertUntouched()

    def test_apply_refuses_a_refused_plan(self):
        refused = plan(proposal(blocks=(("resolve.search_hosts", "would resolve to none"),)), self.manifest)
        result = apply(refused, self.manifest, confirm=True, verified=passing())
        self.assertFalse(result.written)
        self.assertIn(REFUSE_PLAN_REFUSED, [item.code for item in result.refusals])
        self.assertUntouched()

    def test_apply_refuses_a_patch_that_materialised_nothing(self):
        nothing = plan(no_fingerprint(OTHER_HOOK), self.manifest)
        result = apply(nothing, self.manifest, confirm=True, verified=passing())
        self.assertFalse(result.written)
        self.assertUntouched()

    def test_apply_refuses_when_the_manifest_changed_since_the_plan(self):
        self.manifest.write_text(
            self.original.replace('"policy_revision": "2026-08-01"', '"policy_revision": "2026-09-09"'),
            encoding="utf-8",
        )
        moved = self.manifest.read_text(encoding="utf-8")
        result = apply(self.patch, self.manifest, confirm=True, verified=passing())
        self.assertFalse(result.written)
        self.assertEqual([item.code for item in result.refusals], [REFUSE_STALE])
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), moved)

    def test_a_version_with_no_verification_result_is_refused_not_assumed_fine(self):
        result = apply(self.patch, self.manifest, confirm=True, verified=passing(("439",)))
        self.assertFalse(result.written)
        self.assertIn(REFUSE_UNVERIFIED, [item.code for item in result.refusals])
        self.assertIn("430", "".join(item.reason for item in result.refusals))
        self.assertUntouched()

    def test_no_verification_at_all_is_refused(self):
        result = apply(self.patch, self.manifest, confirm=True)
        self.assertFalse(result.written)
        codes = [item.code for item in result.refusals]
        self.assertEqual(codes.count(REFUSE_UNVERIFIED), 2)
        self.assertUntouched()

    def test_a_version_that_verified_as_a_different_host_is_refused(self):
        results = passing(("439",)) + [
            VerifyResult("430", HOOK, VERIFY_CHANGED, before=HOSTS["430"], after="LX/0AqB;",
                         reason="selects the wrong class")
        ]
        result = apply(self.patch, self.manifest, confirm=True, verified=results)
        self.assertFalse(result.written)
        self.assertIn(REFUSE_UNVERIFIED, [item.code for item in result.refusals])
        self.assertUntouched()

    def test_an_unchecked_version_is_refused_exactly_like_a_failing_one(self):
        results = passing(("439",)) + [
            VerifyResult("430", HOOK, VERIFY_UNCHECKED, reason="no decode was given for 430")
        ]
        result = apply(self.patch, self.manifest, confirm=True, verified=results)
        self.assertFalse(result.written)
        self.assertIn(REFUSE_UNVERIFIED, [item.code for item in result.refusals])
        self.assertUntouched()

    def test_a_failing_version_outside_the_measured_set_still_refuses(self):
        results = passing() + [
            VerifyResult("440", HOOK, VERIFY_CHANGED, before="LX/DHo;", after="LX/DVk;",
                         reason="selects a different class on 440")
        ]
        result = apply(self.patch, self.manifest, confirm=True, verified=results)
        self.assertFalse(result.written)
        self.assertIn(REFUSE_UNVERIFIED, [item.code for item in result.refusals])
        self.assertUntouched()

    def test_applying_to_a_manifest_the_patch_was_not_planned_against_is_a_failure(self):
        elsewhere = manifest_with(self.root / "elsewhere", HOOK, BY_AGENT_HOST)
        with self.assertRaises(PatchError) as caught:
            apply(self.patch, elsewhere, confirm=True, verified=passing())
        self.assertIn("planned against", str(caught.exception))

    def test_apply_refuses_something_that_is_not_a_patch(self):
        with self.assertRaises(PatchError):
            apply({"hook_id": HOOK}, self.manifest, confirm=True)  # type: ignore[arg-type]

    def test_an_apply_result_cannot_both_write_and_refuse(self):
        from dfinsta_pipeline.manifest_patch import ApplyResult, Refusal

        with self.assertRaises(PatchError):
            ApplyResult(HOOK, self.manifest, True, (Refusal("x", "y"),))
        with self.assertRaises(PatchError):
            ApplyResult(HOOK, self.manifest, False, ())


def load_manifest_from_text(document: str):
    with tempfile.TemporaryDirectory(prefix="dfinsta-check-") as scratch:
        path = Path(scratch) / "hooks.json"
        path.write_text(document, encoding="utf-8")
        return load_manifest(path)


# ------------------------------------------------------------------ verifying it


class VerifyTests(TempManifestCase):
    def setUp(self) -> None:
        super().setUp()
        self.patch = plan(proposal(), self.manifest)
        self.decodes = self.root / "decodes"
        self.decodes.mkdir()

    def decode(self, version: str, **kwargs: Any) -> DecodeUnderTest:
        return build_decode(self.decodes, version, **kwargs)

    def test_a_fingerprint_that_selects_the_known_host_verifies_on_every_version(self):
        results = verify(self.patch, [self.decode("439"), self.decode("430")])
        self.assertEqual([item.version for item in results], ["439", "430"])
        for item in results:
            self.assertEqual(item.status, VERIFY_OK, item.reason)
            self.assertEqual(item.after, HOSTS[item.version])
            # The by_agent fingerprint it replaces resolves to nothing without an
            # agent, which is why `expected` and not `before` is the answer key.
            self.assertEqual(item.before, "")
            self.assertEqual(item.outcome_before, "needs_agent")
            self.assertEqual(item.outcome_after, "resolved")

    def test_a_version_with_no_decode_is_reported_unchecked_and_is_not_a_pass(self):
        results = verify(self.patch, [self.decode("439")])
        by_version = {item.version: item for item in results}
        self.assertEqual(sorted(by_version), ["430", "439"])
        self.assertEqual(by_version["430"].status, VERIFY_UNCHECKED)
        self.assertFalse(by_version["430"].ok)
        self.assertFalse(by_version["430"].failed)
        self.assertIn("not a pass", by_version["430"].reason)

    def test_verify_reports_one_result_per_version_even_with_no_decodes_at_all(self):
        results = verify(self.patch, [])
        self.assertEqual([item.status for item in results], [VERIFY_UNCHECKED] * 2)
        self.assertFalse(any(item.ok for item in results))

    def moved_host_fixture(self) -> tuple[Patch, DecodeUnderTest]:
        """A manifest whose current fingerprint resolves somewhere else than the patch does.

        `LX/0AAA;` and the real host both carry the anchor; the index is narrowed
        so only the real host carries the literals, which is what makes the two
        fingerprints disagree instead of both selecting two classes.
        """
        manifest = manifest_with(
            self.root / "obf", HOOK, {"kind": "named", "descriptor": "LX/0AAA;"}
        )
        target = build_decode(
            self.decodes, "439", host=HOSTS["439"], extra_anchored={"LX/0AAA;": HOST_SMALI}
        )
        rewrite_api(target.index, {LITERAL: [HOSTS["439"]], CO_LITERAL: [HOSTS["439"]]})
        return manifest, target

    def test_a_fingerprint_that_moves_the_host_the_manifest_resolves_today_fails(self):
        manifest, target = self.moved_host_fixture()
        # The proposal's answer key AGREES with the manifest — both say LX/0AAA; —
        # and the fingerprint still selects a different class. Nothing but running
        # the resolver could have found this.
        agreeing = hand_built(
            proposal(),
            selections=(
                Selection.measure("439", (LITERAL, CO_LITERAL), ["LX/0AAA;"], "LX/0AAA;"),
                selection("430", [HOSTS["430"]]),
            ),
        )
        patch = plan(agreeing, manifest)
        self.assertEqual(patch.refusals, ())
        found = next(item for item in verify(patch, [target]) if item.version == "439")
        self.assertEqual(found.status, VERIFY_CHANGED)
        self.assertEqual(found.before, "LX/0AAA;")
        self.assertEqual(found.after, HOSTS["439"])
        self.assertTrue(found.failed)
        self.assertIn("One class is not the same as the right class", found.reason)

    def test_a_manifest_and_a_proposal_that_disagree_about_the_host_cannot_verify(self):
        manifest, target = self.moved_host_fixture()
        patch = plan(proposal(), manifest)
        self.assertEqual(patch.refusals, ())
        found = next(item for item in verify(patch, [target]) if item.version == "439")
        self.assertEqual(found.status, VERIFY_CHANGED)
        self.assertIn("Two answer keys that disagree", found.reason)
        self.assertFalse(found.ok)

    def test_a_fingerprint_that_selects_nothing_fails(self):
        target = build_decode(self.decodes, "439", literals_on_host=False)
        results = verify(self.patch, [target])
        found = next(item for item in results if item.version == "439")
        self.assertEqual(found.status, VERIFY_UNRESOLVED)
        self.assertEqual(found.after, "")
        self.assertTrue(found.failed)

    def test_a_decode_for_a_version_the_proposal_never_measured_has_no_baseline(self):
        extra = build_decode(self.decodes, "439", host="LX/0DDD;")
        extra = DecodeUnderTest("440", extra.decode, extra.index)
        results = verify(self.patch, [self.decode("439"), self.decode("430"), extra])
        found = next(item for item in results if item.version == "440")
        self.assertEqual(found.status, VERIFY_NO_BASELINE)
        self.assertFalse(found.ok)
        self.assertFalse(found.failed)
        self.assertIn("evidence of nothing", found.reason)

    def test_verify_refuses_two_decodes_for_one_version(self):
        first = self.decode("439")
        with self.assertRaises(PatchError) as caught:
            verify(self.patch, [first, DecodeUnderTest("439", first.decode, first.index)])
        self.assertIn("two decodes", str(caught.exception))

    def test_verify_refuses_a_patch_that_materialised_nothing(self):
        nothing = plan(no_fingerprint(OTHER_HOOK), self.manifest)
        with self.assertRaises(PatchError):
            verify(nothing, [])

    def test_an_index_bound_to_another_decode_reads_as_unchecked_not_as_a_pass(self):
        good = self.decode("439")
        other = self.decode("430")
        crossed = DecodeUnderTest("439", good.decode, other.index)
        results = verify(self.patch, [crossed])
        found = next(item for item in results if item.version == "439")
        self.assertEqual(found.status, VERIFY_UNCHECKED)
        self.assertIn("could not be resolved at all", found.reason)

    def test_verify_writes_nothing(self):
        verify(self.patch, [self.decode("439"), self.decode("430")])
        self.assertUntouched()

    def test_a_verified_patch_applies_end_to_end(self):
        results = verify(self.patch, [self.decode("439"), self.decode("430")])
        self.assertTrue(all(item.ok for item in results))
        outcome = apply(self.patch, self.manifest, confirm=True, verified=results)
        self.assertTrue(outcome.written)
        hooks = {hook.hook_id: hook for hook in load_manifest(self.manifest)}
        self.assertEqual(hooks[HOOK].hosts[0].literal, LITERAL)


class ByAnchorTests(TempManifestCase):
    """The kind that actually moved the number, now that a proposal can express it.

    The agent count fell 2 -> 0 between Instagram 439 and 440 because two
    `by_anchor` entries were hand-written. Until `generalise.generalise_anchor`
    landed, this stage could plan, verify and apply every promotion except that
    one. These are the tests for the path that matters.
    """

    def setUp(self) -> None:
        super().setUp()
        self.patch = plan(anchor_proposal(), self.manifest)
        self.decodes = self.root / "decodes"
        self.decodes.mkdir()

    def test_a_by_anchor_proposal_plans_a_real_patch(self):
        self.assertEqual(self.patch.refusals, ())
        self.assertTrue(self.patch.writable)
        self.assertEqual(dict(self.patch.current or {}), BY_AGENT_HOST)
        self.assertEqual(self.patch.versions, ("439", "430"))

    def test_a_by_anchor_entry_carries_exactly_a_kind_and_a_note(self):
        # No literal and no descriptor: a second fingerprint alongside the anchor
        # would be a second source of truth with no rule for which wins, and
        # `HostFingerprint` refuses one outright.
        self.assertEqual(set(self.patch.proposed or {}), {"kind", "note"})
        self.assertEqual((self.patch.proposed or {})["kind"], "by_anchor")

    def test_a_by_anchor_patch_states_that_the_value_rules_applied_to_nothing(self):
        # The load-bearing half of this test is the CONTROL underneath it: an
        # empty scrub and a passed scrub are the same silence, so the by_anchor
        # patch must say which one it is, and the by_literal patch must show the
        # fields a real scrub covers.
        self.assertEqual(self.patch.checked_values, ())
        self.assertIn("none apply to a by_anchor fingerprint", self.patch.value_rule_note)
        self.assertIn("already in the manifest", self.patch.value_rule_note)
        self.assertIn(self.patch.value_rule_note, "\n".join(render_patch(self.patch)))

        literal_patch = plan(proposal(), self.manifest)
        self.assertEqual(literal_patch.checked_values, ("literal", "co_literals"))
        self.assertIn("applied to: literal, co_literals", literal_patch.value_rule_note)

    def test_a_kind_with_no_value_rule_is_refused_rather_than_exempted(self):
        with mock.patch.object(
            Proposal, "host_entry", return_value={"kind": "by_vibes", "note": ""}
        ):
            patch = plan(anchor_proposal(), self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_NO_VALUE_RULE])
        self.assertFalse(patch.writable)
        self.assertIn("same silence", patch.refusals[0].reason)

    def test_every_fingerprint_kind_the_manifest_accepts_has_a_value_rule(self):
        # If `hook_manifest` gains a kind and this table does not, that kind would
        # otherwise be refused at plan time — which is the safe direction, and this
        # is what says so out loud rather than leaving it to be discovered.
        accepted = {"named", "by_literal", "by_agent", "by_anchor"}
        self.assertEqual(set(VALUE_FIELDS), accepted)
        for kind in accepted:
            with self.subTest(kind=kind):
                HostFingerprint(
                    kind,
                    descriptor="Lcom/instagram/Foo;" if kind == "named" else None,
                    literal="a/b/" if kind == "by_literal" else None,
                )
                if not VALUE_FIELDS[kind]:
                    # An empty rule needs a stated reason; a blank entry would be
                    # an exemption nobody argued for.
                    self.assertIn(kind, NO_VALUE_REASON)
                    self.assertTrue(NO_VALUE_REASON[kind].strip())
        self.assertEqual(set(VALUE_FIELDS), set(STRENGTH) - {"named_obfuscated"} | {"named"})

    def test_a_by_anchor_host_is_never_replaced_by_another_by_anchor(self):
        manifest = manifest_with(self.root / "anchor", HOOK, BY_ANCHOR_HOST)
        patch = plan(anchor_proposal(), manifest)
        self.assertIn(REFUSE_NOT_STRONGER, [item.code for item in patch.refusals])
        self.assertIn("rank 4", patch.refusals[-1].reason)

    def test_a_by_anchor_proposal_still_needs_two_versions(self):
        one = hand_built(anchor_proposal(), selections=(selection("439", [HOSTS["439"]], ()),))
        patch = plan(one, self.manifest)
        self.assertIn(REFUSE_ONE_VERSION, [item.code for item in patch.refusals])

    def test_a_by_anchor_proposal_still_needs_every_version_exact(self):
        wrong = hand_built(
            anchor_proposal(),
            selections=(
                selection("439", [HOSTS["439"]], ()),
                selection("430", ["Lcom/instagram/profile/actionbar/ProfileActionBar;"], ()),
            ),
        )
        patch = plan(wrong, self.manifest)
        self.assertIn(REFUSE_SELECTION_NOT_EXACT, [item.code for item in patch.refusals])

    def test_a_by_anchor_note_carrying_a_path_is_still_refused(self):
        leak = anchor_proposal(note="measured in /home/arnav/AI/dfinsta-redux/work/")
        patch = plan(leak, self.manifest)
        self.assertEqual([item.code for item in patch.refusals], [REFUSE_FORBIDDEN_VALUE])
        self.assertFalse(patch.writable)

    def test_a_by_anchor_patch_verifies_against_a_decode_and_applies(self):
        targets = [build_decode(self.decodes, version) for version in ("439", "430")]
        results = verify(self.patch, targets)
        self.assertEqual([item.status for item in results], [VERIFY_OK, VERIFY_OK])
        for item in results:
            self.assertEqual(item.after, HOSTS[item.version])
            self.assertEqual(item.outcome_after, "resolved")
        outcome = apply(self.patch, self.manifest, confirm=True, verified=results)
        self.assertTrue(outcome.written)
        hooks = {hook.hook_id: hook for hook in load_manifest(self.manifest)}
        self.assertEqual(hooks[HOOK].hosts[0].kind, "by_anchor")
        self.assertIsNone(hooks[HOOK].hosts[0].literal)
        rolled_back = self.manifest.read_text(encoding="utf-8").replace(
            entry_text(self.patch.proposed or {}), entry_text(self.patch.current or {})
        )
        self.assertEqual(rolled_back, self.original)

    def test_an_anchor_that_selects_the_wrong_class_fails_verification(self):
        # The decode's host is not the class the proposal measured, so the anchor
        # scan finds a different one. Nothing but running the resolver sees this.
        target = build_decode(self.decodes, "439", host="LX/0BBB;")
        found = next(item for item in verify(self.patch, [target]) if item.version == "439")
        self.assertEqual(found.status, VERIFY_CHANGED)
        self.assertEqual(found.after, "LX/0BBB;")
        self.assertTrue(found.failed)


@unittest.skipUnless(
    all(
        path.exists()
        for path in (
            REPO / "work" / "439-explore" / "stock-439",
            REPO / "work" / "430-clean-build-v2" / "stock-430",
            REPO / "work" / "index-439",
            REPO / "work" / "index-430",
        )
    ),
    "needs the real 430 and 439 decodes and indexes",
)
class RealDecodeLoopTests(unittest.TestCase):
    """The whole loop, on real decodes, on a copy of the real manifest.

    generalise_anchor -> plan -> verify -> apply, for the hook whose anchor has
    the tightest prefilter (26 survivors of 181,421 on 439), so the scan is
    seconds rather than a minute. This is the first path by which the pipeline
    can propose and commit the fingerprint that actually took the agent count
    from 2 to 0, and it is the regression test for that whole claim.
    """

    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="dfinsta-loop-test-")
        self.addCleanup(self._scratch.cleanup)
        self.root = Path(self._scratch.name)
        self.manifest = manifest_with(self.root, HOOK, BY_AGENT_HOST)
        self.original = self.manifest.read_text(encoding="utf-8")
        self.targets = [
            DecodeUnderTest(
                "430",
                REPO / "work" / "430-clean-build-v2" / "stock-430",
                REPO / "work" / "index-430",
            ),
            DecodeUnderTest(
                "439",
                REPO / "work" / "439-explore" / "stock-439",
                REPO / "work" / "index-439",
            ),
        ]

    def test_the_pipeline_proposes_and_commits_the_fingerprint_a_human_hand_wrote(self):
        from dfinsta_pipeline.generalise import KnownHost, generalise_anchor

        hooks = {hook.hook_id: hook for hook in load_manifest(self.manifest)}
        known = [
            KnownHost("439", self.targets[1].decode, "LX/0Di2;", "smali_classes6/X/0Di2.smali"),
            KnownHost("430", self.targets[0].decode, "LX/06X7;", "smali_classes6/X/06X7.smali"),
        ]
        proposal = generalise_anchor(hooks[HOOK], known)
        self.assertEqual(proposal.kind, "by_anchor", proposal.reason)

        patch = plan(proposal, self.manifest)
        self.assertEqual(patch.refusals, (), [item.reason for item in patch.refusals])
        self.assertEqual(set(patch.proposed or {}), {"kind", "note"})

        results = verify(patch, self.targets)
        self.assertEqual([(item.version, item.status) for item in results],
                         [("439", VERIFY_OK), ("430", VERIFY_OK)])
        self.assertEqual({item.version: item.after for item in results},
                         {"439": "LX/0Di2;", "430": "LX/06X7;"})
        # Today, with the by_agent fingerprint, the hook resolves to nothing at
        # all on both versions — which is precisely why `expected` and not
        # `before` has to be the answer key.
        self.assertEqual({item.before for item in results}, {""})

        outcome = apply(patch, self.manifest, confirm=True, verified=results)
        self.assertTrue(outcome.written, [item.reason for item in outcome.refusals])
        committed = {hook.hook_id: hook for hook in load_manifest(self.manifest)}[HOOK]
        self.assertEqual(committed.hosts[0].kind, "by_anchor")
        # And what it committed is the kind the shipped manifest already carries,
        # derived from measurement instead of from a person reading a JSON file.
        shipped = {hook.hook_id: hook for hook in load_manifest(REAL_MANIFEST)}[HOOK]
        self.assertEqual(committed.hosts[0].kind, shipped.hosts[0].kind)
        rolled_back = self.manifest.read_text(encoding="utf-8").replace(
            entry_text(patch.proposed or {}), entry_text(patch.current or {})
        )
        self.assertEqual(rolled_back, self.original)


def rewrite_api(index_dir: Path, api_paths: Mapping[str, Sequence[str]]) -> None:
    """Narrow an index's literal -> classes map after the fact."""
    path = index_dir / "api_surface.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["api_paths"] = {key: list(value) for key, value in api_paths.items()}
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------- reading a proposals file


class ProposalsFileTests(TempManifestCase):
    def test_a_written_proposals_file_round_trips_into_real_proposals(self):
        from dfinsta_pipeline.generalise import write_proposals

        path = self.root / "proposals.json"
        write_proposals(path, [proposal(), no_fingerprint()], "439")
        recovered = read_proposals(path)
        self.assertEqual([item.hook_id for item in recovered], [HOOK, OTHER_HOOK])
        self.assertTrue(recovered[0].found)
        self.assertFalse(recovered[1].found)
        self.assertEqual(recovered[0].literal, LITERAL)
        self.assertEqual({item.version for item in recovered[0].selections}, {"439", "430"})

    @unittest.skipUnless(PROPOSALS_439.is_file(), "the real 439 proposals are not on disk")
    def test_the_real_439_proposals_are_readable_and_plan_refuses_both(self):
        recovered = read_proposals(PROPOSALS_439)
        self.assertEqual(len(recovered), 2)
        for item in recovered:
            patch = plan(item, REAL_MANIFEST)
            self.assertTrue(patch.refused, f"{item.hook_id} was not refused")

    def test_a_by_anchor_proposal_round_trips_through_a_proposals_file(self):
        from dfinsta_pipeline.generalise import write_proposals

        path = self.root / "anchor-proposals.json"
        write_proposals(path, [anchor_proposal()], "439")
        recovered = read_proposals(path)
        self.assertEqual(recovered[0].kind, "by_anchor")
        self.assertTrue(recovered[0].found)
        self.assertEqual(recovered[0].literal, "")
        self.assertEqual(recovered[0].co_literals, ())
        self.assertEqual(set(recovered[0].host_entry()), {"kind", "note"})

    @unittest.skipUnless(BY_ANCHOR_PROPOSAL.is_file(), "the by_anchor proposal is not on disk")
    def test_the_hand_written_by_anchor_document_is_in_another_schema_and_is_refused(self):
        # `generalise.Proposal` can express `by_anchor` now, so this file is no
        # longer refused for its KIND — it is refused for its SHAPE. Its
        # `measured[]` entries carry `anchor_matched`/`known_host`/`classes_scanned`
        # rather than a serialised `Selection`, and reading them partially would
        # manufacture a measurement nobody took.
        with self.assertRaises(PatchError) as caught:
            read_proposals(BY_ANCHOR_PROPOSAL)
        message = str(caught.exception)
        self.assertIn("missing", message)
        self.assertIn("count", message)
        self.assertNotIn("does not model", message)

    def test_a_file_that_is_not_a_proposals_document_is_refused(self):
        path = self.root / "nope.json"
        path.write_text(json.dumps({"hooks": []}), encoding="utf-8")
        with self.assertRaises(PatchError):
            read_proposals(path)


# ------------------------------------------------------------------------- cli


class CommandLineTests(TempManifestCase):
    def setUp(self) -> None:
        super().setUp()
        from dfinsta_pipeline.generalise import write_proposals

        self.proposals = self.root / "proposals.json"
        write_proposals(self.proposals, [proposal()], "439")
        self.decodes = self.root / "decodes"
        self.decodes.mkdir()
        self.targets = [build_decode(self.decodes, version) for version in ("439", "430")]

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = main(list(argv))
        return status, out.getvalue()

    def decode_args(self) -> list[str]:
        args = []
        for target in self.targets:
            args += ["--decode", f"{target.version}:{target.decode}:{target.index}"]
        return args

    def test_plan_prints_the_diff_and_writes_nothing(self):
        status, output = self.run_cli(
            "plan", "--proposals", str(self.proposals), "--manifest", str(self.manifest)
        )
        self.assertEqual(status, 0)
        self.assertIn("PLANNED", output)
        self.assertIn(f'+          "literal": "{LITERAL}"', output)
        self.assertUntouched()

    def test_apply_without_confirm_is_a_dry_run(self):
        status, output = self.run_cli(
            "apply",
            "--proposals",
            str(self.proposals),
            "--manifest",
            str(self.manifest),
            *self.decode_args(),
        )
        self.assertEqual(status, 1)
        self.assertIn("NOT WRITTEN", output)
        self.assertIn(REFUSE_UNCONFIRMED, output)
        self.assertUntouched()

    def test_apply_with_confirm_writes_the_manifest(self):
        status, output = self.run_cli(
            "apply",
            "--proposals",
            str(self.proposals),
            "--manifest",
            str(self.manifest),
            *self.decode_args(),
            "--confirm",
        )
        self.assertEqual(status, 0, output)
        self.assertIn("WRITTEN", output)
        hooks = {hook.hook_id: hook for hook in load_manifest(self.manifest)}
        self.assertEqual(hooks[HOOK].hosts[0].kind, "by_literal")

    def test_apply_with_confirm_but_a_missing_decode_still_writes_nothing(self):
        status, output = self.run_cli(
            "apply",
            "--proposals",
            str(self.proposals),
            "--manifest",
            str(self.manifest),
            "--decode",
            f"439:{self.targets[0].decode}:{self.targets[0].index}",
            "--confirm",
        )
        self.assertEqual(status, 1)
        self.assertIn(REFUSE_UNVERIFIED, output)
        self.assertUntouched()

    def test_verify_reports_per_version_and_exits_nonzero_on_an_unchecked_one(self):
        status, output = self.run_cli(
            "verify",
            "--proposals",
            str(self.proposals),
            "--manifest",
            str(self.manifest),
            "--decode",
            f"439:{self.targets[0].decode}:{self.targets[0].index}",
        )
        self.assertEqual(status, 1)
        self.assertIn(VERIFY_UNCHECKED, output)
        self.assertIn("430", output)
        self.assertUntouched()

    def test_a_malformed_decode_argument_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_cli(
                "verify",
                "--proposals",
                str(self.proposals),
                "--manifest",
                str(self.manifest),
                "--decode",
                "439-and-a-path",
            )

    def test_the_json_output_carries_the_refusals(self):
        manifest = manifest_with(self.root / "anchor", HOOK, BY_ANCHOR_HOST)
        status, output = self.run_cli(
            "plan", "--proposals", str(self.proposals), "--manifest", str(manifest), "--json"
        )
        self.assertEqual(status, 1)
        report = json.loads(output)
        self.assertEqual(report[0]["plan"]["hook_id"], HOOK)
        self.assertTrue(report[0]["plan"]["refused"])
        self.assertIn(
            REFUSE_NOT_STRONGER, [item["code"] for item in report[0]["plan"]["refusals"]]
        )

    def test_a_hook_the_proposals_file_does_not_hold_is_an_error(self):
        status, output = self.run_cli(
            "plan",
            "--proposals",
            str(self.proposals),
            "--manifest",
            str(self.manifest),
            "--hook",
            "nope",
        )
        self.assertEqual(status, 2)
        self.assertIn("no proposal for", output)


class RepoManifestTests(unittest.TestCase):
    def test_no_test_in_this_module_writes_the_repo_manifest(self):
        self.assertEqual(REAL_MANIFEST.read_bytes(), REAL_MANIFEST_BYTES)

    def test_the_repo_manifest_is_written_in_the_form_this_stage_writes(self):
        # If this ever fails, `plan` refuses every patch with `would_reformat` and
        # the stage is inert — a failure that would otherwise look like a policy.
        raw = REAL_MANIFEST.read_text(encoding="utf-8")
        self.assertEqual(serialise(json.loads(raw)), raw)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
