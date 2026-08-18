"""Answering the feature gate: the payload seam, the rulings, and the journal.

`tests/test_submission.py` covers the client as it was when every gate's answer
was a bare `GateDecision`. This file covers the change that made a second kind of
gate answerable at all — the one whose payload is
`FeatureGateSubmissionV1 {decision, dispositions}` and whose human supplies a
ruling per candidate. See `docs/ANSWERING_THE_FEATURE_GATE.md`.

Four things can silently substitute a human's rulings, and each gets tests that
attack it from the direction a broken implementation would take.

**A detail can be dropped.** `Answer` gained an untyped `detail`, and the whole
value of the field is its default: a kind that does not understand a detail must
*refuse* it. Dropping it submits a bare `approve` while the human believes they
ruled on four specific surfaces, and the receipt says `accepted`.

**The candidate list can become an input.** `feature_gate.validate_submission`
never fetches the assessment blob — it compares the ruled set against
`request.candidate_ids`, which is whatever the deriving side pinned. So nothing
downstream catches a human ruling on a list the pinned bytes do not contain, and
`_feature_rulings` is the only place it can be stopped. It iterates the DERIVED
tuple and looks each id up in the human's file, never the reverse, which is why
`test_the_emitted_order_is_the_derived_tuples_not_the_files` asserts on a *list*
of keys: two dicts with the same pairs in different orders compare equal, and the
document's digest does not.

**The journal can pair one answer's decision with another answer's rulings.** A
`GateDecision` says nothing about what rode with it, so a resubmission rebuilds
the payload from whatever is on disk now. `payload_sha256` closes that, and the
legacy case is the sharp one: an entry written before the field existed reports
`None`, and pairing that with a payload that HAS a digest must refuse rather than
read as "no payload, nothing to compare".

**Two derivations can agree while both being wrong.** So the end-to-end tests
here do not stub anything: `assessment_record.record` writes a real ledger and a
real content store under a `tempfile` root, `configure_runtime(read_only=True)`
opens it the way the client does, and the subject the client derives is compared
against a request derived independently from the recorded values. Each such claim
carries a positive control, because "the two agree" is also what a hash that
ignores its input would say.

One thing here pins today's behaviour rather than the intended behaviour, and it
is recorded so it is not mistaken for approval. `_feature_assessment_payload`
lets `feature_gate`'s own `ValueError`s escape — a verdict outside
`feature_gate.VERDICTS`, an over-long rationale, or any non-`ignore` verdict with
a blank rationale (which is what `--rulings-template` emits) reaches `main`,
which catches only `SubmissionRefused` and `WorkflowUpdateFailedError`. So an
ordinary typo in a hand-edited rulings file produces a traceback and exit 1
instead of `refused: …` and exit 2, which is the habit the module's own docstring
says a client answering a gate must not teach. `FACT 12` therefore accepts
`ValueError` *or* `SubmissionRefused`: the refusal is what matters and either
spelling of it should keep that test passing.

Fixtures are `tests/test_submission.py`'s for the client and
`tests/test_assessment_record.py`'s for the recorded state; nothing here builds a
second harness. `ledger_fingerprint` is imported from the latter rather than from
`tests/test_submission_resolver.py`, where the same function lives, because that
module pulls in the whole phase-B verification fixture to do it.

Not covered here, by arrangement: `read_pending_gate`, `_run` and `main`, and
everything `tests/test_submission.py` already pins — in particular the contents of
`GATE_KINDS`, which is pinned there and deliberately not repeated.
"""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dfinsta_pipeline import activities, assessment_record, feature_gate
from dfinsta_pipeline.contracts import (
    SHA256_PATTERN,
    ArtifactRef,
    GateDecision,
    GateRequest,
    canonical_json,
    canonical_sha256,
)
from dfinsta_pipeline.submission import (
    FEATURE_ASSESSMENT_GATE,
    IDEMPOTENCY_ID_PREFIX,
    REPLAY_VERIFICATION_GATE,
    Answer,
    DerivedSubject,
    GateKind,
    PendingGate,
    SubmissionRefused,
    _decision_payload,
    _feature_assessment_payload,
    _feature_rulings,
    _payload_digest,
    _read_rulings,
    _require_recorded_payload_matches,
    _resolve_feature_assessment,
    _rulings_template,
    assemble_decision,
    journal_path,
    read_journal,
    select_gate_kind,
    submit_answer,
    write_journal,
)

from tests.test_assessment import NOVEL_MEMBERS, write_manifest
from tests.test_assessment_record import ledger_fingerprint, write_fake_index
from tests.test_submission import (
    GATE_ID,
    NOW,
    WORKFLOW_ID,
    TemporaryRootTestCase,
    make_answer,
    make_decision,
    make_derived,
    make_pending,
    make_principal,
    make_request,
)


# --------------------------------------------------------------------- fixtures

#: The candidate ids stage 4a actually mints, derived from the same tuple the
#: assessment tests use so a change to the producer's spelling reaches here.
CANDIDATES = tuple(f"gap:{literal}" for literal in NOVEL_MEMBERS)

#: What a human's `rulings.json` holds for those candidates.
#:
#: `defer` rather than `offer_toggle`, and the reason is load-bearing: this file
#: records against a state root with no observation store, so every candidate
#: reaches the gate reading "no device has looked for this". `block` and
#: `offer_toggle` are refused for such a candidate — see
#: `feature_gate._require_measurement_before_acting` — while `defer` is not, and
#: still carries a rationale, so every clause these tests are actually about is
#: exercised unchanged. `AnsweringWithoutMeasurementTests` covers the refusal.
RULINGS = {
    candidate: {"verdict": "defer", "rationale": f"revisit {candidate} next version"}
    for candidate in CANDIDATES
}

FEATURE_RUN_ID = "run-feature-gate-client-1"
FEATURE_ACTOR = "sam.operator"
OWNER_TOKEN = "feature-gate-owner-1"


def feature_answer(detail: object = None, verdict: str = "approve") -> Answer:
    """The human's answer to a per-candidate gate: a verdict plus their file."""
    return Answer(verdict, "every candidate ruled on", RULINGS if detail is None else detail)  # type: ignore[arg-type]


