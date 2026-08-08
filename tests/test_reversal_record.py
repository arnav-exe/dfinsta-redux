"""The reversal gate's producer, its consumer, and the client that answers it.

`tests/test_reversal_gate.py` covers the wire contracts and
`tests/test_reversal_workflow.py` covers the join on a real Temporal environment.
This file covers the two ends that are not the Workflow: the module that builds a
docket and files the run-keyed row a gate is reachable through, and the module
that turns an admitted answer into a recorded withdrawal and a rewritten manifest.

The fixture below is shared with the Workflow tests and with
`tools/capture_history_corpus.py`, imported rather than restated, so a change to
what a docket looks like arrives there as a failed capture rather than as a corpus
quietly describing something the pipeline no longer produces.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline import activities, reversal, reversal_record
from dfinsta_pipeline.activities import configure_runtime
from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.reversal_gate import (
    DOCKET_ARTIFACT_KIND,
    ReversalRulingsV1,
    ReversalRulingV1,
    docket_subjects,
)
from dfinsta_pipeline.reversal_record import RecordError
from dfinsta_pipeline.store import ContentStore
from tests.test_retirement_workflow import _claim

RUN_ID = "reconsider-441"
ACTOR = "arnav"
VERSION = "441"
STAMP = "2026-08-09T12:00:00+00:00"

#: The url-block hook. Enforces both endpoints below and has never executed,
#: which is exactly what `block_inert` is about.
BLOCKER = "tigon_url_block"
#: A hook that runs on every measured version, retired anyway. `retirement_returned`.
LIVING = "set_app_context"

FEED = "feed/timeline_stream/"
EXPLORE = "explore/topical_explore/"

#: One gate decision covering both endpoints — which is the whole reason
#: `reversal.withdrawn` is keyed on `(original_decision_id, subject)` rather than
#: on the id alone. A docket that grouped by decision would ask one question about
#: two endpoints and unblock both on one answer.
BLOCK_DECISION = "decision-feature-441"
RETIRE_DECISION = "decision-retire-441"

#: Shaped like the real file: a decoy method above the guard, and the guard
#: itself naming both endpoints. `unenforced_endpoints` reads only the guard, so
#: with this present both blocks count as *enforced* and `block_inert` judges
#: them — without it the rule reports that it could not read the source.
GUARD_SOURCE = """.class public final Lcom/dfinstagram/hooks;
.method public static replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    .locals 1

    const-string v0, "/rewritten/only/"

    return-object p0
.end method

.method public static throwIfBlocked(Ljava/net/URI;)V
    .locals 3

    const-string v1, "feed/timeline_stream/"

    const-string v1, "disable_feed"

    const-string v1, "explore/topical_explore/"

    const-string v1, "disable_explore"

.end method
"""


def _ruling(candidate: str, verdict: str, decision_id: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "record": {
                "candidate_id": f"gap:{candidate}",
                "verdict": verdict,
                "rationale": f"{candidate} is a consumption surface",
                "run_id": "assess-441",
                "decision_id": decision_id,
                "assessment_sha256": "c" * 64,
                "policy_revision": "2026-08-01",
                "recorded_at": "2026-08-08T10:00:00+00:00",
            },
        },
        sort_keys=True,
    )


def _hook_entry(
    hook_id: str,
    intent: str,
    *,
    strategy: str = "call_site",
    deps: list[str] | None = None,
) -> dict:
    """One manifest hook `hook_manifest.load_manifest` accepts.

    Shaped like `tests/test_rulings.py::hook_entry`, and for the same reason: the
    manifest this fixture writes is rewritten by `apply_unblock`, which loads the
    result before renaming it.
    """

    marker = f"# {hook_id}"
    return {
        "hook_id": hook_id,
        "intent": intent,
        "tier": "robust",
        "status": "active",
        "strategy": strategy,
        "semantic_deps": list(deps or ()),
        "hosts": [
            {"kind": "named", "descriptor": "Lcom/instagram/api/tigon/TigonServiceLayer;"}
        ],
        "anchor": ['const-string v0, "placeholder"'],
        "payload": [
            f"invoke-static {{}}, Lcom/dfinstagram/probe;->h_{hook_id}()V",
            marker,
        ],
        "marker": marker,
        "expected_marker_count": 1,
    }


def write_reversal_fixture(
    root: Path, *, with_source: bool = True, actor: str = ACTOR
) -> Path:
    """A repository root that has three decisions the evidence has stopped backing.

    Two blocked endpoints whose enforcing hook has never executed, and a hook
    retired from 441 that ran on 441. Three findings, two kinds, and two of the
    three sharing one `original_decision_id` — which is the arrangement that makes
    the difference between grouping by decision and grouping by decision *and
    subject* observable rather than theoretical.

    `actor` is a parameter because `tools/capture_history_corpus.py` builds a
    docket from this fixture, and a committed History must name neither a person
    nor a machine. The docket itself stays in CAS rather than crossing into
    History, so this is belt as well as braces — but the leak scan forbids this
    repository's owner by name, and a fixture that depends on that staying true is
    a fixture one refactor away from failing the capture.
    """

    manifest = root / "manifest"
    for name in ("static_evidence", "runtime_evidence", "differentials"):
        (manifest / name).mkdir(parents=True, exist_ok=True)

    # Through `manifest_patch.serialise`, not `json.dumps`: `plan_unblock` refuses
    # a manifest that is not already in canonical form, because writing it would
    # reformat lines nobody reviewed — and `write_manifest_atomically` loads the
    # result through `hook_manifest.load_manifest` before renaming it, so a
    # fixture the loader refuses would make every publish test pass for the wrong
    # reason.
    from dfinsta_pipeline.manifest_patch import serialise

    (manifest / "hooks.json").write_text(
        serialise(
            {
                "schema_version": 1,
                "policy_revision": "2026-08-01",
                "hooks": [
                    _hook_entry(
                        BLOCKER,
                        "block consumption endpoints by URL",
                        strategy="url_block",
                        deps=[f"/{FEED}", f"/{EXPLORE}"],
                    ),
                    _hook_entry(LIVING, "set the app context"),
                ],
            }
        ),
        encoding="utf-8",
    )

    (manifest / "rulings.jsonl").write_text(
        "\n".join(
            [
                _ruling(FEED, "block", BLOCK_DECISION),
                # `offer_toggle`, not a second `block`: `BLOCKING_VERDICTS` holds
                # both, and an inert `offer_toggle` was invisible to `reconsider`
                # until it did. Keeping one of each here means a narrowing of that
                # set fails this file.
                _ruling(EXPLORE, "offer_toggle", BLOCK_DECISION),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    for version in ("440", VERSION):
        (manifest / "static_evidence" / f"{version}.jsonl").write_text(
            "\n".join(
                _claim(
                    hook,
                    "static_verified",
                    version,
                    "passed",
                    {"attribution": "sole", "build_verification_passed": True},
                )
                for hook in (LIVING, BLOCKER)
            )
            + "\n",
            encoding="utf-8",
        )
        (manifest / "runtime_evidence" / f"{version}.jsonl").write_text(
            "\n".join(
                [
                    _claim(LIVING, "runtime_probe", version, "passed",
                           {"hooks_that_ran": [LIVING]}),
                    _claim(BLOCKER, "runtime_probe", version, "inconclusive",
                           {"hooks_that_ran": []}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    (manifest / "differentials" / f"440-{VERSION}.jsonl").write_text(
        "\n".join(
            [
                _claim(LIVING, "differential", VERSION, "passed", {"baseline_version": "440"}),
                _claim(BLOCKER, "differential", VERSION, "inconclusive",
                       {"baseline_version": "440", "reason": "baseline_not_a_pass"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (manifest / "retirements.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hook_id": LIVING,
                "effective_from": VERSION,
                "decision_id": RETIRE_DECISION,
                "ruled_by": actor,
                "rationale": "Believed dormant on 441.",
                "recorded_at": "2026-08-08T10:00:00+00:00",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    write_observations(root, observed={FEED: 9, EXPLORE: 4})

    source = root / "dfinsta_source_439" / "newCode" / "com" / "dfinstagram"
    if with_source:
        source.mkdir(parents=True, exist_ok=True)
        (source / "hooks.smali").write_text(GUARD_SOURCE, encoding="utf-8")
    return root


def write_observations(
    root: Path,
    *,
    observed: dict[str, int],
    watched: tuple[str, ...] = (FEED, EXPLORE),
    version: str = VERSION,
    session_id: str = "441-feed-1",
) -> Path:
    """One device session for the version, under `root` and never the repository.

    The base fixture records **both** endpoints as observed, so
    `block_never_observed` runs and finds nothing. That is deliberate: this file's
    tests are about grouping, keying and publishing, and a rule that reported
    itself skipped in every one of them would make "a skipped rule" the ordinary
    state here rather than the thing worth asserting.
    """

    path = root / "manifest" / "observations" / f"{version}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "build_sha256": "d" * 64,
                "recorded_at": "2026-08-09T09:00:00+00:00",
                "session_id": session_id,
                "surface": "feed_tab",
                "watched": list(watched),
                "counts": dict(sorted(observed.items())),
                "total": sum(observed.values()),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_empty_index(directory: Path) -> Path:
    """An index that holds no API paths at all, so every endpoint reads as absent.

    Enough of one for `HookIndex.load`: a header, an api surface, and the
    structural file it checks for existence but does not read here.
    """

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "header.json").write_text(
        json.dumps({"schema_version": 1, "decode_path": "decode-441"}), encoding="utf-8"
    )
    (directory / "api_surface.json").write_text(
        json.dumps({"api_paths": {}, "resources": {}, "stable_types": {}}), encoding="utf-8"
    )
    (directory / "structural.jsonl").write_text("", encoding="utf-8")
    return directory


class ReversalFixtureTestCase(unittest.TestCase):
    """A temp root with three questionable decisions and a fresh state root."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        write_reversal_fixture(self.root)
        self.state = self.root / "state"
        self.manifest = self.root / "manifest" / "hooks.json"

    def record(self, **overrides):
        arguments = {
            "run_id": RUN_ID,
            "version": VERSION,
            "allowed_actor": ACTOR,
            "owner_token": "owner-1",
            "root": self.root,
        }
        arguments.update(overrides)
        return reversal_record.record(self.state, **arguments)


