"""What a human's ruling changes, pinned on the module that finally reads it.

Stage 4's gate has asked a human to rule `block` / `offer_toggle` / `ignore` /
`defer` on every candidate since it was written, and until `rulings.py` nothing
consumed the verdict. So these tests are about a *consumer*, and the failures
worth catching are the ones where a consumer looks connected and is not:

* **A decision filed where nothing reads it.** `_hook_for` picks the hook that
  declares itself `strategy == "url_block"`. The first version of it took the
  first hook declaring any URI-path dep, in file order, which is the url-block
  hook today and would silently become an endpoint-*rewriting* hook the moment
  the manifest were reordered. :class:`HookSelectionTests` builds exactly that
  manifest — a rewriting hook first — so a revert fails rather than passes.

* **A block with no record of who decided it.** :func:`rulings.apply` writes the
  ruling store *before* the manifest. `StoreBeforeManifestTests` fails the
  manifest write and asserts the rulings survived: a decision recorded with no
  block is recoverable, a block with nobody's name on it is not.

* **An absence read as a pass.** `plan` reads DFInsta's own smali to say which
  rulings the app does not yet enforce. A source it could not open must produce a
  note and still report the endpoints — "I could not check" is not "there is
  nothing to check", which is the shape of every absence-assertion bug this
  project has shipped. Wherever an enforcement test asserts an *empty* result it
  carries a positive control in the same test, because `unenforced_endpoints`
  returning `()` against the shipped manifest is worth nothing on its own: a
  function that can only ever return `()` also returns `()`.

* **A suppression that becomes a silent no.** `ignore` stops a candidate being
  surfaced again; `defer` deliberately does not, and a suppressed candidate is
  *reported* in `settled` rather than deleted. Both are driven through
  `assessment.document(..., suppressed=...)` rather than asserted on the helper,
  because the document is the artifact a human signs.

* **A false "two derivations disagree".** `operation_input` carries
  `rulings_sha256` because the store changes the *output*.
  :class:`OperationKeyTests` reproduces the real state that needs it — a crash
  between `complete_operation` and the authority row — where a key that ignored
  the store would find a completed operation recording different bytes and blame
  the derivation.

The manifest fixtures are written through `manifest_patch.serialise`, which is
what the round-trip guard requires — one test writes the same document with a
different dumper on purpose, and watches `plan` refuse it. Every fixture lands in
a `tempfile` directory: `manifest/hooks.json`, `manifest/rulings.jsonl` and the
source trees are read but never written. `tests/test_manifest_patch.py` is the
model for the plan/apply/refusal shape and `tests/test_assessment_record.py` for
the state root, the index fixture and the file fingerprint.

Five of these began life in a `KnownGapTests` class, as characterisations of
behaviour that was reported rather than fixed — an ambiguous manifest reading as
clean, `read_store` leaking a `TypeError`, a CAS miss leaking an `OSError`, a
covered endpoint being appended twice, and a whole-file key scan. All five were
closed, so all five now assert the closed behaviour, and each carries a positive
control in the same test: a refusal that fires for every input is not a check,
and the way to tell the difference is to show the same call still succeeding on
the input it is supposed to accept.
"""

import difflib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Sequence
from unittest import mock

from dfinsta_pipeline import assessment, assessment_record, rulings
from dfinsta_pipeline.assessment import AssessmentError
from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.feature_gate import FeatureDispositionV1, FeatureDispositionsV1
from dfinsta_pipeline.hook_index import HookIndex
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.manifest_patch import serialise
from dfinsta_pipeline.runtime_identity import probe_call
from dfinsta_pipeline.rulings import (
    BLOCKING_VERDICTS,
    REFUSE_DOES_NOT_LOAD,
    REFUSE_NO_HOOK,
    REFUSE_PLAN_REFUSED,
    REFUSE_REFORMATS,
    REFUSE_STALE,
    REFUSE_UNCONFIRMED,
    SUPPRESSING_VERDICTS,
    Ruling,
    RulingError,
    append_rulings,
    endpoint_of,
    existing_preference_keys,
    guarded_endpoints,
    read_store,
    required_build_strings,
    suppressed_candidates,
    audit,
    describe_audit,
    undeclared_endpoints,
    unenforced_endpoints,
)
from dfinsta_pipeline.store import ContentStore

from tests.test_assessment import (
    CURATED_MEMBERS,
    NOVEL_MEMBERS,
    blocking_hooks,
    surface_for,
    write_manifest,
)

# A generic (size, nanosecond mtime, digest) triple. Named for the ledger where
# it was written and used here on the manifest and the ruling store, because the
# statement wanted is the same one: this file was not written. All three
# components, because a same-size overwrite keeps the size, a second-granularity
# mtime misses a fast rewrite, and a digest alone misses a rewrite of identical
# bytes. Every use below has a control that moves all three.
from tests.test_assessment_record import ledger_fingerprint as file_fingerprint
from tests.test_assessment_record import write_fake_index
from tests.test_hook_index import write_index

ROOT = Path(__file__).resolve().parents[1]

#: DFInsta's own source, which is the app's side of every claim in the manifest.
#: `dfinsta_source_430`'s copy is byte-identical, so one path covers both.
REAL_SOURCE = ROOT / "dfinsta_source_439/newCode/com/dfinstagram/hooks.smali"
REAL_MANIFEST = ROOT / "manifest/hooks.json"

#: The seven endpoint literals `throwIfBlocked` actually tests, read off the smali
#: by hand. Written out rather than derived, so this pins the app's behaviour
#: instead of re-deriving it with the function under test.
GUARDED = (
    "/api/v1/clips/homecoming/",
    "/clips/discover",
    "/discover/topical_explore",
    "/feed/reels_tray/",
    "/feed/timeline/",
    "/feed/timeline_stream/",
    "/profile_ads/get_profile_ads/",
)

#: The five preference keys `throwIfBlocked` reads, in the order it reads them.
#: `disable_feed` first, matching the first literal it tests. A whole-file scan
#: put `disable_reels` at the front instead, from `replaceReelsEndpoint` above
#: the guard — the same set by luck, and a different answer the day some other
#: method reads a key this one does not.
PREFERENCE_KEYS = (
    "disable_feed",
    "disable_explore",
    "disable_reels",
    "disable_stories",
    "disable_adds",
)

POLICY_REVISION = "2026-08-01"
OTHER_REVISION = "2026-09-01"
ASSESSMENT_SHA256 = "aa" * 32
RUN_ID = "run-rulings-1"
DECISION_ID = "decision-rulings-1"
RECORDED_AT = "2026-08-04T09:00:00Z"

#: Three endpoints, chosen so the two independent questions a plan asks —
#: "does the manifest already cover it" and "does the app already test it" —
#: can be varied one at a time against the fixture manifest below.
#:
#:   * :data:`UNGUARDED_GAP` — declared by neither. A real 441 candidate, and
#:     the containment rule is why it is a gap: `/feed/reels_tray/` does not
#:     cover `feed/reels_media_stream/` in either direction. This was
#:     `feed/timeline_stream/` until 2026-08-08, when that endpoint became the
#:     first of the six ruled that day to gain a guard — so it must be replaced
#:     again, not deleted, as each of the remaining five is written.
#:   * :data:`GUARDED_GAP` — not in the fixture manifest, but `throwIfBlocked`
#:     tests it, so a ruling on it is a manifest addition and NOT custom code.
#:   * :data:`COVERED_GAP` — covered by the fixture manifest's `/feed/timeline/`
#:     under the containment rule `assessment.is_blocked` uses.
UNGUARDED_GAP = "feed/reels_media_stream/"
GUARDED_GAP = "profile_ads/get_profile_ads/"
COVERED_GAP = "feed/timeline/"


# ------------------------------------------------------------------- fixtures


def hook_entry(hook_id: str, strategy: str, deps: Iterable[str]) -> dict[str, Any]:
    """One manifest hook `load_manifest` accepts, carrying a probe call.

    `apply` writes through `write_manifest_atomically`, which loads the result
    before it renames it, so a fixture the loader refuses would make every apply
    test pass for the wrong reason.
    """
    marker = f"# {hook_id}"
    return {
        "hook_id": hook_id,
        "intent": "block a continuous-content surface",
        "tier": "robust",
        "strategy": strategy,
        "semantic_deps": list(deps),
        "hosts": [
            {"kind": "named", "descriptor": "Lcom/instagram/api/tigon/TigonServiceLayer;"}
        ],
        "anchor": ['const-string v0, "placeholder"'],
        "payload": [probe_call(hook_id), marker],
        "marker": marker,
        "expected_marker_count": 1,
    }