class RecordingClient:
    """The smallest thing `submit_answer` can talk to.

    A real Temporal client is not needed to observe what this module decides to
    send: the questions here are which payload was assembled and under which
    update id, both of which arrive at `execute_update`. What a worker would then
    do with them is `test_submission_temporal`'s business.
    """

    def __init__(self, receipt: Any = None) -> None:
        self.calls: list[tuple[str, Any, str]] = []
        self.handles: list[str] = []
        self.receipt = {"accepted": True} if receipt is None else receipt

    def get_workflow_handle(self, workflow_id: str) -> "RecordingClient":
        self.handles.append(workflow_id)
        return self

    async def execute_update(self, name: str, payload: Any, *, id: str) -> Any:
        self.calls.append((name, payload, id))
        return self.receipt


def _detail_payload(pending: PendingGate, decision: GateDecision, answer: Answer) -> object:
    """A payload that varies with the human's detail and with nothing else.

    Two answers can differ only in their attachments — same verdict, same
    rationale, therefore the same decision and the same `decision_id`. This kind
    makes that case reachable without a ledger, which is what isolates the
    journal's payload check from the decision check that runs just before it.
    """
    return {"decision": dataclasses.asdict(decision), "detail": answer.detail}


#: Registered nowhere: `select_gate_kind` takes `kinds` as a parameter precisely
#: so a caller can supply a kind, and a resolver here would grant nothing that
#: constructing a `PendingGate` by hand does not already grant.
DETAIL_GATE = GateKind(
    name="detail-carrying",
    update_name="submit_detail",
    matches=lambda gate_id, run_id: gate_id == f"{run_id}-final-verification-gate",
    resolve=lambda run_id: make_derived(),
    payload=_detail_payload,
)


# ------------------------------------------------------------- the payload seam


class PayloadSeamTests(unittest.TestCase):
    """What a gate's update carries, and what a gate must never quietly discard."""

    def test_a_kind_that_cannot_send_a_detail_refuses_rather_than_dropping_it(self) -> None:
        """FACT 1. Dropping a detail submits a bare verdict a human never gave.

        Mutation: return `decision` regardless of `answer.detail`. Every static
        check still passes — the decision is well formed, the ids are derived,
        the subject was verified — and a human who wrote four rulings has a bare
        `approve` submitted on their behalf with `accepted True` printed back.

        The falsy details matter as much as the truthy one. `if answer.detail:`
        instead of `is not None` reads an empty rulings object as "no detail" and
        drops it, which is the same failure wearing a subtler mutation.
        """
        pending = make_pending()
        decision = make_decision()

        # The default is the seam: a kind that names no payload builder gets this
        # one, so the refusal is what every present and future gate inherits.
        self.assertIs(GateKind.__dataclass_fields__["payload"].default, _decision_payload)
        self.assertIs(REPLAY_VERIFICATION_GATE.payload, _decision_payload)

        for detail in ({"gap:feed/reels_media/": {}}, {}, [], "", 0, False, RULINGS):
            with self.subTest(detail=detail):
                with self.assertRaises(SubmissionRefused) as raised:
                    _decision_payload(
                        pending, decision, Answer("approve", "looks right", detail)
                    )
                self.assertEqual(
                    str(raised.exception),
                    "The replay-final-verification gate takes a verdict and a rationale "
                    "and nothing else; this answer carries additional detail that nothing "
                    "here would send.",
                )

        # Positive control, and the identity is the point: `_payload_digest` reads
        # `payload is decision`, so returning a copy here would change every
        # existing gate's update id and journal shape.
        self.assertIs(_decision_payload(pending, decision, make_answer()), decision)

    def test_the_payload_digest_is_none_only_when_the_payload_is_the_decision_itself(
        self,
    ) -> None:
        """FACT 2. Identity, not equality — an equal copy still gets a digest.

        `None` is what keeps every pre-existing gate byte-identical: same journal
        entry, same update id, same deduplication. So the test that it is `None`
        has to be a test about *which object*, not about what it holds. A version
        reading `payload == decision` would silently make a distinct-but-equal
        payload behave like the decision, and the digest that was supposed to
        distinguish two answers would vanish.

        Mutation: always return a digest. The journal grows a `payload_sha256`
        key for the replay gate and its update id stops being the decision's
        idempotency id — both of which `FACT 8` observes end to end.
        """
        decision = make_decision()
        equal_copy = GateDecision.from_dict(dataclasses.asdict(decision))
        self.assertEqual(equal_copy, decision)
        self.assertIsNot(equal_copy, decision)

        self.assertIsNone(_payload_digest(decision, decision))
        self.assertEqual(_payload_digest(equal_copy, decision), canonical_sha256(equal_copy))
        self.assertIsNotNone(_payload_digest(equal_copy, decision))

        # And otherwise it is the canonical digest of the whole payload, so two
        # answers differing only in their attachments are two different updates.
        payload = {"decision": dataclasses.asdict(decision), "detail": RULINGS}
        self.assertEqual(_payload_digest(payload, decision), canonical_sha256(payload))
        reordered = {key: payload[key] for key in reversed(list(payload))}
        self.assertNotEqual(list(reordered), list(payload))
        self.assertEqual(
            _payload_digest(reordered, decision), _payload_digest(payload, decision)
        )
        other = {**payload, "detail": {**RULINGS, CANDIDATES[0]: {"verdict": "block", "rationale": "no"}}}
        self.assertNotEqual(_payload_digest(other, decision), _payload_digest(payload, decision))


# ---------------------------------------------------------- the rulings decoder