class DocketBuildingTests(ReversalFixtureTestCase):
    """What a sweep becomes, and what it refuses to become."""

    def test_a_docket_groups_by_decision_and_subject(self) -> None:
        document, items = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        self.assertEqual(
            [(entry["kind"], entry["subject"]) for entry in document["items"]],
            [("block", EXPLORE), ("block", FEED), ("retirement", LIVING)],
        )
        # Two rulings under ONE decision id produce two items, not one. The whole
        # point of `withdrawn` being keyed on the pair.
        blocks = [e for e in document["items"] if e["kind"] == "block"]
        self.assertEqual({BLOCK_DECISION}, {e["original_decision_id"] for e in blocks})
        self.assertEqual(2, len({e["item_id"] for e in blocks}))
        self.assertEqual(3, len(items))

    def test_two_triggers_on_one_decision_make_one_item(self) -> None:
        """A block that is both inert and gone is one question, not two.

        Asking twice would let a human answer `withdraw` to one and `keep` to the
        other on the same `(decision, subject)` key — and `reversal.append` would
        then refuse the second *after* the first had rewritten the manifest.
        """
        index = write_empty_index(self.root / "index")
        document, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01", index_dir=index
        )
        feed = next(e for e in document["items"] if e["subject"] == FEED)
        self.assertEqual(["block_inert", "block_endpoint_absent"], feed["triggers"])
        self.assertEqual(2, len(feed["summaries"]))
        # Still three items, not five.
        self.assertEqual(3, len(document["items"]))
        # And with an index supplied, that rule is no longer skipped.
        self.assertEqual([], document["rules_not_run"])

    def test_a_skipped_rule_travels_inside_the_signed_document(self) -> None:
        """The half of the result that is not a finding.

        Without it a human signs "three decisions look wrong" with no way to see
        that a fourth rule never ran.
        """
        document, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        self.assertTrue(document["rules_not_run"])
        self.assertIn("block_endpoint_absent", document["rules_not_run"][0])

    def test_an_unreadable_app_source_is_reported_rather_than_assumed(self) -> None:
        """`block_inert` still runs and says what it could not see.

        An absent source makes every declared block look enforced, so the rule
        judges endpoints whose enforcement nobody checked. That is a caveat, not a
        skip, and it has to reach the human.
        """
        root = Path(self.directory.name).resolve() / "no-source"
        root.mkdir()
        write_reversal_fixture(root, with_source=False)
        document, _ = reversal_record.build_docket(
            root, version=VERSION, policy_revision="2026-08-01"
        )
        self.assertTrue(
            any("could not read the app source" in line for line in document["rules_not_run"])
        )

    def test_a_clean_sweep_refuses_to_build_a_docket(self) -> None:
        """A gate with no question does not need a human.

        Every rule runs here — the app source is present and an index is supplied
        — so this is the refusal that really means "nothing is wrong", and it is
        deliberately a different message from the one below.
        """
        (self.root / "manifest" / "rulings.jsonl").write_text("", encoding="utf-8")
        (self.root / "manifest" / "retirements.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(RecordError) as caught:
            reversal_record.build_docket(
                self.root,
                version=VERSION,
                policy_revision="2026-08-01",
                index_dir=write_empty_index(self.root / "index"),
            )
        self.assertIn("does not need a human", str(caught.exception))
        self.assertNotIn("incomplete sweep", str(caught.exception))

    def test_an_incomplete_sweep_that_found_nothing_is_refused_separately(self) -> None:
        """The message that matters, and the reason this branch exists at all.

        No findings *and* a rule that could not run is not "nothing is wrong". It
        is an empty answer to a question nobody finished asking, and recording it
        would put a reassuring silence in front of a human.
        """
        root = Path(self.directory.name).resolve() / "silent"
        root.mkdir()
        write_reversal_fixture(root, with_source=False)
        (root / "manifest" / "rulings.jsonl").write_text("", encoding="utf-8")
        (root / "manifest" / "retirements.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(RecordError) as caught:
            reversal_record.build_docket(
                root, version=VERSION, policy_revision="2026-08-01"
            )
        message = str(caught.exception)
        self.assertIn("incomplete sweep", message)
        self.assertIn("could not read the app source", message)
        # And it is NOT the "no question" refusal, which would read as a clean bill.
        self.assertNotIn("does not need a human", message)

    def test_item_ids_derive_from_the_triple_and_nothing_else(self) -> None:
        """Stable across re-derivation, and independent of the prose."""
        first, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        second, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        self.assertEqual(
            [e["item_id"] for e in first["items"]], [e["item_id"] for e in second["items"]]
        )
        edited = json.loads(json.dumps(first))
        edited["items"][0]["summaries"] = ["a rewritten summary"]
        # The id survives an edit to the prose; the item DIGEST does not, which is
        # what binds a ruling to what a human actually read.
        self.assertEqual(
            [i.item_id for i in docket_subjects(first)],
            [i.item_id for i in docket_subjects(edited)],
        )
        self.assertNotEqual(
            docket_subjects(first)[0].item_sha256,
            docket_subjects(edited)[0].item_sha256,
        )

    def test_a_docket_whose_ids_were_edited_is_refused(self) -> None:
        document, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        document["items"][0]["item_id"] = "block-0000000000000000"
        with self.assertRaises(Exception) as caught:
            docket_subjects(document)
        self.assertIn("does not derive", str(caught.exception))


class RecordingTests(ReversalFixtureTestCase):
    """The run-keyed row, and what makes it idempotent."""

    def test_a_recorded_docket_is_reachable_from_the_run_id_alone(self) -> None:
        recorded = self.record()
        self.assertEqual(DOCKET_ARTIFACT_KIND, recorded.docket.kind)
        again = reversal_record.resolve(self.state, RUN_ID)
        self.assertEqual(recorded.docket.sha256, again.docket.sha256)
        self.assertEqual(recorded.item_ids, again.item_ids)
        self.assertEqual(ACTOR, again.allowed_actor)
        self.assertEqual("2026-08-01", again.policy_revision)

    def test_recording_the_same_docket_twice_is_idempotent(self) -> None:
        first = self.record()
        second = self.record()
        self.assertEqual(first.operation_key, second.operation_key)
        self.assertEqual(first.docket.sha256, second.docket.sha256)

    def blobs(self, state: Path | None = None) -> set[str]:
        cas = (state or self.state) / "cas"
        return {str(p.relative_to(cas)) for p in cas.rglob("*") if p.is_file()} if cas.exists() else set()

    def key_for(self, label: str, **overrides) -> str:
        """The operation key for one recording, **under a fixed run id**.

        The run id is inside `operation_input`'s payload, so recording under a
        fresh one to get a fresh ledger made every comparison below true before
        the input under test was considered. Two calls with nothing changed
        returned different keys, and four tests asserted only that keys differ.
        So the run id is held and the *state root* varies instead — which is also
        closer to the question, since two machines recording the same sweep should
        agree.

        `test_two_recordings_with_nothing_changed_agree` is the control that makes
        every `assertNotEqual` below mean something.
        """

        return reversal_record.record(
            self.root / f"state-{label}",
            run_id=RUN_ID,
            version=VERSION,
            allowed_actor=ACTOR,
            owner_token=f"owner-{label}",
            root=self.root,
            **overrides,
        ).operation_key

    def test_two_recordings_with_nothing_changed_agree(self) -> None:
        """The positive control for every `assertNotEqual` below.

        Without it a `key_for` that varied on something incidental — the run id,
        the state root, a clock — would make all of them pass while testing
        nothing. That is exactly what happened.
        """
        self.assertEqual(self.key_for("same-a"), self.key_for("same-b"))

    def test_a_changed_input_makes_a_different_operation(self) -> None:
        """The key is the inputs, so an evidence change is a different question."""
        first = self.key_for("evidence-before")
        (self.root / "manifest" / "runtime_evidence" / f"{VERSION}.jsonl").write_text(
            _claim(BLOCKER, "runtime_probe", VERSION, "inconclusive",
                   {"hooks_that_ran": [], "note": "re-measured"}) + "\n"
            + _claim(LIVING, "runtime_probe", VERSION, "passed",
                     {"hooks_that_ran": [LIVING]}) + "\n",
            encoding="utf-8",
        )
        self.assertNotEqual(first, self.key_for("evidence-after"))

    def test_a_second_docket_under_one_run_is_refused_before_any_write(self) -> None:
        """A refused record that had already put a blob in CAS leaves an orphan
        nothing points at, and the store has no sweeper.

        The perturbation has to move the **document**, not only the key. The first
        version of this changed an evidence detail that `reconsider` does not read,
        so the docket bytes were identical — and CAS is content-addressed, so an
        orphaned `put_bytes` would have written the very same blob and the count
        could not move whatever the code did. Making the blocking hook pass a probe
        removes both `block_inert` findings, which changes the docket itself.
        """
        recorded = self.record()
        blobs = self.blobs()
        (self.root / "manifest" / "runtime_evidence" / f"{VERSION}.jsonl").write_text(
            "\n".join(
                _claim(hook, "runtime_probe", VERSION, "passed", {"hooks_that_ran": [hook]})
                for hook in (LIVING, BLOCKER)
            ) + "\n",
            encoding="utf-8",
        )
        # The premise, asserted rather than assumed: the docket really is a
        # different document now, so an orphaned `put_bytes` would land under a
        # digest that is not already in the store and the count below can move.
        after, _ = reversal_record.build_docket(
            self.root, version=VERSION, policy_revision="2026-08-01"
        )
        self.assertNotEqual(
            recorded.docket.sha256,
            hashlib.sha256(canonical_json(after).encode("utf-8")).hexdigest(),
        )
        with self.assertRaises(RecordError) as caught:
            self.record()
        self.assertIn("different reversal docket", str(caught.exception))
        self.assertEqual(blobs, self.blobs(), "a refused record left an orphan in CAS")

    def test_the_app_source_is_an_input_even_when_it_is_missing(self) -> None:
        """Absence is an input. A rule that could not read the source gives a
        different answer from one that read it, so the two must not share a key."""
        with_source = self.key_for("with-source")
        (self.root / "dfinsta_source_439" / "newCode" / "com" / "dfinstagram" / "hooks.smali").unlink()
        self.assertNotEqual(with_source, self.key_for("without-source"))

    def test_evidence_after_the_docket_version_is_an_input_too(self) -> None:
        """Where this deliberately differs from `retirement_record`.

        `reconsider` reaches `roster`, which walks EVERY version with evidence
        from the baseline — so a 442 probe changes whether a hook counts as
        never-having-run and therefore changes the findings. Scoping the key to
        `<= version` left it still while the docket moved.
        """
        before = self.key_for("441-only")
        for kind in ("static_evidence", "runtime_evidence"):
            (self.root / "manifest" / kind / "442.jsonl").write_text(
                _claim(BLOCKER, "runtime_probe" if kind == "runtime_evidence" else "static_verified",
                       "442", "passed",
                       {"hooks_that_ran": [BLOCKER]} if kind == "runtime_evidence"
                       else {"attribution": "sole", "build_verification_passed": True}) + "\n",
                encoding="utf-8",
            )
        self.assertNotEqual(before, self.key_for("442-too"))

    def test_the_observation_store_is_an_input_even_when_it_is_missing(self) -> None:
        """`block_never_observed` reads it, so it decides the docket.

        Three states, three keys. "No device session has been taken" and "a
        session was taken and every watched path was seen" both produce no
        finding, and a key that stood still between them would let the docket
        built before the phone was walked be adopted by the run that walked it —
        the exact defect this module already had once with the evidence series.
        """
        both_seen = self.key_for("observed")
        write_observations(self.root, observed={FEED: 9})
        one_never = self.key_for("one-never-observed")
        (self.root / "manifest" / "observations" / f"{VERSION}.jsonl").unlink()
        none_at_all = self.key_for("no-sessions")
        self.assertEqual(3, len({both_seen, one_never, none_at_all}))

    def test_the_observation_store_for_another_version_is_not_an_input(self) -> None:
        """`never_observed` is asked about one version and reads one file.

        Keying on a 440 session would make a 441 docket change identity because
        an unrelated port's device evidence arrived — the "must not change its
        identity because an unrelated file arrived" rule this function opens with.
        """
        before = self.key_for("441-only")
        write_observations(self.root, observed={FEED: 1}, version="440", session_id="440-1")
        self.assertEqual(before, self.key_for("440-too"))

    def test_the_baseline_is_an_input(self) -> None:
        """`roster` walks the evidence series from it, so two sweeps with the same
        files and different baselines are two different questions."""
        self.assertNotEqual(
            self.key_for("base-439", baseline="439"),
            self.key_for("base-440", baseline="440"),
        )

    def test_a_run_id_that_could_never_make_a_gate_is_refused_before_any_write(self) -> None:
        with self.assertRaises(RecordError) as caught:
            self.record(run_id="reconsider 441!")
        self.assertIn("no gate could ever be raised", str(caught.exception))
        self.assertFalse((self.state / "ledger.sqlite3").exists())

    def test_a_docket_only_an_agent_may_answer_is_refused_at_record_time(self) -> None:
        """Not at publish time, which is where `Reversal` would refuse it.

        A gate a human waits a week on and whose answer can then never be written
        is the "answerable in a test, unanswerable in production" trap arriving a
        week late.
        """
        with self.assertRaises(RecordError) as caught:
            self.record(allowed_actor="agent")
        self.assertIn("A human withdraws a decision", str(caught.exception))
        self.assertFalse((self.state / "ledger.sqlite3").exists())

    def test_an_expected_digest_that_disagrees_is_refused(self) -> None:
        with self.assertRaises(RecordError) as caught:
            self.record(expect_docket_sha256="d" * 64)
        self.assertIn("recomputes rather than adopting", str(caught.exception))

    def test_an_index_changes_the_docket_and_the_key(self) -> None:
        without = self.record()
        index = write_empty_index(self.root / "index")
        other = reversal_record.record(
            self.state,
            run_id="reconsider-441-indexed",
            version=VERSION,
            allowed_actor=ACTOR,
            owner_token="owner-2",
            root=self.root,
            index_dir=index,
        )
        self.assertNotEqual(without.operation_key, other.operation_key)
        self.assertNotEqual(without.docket.sha256, other.docket.sha256)


class PublishingTests(ReversalFixtureTestCase):
    """The consumer. Four gates in this project have shipped without one."""

    def setUp(self) -> None:
        super().setUp()
        previous = getattr(activities, "_runtime", None)
        self.addCleanup(setattr, activities, "_runtime", previous)
        self.recorded = self.record()
        configure_runtime(self.state)

    def admit(self, verdicts: dict[str, str], *, decision_id: str = "decision-gate-1") -> None:
        """Write the ledger rows the admitting Activity would write.

        Straight to the ledger rather than through Temporal, because this file is
        about the producer and the consumer; `tests/test_reversal_workflow.py`
        runs the same path with the real Activity and a real server.
        """

        store = ContentStore(self.state / "cas")
        document = ReversalRulingsV1(
            1,
            self.recorded.docket.sha256,
            self.recorded.version,
            self.recorded.policy_revision,
            tuple(
                ReversalRulingV1(
                    1, item.item_id, verdicts[item.item_id], "as decided", item.item_sha256
                )
                for item in self.recorded.items
            ),
        )
        body = canonical_json(document.to_dict()).encode("utf-8")
        reference = store.put_bytes(
            kind="reversal-rulings-v1",
            data=body,
            producer_operation_id=f"client-{document.sha256}",
            input_hashes=(self.recorded.docket.sha256,),
        )
        Ledger(self.state / "ledger.sqlite3").record_admitted_reversal_rulings(
            {
                "run_id": RUN_ID,
                "decision_id": decision_id,
                "rulings_sha256": reference.sha256,
                "rulings_size": reference.size,
                "docket_sha256": document.docket_sha256,
                "version": document.version,
                "policy_revision": document.policy_revision,
            }
        )

    def all_verdicts(self, verdict: str) -> dict[str, str]:
        return {item.item_id: verdict for item in self.recorded.items}

    def publish(self, **overrides):
        arguments = {"recorded_at": STAMP, "confirm": True, "root": self.root}
        arguments.update(overrides)
        return reversal_record.publish_admitted(self.state, RUN_ID, **arguments)

    def test_a_withdrawal_reaches_the_record_and_the_manifest(self) -> None:
        """The whole point. Anything short of this has been shipped broken before.

        Not "the rulings were admitted" — that was true of every disconnected gate
        this project has built. The assertions are that `reversal.withdrawn` finds
        both rows and that `manifest/hooks.json` no longer declares the endpoints.
        """
        self.admit(self.all_verdicts("withdraw"))
        published = self.publish()
        self.assertEqual({FEED, EXPLORE, LIVING}, set(published.withdrawn))
        self.assertEqual(self.manifest, published.manifest_path)

        blocks = reversal.withdrawn("block", self.root)
        self.assertEqual(
            {(BLOCK_DECISION, FEED), (BLOCK_DECISION, EXPLORE)}, set(blocks)
        )
        # The GATE's decision id, not one this module minted.
        self.assertEqual({"decision-gate-1"}, {r.decision_id for r in blocks.values()})
        self.assertEqual({ACTOR}, {r.ruled_by for r in blocks.values()})
        self.assertEqual({""}, {r.effective_from for r in blocks.values()})

        retirements = reversal.withdrawn("retirement", self.root)
        self.assertEqual([(RETIRE_DECISION, LIVING)], list(retirements))
        # Derived, never chosen: the version AFTER the port the docket was built
        # from, so a hook cannot be restored into a port already assessed.
        self.assertEqual("442", retirements[(RETIRE_DECISION, LIVING)].effective_from)

        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        deps = [d for h in document["hooks"] for d in h.get("semantic_deps") or ()]
        self.assertEqual([], deps)

    def test_the_original_decision_rows_survive(self) -> None:
        """A reversal is a new decision, not an edit. Both rows exist afterwards."""
        before = (self.root / "manifest" / "rulings.jsonl").read_text(encoding="utf-8")
        retirements_before = (self.root / "manifest" / "retirements.jsonl").read_text(
            encoding="utf-8"
        )
        self.admit(self.all_verdicts("withdraw"))
        self.publish()
        self.assertEqual(
            before, (self.root / "manifest" / "rulings.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(
            retirements_before,
            (self.root / "manifest" / "retirements.jsonl").read_text(encoding="utf-8"),
        )

    def test_keep_and_defer_change_nothing(self) -> None:
        verdicts = self.all_verdicts("keep")
        verdicts[self.recorded.items[-1].item_id] = "defer"
        self.admit(verdicts)
        published = self.publish()
        self.assertEqual((), published.withdrawn)
        self.assertIsNone(published.manifest_path)
        self.assertEqual(2, len(published.kept))
        self.assertEqual(1, len(published.deferred))
        self.assertEqual({}, reversal.withdrawn("block", self.root))
        self.assertEqual({}, reversal.withdrawn("retirement", self.root))
        self.assertIn(
            f"/{FEED}",
            json.loads(self.manifest.read_text(encoding="utf-8"))["hooks"][0]["semantic_deps"],
        )

    def test_publishing_without_confirm_is_refused_and_writes_nothing(self) -> None:
        """A human signed the decision at the gate; this flag is a second, separate
        permission — for this machine rewriting this file now."""
        self.admit(self.all_verdicts("withdraw"))
        with self.assertRaises(RecordError) as caught:
            self.publish(confirm=False)
        self.assertIn("pass confirm", str(caught.exception))
        self.assertEqual([], reversal.read_reversals(self.root))

    def test_a_timestamp_that_is_not_a_timestamp_is_refused(self) -> None:
        """Parsed, not merely non-blank.

        `Reversal.__post_init__` checks every other field for non-emptiness and
        does not check `recorded_at` at all, so `--recorded-at banana` went into
        `manifest/reversals.jsonl` and the command exited 0 — in a file whose
        whole contract is that nothing is ever deleted from it.
        """
        self.admit(self.all_verdicts("withdraw"))
        for stamp, message in (
            ("   ", "must not read the clock"),
            ("banana", "not an ISO 8601 timestamp"),
            ("2026-08-09", "no UTC offset"),
            ("2026-08-09T12:00:00", "no UTC offset"),
        ):
            with self.subTest(stamp=stamp):
                with self.assertRaises(RecordError) as caught:
                    self.publish(recorded_at=stamp)
                self.assertIn(message, str(caught.exception))
                self.assertEqual([], reversal.read_reversals(self.root))
        # The positive control: a well-formed stamp goes through, so the refusals
        # above are about the value and not about the publish path being broken.
        self.assertTrue(self.publish(recorded_at="2026-08-09T12:00:00Z").withdrawn)

    def test_publishing_opens_every_ledger_read_only(self) -> None:
        """Structurally, not by promise — the same rule the client lives by.

        **Every** handle, not "a handle": a grep for `read_only=True` would pass
        against a second, writable one opened three lines later, and this records
        the flag for each `Ledger` the publish constructs.

        Not tested by sealing the file: SQLite's read-only mode still needs to
        create `-wal`/`-shm` beside a WAL database, so an unwritable directory
        breaks the correct implementation as well as the wrong one — a guard that
        cannot distinguish them is not a guard.
        """
        from unittest.mock import patch

        opened: list[bool] = []

        class Recording(Ledger):
            def __init__(self, path, *, read_only: bool = False):
                opened.append(read_only)
                super().__init__(path, read_only=read_only)

        self.admit(self.all_verdicts("keep"))
        with patch.object(reversal_record, "Ledger", Recording):
            published = self.publish()
        self.assertEqual(3, len(published.kept))
        self.assertTrue(opened, "the publish opened no ledger at all")
        self.assertEqual([True] * len(opened), opened)

    def test_a_keep_or_a_defer_on_an_already_withdrawn_decision_is_refused(self) -> None:
        """The guard was on the `withdraw` branch only, and that was the bug.

        A human ruling `keep` on a decision already withdrawn on the record says
        the block stands while the record says it does not — and the report said
        "kept in force" over a manifest that no longer declared the endpoint.
        Reporting a contradiction as an outcome is what this gate exists to
        prevent.

        **Both** inert verdicts, because the guard sits on one branch and a rule
        written for `keep` alone would let `defer` through — and "not yet" over a
        block that is already gone is the same lie with a softer word.
        """
        for verdict in ("keep", "defer"):
            with self.subTest(verdict=verdict):
                self.setUp()
                self.recorded = self.record()
                configure_runtime(self.state)
                self.admit(self.all_verdicts(verdict))
                reversal.append(
                    reversal.Reversal(
                        schema_version=1,
                        withdraws="block",
                        subject=FEED,
                        original_decision_id=BLOCK_DECISION,
                        decision_id="decision-by-hand",
                        ruled_by=ACTOR,
                        rationale="withdrawn out of band",
                        recorded_at=STAMP,
                    ),
                    root=self.root,
                )
                with self.assertRaises(RecordError) as caught:
                    self.publish()
                message = str(caught.exception)
                self.assertIn("contradict", message)
                self.assertIn(FEED, message)
                self.assertIn(verdict, message)

    def test_admitted_rulings_that_leave_an_item_unanswered_are_refused(self) -> None:
        """`validate_submission` proved this at admission, so reaching it means the
        ledger disagrees with itself — and the lookup below it would otherwise
        raise `KeyError` past every handler."""
        self.admit(self.all_verdicts("withdraw"))
        # Overwrite CAS with a short document under the same digest is impossible,
        # so admit a *different* run's shorter rulings under this run instead.
        store = ContentStore(self.state / "cas")
        document = ReversalRulingsV1(
            1,
            self.recorded.docket.sha256,
            self.recorded.version,
            self.recorded.policy_revision,
            (
                ReversalRulingV1(
                    1,
                    self.recorded.items[0].item_id,
                    "withdraw",
                    "only one",
                    self.recorded.items[0].item_sha256,
                ),
            ),
        )
        body = canonical_json(document.to_dict()).encode("utf-8")
        reference = store.put_bytes(
            kind="reversal-rulings-v1",
            data=body,
            producer_operation_id=f"client-{document.sha256}",
            input_hashes=(self.recorded.docket.sha256,),
        )
        # A second run id, because the admitted row is append-only per run.
        reversal_record.record(
            self.state,
            run_id="reconsider-short",
            version=VERSION,
            allowed_actor=ACTOR,
            owner_token="owner-short",
            root=self.root,
        )
        Ledger(self.state / "ledger.sqlite3").record_admitted_reversal_rulings(
            {
                "run_id": "reconsider-short",
                "decision_id": "decision-gate-short",
                "rulings_sha256": reference.sha256,
                "rulings_size": reference.size,
                "docket_sha256": document.docket_sha256,
                "version": document.version,
                "policy_revision": document.policy_revision,
            }
        )
        with self.assertRaises(RecordError) as caught:
            reversal_record.publish_admitted(
                self.state,
                "reconsider-short",
                recorded_at=STAMP,
                confirm=True,
                root=self.root,
            )
        self.assertIn("do not answer", str(caught.exception))

    def test_the_reversal_is_recorded_before_the_manifest_is_written(self) -> None:
        """The order `apply_unblock`'s docstring names, asserted from this side.

        A manifest write that succeeded while the record failed would unblock an
        endpoint with nobody's decision behind it. Forced by making the manifest
        write fail: the record must survive.
        """
        self.admit({self.recorded.items[0].item_id: "withdraw",
                    self.recorded.items[1].item_id: "keep",
                    self.recorded.items[2].item_id: "keep"})
        first = self.recorded.document["items"][0]["subject"]
        # The reversals file lives OUTSIDE the manifest directory here, so that
        # sealing that directory stops only the manifest write. With both under
        # it, the append fails too and the test passes for the wrong reason —
        # which is what it did first time.
        reversals = self.root / "reversals.jsonl"
        self.manifest.parent.chmod(0o555)
        self.addCleanup(self.manifest.parent.chmod, 0o755)
        with self.assertRaises(Exception):
            self.publish(reversals_path=reversals)
        recorded = reversal.withdrawn("block", self.root, path=reversals)
        self.assertIn(
            (BLOCK_DECISION, first),
            recorded,
            "the manifest write failed and the human's decision was lost with it",
        )

    def test_publishing_an_unanswered_gate_refuses_rather_than_reporting_nothing(self) -> None:
        """`ValueError` out of the ledger, not an empty result.

        A publish with no admitted rulings must not read as "nothing to withdraw":
        the two states are "a human said no" and "nobody has answered", and this
        project's most repeated defect is reporting the second as the first.
        """
        with self.assertRaises(ValueError):
            self.publish()

    def test_publishing_twice_is_idempotent(self) -> None:
        """So a partial failure can be finished by running the command again."""
        self.admit(self.all_verdicts("withdraw"))
        first = self.publish()
        second = self.publish()
        self.assertEqual((), second.withdrawn)
        self.assertEqual(set(first.withdrawn), set(second.already_recorded))
        self.assertEqual(3, len(reversal.read_reversals(self.root)))

    def test_rulings_that_answer_another_docket_are_refused_not_crashed_on(self) -> None:
        """The row and the blob and the operation are three records of one fact.

        A mismatch used to surface as a `KeyError` past every handler; the honest
        answer is a refusal that says nothing here can tell which docket the human
        read.
        """
        other = reversal_record.record(
            self.state,
            run_id="reconsider-441-other",
            version=VERSION,
            allowed_actor=ACTOR,
            owner_token="owner-2",
            root=self.root,
            index_dir=write_empty_index(self.root / "index"),
        )
        store = ContentStore(self.state / "cas")
        document = ReversalRulingsV1(
            1,
            other.docket.sha256,
            other.version,
            other.policy_revision,
            tuple(
                ReversalRulingV1(1, item.item_id, "withdraw", "as decided", item.item_sha256)
                for item in other.items
            ),
        )
        body = canonical_json(document.to_dict()).encode("utf-8")
        reference = store.put_bytes(
            kind="reversal-rulings-v1",
            data=body,
            producer_operation_id=f"client-{document.sha256}",
            input_hashes=(other.docket.sha256,),
        )
        Ledger(self.state / "ledger.sqlite3").record_admitted_reversal_rulings(
            {
                "run_id": RUN_ID,
                "decision_id": "decision-gate-1",
                "rulings_sha256": reference.sha256,
                "rulings_size": reference.size,
                "docket_sha256": document.docket_sha256,
                "version": document.version,
                "policy_revision": document.policy_revision,
            }
        )
        with self.assertRaises(RecordError) as caught:
            self.publish()
        self.assertIn("different docket", str(caught.exception))
        self.assertEqual([], reversal.read_reversals(self.root))

    def test_a_recorded_withdrawal_the_manifest_still_declares_is_refused(self) -> None:
        """The one state this cannot resolve, so it refuses rather than guessing.

        Reached by recording the reversal and leaving the manifest alone — which
        is what a crash between `append` and the manifest write would leave. A
        second publish must not silently re-apply, because it cannot tell that
        state from a human having deliberately re-blocked the endpoint.
        """
        self.admit(self.all_verdicts("withdraw"))
        reversal.append(
            reversal.Reversal(
                schema_version=1,
                withdraws="block",
                subject=FEED,
                original_decision_id=BLOCK_DECISION,
                decision_id="decision-by-hand",
                ruled_by=ACTOR,
                rationale="recorded, manifest not yet written",
                recorded_at=STAMP,
            ),
            root=self.root,
        )
        with self.assertRaises(RecordError) as caught:
            self.publish()
        self.assertIn("still declares it", str(caught.exception))

    def test_an_endpoint_no_hook_declares_is_refused_rather_than_recorded(self) -> None:
        """`plan_unblock`'s own guard, reached through this consumer.

        A reversal that changes nothing would record an intention and leave the
        block in place, which is the shape this whole module exists to avoid.
        """
        self.admit(self.all_verdicts("withdraw"))
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["hooks"][0]["semantic_deps"] = [f"/{FEED}"]
        from dfinsta_pipeline.manifest_patch import serialise

        self.manifest.write_text(serialise(document), encoding="utf-8")
        with self.assertRaises(RecordError) as caught:
            self.publish()
        self.assertIn("nothing to withdraw", str(caught.exception))


class ClientResolutionTests(ReversalFixtureTestCase):
    """The client re-derives the subject from a run id and refuses otherwise."""

    def setUp(self) -> None:
        super().setUp()
        previous = getattr(activities, "_runtime", None)
        self.addCleanup(setattr, activities, "_runtime", previous)
        self.recorded = self.record()
        configure_runtime(self.state, read_only=True)

    def test_the_client_reaches_the_same_subject_the_activity_would(self) -> None:
        from dfinsta_pipeline.reversal_gate import derive_reversal_gate_request
        from dfinsta_pipeline.submission import REVERSAL_GATE

        derived = REVERSAL_GATE.resolve(RUN_ID)
        expected = derive_reversal_gate_request(
            self.recorded.run_id,
            self.recorded.docket,
            self.recorded.version,
            self.recorded.policy_revision,
            self.recorded.allowed_actor,
            self.recorded.items,
        )
        self.assertEqual(expected.sha256, derived.subject_sha256)
        self.assertEqual(expected.sha256, derived.admission_sha256)
        self.assertEqual(expected.sha256, derived.prepared_sha256)
        self.assertEqual(f"{RUN_ID}-reversal-gate", derived.gate_id)
        self.assertEqual(ACTOR, derived.allowed_actor)

    def test_the_gate_id_predicate_matches_only_this_gate(self) -> None:
        from dfinsta_pipeline.submission import REVERSAL_GATE, select_gate_kind

        self.assertIs(REVERSAL_GATE, select_gate_kind(f"{RUN_ID}-reversal-gate", RUN_ID))
        for gate_id in (
            f"{RUN_ID}-reversal-gate-2",
            f"x{RUN_ID}-reversal-gate",
            f"{RUN_ID}-reversal",
            f"{RUN_ID}-hook-retirement-gate",
            RUN_ID,
        ):
            with self.subTest(gate_id=gate_id):
                self.assertFalse(REVERSAL_GATE.matches(gate_id, RUN_ID))

    def pending(self):
        from dfinsta_pipeline.contracts import GateRequest
        from dfinsta_pipeline.submission import REVERSAL_GATE, PendingGate

        derived = REVERSAL_GATE.resolve(RUN_ID)
        published = GateRequest(
            schema_version=1,
            run_id=derived.run_id,
            gate_id=derived.gate_id,
            subject_sha256=derived.subject_sha256,
            admission_sha256=derived.subject_sha256,
            prepared_sha256=derived.subject_sha256,
            policy_revision=derived.policy_revision,
            issued_at="2026-08-09T12:00:00+00:00",
            expires_at="2026-08-16T12:00:00+00:00",
        )
        return PendingGate(RUN_ID, REVERSAL_GATE, published, derived)

    def a_decision(self, **overrides):
        from dfinsta_pipeline.contracts import GateDecision

        derived = self.pending().derived
        arguments = {
            "schema_version": 1,
            "decision_id": "decision-client-1",
            "idempotency_id": "idempotency-client-1",
            "actor": derived.allowed_actor,
            "run_id": derived.run_id,
            "gate_id": derived.gate_id,
            "subject_sha256": derived.subject_sha256,
            "admission_sha256": derived.subject_sha256,
            "prepared_sha256": derived.subject_sha256,
            "policy_revision": derived.policy_revision,
            "decision": "approve",
            "rationale": "reviewed the evidence",
            "issued_at": "2026-08-09T12:00:00+00:00",
        }
        arguments.update(overrides)
        return GateDecision(**arguments)

    def build(self, detail, decision=None):
        from dfinsta_pipeline.submission import Answer

        pending = self.pending()
        return pending.kind.payload(
            pending,
            decision or self.a_decision(),
            Answer("approve", "reviewed", detail=detail),
        )

    def test_the_client_refuses_to_send_what_it_could_not_admit(self) -> None:
        """The last step of `_reversal_payload` is the point of `_reversal_payload`.

        Deleting the self-validation leaves every other test green, because they
        all hand it a correct decision. This hands it one bound to a digest nobody
        derived — the failure a human would otherwise discover at a worker, in a
        log they cannot see.
        """
        from dfinsta_pipeline.submission import SubmissionRefused

        with self.assertRaises(SubmissionRefused) as caught:
            self.build(
                self.rulings_for(),
                decision=self.a_decision(
                    subject_sha256="f" * 64,
                    admission_sha256="f" * 64,
                    prepared_sha256="f" * 64,
                ),
            )
        self.assertIn("cannot admit its own answer", str(caught.exception))
        # Positive control: the same call with the derived digest is sent.
        self.assertIsNotNone(self.build(self.rulings_for()))

    def rulings_for(self, verdict: str = "withdraw") -> dict:
        return {
            item.item_id: {"verdict": verdict, "rationale": "as decided"}
            for item in self.recorded.items
        }

    def test_the_client_builds_a_submission_its_own_authority_admits(self) -> None:
        """The last step of the payload builder is the point: it runs the
        admitting side's validator over its own answer, so a human's decision does
        not fail at a worker where they cannot see why."""
        from dfinsta_pipeline.reversal_gate import ReversalGateSubmissionV1

        submission = self.build(self.rulings_for())
        self.assertIsInstance(submission, ReversalGateSubmissionV1)
        self.assertEqual("reversal-rulings-v1", submission.rulings.kind)

    def test_the_emission_order_is_the_dockets_not_the_files(self) -> None:
        """So the document's digest cannot depend on how somebody ordered their
        editor."""
        from dfinsta_pipeline.reversal_gate import ReversalRulingsV1

        reversed_detail = dict(reversed(list(self.rulings_for().items())))
        submission = self.build(reversed_detail)
        body = activities.runtime().store.read_blob(
            submission.rulings.sha256, submission.rulings.size
        )
        document = ReversalRulingsV1.from_dict(json.loads(body.decode("utf-8")))
        self.assertEqual(
            list(self.recorded.item_ids), [r.item_id for r in document.rulings]
        )

    def test_each_ruling_carries_the_recorded_docket_item_digest(self) -> None:
        """Taken from the recorded docket, never from the human's file — and the
        admitting side checks it, which the retirement gate's equivalent does
        not."""
        from dfinsta_pipeline.reversal_gate import ReversalRulingsV1

        submission = self.build(self.rulings_for())
        body = activities.runtime().store.read_blob(
            submission.rulings.sha256, submission.rulings.size
        )
        document = ReversalRulingsV1.from_dict(json.loads(body.decode("utf-8")))
        self.assertEqual(
            {i.item_id: i.item_sha256 for i in self.recorded.items},
            {r.item_id: r.item_sha256 for r in document.rulings},
        )

    def test_the_client_refuses_an_answer_it_cannot_send(self) -> None:
        from dfinsta_pipeline.submission import SubmissionRefused

        full = self.rulings_for()
        missing = {k: v for k, v in list(full.items())[:1]}
        invented = dict(full, **{"block-0000000000000000": {"verdict": "keep", "rationale": "x"}})
        bad_verdict = dict(full)
        bad_verdict[self.recorded.items[0].item_id] = {"verdict": "unblock", "rationale": "x"}
        blank = dict(full)
        blank[self.recorded.items[0].item_id] = {"verdict": "keep", "rationale": "  "}
        for detail, message in (
            (None, "a ruling for every decision"),
            ("not a mapping", "a ruling for every decision"),
            (missing, "No ruling for decision"),
            (invented, "does not cover"),
            (bad_verdict, "expected one of"),
            (blank, "no rationale"),
            ({self.recorded.items[0].item_id: "not an object"}, "must be an object"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(SubmissionRefused) as caught:
                    self.build(detail)
                self.assertIn(message, str(caught.exception))

    def test_the_clients_ledger_cannot_write(self) -> None:
        """Structurally unable to create the state it is checking."""
        with self.assertRaises(RuntimeError):
            activities.runtime().ledger.record_reversal_docket_authority(
                {
                    "run_id": "smuggled",
                    "operation_key": "k",
                    "input_sha256": "a" * 64,
                    "docket_sha256": "b" * 64,
                    "version": VERSION,
                    "policy_revision": "2026-08-01",
                    "allowed_actor": ACTOR,
                }
            )


class CommandLineTests(ReversalFixtureTestCase):
    """Exit codes. `refused:` and 2, never a traceback and 1.

    Nothing called `main` at all until this class existed, so every refusal path
    through the CLI was unasserted — including the one the module's own error
    handling is written for.
    """

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reversal_record.main(
                ["--state-root", str(self.state), "--root", str(self.root), *argv]
            )
        return code, out.getvalue(), err.getvalue()

    def test_recording_and_showing_succeed_and_print_the_skipped_rules(self) -> None:
        """The positive control every refusal below needs."""
        code, out, _ = self.run_cli(
            "record", "--run-id", RUN_ID, "--version", VERSION,
            "--allowed-actor", ACTOR, "--owner-token", "owner-1",
        )
        self.assertEqual(0, code)
        self.assertIn("RULE NOT RUN", out)
        self.assertIn(FEED, out)
        code, out, _ = self.run_cli("show", "--run-id", RUN_ID)
        self.assertEqual(0, code)
        self.assertIn(ACTOR, out)

    def test_a_run_that_was_never_recorded_is_refused_with_two(self) -> None:
        """A typo in a run id is the most ordinary way this is used wrongly, and
        the ledger raises a bare `ValueError` for it."""
        code, _, err = self.run_cli("show", "--run-id", "reconsider-typo")
        self.assertEqual(2, code)
        self.assertIn("refused:", err)

    def test_every_refusal_path_exits_two(self) -> None:
        self.run_cli(
            "record", "--run-id", RUN_ID, "--version", VERSION,
            "--allowed-actor", ACTOR, "--owner-token", "owner-1",
        )
        for argv, expected in (
            (("record", "--run-id", "not an id", "--version", VERSION,
              "--allowed-actor", ACTOR, "--owner-token", "o"), "valid identifier"),
            (("record", "--run-id", "reconsider-agent", "--version", VERSION,
              "--allowed-actor", "agent", "--owner-token", "o"), "A human withdraws"),
            (("record", "--run-id", "reconsider-x", "--version", "not-a-version",
              "--allowed-actor", ACTOR, "--owner-token", "o"), "not a version number"),
            (("publish", "--run-id", RUN_ID, "--recorded-at", STAMP), "pass confirm"),
            (("publish", "--run-id", RUN_ID, "--recorded-at", "banana", "--confirm"),
             "ISO 8601"),
        ):
            with self.subTest(argv=argv[0] + " " + argv[2]):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(2, code, err)
                self.assertIn("refused:", err)
                self.assertIn(expected, err)

    def test_a_starter_that_cannot_reach_a_server_is_refused_not_a_traceback(self) -> None:
        """The documented failure mode of a pinned start. It left as a bare
        `RuntimeError` and exit 1, where this module's contract is 2."""
        code, _, err = self.run_cli(
            "raise", "--run-id", RUN_ID, "--endpoint", "127.0.0.1:1",
            "--gate-timeout-seconds", "60",
        )
        self.assertEqual(2, code)
        self.assertIn("refused:", err)

    def test_publishing_reads_the_ledger_without_being_able_to_write_it(self) -> None:
        """`publish_admitted` opens the ledger read-only, as the client does."""
        self.record()
        with self.assertRaises(RuntimeError):
            Ledger(self.state / "ledger.sqlite3", read_only=True).record_decision(None)


if __name__ == "__main__":
    unittest.main()