def write_hook_manifest(
    path: Path, hooks: Sequence[dict[str, Any]], *, dumper: Any = serialise
) -> Path:
    """Write a manifest in the form the round-trip guard requires.

    `dumper` exists so one test can write the same document in a *different*
    form and watch `plan` refuse it.
    """
    document = {"schema_version": 1, "policy_revision": POLICY_REVISION, "hooks": list(hooks)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumper(document), encoding="utf-8")
    return path


def dispositions(
    *pairs: tuple[str, str],
    assessment_sha256: str = ASSESSMENT_SHA256,
    policy_revision: str = POLICY_REVISION,
) -> FeatureDispositionsV1:
    """The admitted document, built from `(candidate_id, verdict)` pairs."""
    return FeatureDispositionsV1(
        1,
        assessment_sha256,
        policy_revision,
        tuple(
            FeatureDispositionV1(
                1, candidate_id, verdict, "" if verdict == "ignore" else "measured"
            )
            for candidate_id, verdict in pairs
        ),
    )


def a_ruling(
    candidate_id: str,
    verdict: str,
    *,
    policy_revision: str = POLICY_REVISION,
    recorded_at: str = RECORDED_AT,
    run_id: str = RUN_ID,
) -> Ruling:
    return Ruling(
        candidate_id=candidate_id,
        verdict=verdict,
        rationale="measured",
        run_id=run_id,
        decision_id=DECISION_ID,
        assessment_sha256=ASSESSMENT_SHA256,
        policy_revision=policy_revision,
        recorded_at=recorded_at,
    )


class RulingTestCase(unittest.TestCase):
    """A temp root, a manifest with one url-block hook, and a store path."""

    #: Spelled as the shipped manifest spells them — leading slash, one `api/v1/`
    #: prefix — because the candidate ids the gate mints are spelled the other
    #: way, and that difference is load-bearing everywhere below.
    BLOCK_DEPS = ("/feed/timeline/", "/feed/reels_tray/")

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.store_path = self.tmp / "store" / "rulings.jsonl"
        self.manifest = self.write_manifest()

    def write_manifest(self, hooks: Sequence[dict[str, Any]] | None = None, **kwargs: Any) -> Path:
        if hooks is None:
            hooks = [
                hook_entry("replace_reels_discover_endpoint", "endpoint_replace", ["clips/discover/"]),
                hook_entry("tigon_url_block", "url_block", self.BLOCK_DEPS),
            ]
        return write_hook_manifest(self.tmp / "hooks.json", hooks, **kwargs)

    def plan(self, *pairs: tuple[str, str], **kwargs: Any) -> rulings.RulingPlan:
        arguments: dict[str, Any] = {
            "run_id": RUN_ID,
            "decision_id": DECISION_ID,
            "recorded_at": RECORDED_AT,
            "manifest_path": self.manifest,
            "source_path": REAL_SOURCE,
        }
        arguments.update(kwargs)
        document = arguments.pop("dispositions", None) or dispositions(*pairs)
        return rulings.plan(document, **arguments)

    def entry(self, document: str, hook_id: str) -> dict[str, Any]:
        data = json.loads(document)
        return next(hook for hook in data["hooks"] if hook["hook_id"] == hook_id)

    def require_real_source(self) -> None:
        if not REAL_SOURCE.is_file():
            self.skipTest(f"DFInsta source not present: {REAL_SOURCE}")


# ------------------------------------------------- the endpoint a ruling names