class FeatureRulingsTests(unittest.TestCase):
    """The one place a ruling about a document nobody read can be stopped.

    `validate_submission` compares the ruled set against `request.candidate_ids`
    and never fetches the assessment blob, so if the candidate list came from the
    human's file every clause downstream would pass. The decoder therefore
    iterates the derived tuple and looks each id up — never the reverse.
    """

    def test_a_candidate_the_file_names_but_the_gate_does_not_cover_is_refused_by_name(
        self,
    ) -> None:
        """FACT 3. By name, before anything is signed.

        A renamed or invented id is the `phase-a-approval` trap in its
        per-candidate form: a human ruling on a set the pinned bytes do not
        contain. Naming it is what makes a typo fixable rather than mysterious.
        """
        for extra in ("gap:feed/reels_media_v2/", "gap:direct/inbox/", "typo"):
            with self.subTest(extra=extra):
                with self.assertRaises(SubmissionRefused) as raised:
                    _feature_rulings({**RULINGS, extra: RULINGS[CANDIDATES[0]]}, CANDIDATES)
                self.assertEqual(
                    str(raised.exception),
                    f"Rulings name a candidate this gate does not cover: {extra}",
                )
        # A renamed id is both halves at once, and the unknown one is named first.
        renamed = {("renamed" if key == CANDIDATES[1] else key): value for key, value in RULINGS.items()}
        with self.assertRaises(SubmissionRefused) as raised:
            _feature_rulings(renamed, CANDIDATES)
        self.assertEqual(
            str(raised.exception), "Rulings name a candidate this gate does not cover: renamed"
        )

    def test_a_derived_candidate_the_file_omits_is_refused_by_name(self) -> None:
        """FACT 4. Absence is never a pass, said on the client side.

        Mutation: iterate `detail` rather than `candidates`. The unknown-key check
        still passes, the submission still builds, and the omitted candidate is
        simply not ruled on — which `validate_submission` would catch at the
        worker if the client had not already built the request from the same
        short list. This is the direction that makes the check load-bearing.
        """
        for omitted in CANDIDATES:
            with self.subTest(omitted=omitted):
                with self.assertRaises(SubmissionRefused) as raised:
                    _feature_rulings(
                        {key: value for key, value in RULINGS.items() if key != omitted},
                        CANDIDATES,
                    )
                self.assertEqual(str(raised.exception), f"No ruling for candidate {omitted}")
        # An explicit null is an omission too: a key with nothing behind it is
        # exactly the human who scrolled past.
        with self.assertRaises(SubmissionRefused) as raised:
            _feature_rulings({**RULINGS, CANDIDATES[2]: None}, CANDIDATES)
        self.assertEqual(str(raised.exception), f"No ruling for candidate {CANDIDATES[2]}")
        # Positive control: the complete file is accepted.
        self.assertEqual(list(_feature_rulings(RULINGS, CANDIDATES)), list(CANDIDATES))

    def test_the_emitted_order_is_the_derived_tuples_not_the_files(self) -> None:
        """FACT 5. The digest must not depend on how a human ordered their editor.

        Asserted on a *list* of keys, because two dicts holding the same pairs in
        different orders compare equal and the canonical bytes of the document
        built from them do not. A test using `assertEqual` on the mapping alone
        would pass under the very mutation this exists to catch.
        """
        reversed_file = {candidate: RULINGS[candidate] for candidate in reversed(CANDIDATES)}
        self.assertNotEqual(list(reversed_file), list(CANDIDATES), "fixture is inert")

        self.assertEqual(list(_feature_rulings(reversed_file, CANDIDATES)), list(CANDIDATES))
        # Every order of the file gives the request's order back.
        rotated = {candidate: RULINGS[candidate] for candidate in CANDIDATES[2:] + CANDIDATES[:2]}
        self.assertNotEqual(list(rotated), list(CANDIDATES))
        self.assertEqual(list(_feature_rulings(rotated, CANDIDATES)), list(CANDIDATES))
        # And the derived order is what is followed, not some sort of the two.
        odd_order = (CANDIDATES[3], CANDIDATES[0], CANDIDATES[2], CANDIDATES[1])
        self.assertNotEqual(list(odd_order), sorted(odd_order))
        self.assertEqual(list(_feature_rulings(RULINGS, odd_order)), list(odd_order))

    def test_a_malformed_detail_entry_verdict_or_rationale_is_refused(self) -> None:
        """FACT 6. Four shapes, four refusals, each naming what it could not read."""
        for detail in (None, [], "approve all", 7, ()):
            with self.subTest(detail=detail):
                with self.assertRaises(SubmissionRefused) as raised:
                    _feature_rulings(detail, CANDIDATES)
                self.assertEqual(
                    str(raised.exception),
                    "This gate needs a ruling for every candidate: pass --rulings with a "
                    'JSON object mapping each candidate id to {"verdict": …, '
                    '"rationale": …, "consent": …}',
                )

        for entry in ("offer_toggle", ["offer_toggle", "why"], 7, True):
            with self.subTest(entry=entry):
                with self.assertRaises(SubmissionRefused) as raised:
                    _feature_rulings({**RULINGS, CANDIDATES[0]: entry}, CANDIDATES)
                self.assertEqual(
                    str(raised.exception), f"Ruling for {CANDIDATES[0]} must be an object"
                )

        for label, entry in (
            ("verdict missing", {"rationale": "why"}),
            ("verdict not a string", {"verdict": ["block"], "rationale": "why"}),
            ("verdict null", {"verdict": None, "rationale": "why"}),
            ("rationale not a string", {"verdict": "block", "rationale": ["why"]}),
            ("rationale null", {"verdict": "block", "rationale": None}),
        ):
            with self.subTest(entry=label):
                with self.assertRaises(SubmissionRefused) as raised:
                    _feature_rulings({**RULINGS, CANDIDATES[1]: entry}, CANDIDATES)
                self.assertEqual(
                    str(raised.exception),
                    f"Ruling for {CANDIDATES[1]} needs a verdict and a rationale",
                )

        # An absent rationale is one of the two omissions allowed here, because
        # whether a blank one is acceptable depends on the verdict and that
        # judgement belongs to `feature_gate.validate_submission`. An absent
        # `consent` is the other, and for `ignore` it stays absent all the way
        # through: the no-op verdict owes neither.
        self.assertEqual(
            _feature_rulings({**RULINGS, CANDIDATES[1]: {"verdict": "ignore"}}, CANDIDATES)[
                CANDIDATES[1]
            ],
            ("ignore", "", ""),
        )