class EndpointOfTests(unittest.TestCase):
    """`gap:` is the only namespace this module knows how to act on."""

    def test_endpoint_of_yields_the_literal_and_refuses_any_other_namespace(self):
        """A wrong guess here writes a made-up literal into the manifest.

        Split on the namespace rather than stripped by length, so an id from
        some other producer is refused instead of yielding an endpoint that
        merely looks plausible — and an empty one is refused too, because
        `"gap:"` would otherwise add `""` to `semantic_deps`, which every
        containment check in `assessment.is_blocked` matches.
        """
        self.assertEqual(endpoint_of("gap:feed/timeline_stream/"), "feed/timeline_stream/")
        self.assertEqual(endpoint_of("gap:/api/v1/clips/homecoming/"), "/api/v1/clips/homecoming/")

        for candidate in ("feed/timeline_stream/", "miss:feed/timeline_stream/", "", "gap"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(RulingError) as caught:
                    endpoint_of(candidate)
                self.assertIn("gap:", str(caught.exception))

        for empty in ("gap:", "gap:   "):
            with self.subTest(candidate=empty):
                with self.assertRaisesRegex(RulingError, "names no endpoint"):
                    endpoint_of(empty)


# ------------------------------------------------------- which hook gains it


class HookSelectionTests(RulingTestCase):
    """The hook is chosen by what it declares itself to be, never by position."""

    def test_hook_for_keys_on_the_url_block_strategy_not_on_file_order(self):
        """The manifest here is ordered the way the revert would get wrong.

        The FIRST hook declaring a URI-path dep is `replace_reels_discover_endpoint`,
        which *rewrites* an endpoint rather than blocking it — its literals name
        what it replaces, and they live at a host call site rather than in
        `throwIfBlocked`. A blocked endpoint filed there is a decision recorded
        in a place nothing reads, and every static check still passes.
        """
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"][0]["hook_id"], "replace_reels_discover_endpoint")
        self.assertEqual(data["hooks"][0]["semantic_deps"], ["clips/discover/"])
        self.assertEqual(rulings._hook_for(data, UNGUARDED_GAP), "tigon_url_block")

        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        self.assertEqual(plan.manifest_additions, ((UNGUARDED_GAP, "tigon_url_block"),))
        self.assertEqual(
            self.entry(plan.document_after, "tigon_url_block")["semantic_deps"],
            [*self.BLOCK_DEPS, UNGUARDED_GAP],
        )
        # And the rewriting hook is untouched, which is the half a file-order
        # revert gets wrong while still producing a plan that applies cleanly.
        self.assertEqual(
            self.entry(plan.document_after, "replace_reels_discover_endpoint")["semantic_deps"],
            ["clips/discover/"],
        )

    def test_hook_for_refuses_when_there_is_not_exactly_one_url_block_hook(self):
        """Zero is nowhere to record it; two makes the choice arbitrary."""
        cases = {
            "zero": [hook_entry("replace_reels", "endpoint_replace", ["clips/discover/"])],
            "two": [
                hook_entry("tigon_url_block", "url_block", ["/feed/timeline/"]),
                hook_entry("second_url_block", "url_block", ["/feed/reels_tray/"]),
            ],
        }
        for label, hooks in cases.items():
            with self.subTest(hooks=label):
                self.manifest = self.write_manifest(hooks)
                data = json.loads(self.manifest.read_text(encoding="utf-8"))
                self.assertIsNone(rulings._hook_for(data, UNGUARDED_GAP))

                plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
                self.assertEqual([item.code for item in plan.refusals], [REFUSE_NO_HOOK])
                # Refused means nothing to write, not "written with a warning".
                self.assertIsNone(plan.document_after)
                self.assertEqual(plan.manifest_additions, ())


# --------------------------------------------------------------------- the plan


class PlanTests(RulingTestCase):
    """What a set of rulings would change, before anything changes."""

    def test_block_and_offer_toggle_add_to_semantic_deps_and_ignore_and_defer_do_not(self):
        """Both blocking verdicts guard the endpoint; the difference is the default.

        The project's feature policy makes `offer_toggle` the default shape for
        anything judged addictive — a switch rather than a silent removal — so
        treating it as "not a block" would leave every toggled endpoint reaching
        the network and stage 4a would propose it again forever.
        """
        plan = self.plan(
            ("gap:feed/timeline_stream/", "block"),
            ("gap:feed/reels_media/", "offer_toggle"),
            ("gap:feed/reels_media_stream/", "ignore"),
            ("gap:feed/injected_reels_media/", "defer"),
        )
        self.assertEqual(plan.refusals, ())
        self.assertEqual(
            plan.manifest_additions,
            (
                ("feed/timeline_stream/", "tigon_url_block"),
                ("feed/reels_media/", "tigon_url_block"),
            ),
        )
        self.assertEqual(
            self.entry(plan.document_after, "tigon_url_block")["semantic_deps"],
            [*self.BLOCK_DEPS, "feed/timeline_stream/", "feed/reels_media/"],
        )
        self.assertEqual(BLOCKING_VERDICTS, frozenset({"block", "offer_toggle"}))

    def test_all_four_verdicts_are_recorded_to_the_store_regardless(self):
        """`ignore` changes nothing in the app, so nothing but this records it.

        Without the store the candidate returns at every gate and a human
        re-decides it at every gate, which is the whole reason the module keeps
        one — so the store must carry the verdicts that change no bytes just as
        much as the ones that do.
        """
        verdicts = ("block", "offer_toggle", "ignore", "defer")
        plan = self.plan(*((f"gap:feed/v{index}/", verdict) for index, verdict in enumerate(verdicts)))
        self.assertEqual([item.verdict for item in plan.store], list(verdicts))

        written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)
        self.assertEqual(refusals, ())
        self.assertTrue(written)
        recorded = read_store(self.store_path)
        self.assertEqual([item.verdict for item in recorded], list(verdicts))
        # Round-trip, because a store that cannot be read back is a record of
        # nothing. Every field the ruling was made with survives.
        self.assertEqual(
            [item.to_dict() for item in recorded], [item.to_dict() for item in plan.store]
        )
        self.assertEqual({item.run_id for item in recorded}, {RUN_ID})
        self.assertEqual({item.decision_id for item in recorded}, {DECISION_ID})
        self.assertEqual({item.assessment_sha256 for item in recorded}, {ASSESSMENT_SHA256})

    def test_an_endpoint_already_in_semantic_deps_produces_a_note_and_no_addition(self):
        """Re-ruling something already blocked must not duplicate the entry.

        The ruling is still recorded — a human decided it again and that is a
        fact about this run — but the manifest is left alone and the plan says
        so, rather than silently writing a second identical rule.
        """
        self.manifest = self.write_manifest(
            [hook_entry("tigon_url_block", "url_block", [UNGUARDED_GAP])]
        )
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        self.assertEqual(plan.refusals, ())
        self.assertEqual(plan.manifest_additions, ())
        self.assertFalse(plan.changes_manifest)
        self.assertEqual(len(plan.store), 1)
        self.assertEqual(len(plan.notes), 1)
        self.assertIn(
            f"{UNGUARDED_GAP} is already covered by tigon_url_block's {UNGUARDED_GAP!r}",
            plan.notes[0],
        )
        self.assertEqual(plan.document_after, plan.document_before)

    def test_an_endpoint_covered_by_a_broader_rule_is_a_note_not_a_second_entry(self):
        """Coverage is containment, the same comparison `assessment.is_blocked` uses.

        The manifest writes `/feed/timeline/` and a candidate id yields
        `feed/timeline/`, so an equality check saw two different strings and
        appended a rule the first already covers — while the note that exists to
        say "nothing to change" stayed silent. The note now names the rule that
        covers it, so a reader can check the claim instead of taking it.

        The positive control is in the same plan: an endpoint that genuinely is
        not covered still becomes an addition. A containment test that matched
        everything would suppress that one too, and the whole stage would go
        quiet in exactly the way "no new features" looks like.
        """
        plan = self.plan((f"gap:{COVERED_GAP}", "block"), (f"gap:{UNGUARDED_GAP}", "block"))
        self.assertEqual(plan.refusals, ())
        self.assertEqual(plan.manifest_additions, ((UNGUARDED_GAP, "tigon_url_block"),))
        self.assertEqual(
            self.entry(plan.document_after, "tigon_url_block")["semantic_deps"],
            [*self.BLOCK_DEPS, UNGUARDED_GAP],
        )
        self.assertEqual(len(plan.notes), 1)
        self.assertEqual(
            plan.notes[0],
            f"{COVERED_GAP} is already covered by tigon_url_block's '/feed/timeline/'; "
            "the ruling is recorded and the manifest is unchanged",
        )
        # Both rulings are still recorded. "The manifest already says this" is
        # not "the human did not decide it".
        self.assertEqual(
            [item.candidate_id for item in plan.store],
            [f"gap:{COVERED_GAP}", f"gap:{UNGUARDED_GAP}"],
        )

    def test_a_candidate_in_another_namespace_is_refused_rather_than_guessed_at(self):
        """The gate's id pattern allows any `namespace:`, and only `gap:` is actionable.

        Acting on one anyway would put a made-up literal into `semantic_deps`,
        where `assessment.blocked_endpoints` would then read it as a rule
        forever. Only a *blocking* verdict has to name an endpoint, so an
        `ignore` on the same id is recorded without complaint.
        """
        plan = self.plan(("miss:feed/timeline_stream/", "block"))
        self.assertEqual([item.code for item in plan.refusals], [rulings.REFUSE_NOT_A_GAP])
        self.assertIsNone(plan.document_after)

        plan = self.plan(("miss:feed/timeline_stream/", "ignore"))
        self.assertEqual(plan.refusals, ())
        self.assertEqual([item.candidate_id for item in plan.store], ["miss:feed/timeline_stream/"])

    def test_a_manifest_that_does_not_round_trip_is_refused_and_yields_no_document(self):
        """A whole-file reformat makes the review of a two-line change worthless.

        Same guard `manifest_patch.plan` carries, and it has to be here too:
        this module writes the manifest through its own path, so a copy of the
        rule that lived only in the other stage would not run.
        """
        self.manifest = self.write_manifest(
            dumper=lambda data: json.dumps(data, indent=4) + "\n"
        )
        before = self.manifest.read_text(encoding="utf-8")
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        self.assertEqual([item.code for item in plan.refusals], [REFUSE_REFORMATS])
        self.assertIsNone(plan.document_after)
        self.assertEqual(plan.manifest_additions, ())

        # And a refused plan may not be applied even with confirm.
        written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)
        self.assertFalse(written)
        self.assertIn(REFUSE_PLAN_REFUSED, [item.code for item in refusals])
        self.assertFalse(self.store_path.exists())
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), before)

    def test_everything_outside_the_patched_entry_is_byte_identical(self):
        """Asserted on the raw file, with an independent dumper.

        `serialise` is what wrote both sides, so comparing against it would only
        prove the module agrees with itself. This compares the bytes on disk
        line by line — the change must be exactly one line becoming two, the
        previous last dep gaining a comma and the new dep beneath it — and then
        re-checks the same claim structurally with `json.dumps(sort_keys=True)`,
        which shares no code with the manifest writer.
        """
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        before = self.manifest.read_text(encoding="utf-8")
        written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)
        self.assertEqual((written, refusals), (True, ()))
        after = self.manifest.read_text(encoding="utf-8")

        old = before.splitlines(keepends=True)
        new = after.splitlines(keepends=True)
        opcodes = [
            code for code in difflib.SequenceMatcher(None, old, new).get_opcodes()
            if code[0] != "equal"
        ]
        self.assertEqual(len(opcodes), 1, f"more than one region changed: {opcodes}")
        tag, first, last, other_first, other_last = opcodes[0]
        self.assertEqual(tag, "replace")
        self.assertEqual(last - first, 1)
        self.assertEqual(other_last - other_first, 2)
        replaced = old[first]
        inserted = new[other_first:other_last]
        self.assertEqual(inserted[0], replaced.rstrip("\n") + ",\n")
        self.assertEqual(inserted[1].strip(), json.dumps(UNGUARDED_GAP))
        # Byte-identical either side of the one changed region.
        self.assertEqual(old[:first], new[:other_first])
        self.assertEqual(old[last:], new[other_last:])

        expected = json.loads(before)
        for hook in expected["hooks"]:
            if hook["hook_id"] == "tigon_url_block":
                hook["semantic_deps"] = [*self.BLOCK_DEPS, UNGUARDED_GAP]
        self.assertEqual(
            json.dumps(json.loads(after), sort_keys=True),
            json.dumps(expected, sort_keys=True),
        )


# -------------------------------------------------------------------- applying


class ApplyTests(RulingTestCase):
    """Nothing is written without a confirmation, and never over a moved file."""

    def test_apply_without_confirm_writes_neither_the_manifest_nor_the_store(self):
        """A dry run in the strongest sense: no file is opened for writing.

        Asserted with a size/mtime/digest fingerprint rather than by reading the
        content back, because a rewrite of identical bytes is still a write —
        and the control at the end moves all three components, so the assertion
        is not one a stopped clock would also pass.
        """
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        before = file_fingerprint(self.manifest)

        written, refusals = rulings.apply(plan, confirm=False, store_path=self.store_path)
        self.assertFalse(written)
        self.assertEqual([item.code for item in refusals], [REFUSE_UNCONFIRMED])
        self.assertFalse(self.store_path.exists())
        self.assertEqual(file_fingerprint(self.manifest), before)

        written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)
        self.assertEqual((written, refusals), (True, ()))
        self.assertTrue(self.store_path.exists())
        self.assertNotEqual(file_fingerprint(self.manifest), before)

    def test_apply_refuses_when_the_manifest_changed_since_the_plan(self):
        """The document reviewed is not the document that would be written."""
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        moved = self.write_manifest(
            [
                hook_entry("replace_reels_discover_endpoint", "endpoint_replace", ["clips/discover/"]),
                hook_entry("tigon_url_block", "url_block", [*self.BLOCK_DEPS, "/x/y/"]),
            ]
        )
        before = file_fingerprint(moved)

        written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)
        self.assertFalse(written)
        self.assertEqual([item.code for item in refusals], [REFUSE_STALE])
        self.assertEqual(file_fingerprint(moved), before)
        # And the store is untouched too: a refusal is not a partial apply.
        self.assertFalse(self.store_path.exists())


class StoreBeforeManifestTests(RulingTestCase):
    """The order of the two writes is the point, not an implementation detail.

    A decision recorded with no block is recoverable — the plan can be re-run.
    A block with no record of who decided it is not: the manifest would say an
    endpoint is blocked and nothing in the tree would say who ruled that, which
    is precisely the disconnection this module exists to close.
    """

    def test_the_store_is_written_before_the_manifest(self):
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"))
        before = file_fingerprint(self.manifest)
        failure = OSError("no space left on device")

        with mock.patch.object(rulings, "write_manifest_atomically", side_effect=failure) as write:
            written, refusals = rulings.apply(plan, confirm=True, store_path=self.store_path)

        self.assertTrue(write.called, "the manifest write was never attempted")
        self.assertFalse(written)
        self.assertEqual([item.code for item in refusals], [REFUSE_DOES_NOT_LOAD])
        self.assertEqual(file_fingerprint(self.manifest), before)

        recorded = read_store(self.store_path)
        self.assertEqual([item.candidate_id for item in recorded], [f"gap:{UNGUARDED_GAP}"])
        self.assertEqual(recorded[0].verdict, "block")

    def test_append_rulings_never_rewrites_what_is_already_there(self):
        """A human changing their mind is a second record, not an edit."""
        append_rulings(self.store_path, [a_ruling("gap:a/", "ignore")])
        first = self.store_path.read_text(encoding="utf-8")
        append_rulings(self.store_path, [a_ruling("gap:a/", "defer")])
        second = self.store_path.read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first))
        self.assertEqual([item.verdict for item in read_store(self.store_path)], ["ignore", "defer"])


class StoreReaderTests(RulingTestCase):
    """A store this cannot read is not one to guess about."""

    def test_read_store_refuses_an_unreadable_line_and_an_unknown_schema(self):
        self.assertEqual(read_store(self.tmp / "never-written.jsonl"), [])
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        self.store_path.write_text("{not json\n", encoding="utf-8")
        with self.assertRaisesRegex(RulingError, "unreadable ruling"):
            read_store(self.store_path)

        self.store_path.write_text(
            canonical_json({"schema_version": 2, "record": {}}) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RulingError, "unsupported ruling schema"):
            read_store(self.store_path)

    def test_every_malformed_line_is_a_ruling_error_naming_the_line(self):
        """One error type out of this module, and a line number on every one.

        `Ruling.from_dict` used to check only for *missing* keys and then splat
        the whole record, so a hand-added `"note"` came out as a bare
        `TypeError` — past `rulings.main`'s `except (RulingError, ValueError)`,
        and past every caller of `suppressed_candidates`, none of which name it.
        A line with no `record` was a `KeyError` for the same reason: a reader
        that refuses through two channels has one channel that is decorative.

        The positive control is last and is the point: a well-formed store still
        round-trips. A reader that refused everything would satisfy every
        assertion above it.
        """
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        record = a_ruling("gap:a/", "ignore").to_dict()
        cases = {
            "unknown field": (
                {"schema_version": 1, "record": {**record, "note": "by hand"}},
                "unknown field",
            ),
            "missing field": (
                {"schema_version": 1, "record": {k: v for k, v in record.items() if k != "verdict"}},
                "missing verdict",
            ),
            "no record": ({"schema_version": 1}, "carries no record"),
            "record is not an object": (
                {"schema_version": 1, "record": ["gap:a/"]},
                "must be an object",
            ),
            "line is not an object": ([1, 2, 3], "must be an object"),
        }
        for label, (line, expected) in cases.items():
            with self.subTest(line=label):
                # Line 1 blank and line 2 bad, so the line number in the message
                # is a real count and not the constant 1.
                self.store_path.write_text("\n" + canonical_json(line) + "\n", encoding="utf-8")
                with self.assertRaises(RulingError) as caught:
                    read_store(self.store_path)
                self.assertIn(expected, str(caught.exception))
                self.assertIn(f"{self.store_path}:2:", str(caught.exception))

        # The control: exactly the same reader, on a store it must accept.
        self.store_path.write_text(
            canonical_json({"schema_version": 1, "record": record}) + "\n", encoding="utf-8"
        )
        self.assertEqual([item.to_dict() for item in read_store(self.store_path)], [record])

    def test_a_ruling_refuses_an_unknown_verdict_and_a_missing_field(self):
        with self.assertRaisesRegex(RulingError, "is not one of"):
            a_ruling("gap:a/", "maybe")
        with self.assertRaisesRegex(RulingError, "needs a run id"):
            Ruling("gap:a/", "block", "why", "", DECISION_ID, ASSESSMENT_SHA256, POLICY_REVISION, "t")


# ----------------------------------------------------------------- suppression


class SuppressionTestCase(RulingTestCase):
    """A real index over the real nine-member grouping, plus a ruling store."""

    def setUp(self) -> None:
        super().setUp()
        decode = (self.tmp / "stock-430").resolve()
        decode.mkdir()
        self.index = HookIndex.load(
            write_index(
                self.tmp / "index",
                decode=str(decode),
                api_paths=surface_for({"LX/05jj;": CURATED_MEMBERS}),
            )
        )
        self.hooks = blocking_hooks()

    def settled_for(self, revision: str = POLICY_REVISION) -> dict[str, dict[str, Any]]:
        """The shape `assessment_record.record` passes to `document`."""
        return {
            candidate: {
                "verdict": ruling.verdict,
                "run_id": ruling.run_id,
                "recorded_at": ruling.recorded_at,
            }
            for candidate, ruling in suppressed_candidates(revision, self.store_path).items()
        }

    def document(self, revision: str = POLICY_REVISION) -> dict[str, Any]:
        return assessment.document(self.index, self.hooks, suppressed=self.settled_for(revision))

    def ids(self, document: dict[str, Any]) -> list[str]:
        return [entry["candidate_id"] for entry in document["candidates"]]