# ---------------------------------------------------------------------- journal


class JournalPayloadDigestTests(TemporaryRootTestCase, unittest.IsolatedAsyncioTestCase):
    """The journal has to say what rode with the decision, and stay compatible.

    `submit_answer` is the only place the journal and the payload builder meet,
    and both are reached here — the async base is what lets a test drive it, and
    `TemporaryRootTestCase` is still the one that owns the throwaway directory.
    """

    def setUp(self) -> None:
        super().setUp()
        self.journal_root = self.root / "submissions"
        self.decision = make_decision()

    def entry(self, **overrides: Any) -> dict[str, Any]:
        """A journal entry as it was written *before* payloads existed."""
        document: dict[str, Any] = {
            "schema_version": 1,
            "workflow_id": WORKFLOW_ID,
            "gate_id": GATE_ID,
            "subject_sha256": self.decision.subject_sha256,
            "decision": dataclasses.asdict(self.decision),
        }
        document.update(overrides)
        return document

    def write_raw(self, document: dict[str, Any]) -> Path:
        self.journal_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = journal_path(self.journal_root, WORKFLOW_ID, GATE_ID)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def read(self):
        return read_journal(
            self.journal_root, WORKFLOW_ID, GATE_ID, self.decision.subject_sha256
        )

    def test_read_journal_takes_both_shapes_and_refuses_anything_else(self) -> None:
        """FACT 7. An entry written before this field existed is still valid.

        Refusing it would strand a human mid-answer on an upgrade: they answered,
        the connection dropped, they upgraded the client, and now the recorded
        answer they are supposed to resubmit cannot be read at all. So `None` is
        a legitimate value and the strictness has to be widened by exactly one
        key — not relaxed.
        """
        self.write_raw(self.entry())
        legacy = self.read()
        self.assertEqual(legacy.decision, self.decision)
        self.assertIsNone(legacy.payload_sha256)

        self.write_raw(self.entry(payload_sha256="a" * 64))
        current = self.read()
        self.assertEqual(current.decision, self.decision)
        self.assertEqual(current.payload_sha256, "a" * 64)

        # Widened by one key, in both shapes: an unknown field is still a client
        # too old to understand what it is being asked to resubmit.
        for label, document, unknown in (
            ("with a payload", self.entry(payload_sha256="a" * 64, submitted_at="now"), "submitted_at"),
            ("without one", self.entry(payload_digest="a" * 64), "payload_digest"),
        ):
            self.write_raw(document)
            with self.subTest(entry=label):
                with self.assertRaises(SubmissionRefused) as raised:
                    self.read()
                self.assertEqual(
                    str(raised.exception), f"Unknown journal entry field: {unknown}"
                )

        for malformed in ("not-a-digest", "A" * 64, "a" * 63, "", 7, ["a" * 64]):
            self.write_raw(self.entry(payload_sha256=malformed))
            with self.subTest(payload_sha256=malformed):
                with self.assertRaises(SubmissionRefused) as raised:
                    self.read()
                self.assertEqual(str(raised.exception), "Invalid journal payload hash")

    async def test_write_journal_omits_the_payload_key_entirely_when_there_is_none(
        self,
    ) -> None:
        """FACT 8. Not an explicit null — no key at all.

        Asserted on the raw JSON text rather than on the parsed object, because
        `{"payload_sha256": null}` parses to the same thing and is a different
        file. Byte-identity with the pre-change entry is what makes upgrading the
        client mid-answer safe.

        Driven through `submit_answer` as well as directly, because the digest
        that reaches `write_journal` comes from `_payload_digest`. Mutation:
        make that function always return a digest. The direct call still writes a
        clean entry and the end-to-end one grows a key — and the replay gate's
        update id stops being the decision's own idempotency id, which is the
        deduplication property a resubmission after a dropped connection needs.
        """
        path = write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, self.decision)
        body = path.read_text(encoding="utf-8")
        self.assertNotIn("payload_sha256", body)
        self.assertEqual(sorted(json.loads(body)), sorted(self.entry()))
        self.assertEqual(body, canonical_json(self.entry()))

        # Positive control: the key appears when there is a digest to record, so
        # the assertion above is about the absent digest and not about a writer
        # that cannot emit the field.
        with_payload = write_journal(
            self.journal_root, WORKFLOW_ID, GATE_ID, self.decision, "b" * 64
        ).read_text(encoding="utf-8")
        self.assertIn("payload_sha256", with_payload)
        self.assertEqual(json.loads(with_payload), self.entry(payload_sha256="b" * 64))

        # End to end: the replay gate's payload IS its decision, so the entry a
        # real submission leaves behind is the pre-change one, byte for byte.
        path.unlink()
        client = RecordingClient()
        outcome = await submit_answer(
            client,
            make_pending(),
            make_principal(),
            make_answer(),
            journal_root=self.journal_root,
            issued_at=NOW,
        )
        self.assertTrue(outcome.accepted)
        self.assertFalse(outcome.resubmitted)
        self.assertEqual(outcome.journal.read_text(encoding="utf-8"), canonical_json(self.entry()))
        name, payload, update_id = client.calls[0]
        self.assertEqual(name, "submit_verification_decision")
        self.assertEqual(payload, self.decision)
        self.assertEqual(update_id, self.decision.idempotency_id)
        self.assertNotEqual(
            update_id, f"{IDEMPOTENCY_ID_PREFIX}{canonical_sha256(self.decision)}"
        )

    async def test_a_journalled_decision_paired_with_a_different_payload_is_refused(
        self,
    ) -> None:
        """FACT 9. The defect that can silently substitute a human's rulings.

        A `GateDecision` says nothing about what rode with it, and two answers
        that differ only in their attachments produce the *same* decision — same
        verdict, same rationale, therefore the same `decision_id`. So a
        resubmission after an edited rulings file would send the first attempt's
        decision carrying the second attempt's rulings, and print `accepted`.

        The legacy pairing is the sharp one. An entry with no `payload_sha256`
        reports `None`, and a payload that HAS a digest must refuse rather than
        read the absent field as "nothing rode with it".

        Mutation: drop the `_require_recorded_payload_matches` call from
        `submit_answer`, or make it return unconditionally. Every other check
        still passes — the actor, the run, the gate, all six hashes, the verdict
        and the rationale are identical, because only the attachment moved.
        """
        pending = PendingGate(WORKFLOW_ID, DETAIL_GATE, make_request(), make_derived())
        first = feature_answer()
        # Not `make_decision()`: that one carries the replay gate's rationale, and
        # the rationale is inside the decision. This is what `submit_answer`
        # assembles for `first`, computed independently of it.
        signed = assemble_decision(make_derived(), make_principal(), first, NOW)
        client = RecordingClient()

        outcome = await submit_answer(
            client, pending, make_principal(), first, journal_root=self.journal_root, issued_at=NOW
        )
        self.assertFalse(outcome.resubmitted)
        recorded = read_journal(
            self.journal_root, WORKFLOW_ID, GATE_ID, self.decision.subject_sha256
        )
        self.assertEqual(recorded.decision, signed)
        self.assertEqual(
            recorded.payload_sha256,
            canonical_sha256(_detail_payload(pending, signed, first)),
        )

        edited = feature_answer(
            {**RULINGS, CANDIDATES[0]: {"verdict": "block", "rationale": "changed my mind"}}
        )
        # The decision is untouched by the edit, which is the whole problem.
        self.assertEqual(
            assemble_decision(make_derived(), make_principal(), edited, NOW), recorded.decision
        )
        with self.assertRaises(SubmissionRefused) as raised:
            await submit_answer(
                client,
                pending,
                make_principal(),
                edited,
                journal_root=self.journal_root,
                issued_at=NOW,
            )
        message = str(raised.exception)
        self.assertIn("submitted with different content than this one", message)
        self.assertIn(str(journal_path(self.journal_root, WORKFLOW_ID, GATE_ID)), message)
        self.assertEqual(len(client.calls), 1, "the edited answer was not sent")

        # Positive control: the unedited answer resubmits, verbatim and deduped.
        again = await submit_answer(
            client, pending, make_principal(), first, journal_root=self.journal_root, issued_at=NOW
        )
        self.assertTrue(again.resubmitted)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][1], client.calls[0][1])
        self.assertEqual(client.calls[1][2], client.calls[0][2])

        # The legacy pairing, and it is the sharp one: an entry written before
        # payloads existed, resubmitted for a gate that has one. Everything the
        # decision binds still agrees — only the field that did not exist yet is
        # absent — so `None` must not read as "nothing rode with it".
        write_journal(self.journal_root, WORKFLOW_ID, GATE_ID, signed)
        legacy = read_journal(
            self.journal_root, WORKFLOW_ID, GATE_ID, self.decision.subject_sha256
        )
        self.assertEqual(legacy.decision, signed)
        self.assertIsNone(legacy.payload_sha256)
        with self.assertRaises(SubmissionRefused) as raised:
            await submit_answer(
                client, pending, make_principal(), first, journal_root=self.journal_root, issued_at=NOW
            )
        self.assertIn("(none vs ", str(raised.exception))
        self.assertEqual(len(client.calls), 2, "nothing was sent for the legacy entry")
        with self.assertRaises(SubmissionRefused):
            _require_recorded_payload_matches(
                legacy, _detail_payload(pending, signed, first), pending, self.journal_root
            )
        # And it passes when the two agree, in both shapes.
        self.assertIsNone(
            _require_recorded_payload_matches(
                legacy, legacy.decision, pending, self.journal_root
            )
        )
        self.assertIsNone(
            _require_recorded_payload_matches(
                recorded,
                _detail_payload(pending, recorded.decision, first),
                pending,
                self.journal_root,
            )
        )


# --------------------------------------------------- the real recorded assessment


class RecordedFeatureGateTests(unittest.TestCase):
    """The client against a real recorded assessment, from a run id alone.

    Nothing is stubbed: `assessment_record.record` writes a real SQLite ledger
    and a real content store under a `tempfile` root, and the client reads them
    back through `configure_runtime(read_only=True)` — the same call the CLI
    makes, opening the database through SQLite's `mode=ro`.

    Two derivations that agree prove something only when they are two
    derivations. So every subject compared here is compared against one built by
    `feature_gate.derive_feature_gate_request` from the recorded values directly,
    and each such assertion carries a control showing the hash is capable of
    moving.
    """

    def setUp(self) -> None:
        # `configure_runtime` sets a module global. Restore whatever was there so
        # a later test in the same process is not handed this one's state root.
        previous_runtime = activities._runtime
        self.addCleanup(setattr, activities, "_runtime", previous_runtime)

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # `.resolve()` because `record` and `configure_runtime` both resolve the
        # state root and `/tmp` is a symlink on some systems.
        self.tmp = Path(holder.name).resolve()
        self.state = self.tmp / "state"
        self.ledger_path = self.state / "ledger.sqlite3"
        self.index = write_fake_index(self.tmp / "index")
        self.manifest = write_manifest(self.tmp / "hooks.json")
        self.recorded = assessment_record.record(
            self.state,
            run_id=FEATURE_RUN_ID,
            index_dir=self.index,
            manifest_path=self.manifest,
            allowed_actor=FEATURE_ACTOR,
            owner_token=OWNER_TOKEN,
        )
        # The rulings this file writes are about the candidates stage 4a really
        # mints, so a change to the producer's spelling reaches here as a failed
        # fixture rather than as a test ruling on ids nobody produces.
        self.assertEqual(self.recorded.candidate_ids, CANDIDATES)
        # Independently derived, from the recorded values and nothing the client
        # produced. Everything below is compared against this.
        self.request = feature_gate.derive_feature_gate_request(
            self.recorded.run_id,
            self.recorded.assessment,
            self.recorded.policy_revision,
            self.recorded.allowed_actor,
            self.recorded.candidate_ids,
        )
        activities.configure_runtime(self.state, read_only=True)
        self.assertIs(activities.runtime().ledger.read_only, True)
        self.derived = _resolve_feature_assessment(FEATURE_RUN_ID)
        self.principal = make_principal(FEATURE_ACTOR)
        self.pending = PendingGate(
            "feature-gate-workflow",
            FEATURE_ASSESSMENT_GATE,
            GateRequest(
                schema_version=1,
                run_id=FEATURE_RUN_ID,
                gate_id=self.request.gate_id,
                subject_sha256=self.request.sha256,
                admission_sha256=self.request.sha256,
                prepared_sha256=self.request.sha256,
                policy_revision=self.request.policy_revision,
                issued_at=NOW.isoformat(),
                expires_at=(NOW.replace(day=8)).isoformat(),
            ),
            self.derived,
        )

    # -- helpers

    def build_payload(self, detail: object = None, derived: DerivedSubject | None = None):
        """Assemble a decision for `derived` and run the gate's payload builder."""
        subject = self.derived if derived is None else derived
        answer = feature_answer(detail)
        decision = assemble_decision(subject, self.principal, answer, NOW)
        pending = dataclasses.replace(self.pending, derived=subject)
        return _feature_assessment_payload(pending, decision, answer)

    def fetch(self, reference: ArtifactRef) -> feature_gate.FeatureDispositionsV1:
        """Read the dispositions back out of CAS, by the ref the client minted."""
        body = activities.runtime().store.read_blob(reference.sha256, reference.size)
        return feature_gate.FeatureDispositionsV1.from_dict(json.loads(body.decode("utf-8")))

    # -- the tests

    def test_the_resolver_reproduces_the_recorded_gate_request_hash(self) -> None:
        """FACT 11. The client's subject is the producer's, or it signs nothing.

        Mutation: set `admission_sha256` (or `prepared_sha256`) to anything but
        the request hash. `DerivedSubject` still validates — it only asks for a
        SHA — `verify_published_gate` still compares six fields, and the human
        signs a hash nobody derived. This gate's subject is one derived object
        and the workflow binds a decision to it three times over.
        """
        self.assertIs(type(self.derived), DerivedSubject)
        self.assertEqual(self.derived.run_id, FEATURE_RUN_ID)
        self.assertEqual(self.derived.gate_id, f"{FEATURE_RUN_ID}-feature-assessment-gate")
        self.assertEqual(self.derived.gate_id, self.request.gate_id)
        self.assertEqual(self.derived.policy_revision, self.recorded.policy_revision)
        self.assertEqual(self.derived.allowed_actor, self.recorded.allowed_actor)

        self.assertEqual(self.derived.subject_sha256, self.request.sha256)
        self.assertEqual(self.derived.admission_sha256, self.request.sha256)
        self.assertEqual(self.derived.prepared_sha256, self.request.sha256)

        # Positive control: this hash moves when what it covers moves, so the
        # agreement above is agreement rather than a hash that ignores its input.
        moved = feature_gate.derive_feature_gate_request(
            self.recorded.run_id,
            dataclasses.replace(self.recorded.assessment, input_hashes=()),
            self.recorded.policy_revision,
            self.recorded.allowed_actor,
            self.recorded.candidate_ids,
        )
        self.assertNotEqual(moved.sha256, self.request.sha256)
        self.assertNotEqual(self.derived.subject_sha256, moved.sha256)

    def test_the_shipped_registry_selects_the_feature_kind_for_the_derived_gate_id(
        self,
    ) -> None:
        """FACT 10. Reachable by the gate id the producer actually mints.

        `GATE_KINDS` itself is pinned in `tests/test_submission.py` and is not
        repeated here. What this adds is the wiring: a correct resolver
        registered under an id nothing mints leaves every real gate unanswerable,
        and the failure reads as "no resolver registered" rather than as the
        wiring mistake it is. The gate id comes from the recorded state rather
        than from a literal, so the producer's suffix and the client's matcher
        are compared against each other.
        """
        kind = select_gate_kind(self.derived.gate_id, FEATURE_RUN_ID)
        self.assertIs(kind, FEATURE_ASSESSMENT_GATE)
        self.assertIs(kind.resolve, _resolve_feature_assessment)
        self.assertIs(kind.payload, _feature_assessment_payload)
        self.assertEqual(kind.update_name, "submit_feature_dispositions")

        # A near miss is not a match: the suffix is matched whole and run-scoped.
        for gate_id in (
            f"{FEATURE_RUN_ID}-feature-assessment-gate-2",
            f"x{FEATURE_RUN_ID}-feature-assessment-gate",
            f"{FEATURE_RUN_ID}-feature-assessment",
            f"{self.derived.gate_id}-2",
            FEATURE_RUN_ID,
        ):
            with self.subTest(gate_id=gate_id):
                self.assertFalse(FEATURE_ASSESSMENT_GATE.matches(gate_id, FEATURE_RUN_ID))
                with self.assertRaises(SubmissionRefused):
                    select_gate_kind(gate_id, FEATURE_RUN_ID)
        # Nor does another run's feature gate, or this run's other gate.
        self.assertFalse(FEATURE_ASSESSMENT_GATE.matches(self.derived.gate_id, "run-other"))
        self.assertIs(
            select_gate_kind(f"{FEATURE_RUN_ID}-final-verification-gate", FEATURE_RUN_ID),
            REPLAY_VERIFICATION_GATE,
        )

    def test_the_payload_is_a_submission_the_admitting_side_would_accept(self) -> None:
        """FACT 12. The client runs the admitting side's own validator on itself.

        If it cannot admit its own answer it refuses here, rather than making a
        human's decision fail at a worker where they cannot see why.

        Mutation: delete the `validate_submission(...)` line. Nothing else in the
        builder notices a decision whose subject is not the derived request hash
        — the rulings decode, the document builds, the blob lands in CAS — so the
        tampered subject below stops raising and the payload comes back looking
        perfectly well formed. That is what makes the refusal observable.
        """
        payload = self.build_payload()

        self.assertIs(type(payload), feature_gate.FeatureGateSubmissionV1)
        self.assertEqual(payload.schema_version, 1)
        self.assertEqual(payload.decision.subject_sha256, self.request.sha256)
        self.assertEqual(payload.dispositions.kind, feature_gate.DISPOSITIONS_ARTIFACT_KIND)

        # In CAS, readable by the ref the client minted, and holding exactly the
        # document whose digest that ref pins.
        document = self.fetch(payload.dispositions)
        self.assertEqual(document.assessment_sha256, self.recorded.assessment.sha256)
        self.assertEqual(document.policy_revision, self.recorded.policy_revision)
        self.assertEqual(
            [item.candidate_id for item in document.dispositions],
            list(self.recorded.candidate_ids),
        )
        self.assertEqual(document.sha256, payload.dispositions.sha256)
        self.assertEqual(
            len(canonical_json(document).encode("utf-8")), payload.dispositions.size
        )

        # The admitting side's own check, against the independently derived
        # request. This is the only evidence the two halves of the design meet.
        self.assertIsNone(
            feature_gate.validate_submission(
                self.request, payload, document, self.recorded.document
            )
        )

        # And a case it rejects, so the pass above is about agreement rather than
        # about a validator that never fires. Only `validate_submission` can
        # notice this: the subject hash reaches the decision and nothing else.
        tampered = dataclasses.replace(
            self.derived,
            subject_sha256="9" * 64,
            admission_sha256="9" * 64,
            prepared_sha256="9" * 64,
        )
        with self.assertRaises((ValueError, SubmissionRefused)) as raised:
            self.build_payload(derived=tampered)
        self.assertIn("does not bind the derived gate request", str(raised.exception))

    def test_building_a_payload_makes_no_ledger_write(self) -> None:
        """FACT 13. CAS is availability; the ledger is authority, and stays shut.

        The client writes the dispositions document to the content store, which
        is deliberate — `put_blob` touches no ledger table and an `ArtifactRef`
        acquires provenance only when `record_effect` binds it to an operation
        key — but it must make no ledger write at all. A client that can create
        the state it is checking is not checking anything.

        Fingerprinted on size, nanosecond mtime and digest together, because each
        alone is weak: a same-size overwrite keeps the size, a second-granularity
        check misses a fast rewrite, and a digest alone misses a rewrite of
        identical bytes.
        """
        before = ledger_fingerprint(self.ledger_path)

        first = self.build_payload()
        second = self.build_payload(
            {candidate: {"verdict": "ignore", "rationale": ""} for candidate in CANDIDATES}
        )

        self.assertEqual(ledger_fingerprint(self.ledger_path), before)
        self.assertNotEqual(first.dispositions.sha256, second.dispositions.sha256)
        # The blobs did land, so the unchanged ledger is not an unchanged client.
        self.assertIsNotNone(self.fetch(first.dispositions))
        self.assertIsNotNone(self.fetch(second.dispositions))

        # Positive control, component by component: without it, "the fingerprint
        # did not change" could be true because the fingerprint cannot change.
        # One recording moves the mtime and the digest but not necessarily the
        # size — SQLite allocates a page at a time — so the size half keeps
        # recording until it moves rather than asserting a page count.
        def record(run_id: str) -> None:
            assessment_record.record(
                self.state,
                run_id=run_id,
                index_dir=self.index,
                manifest_path=self.manifest,
                allowed_actor=FEATURE_ACTOR,
                owner_token=OWNER_TOKEN,
            )

        record("run-feature-gate-control-1")
        after_one = ledger_fingerprint(self.ledger_path)
        self.assertNotEqual(after_one[1], before[1], "mtime_ns did not move")
        self.assertNotEqual(after_one[2], before[2], "sha256 did not move")
        for extra in range(2, 12):
            record(f"run-feature-gate-control-{extra}")
            after = ledger_fingerprint(self.ledger_path)
            if after[0] != before[0]:
                break
        else:  # pragma: no cover - the file grew well before this
            self.fail("the ledger file never grew, so the size check is inert")
        self.assertNotEqual(after[1], before[1])
        self.assertNotEqual(after[2], before[2])

    def test_the_dispositions_ref_is_a_pure_function_of_the_document_and_prefixed(
        self,
    ) -> None:
        """FACT 14. Same rulings, same ref; and never mistakable for an operation.

        `producer_operation_id` is a bare 64-hex digest everywhere the ledger
        mints one. A client-minted ref carrying that shape could be presented as
        the output of an operation nobody ran, so it is prefixed and the prefix
        is structural rather than cosmetic.

        Purity is what makes a resubmission of the same rulings mint the same ref
        — which is the same property the journal's payload digest relies on.
        """
        first = self.build_payload().dispositions
        second = self.build_payload().dispositions

        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        for field in dataclasses.fields(ArtifactRef):
            with self.subTest(field=field.name):
                self.assertEqual(getattr(first, field.name), getattr(second, field.name))

        self.assertTrue(first.producer_operation_id.startswith("client-"))
        self.assertIsNone(
            SHA256_PATTERN.fullmatch(first.producer_operation_id),
            "a bare 64-hex id is what a real operation key looks like",
        )
        self.assertEqual(first.producer_operation_id, f"client-{first.sha256}")
        self.assertEqual(first.input_hashes, (self.recorded.assessment.sha256,))

        # Different rulings, different document, therefore a different id: the id
        # follows the bytes rather than being a constant that happens to be safe.
        # `ignore` rather than `block`, because no device has looked for these
        # candidates in this state root and the client refuses to send an answer
        # the admitting side would reject — which is `AnsweringWithoutMeasurementTests`.
        other = self.build_payload(
            {candidate: {"verdict": "ignore", "rationale": ""} for candidate in CANDIDATES}
        ).dispositions
        self.assertNotEqual(other.sha256, first.sha256)
        self.assertNotEqual(other.producer_operation_id, first.producer_operation_id)

    def test_the_rulings_template_names_exactly_the_derived_candidate_ids(self) -> None:
        """FACT 16. So a renamed, dropped or invented id is refused by name.

        The template is the only thing standing between a human and hand-typing
        a candidate list. Its ids come from the derived request, so a file built
        from it and then edited is refused *before* anything is signed rather
        than quietly ruling on a set the pinned bytes do not contain.
        """
        template = json.loads(_rulings_template(self.pending))

        self.assertEqual(list(template), list(self.recorded.candidate_ids))
        self.assertEqual(list(template), list(self.request.candidate_ids))
        for entry in template.values():
            self.assertEqual(entry, {"verdict": "defer", "rationale": "", "consent": ""})
        # The template is INVALID as emitted, deliberately: every verdict but
        # `ignore` needs a rationale, so a human cannot answer this gate without
        # typing something for each candidate. A template that submitted cleanly
        # unedited would let someone approve four rulings they never made.
        with self.assertRaises(SubmissionRefused) as raised:
            _feature_rulings(template, self.recorded.candidate_ids)
        self.assertIn("no rationale", str(raised.exception))
        self.assertIn(self.recorded.candidate_ids[0], str(raised.exception))
        # Filled in, the same keys decode in the derived order.
        filled = {
            candidate: {"verdict": "block", "rationale": "because"}
            for candidate in template
        }
        self.assertEqual(
            list(_feature_rulings(filled, self.recorded.candidate_ids)),
            list(self.recorded.candidate_ids),
        )