class SuppressionTests(SuppressionTestCase):
    """What a human settled last time must not be re-asked; what they deferred must."""

    def test_ignore_suppresses_on_the_next_assessment_and_defer_does_not(self):
        """`defer` is an explicit "not decided yet", so it coming back is the point.

        Suppressing it would turn indecision into a silent no — the candidate
        would vanish from every future gate with nobody having ruled on it.
        """
        self.assertEqual(self.ids(self.document()), [f"gap:{lit}" for lit in NOVEL_MEMBERS])

        append_rulings(
            self.store_path,
            [
                a_ruling("gap:feed/reels_media/", "ignore"),
                a_ruling("gap:feed/reels_media_stream/", "defer"),
            ],
        )
        self.assertEqual(sorted(self.settled_for()), ["gap:feed/reels_media/"])

        surviving = self.ids(self.document())
        self.assertNotIn("gap:feed/reels_media/", surviving)
        self.assertIn("gap:feed/reels_media_stream/", surviving)
        self.assertEqual(len(surviving), 3)

        # Last, so that a set which gained `defer` fails on the candidate that
        # vanished rather than on the constant that let it.
        self.assertEqual(SUPPRESSING_VERDICTS, frozenset({"ignore"}))

    def test_a_suppressed_candidate_is_reported_in_settled_not_dropped(self):
        """A shorter list with no explanation is unreviewable.

        A human at this gate has to be able to see what a human at the last one
        decided, and why the list is shorter than the grouping. Deleting the
        candidate instead would leave the two numbers in `counts` agreeing with
        a document that has quietly lost a row.
        """
        append_rulings(self.store_path, [a_ruling("gap:feed/reels_media/", "ignore")])
        document = self.document()

        self.assertEqual(document["counts"]["candidates"], 3)
        self.assertEqual(document["counts"]["settled"], 1)
        self.assertEqual(len(document["candidates"]), 3)
        self.assertEqual(len(document["settled"]), 1)
        self.assertEqual(
            document["settled"][0],
            {
                "candidate_id": "gap:feed/reels_media/",
                "verdict": "ignore",
                "run_id": RUN_ID,
                "recorded_at": RECORDED_AT,
            },
        )
        # The grouping still lists all nine members, so the record of what the
        # class holds does not shrink with the open list.
        self.assertEqual(document["groupings"][0]["size"], len(CURATED_MEMBERS))

    def test_suppressed_candidates_is_scoped_to_the_policy_revision(self):
        """A judgement made under rules that no longer apply is not a judgement.

        Same dimension `decisions.reusable` makes a resolution's reuse hang on:
        changing the policy brings every previously-ignored candidate back for a
        fresh decision rather than carrying the old answer forward silently.
        """
        append_rulings(
            self.store_path,
            [a_ruling("gap:feed/reels_media/", "ignore", policy_revision=OTHER_REVISION)],
        )
        self.assertEqual(suppressed_candidates(OTHER_REVISION, self.store_path).keys(), {"gap:feed/reels_media/"})
        self.assertEqual(suppressed_candidates(POLICY_REVISION, self.store_path), {})

        self.assertIn("gap:feed/reels_media/", self.ids(self.document(POLICY_REVISION)))
        self.assertNotIn("gap:feed/reels_media/", self.ids(self.document(OTHER_REVISION)))

    def test_a_later_non_suppressing_ruling_un_suppresses(self):
        """`defer` after `ignore` means the human reopened it.

        The store is append-only and the latest ruling per candidate wins, so a
        reader that only ever accumulated suppressions would make an `ignore`
        permanent and unreversible except by editing history.
        """
        append_rulings(self.store_path, [a_ruling("gap:feed/reels_media/", "ignore")])
        self.assertNotIn("gap:feed/reels_media/", self.ids(self.document()))

        append_rulings(
            self.store_path,
            [a_ruling("gap:feed/reels_media/", "defer", recorded_at="2026-09-01T00:00:00Z")],
        )
        self.assertEqual(suppressed_candidates(POLICY_REVISION, self.store_path), {})
        self.assertIn("gap:feed/reels_media/", self.ids(self.document()))
        # Both records survive: the history of what was decided and when is the
        # value of keeping the file at all.
        self.assertEqual(len(read_store(self.store_path)), 2)

    def test_candidate_ids_reads_only_candidates_so_a_fully_settled_run_raises(self):
        """A gate over nothing would record a human's approval of an empty list.

        `candidate_ids` is the one decoder, and it must not fall back to
        `settled` — the settled entries are a record of closed decisions, not
        candidates to rule on again.
        """
        append_rulings(
            self.store_path,
            [a_ruling(f"gap:{literal}", "ignore") for literal in NOVEL_MEMBERS],
        )
        document = self.document()
        self.assertEqual(document["candidates"], [])
        self.assertEqual(document["counts"], {"groupings": 1, "candidates": 0, "settled": 4, "judged": 0})
        self.assertEqual(
            [entry["candidate_id"] for entry in document["settled"]],
            [f"gap:{literal}" for literal in NOVEL_MEMBERS],
        )
        with self.assertRaisesRegex(AssessmentError, "no candidates"):
            assessment.candidate_ids(document)


# ------------------------------------------------------------- the operation key


class OperationKeyTests(RulingTestCase):
    """The ruling store changes the output, so it has to change the key."""

    def setUp(self) -> None:
        super().setUp()
        self.index = write_fake_index(self.tmp / "index")
        self.recorded_manifest = write_manifest(self.tmp / "recorded-hooks.json")

    def record(self, state_root: Path, **overrides: Any):
        arguments: dict[str, Any] = {
            "run_id": RUN_ID,
            "index_dir": self.index,
            "manifest_path": self.recorded_manifest,
            "allowed_actor": "sam.operator",
            "owner_token": "stage4-owner-1",
            "rulings_path": self.store_path,
        }
        arguments.update(overrides)
        return assessment_record.record(state_root, **arguments)

    def test_operation_input_carries_rulings_sha256(self):
        """Recording twice over an unchanged store is idempotent; a grown store is not.

        The state that needs this is real and reachable: `record` completes the
        operation and files the run-keyed authority row afterwards, so a crash
        between them leaves a completed operation with no row. Re-recording then
        goes straight to `begin_operation`, and a key that ignored the store
        would find a completed operation recording *different bytes* and refuse
        with "two derivations disagree" — which would be true, and would name
        the wrong cause. The store grew; the derivation is fine.
        """
        append_rulings(self.store_path, [a_ruling("gap:feed/reels_media/", "ignore")])
        state = self.tmp / "state"
        first = self.record(state)
        again = self.record(state)
        self.assertEqual(again.operation_key, first.operation_key)
        self.assertEqual(again.assessment.sha256, first.assessment.sha256)
        self.assertNotIn("gap:feed/reels_media/", first.candidate_ids)

        # The crash: the operation completes, the authority row never lands.
        crashed = self.tmp / "crashed"
        captured: list[dict[str, Any]] = []

        def explode(authority: dict[str, Any]) -> None:
            captured.append(dict(authority))
            raise OSError("power cut")

        with mock.patch.object(Ledger, "record_assessment_authority", side_effect=explode):
            with self.assertRaises(OSError):
                self.record(crashed)
        self.assertEqual(len(captured), 1)
        with self.assertRaises(ValueError):
            Ledger(crashed / "ledger.sqlite3").recorded_assessment_for_run(RUN_ID)

        # A human rules on one more candidate, so the document this input
        # computes is genuinely different from the completed operation's output.
        append_rulings(self.store_path, [a_ruling("gap:feed/reels_media_stream/", "ignore")])
        recovered = self.record(crashed)
        self.assertNotEqual(recovered.operation_key, captured[0]["operation_key"])
        self.assertNotEqual(recovered.assessment.sha256, captured[0]["document_sha256"])
        self.assertNotIn("gap:feed/reels_media_stream/", recovered.candidate_ids)

        # Last, and deliberately last: the field itself. Asserted after the
        # behaviour so that a version without it fails on what actually goes
        # wrong rather than on a missing dictionary key.
        payload = assessment_record.operation_input(
            RUN_ID, "ab" * 32, "cd" * 32, POLICY_REVISION, "ef" * 32
        )
        self.assertEqual(payload["rulings_sha256"], "ef" * 32)


# ----------------------------------------------- what the app actually enforces


#: Declared blocked in the shipped manifest and NOT yet tested by
#: `throwIfBlocked`. Six endpoints entered this state on 2026-08-08 when the
#: owner ruled `block` on every candidate Instagram 441 exposed; they leave it
#: one at a time as the guard is written, and this tuple shrinks to `()` again.
#: `feed/timeline_stream/` left it the same day, the first of the six.
#: Pinned rather than computed, so the app work stays visible: an empty
#: expectation would have quietly accepted a manifest that promises six blocks
#: the app does not make.
DECLARED_NOT_YET_ENFORCED = (
    "delivery/background_prefetch",
    "feed/injected_reels_media/",
    "feed/reels_media/",
    "feed/reels_media_stream/",
    "feed/text_post_app_timeline",
)