# ------------------------------------------------------------------ CLI helpers


class ReadRulingsTests(TemporaryRootTestCase):
    """The `--rulings` flag, decoded before it can reach anything that signs."""

    def test_no_file_is_none_and_a_file_that_is_not_an_object_is_refused_by_path(
        self,
    ) -> None:
        """FACT 15. Naming the path is the whole message.

        A human runs this with `--state-root`, `--journal` and `--rulings` all in
        flight; "not readable JSON" without a path is a sentence they cannot act
        on, and the habit it teaches — rerun and see — is the last one anyone
        answering a gate should have.
        """
        self.assertIsNone(_read_rulings(None))

        for name, body in (
            ("truncated.json", '{"gap:feed/reels_media/": {"verdict": "def'),
            ("empty.json", ""),
            ("text.json", "approve everything"),
        ):
            path = self.root / name
            path.write_text(body, encoding="utf-8")
            with self.subTest(body=name):
                with self.assertRaises(SubmissionRefused) as raised:
                    _read_rulings(path)
                self.assertEqual(
                    str(raised.exception), f"Rulings file is not readable JSON: {path}"
                )

        missing = self.root / "absent.json"
        with self.assertRaises(SubmissionRefused) as raised:
            _read_rulings(missing)
        self.assertEqual(str(raised.exception), f"Rulings file is not readable JSON: {missing}")

        for name, body in (
            ("array.json", json.dumps([{"verdict": "defer"}])),
            ("string.json", json.dumps("defer")),
            ("null.json", json.dumps(None)),
            ("number.json", json.dumps(4)),
        ):
            path = self.root / name
            path.write_text(body, encoding="utf-8")
            with self.subTest(body=name):
                with self.assertRaises(SubmissionRefused) as raised:
                    _read_rulings(path)
                self.assertEqual(
                    str(raised.exception), f"Rulings file must hold a JSON object: {path}"
                )

        # Positive control: a real rulings file comes back as the mapping the
        # decoder expects, so the refusals above are about the shape.
        good = self.root / "rulings.json"
        good.write_text(json.dumps(RULINGS), encoding="utf-8")
        self.assertEqual(_read_rulings(good), RULINGS)