class EnforcementTests(RulingTestCase):
    """`semantic_deps` records the decision; the smali records the fact.

    The two disagreeing is the shape of every inert patch this project has
    shipped, so each of these reads DFInsta's own source rather than a fixture
    of what it is believed to say.
    """

    #: A smali file shaped like the real one: one method above `throwIfBlocked`
    #: that reads a key and names a path, and the guard itself. Both scanners
    #: must see only the second. Written rather than reused because the real file
    #: happens to read `disable_reels` in both places, so it cannot distinguish
    #: a scoped scan from a whole-file one by content — only by order.
    DECOY_SOURCE = """.class public final Lcom/dfinstagram/hooks;
.method public static replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    .locals 1

    const-string v0, "disable_outside_the_guard"

    const-string v0, "/rewritten/only/"

    return-object p0
.end method

.method public static throwIfBlocked(Ljava/net/URI;)V
    .locals 3

    const-string v1, "/feed/timeline/"

    const-string v1, "disable_feed"

    const-string v1, "Blocked by DFInsta setting"

.end method

.method public static somethingElse()V
    const-string v0, "disable_after_the_guard"

    const-string v0, "/after/the/guard/"
.end method
"""

    def declared_uri_rules(self, manifest_path: Path) -> tuple[str, ...]:
        """The url-block hook's URI-path deps, read the way the module reads them.

        Spelled out here rather than imported so the invariant below compares two
        independent derivations of "declared" instead of one function with itself.
        """
        from dfinsta_pipeline.assessment import looks_like_uri_rule

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(h for h in data["hooks"] if h["hook_id"] == "tigon_url_block")
        return tuple(dep for dep in entry["semantic_deps"] if looks_like_uri_rule(dep))

    def decoy(self, body: str = "") -> Path:
        path = self.tmp / "decoy" / "hooks.smali"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body or self.DECOY_SOURCE, encoding="utf-8")
        return path

    def test_guarded_endpoints_returns_the_seven_literals_the_app_tests(self):
        """Scoped to `throwIfBlocked`, so a literal elsewhere is not a guard.

        The control is the decoy source: a path constant sits in the rewriting
        method above the guard and another below it, and neither may be reported
        as something the app blocks. A scanner reading the whole file would
        answer this question with strings from code that blocks nothing.
        """
        self.require_real_source()
        self.assertEqual(sorted(guarded_endpoints(REAL_SOURCE)), list(GUARDED))
        # The preference keys and the human-readable message are not endpoints.
        self.assertFalse(
            {item for item in guarded_endpoints(REAL_SOURCE) if item.startswith("disable_")}
        )

        self.assertEqual(guarded_endpoints(self.decoy()), frozenset({"/feed/timeline/"}))

    def test_existing_preference_keys_returns_the_five_keys_in_guard_order(self):
        """Offered for a human to choose from, and deliberately not narrowed to one.

        Deriving a key from an endpoint is not possible: `/feed/reels_tray/` is
        `disable_stories` and `/profile_ads/get_profile_ads/` is `disable_adds`.
        Order matters because it is the order a reader sees them offered in, and
        it is the whole visible difference on the real file: a whole-file scan
        led with `disable_reels`, read from `replaceReelsEndpoint` above the
        guard, which the guard also happens to read. The decoy control is what
        makes the scoping checkable rather than inferable from an order.
        """
        self.require_real_source()
        self.assertEqual(existing_preference_keys(REAL_SOURCE), PREFERENCE_KEYS)
        self.assertEqual(PREFERENCE_KEYS[0], "disable_feed")

        self.assertEqual(existing_preference_keys(self.decoy()), ("disable_feed",))

    def test_a_source_with_no_throw_if_blocked_is_refused_by_both_scanners(self):
        """A file that is not `hooks.smali` must not be scanned as if it were.

        The old split fell back to the whole file when the method was absent, so
        pointing either scanner at the wrong file returned an answer — literals
        from code that guards nothing, presented as the app's block list.
        """
        empty = self.decoy(".class public final Lcom/dfinstagram/other;\n")
        for reader in (guarded_endpoints, existing_preference_keys):
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(RulingError, "declares no throwIfBlocked"):
                    reader(empty)
        # The control: the same two readers on a file that does declare it.
        self.assertEqual(guarded_endpoints(self.decoy()), frozenset({"/feed/timeline/"}))
        self.assertEqual(existing_preference_keys(self.decoy()), ("disable_feed",))

    def test_both_unreadable_source_cases_keep_the_plan_and_note_why(self):
        """A missing source and a wrong one are the same fact, so they behave alike.

        Either way the plan cannot say whether the app already blocks these. The
        asymmetry this test used to pin — `OSError` noted, `RulingError` raised —
        meant an operator's wrong `--source` path destroyed the record of what a
        human decided, while a *missing* path did not.

        `apply` already settled that priority: it writes the store before the
        manifest, because a decision recorded with no block is recoverable and a
        block with no record of who decided it is not. Losing the rulings over a
        path typo is the same trade made the wrong way round.

        The plan survives, the rulings still reach the store, and the note names
        the reason — the one thing that must NOT happen is a plan that silently
        reports nothing unenforced.
        """
        cases = {
            "missing": self.tmp / "nowhere" / "hooks.smali",
            "wrong file": self.decoy(".class public final Lcom/dfinstagram/other;\n"),
        }
        for label, source in cases.items():
            with self.subTest(source=label):
                plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"), source_path=source)
                self.assertFalse(plan.refused)
                self.assertEqual(len(plan.store), 1)
                self.assertEqual(len(plan.notes), 1)
                self.assertIn("could not be read", plan.notes[0])
                self.assertIn("An unchecked source is not a checked one", plan.notes[0])
                # And it reports the endpoint as unenforced rather than claiming
                # the app blocks it. This is the conservative direction and the
                # only safe one: an unchecked source cannot vouch for anything,
                # so every blocking ruling is reported as not yet enforced.
                self.assertEqual(plan.custom_code, (UNGUARDED_GAP,))
                self.assertEqual(plan.preference_keys, ())

        # The control: a real source produces no note and does check the app.
        checked = self.plan((f"gap:{UNGUARDED_GAP}", "block"), source_path=self.decoy())
        self.assertEqual(checked.notes, ())
        self.assertEqual(checked.custom_code, (UNGUARDED_GAP,))

    def test_unenforced_endpoints_is_empty_today_and_names_a_declared_only_endpoint(self):
        """Both halves. The empty result is worthless without the positive case.

        A search that cannot succeed also returns nothing, so the shipped
        manifest agreeing with the shipped source proves the function works only
        alongside a manifest that declares an endpoint `throwIfBlocked` never
        tests — which is exactly the state a recorded ruling creates before a
        human writes the guard.
        """
        self.require_real_source()
        if not REAL_MANIFEST.is_file():
            self.skipTest(f"manifest not present: {REAL_MANIFEST}")
        self.assertEqual(
            sorted(unenforced_endpoints(REAL_MANIFEST, REAL_SOURCE)),
            sorted(DECLARED_NOT_YET_ENFORCED),
            "the shipped manifest's declared-but-unenforced set changed; if the guard "
            "was written for one of these, remove it from DECLARED_NOT_YET_ENFORCED",
        )

        declared = self.write_manifest(
            [
                hook_entry(
                    "tigon_url_block",
                    "url_block",
                    [
                        "/feed/timeline/",
                        UNGUARDED_GAP,
                        # Not a URI-path rule at all, and must not be reported:
                        # `set_app_context` declares one of these for real.
                        "Landroid/app/Application;->onCreate()V",
                    ],
                )
            ]
        )
        self.assertEqual(unenforced_endpoints(declared, REAL_SOURCE), (UNGUARDED_GAP,))

    def test_undeclared_endpoints_reports_a_block_no_hook_records(self):
        """The other direction, which nothing checked and which found a real defect.

        `unenforced_endpoints` asks whether the code does what the manifest says.
        This asks whether the manifest says what the code does — and until
        `/clips/discover` was added to `tigon_url_block`, it did not. The nearest
        declaration was `replace_reels_discover_endpoint`'s `clips/discover/`, and
        containment fails both ways over the leading and trailing slashes, so
        `assessment.blocked_endpoints` could not see it and stage 4a would propose
        blocking what the app already blocks.

        Both halves again: the shipped pair now agrees, and removing the entry
        brings the report back. A clean result from a search that cannot succeed
        is not a clean result.
        """
        self.require_real_source()
        if not REAL_MANIFEST.is_file():
            self.skipTest(f"manifest not present: {REAL_MANIFEST}")
        self.assertEqual(undeclared_endpoints(REAL_MANIFEST, REAL_SOURCE), ())

        document = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
        for hook in document["hooks"]:
            if hook.get("hook_id") == "tigon_url_block":
                hook["semantic_deps"] = [
                    dep for dep in hook["semantic_deps"] if dep != "/clips/discover"
                ]
        without = self.tmp / "without-clips-discover.json"
        without.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(undeclared_endpoints(without, REAL_SOURCE), ("/clips/discover",))

        # And the pair, which is what an operator actually runs. Either direction
        # alone reads as clean while the other is not.
        declared_only, undeclared = audit(REAL_MANIFEST, REAL_SOURCE)
        # The second half is the one this test is named for and it stays empty:
        # the app must never guard an endpoint the manifest does not record. The
        # first half is the five of the six the owner ruled `block` on 2026-08-08
        # that the app has yet to implement.
        self.assertEqual(sorted(declared_only), sorted(DECLARED_NOT_YET_ENFORCED))
        self.assertEqual((), undeclared)
        without_declared, without_undeclared = audit(without, REAL_SOURCE)
        self.assertEqual(sorted(without_declared), sorted(DECLARED_NOT_YET_ENFORCED))
        self.assertEqual(("/clips/discover",), without_undeclared)
        # No longer "agree in both directions": five endpoints are declared and
        # not yet guarded, so the audit correctly reports a disagreement. What this
        # test still pins is that the *undeclared* direction is clean — the app
        # must never guard something the manifest does not record.
        agreed = describe_audit(*audit(REAL_MANIFEST, REAL_SOURCE))
        self.assertIn("the manifest records a decision the app does not implement", agreed)
        reported = describe_audit(*audit(without, REAL_SOURCE))
        self.assertIn("/clips/discover", reported)
        self.assertIn("NOT declared in any hook", reported)

    def test_required_build_strings_is_the_declared_blocks_the_app_enforces(self):
        """What an artifact can be held to, which is not what the manifest claims.

        This function had no test at all until the subtraction was added, and the
        function it feeds is a gate. It returned every declared URI rule, so the
        six rulings recorded on 2026-08-08 became six strings the built DEX was
        required to contain — five of which no source file mentions. The next 441
        port failed post-build verification with a correct APK on disk.
        """
        self.require_real_source()
        if not REAL_MANIFEST.is_file():
            self.skipTest(f"manifest not present: {REAL_MANIFEST}")

        required = required_build_strings(REAL_MANIFEST, REAL_SOURCE)
        # Nothing unenforced is required. The regression, stated as a property.
        self.assertEqual(
            [], sorted(set(required) & set(DECLARED_NOT_YET_ENFORCED))
        )
        # And the positive control, so this cannot pass by requiring nothing:
        # the endpoint that changed sides on 2026-08-08 is required, and so are
        # the six the guard has always tested.
        self.assertIn("feed/timeline_stream/", required)
        self.assertEqual(7, len(required))

        # The invariant that keeps the two answers from drifting: every declared
        # URI rule is either required of the build or reported by the audit, and
        # never both, and never neither.
        declared = self.declared_uri_rules(REAL_MANIFEST)
        unenforced = unenforced_endpoints(REAL_MANIFEST, REAL_SOURCE)
        self.assertEqual(sorted(declared), sorted(set(required) | set(unenforced)))
        self.assertEqual([], sorted(set(required) & set(unenforced)))

    def test_a_declared_block_the_app_does_not_test_is_not_required_of_the_build(self):
        """One manifest, both kinds of dep, so neither half can pass by accident.

        A build cannot be asked to prove a decision nobody has implemented. The
        guarded literal must still be required, or the fix would have been to
        stop checking anything — which is the absence-as-a-pass this module
        refuses everywhere else.
        """
        self.require_real_source()
        mixed = self.write_manifest(
            [
                hook_entry(
                    "tigon_url_block",
                    "url_block",
                    [
                        "/feed/timeline/",  # enforced
                        UNGUARDED_GAP,  # declared only
                        # Not a URI-path rule; belongs to neither answer.
                        "Landroid/app/Application;->onCreate()V",
                    ],
                )
            ]
        )
        self.assertEqual(
            ("/feed/timeline/",), required_build_strings(mixed, REAL_SOURCE)
        )
        self.assertEqual((UNGUARDED_GAP,), unenforced_endpoints(mixed, REAL_SOURCE))

    def test_required_build_strings_refuses_when_the_app_enforces_none_of_them(self):
        """An empty requirement makes the verifier's check pass vacuously.

        The same refusal the no-url-block-hook case already made, at the other
        end: a manifest can now be well-formed, name exactly one blocker, and
        still leave nothing to require. Returning `()` there would hand the
        verifier a check that cannot fail.
        """
        self.require_real_source()
        nothing_enforced = self.write_manifest(
            [hook_entry("tigon_url_block", "url_block", [UNGUARDED_GAP])]
        )
        with self.assertRaises(RulingError) as caught:
            required_build_strings(nothing_enforced, REAL_SOURCE)
        message = str(caught.exception)
        self.assertIn("declares 1 URI-path blocks", message)
        self.assertIn("enforces none of them", message)
        self.assertIn("not the same as a build having nothing to prove", message)

        # The control: add one endpoint the app does test and it returns.
        self.assertEqual(
            ("/feed/timeline/",),
            required_build_strings(
                self.write_manifest(
                    [hook_entry("tigon_url_block", "url_block", [UNGUARDED_GAP, "/feed/timeline/"])]
                ),
                REAL_SOURCE,
            ),
        )

    def test_required_build_strings_refuses_an_ambiguous_manifest(self):
        """Two url-block hooks or none: there is no one set to require."""
        self.require_real_source()
        for label, hooks in {
            "two": [
                hook_entry("tigon_url_block", "url_block", ["/feed/timeline/"]),
                hook_entry("second_url_block", "url_block", ["/feed/timeline/"]),
            ],
            "zero": [hook_entry("replace_reels", "endpoint_replace", ["/feed/timeline/"])],
        }.items():
            with self.subTest(hooks=label):
                with self.assertRaises(RulingError) as caught:
                    required_build_strings(self.write_manifest(hooks), REAL_SOURCE)
                self.assertIn("pass vacuously", str(caught.exception))

    def test_unenforced_endpoints_refuses_an_ambiguous_manifest_rather_than_reading_clean(self):
        """An unanswerable question must not be answered with the clean answer.

        With zero or two `url_block` hooks there is no one hook whose declared
        blocks can be checked, and the old code returned `()` — byte-identical
        to the answer it gives when the manifest and the app agree. This is the
        one function whose entire job is to notice that the manifest claims a
        block the app does not implement, so an absence reported as a pass here
        is the failure the check exists to catch, in the check itself.

        Two positive controls, because a function that raised unconditionally
        would satisfy the refusals alone: the shipped manifest still returns
        `()`, and a one-hook manifest declaring an unguarded endpoint still
        reports it. Both run in this test, on the same source.
        """
        self.require_real_source()
        if not REAL_MANIFEST.is_file():
            self.skipTest(f"manifest not present: {REAL_MANIFEST}")

        cases = {
            "two": [
                hook_entry("tigon_url_block", "url_block", [UNGUARDED_GAP]),
                hook_entry("second_url_block", "url_block", ["/feed/timeline/"]),
            ],
            "zero": [hook_entry("replace_reels", "endpoint_replace", [UNGUARDED_GAP])],
        }
        for label, hooks in cases.items():
            with self.subTest(hooks=label):
                ambiguous = self.write_manifest(hooks)
                with self.assertRaises(RulingError) as caught:
                    unenforced_endpoints(ambiguous, REAL_SOURCE)
                message = str(caught.exception)
                blockers = [h for h in hooks if h["strategy"] == "url_block"]
                # The count and the hook ids, so a reader can act on it, and the
                # sentence that says what the refusal is NOT.
                self.assertIn(f"declares {len(blockers)} hooks", message)
                self.assertIn("url_block", message)
                self.assertIn("not the same as nothing being unenforced", message)
                self.assertIn(str(ambiguous), message)
                for hook in blockers:
                    self.assertIn(hook["hook_id"], message)
                if not blockers:
                    self.assertIn("none", message)

        # Control one: the real manifest, one url-block hook, agrees with the app.
        self.assertEqual(
            sorted(unenforced_endpoints(REAL_MANIFEST, REAL_SOURCE)),
            sorted(DECLARED_NOT_YET_ENFORCED),
        )
        # Control two: one url-block hook, and it does report a real gap.
        self.assertEqual(
            unenforced_endpoints(
                self.write_manifest([hook_entry("tigon_url_block", "url_block", [UNGUARDED_GAP])]),
                REAL_SOURCE,
            ),
            (UNGUARDED_GAP,),
        )

    def test_plan_reports_an_unguarded_endpoint_and_not_a_guarded_one(self):
        """`custom_code` is the field that says "decision, not fact" — per endpoint.

        `/profile_ads/get_profile_ads/` is tested by the app and is not in this
        manifest, so ruling on it is a manifest addition with nothing for a human
        to write. `feed/timeline_stream/` is neither, so it is reported.
        """
        self.require_real_source()
        plan = self.plan((f"gap:{UNGUARDED_GAP}", "block"), (f"gap:{GUARDED_GAP}", "block"))
        self.assertEqual(plan.refusals, ())
        self.assertEqual(plan.notes, ())
        self.assertEqual(
            plan.manifest_additions,
            ((UNGUARDED_GAP, "tigon_url_block"), (GUARDED_GAP, "tigon_url_block")),
        )
        self.assertEqual(plan.custom_code, (UNGUARDED_GAP,))
        self.assertEqual(plan.preference_keys, PREFERENCE_KEYS)
        self.assertIn("THE APP DOES NOT BLOCK THESE YET", plan.describe())
        self.assertIn(UNGUARDED_GAP, plan.describe())

    def test_an_unreadable_source_is_a_note_not_a_silent_nothing_unenforced(self):
        """An unchecked source is not a checked one.

        With no source to read, the honest answer is "I could not tell" plus
        every endpoint reported — including `/profile_ads/get_profile_ads/`,
        which the real app does guard. A plan that reported an empty
        `custom_code` here would look exactly like a plan whose rulings the app
        already enforces.

        The control is the test above: the same ruling against the readable
        source reports nothing for this endpoint, so the difference here is the
        missing file and not the endpoint.
        """
        missing = self.tmp / "nowhere" / "hooks.smali"
        plan = self.plan((f"gap:{GUARDED_GAP}", "block"), source_path=missing)

        self.assertEqual(plan.refusals, ())
        self.assertEqual(len(plan.notes), 1)
        self.assertIn(str(missing), plan.notes[0])
        self.assertIn("could not be read", plan.notes[0])
        self.assertIn("An unchecked source is not a checked one", plan.notes[0])
        self.assertEqual(plan.custom_code, (GUARDED_GAP,))
        self.assertEqual(plan.preference_keys, ())
        self.assertIn("could not be read", plan.describe())