if __name__ == "__main__":
    unittest.main()


class AnsweringWithoutMeasurementTests(RecordedFeatureGateTests):
    """The client will not send a `block` for a candidate no device looked for.

    This is the 2026-08-08 failure caught end to end, at the earliest point it
    can be caught. Six candidates were ruled `block` in one sitting on the two
    static evidence items that were all the gate could offer; across the 72
    device sessions recorded since, five are requested zero times and one is not
    an endpoint at all.

    The refusal happens **in the client**, before anything is sent, because the
    client runs the admitting side's own validator on its own payload. A human
    finds out while they still have the editor open, rather than after a
    round trip to a worker.
    """

    def acting(self, verdict: str) -> dict:
        return {
            candidate: {"verdict": verdict, "rationale": "the app groups it with reels"}
            for candidate in CANDIDATES
        }

    def test_an_acting_verdict_on_an_unmeasured_candidate_is_refused(self) -> None:
        for verdict in ("block", "offer_toggle"):
            with self.subTest(verdict=verdict):
                with self.assertRaises(SubmissionRefused) as caught:
                    self.build_payload(self.acting(verdict))
                message = str(caught.exception)
                self.assertIn("no device has looked for it", message)
                self.assertIn("observe_watch", message, "the repair must be named")
                self.assertIn(
                    "cannot admit its own answer", message,
                    "and it must be clear the client stopped itself",
                )

    def test_the_same_candidates_may_be_ignored_or_deferred(self) -> None:
        """The gate stays answerable without a phone, which was the condition on
        which this restriction was accepted."""
        for verdict, rationale in (("ignore", ""), ("defer", "next version")):
            with self.subTest(verdict=verdict):
                payload = self.build_payload(
                    {c: {"verdict": verdict, "rationale": rationale} for c in CANDIDATES}
                )
                self.assertTrue(payload.dispositions.sha256)

    def test_the_refusal_names_the_candidate_to_go_and_watch(self) -> None:
        """A refusal that says only "not allowed" leaves a human guessing which of
        a hundred candidates to do something about."""
        with self.assertRaises(SubmissionRefused) as caught:
            self.build_payload(
                {
                    **RULINGS,
                    CANDIDATES[1]: {"verdict": "block", "rationale": "kill it"},
                }
            )
        self.assertIn(CANDIDATES[1], str(caught.exception))

    def test_a_mixed_answer_is_refused_only_for_the_acting_candidate(self) -> None:
        """One unmeasured candidate must not make the whole gate unanswerable —
        that outcome was weighed and rejected when this was designed."""
        payload = self.build_payload(
            {**RULINGS, CANDIDATES[0]: {"verdict": "ignore", "rationale": ""}}
        )
        self.assertTrue(payload.dispositions.sha256)