# --------------------------------------------------------------- the ledger row


class LedgerRowTests(unittest.TestCase):
    """Admitted rulings have to be findable, and findable only as admitted."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.state = self.tmp / "state"
        self.ledger_path = self.state / "ledger.sqlite3"
        self.ledger = Ledger(self.ledger_path)
        self.store = ContentStore(self.state / "cas")
        self.document = dispositions((f"gap:{UNGUARDED_GAP}", "block"))
        body = canonical_json(self.document.to_dict()).encode("utf-8")
        self.digest, self.size = self.store.put_blob(body)

    def row(self, **overrides: Any) -> dict[str, Any]:
        record = {
            "run_id": RUN_ID,
            "decision_id": DECISION_ID,
            "dispositions_sha256": self.digest,
            "dispositions_size": self.size,
            "assessment_sha256": ASSESSMENT_SHA256,
            "policy_revision": POLICY_REVISION,
        }
        record.update(overrides)
        return record

    def test_record_admitted_dispositions_is_idempotent_and_refuses_a_second_answer(self):
        """Keyed by run and append-only: a run must not silently gain two answers.

        The state to make impossible is the one in which nobody can say which
        rulings the human actually made. A retried Activity filing the identical
        row is not that state, so it succeeds; a different row for the same run
        is, so it raises.
        """
        self.ledger.record_admitted_dispositions(self.row())
        before = file_fingerprint(self.ledger_path)
        self.ledger.record_admitted_dispositions(self.row())
        self.assertEqual(self.ledger.admitted_dispositions_for_run(RUN_ID), self.row())

        for field, value in (
            ("decision_id", "decision-someone-else"),
            ("dispositions_sha256", "9" * 64),
            ("assessment_sha256", "bb" * 32),
            ("policy_revision", OTHER_REVISION),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "already admitted"):
                    self.ledger.record_admitted_dispositions(self.row(**{field: value}))

        for missing in ("run_id", "decision_id", "dispositions_sha256", "assessment_sha256"):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, "missing"):
                    self.ledger.record_admitted_dispositions(self.row(**{missing: ""}))

        with self.assertRaisesRegex(ValueError, "No dispositions are admitted"):
            self.ledger.admitted_dispositions_for_run("run-never-gated")

        # The reader is what the standalone CLI uses, and it must work without a
        # writable handle — the client has to be structurally unable to create
        # the state it is checking.
        reader = Ledger(self.ledger_path, read_only=True)
        self.assertEqual(reader.admitted_dispositions_for_run(RUN_ID), self.row())
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            reader.record_admitted_dispositions(self.row(run_id="run-2", decision_id="d-2"))

        # Nothing above wrote: the refusals are refusals, not partial writes.
        self.assertEqual(file_fingerprint(self.ledger_path), before)

    def test_admitted_dispositions_fetches_by_reference_and_refuses_the_rest(self):
        """By the reference the row records, never by a path a caller names.

        `read_blob` re-verifies the digest and the size it was asked for, so the
        bytes acted on are the bytes whose hash was admitted — and a document
        naming a different assessment than the row records is refused, because
        applying it would apply rulings made against something else.
        """
        self.ledger.record_admitted_dispositions(self.row())
        reader = Ledger(self.ledger_path, read_only=True)
        document, decision_id = rulings.admitted_dispositions(reader, self.store, RUN_ID)
        self.assertEqual(decision_id, DECISION_ID)
        self.assertEqual(document.to_dict(), self.document.to_dict())

        with self.assertRaisesRegex(ValueError, "No dispositions are admitted"):
            rulings.admitted_dispositions(reader, self.store, "run-never-gated")

        unreadable, size = self.store.put_blob(b"\xff\xfe not a document")
        self.ledger.record_admitted_dispositions(
            self.row(run_id="run-unreadable", decision_id="d-unreadable",
                     dispositions_sha256=unreadable, dispositions_size=size)
        )
        with self.assertRaisesRegex(RulingError, "not a readable document"):
            rulings.admitted_dispositions(reader, self.store, "run-unreadable")

        self.ledger.record_admitted_dispositions(
            self.row(run_id="run-other-assessment", decision_id="d-other",
                     assessment_sha256="bb" * 32)
        )
        with self.assertRaisesRegex(RulingError, "different assessment"):
            rulings.admitted_dispositions(reader, self.store, "run-other-assessment")

        # The recorded size is used, not the blob's own: a row claiming a
        # different length must not resolve to the blob that happens to hash the
        # same, because that check is the store's and this must not bypass it.
        self.ledger.record_admitted_dispositions(
            self.row(run_id="run-wrong-size", decision_id="d-size",
                     dispositions_size=self.size + 1)
        )
        with self.assertRaisesRegex(ValueError, "Blob verification failed"):
            rulings.admitted_dispositions(reader, self.store, "run-wrong-size")

    def test_a_blob_missing_from_cas_is_a_ruling_error_naming_the_run_and_digest(self):
        """A correct refusal has to be a legible one.

        The ledger row and the blob can be restored apart, and the raw form of
        this was `FileNotFoundError: [Errno 2] No such file or directory: '99'`
        — a two-character CAS shard, from a function whose contract is "fetched
        by the reference the row records". It is also an `OSError`, so
        `rulings.main`'s `except (RulingError, ValueError)` let it out as a
        traceback rather than the module's stated `error: …` line.

        The control is at the end: the same store and the same reader still
        fetch the run whose blob is present. A wrapper that turned every read
        into a `RulingError` would pass the first half alone.
        """
        self.ledger.record_admitted_dispositions(self.row())
        self.ledger.record_admitted_dispositions(
            self.row(run_id="run-no-blob", decision_id="d-no-blob", dispositions_sha256="9" * 64)
        )
        with self.assertRaises(RulingError) as caught:
            rulings.admitted_dispositions(self.ledger, self.store, "run-no-blob")
        message = str(caught.exception)
        self.assertIn("run-no-blob", message)
        self.assertIn("9" * 64, message)
        self.assertIn("not in this content store", message)
        # `RulingError` is a `ValueError`, which is what `main` catches — so this
        # path now exits 1 with a message rather than by traceback.
        self.assertIsInstance(caught.exception, ValueError)
        self.assertNotIsInstance(caught.exception, OSError)

        document, decision_id = rulings.admitted_dispositions(self.ledger, self.store, RUN_ID)
        self.assertEqual(decision_id, DECISION_ID)
        self.assertEqual(document.to_dict(), self.document.to_dict())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
