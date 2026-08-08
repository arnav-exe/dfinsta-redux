"""The reversal gate's wire contracts, and the authority that admits an answer.

Pure: no ledger, no store, no clock. `tests/test_reversal_record.py` covers the
producer and consumer and `tests/test_reversal_workflow.py` covers the join on a
real Temporal environment; this file is about what a document must be and what
`validate_submission` refuses.

Every clause of the authority gets its own test, and each starts from a
submission that is otherwise valid — a test that fails for two reasons proves
nothing about either.
"""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

from dfinsta_pipeline import reversal
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json
from dfinsta_pipeline.reversal_gate import (
    DOCKET_ARTIFACT_KIND,
    GATE_ID_SUFFIX,
    KINDS,
    RULINGS_ARTIFACT_KIND,
    VERDICTS,
    WITHDRAWING_VERDICT,
    ReversalGateError,
    ReversalGateRequestV1,
    ReversalGateSubmissionV1,
    ReversalGateV1,
    ReversalRulingsAdmissionV1,
    ReversalRulingsV1,
    ReversalRulingV1,
    ReversalRunRequestV1,
    ReversalRunResultV1,
    ReversalSubjectV1,
    derive_reversal_gate,
    derive_reversal_gate_request,
    derived_gate_id,
    docket_subjects,
    item_id,
    item_sha256,
    validate_submission,
)

#: Resolved from this file rather than from the process CWD, so the source-reading
#: tests below fail loudly when a file moves instead of when a runner is started
#: from the wrong directory. Inside a function, not at module scope: the Temporal
#: sandbox restricts `Path.resolve` and re-imports any module that defines a
#: Workflow class, and that trap has cost this project a whole test file before.
def repository_root() -> "object":
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


RUN_ID = "reconsider-441"
GATE_ID = f"{RUN_ID}{GATE_ID_SUFFIX}"
ACTOR = "arnav"
POLICY = "2026-08-01"
VERSION = "441"

FEED = "feed/timeline_stream/"
BLOCK_DECISION = "decision-feature-441"
RETIRE_DECISION = "decision-retire-441"


def a_docket() -> dict:
    return {
        "schema_version": 1,
        "version": VERSION,
        "policy_revision": POLICY,
        "rules_not_run": ["block_endpoint_absent: no --index given"],
        "items": [
            {
                "item_id": item_id("block", BLOCK_DECISION, FEED),
                "kind": "block",
                "subject": FEED,
                "original_decision_id": BLOCK_DECISION,
                "triggers": ["block_inert"],
                "summaries": ["the block cannot be doing anything"],
                "evidence": ["enforced by tigon_url_block, which ran on: none"],
            },
            {
                "item_id": item_id("retirement", RETIRE_DECISION, "set_app_context"),
                "kind": "retirement",
                "subject": "set_app_context",
                "original_decision_id": RETIRE_DECISION,
                "triggers": ["retirement_returned"],
                "summaries": ["retired from 441 and has executed since"],
                "evidence": ["ran on: 440, 441"],
            },
        ],
    }


def a_reference(kind: str, document) -> ArtifactRef:
    body = canonical_json(document).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return ArtifactRef(
        schema_version=1,
        kind=kind,
        sha256=digest,
        size=len(body),
        uri=f"cas://sha256/{digest}",
        producer_operation_id=f"op-{digest[:16]}",
        input_hashes=(),
    )


def a_request(**overrides) -> ReversalGateRequestV1:
    document = overrides.pop("document", None) or a_docket()
    arguments = {
        "run_id": RUN_ID,
        "docket": a_reference(DOCKET_ARTIFACT_KIND, document),
        "version": VERSION,
        "policy_revision": POLICY,
        "allowed_actor": ACTOR,
        "items": docket_subjects(document),
    }
    arguments.update(overrides)
    return derive_reversal_gate_request(**arguments)


def a_rulings(request: ReversalGateRequestV1, **overrides) -> ReversalRulingsV1:
    verdict = overrides.pop("verdict", WITHDRAWING_VERDICT)
    rationale = overrides.pop("rationale", "the evidence no longer supports it")
    items = overrides.pop("items", request.items)
    arguments = {
        "schema_version": 1,
        "docket_sha256": request.docket.sha256,
        "version": request.version,
        "policy_revision": request.policy_revision,
        "rulings": tuple(
            ReversalRulingV1(1, item.item_id, verdict, rationale, item.item_sha256)
            for item in items
        ),
    }
    arguments.update(overrides)
    return ReversalRulingsV1(**arguments)


def a_decision(request: ReversalGateRequestV1, **overrides) -> GateDecision:
    subject = overrides.pop("subject", request.sha256)
    arguments = {
        "schema_version": 1,
        "decision_id": "decision-gate-1",
        "idempotency_id": "idempotency-gate-1",
        "actor": request.allowed_actor,
        "run_id": request.run_id,
        "gate_id": request.gate_id,
        "subject_sha256": subject,
        "admission_sha256": subject,
        "prepared_sha256": subject,
        "policy_revision": request.policy_revision,
        "decision": "approve",
        "rationale": "reviewed the evidence",
        "issued_at": "2026-08-09T12:00:00+00:00",
    }
    arguments.update(overrides)
    return GateDecision(**arguments)


def a_submission(request, rulings, **overrides) -> ReversalGateSubmissionV1:
    return ReversalGateSubmissionV1(
        1,
        overrides.pop("decision", None) or a_decision(request, **overrides),
        overrides.pop("rulings_ref", None)
        or a_reference(RULINGS_ARTIFACT_KIND, rulings.to_dict()),
    )


class VocabularyTests(unittest.TestCase):
    """The two closed sets, and where they must and must not agree."""

    def test_kinds_agree_with_the_record_layer_without_importing_it(self) -> None:
        """Cross-checked rather than imported, as `retirement_gate.VERDICTS` is.

        This layer is the wire contract and `reversal.KINDS` is the local record.
        A change to either that silently changed the other is the coupling worth
        refusing; a change to one that this test catches is the point.
        """
        self.assertEqual(reversal.KINDS, KINDS)

    def test_the_verdicts_are_closed_and_one_of_them_acts(self) -> None:
        self.assertEqual(("withdraw", "keep", "defer"), VERDICTS)
        self.assertIn(WITHDRAWING_VERDICT, VERDICTS)
        # `keep` and `defer` must both be inert, or a docket answered "not yet"
        # would withdraw something.
        self.assertEqual(1, sum(v == WITHDRAWING_VERDICT for v in VERDICTS))

    def test_the_gate_id_suffix_collides_with_no_other_gate(self) -> None:
        for other in (
            "-hook-retirement-gate",
            "-feature-assessment-gate",
            "-final-verification-gate",
        ):
            with self.subTest(other=other):
                self.assertNotEqual(f"{RUN_ID}{other}", derived_gate_id(RUN_ID))


class GateIdTests(unittest.TestCase):
    def test_a_gate_id_derives_from_the_run_id(self) -> None:
        self.assertEqual(GATE_ID, derived_gate_id(RUN_ID))

    def test_a_run_id_that_cannot_make_an_identifier_is_refused(self) -> None:
        for run_id in ("", "has spaces", "!leading", "a" * 200):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ReversalGateError):
                    derived_gate_id(run_id)

    def test_a_gate_id_is_never_truncated_to_fit(self) -> None:
        """Silently shortening one would still look plausible and would stop
        matching the client's predicate — answerable in a test, unanswerable in
        production."""
        run_id = "r" * 120
        with self.assertRaises(ReversalGateError) as caught:
            derived_gate_id(run_id)
        self.assertIn("not a valid identifier", str(caught.exception))


class ItemIdentityTests(unittest.TestCase):
    """What names a docket item, and what a ruling's digest covers."""

    def test_an_item_id_is_the_triple_and_nothing_else(self) -> None:
        first = item_id("block", BLOCK_DECISION, FEED)
        self.assertEqual(first, item_id("block", BLOCK_DECISION, FEED))
        self.assertTrue(first.startswith("block-"))
        for other in (
            item_id("retirement", BLOCK_DECISION, FEED),
            item_id("block", "decision-other", FEED),
            item_id("block", BLOCK_DECISION, "explore/topical_explore/"),
        ):
            self.assertNotEqual(first, other)

    def test_an_item_id_is_a_valid_identifier_though_a_subject_is_not(self) -> None:
        """An endpoint path has slashes, so the subject cannot be the id."""
        from dfinsta_pipeline.contracts import ID_PATTERN

        self.assertIsNone(ID_PATTERN.fullmatch(FEED))
        self.assertIsNotNone(ID_PATTERN.fullmatch(item_id("block", BLOCK_DECISION, FEED)))

    def test_an_unknown_kind_or_a_blank_field_is_refused(self) -> None:
        for arguments in (
            ("suppression", BLOCK_DECISION, FEED),
            ("block", "   ", FEED),
            ("block", BLOCK_DECISION, ""),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ReversalGateError):
                    item_id(*arguments)

    def test_the_item_digest_covers_the_prose_a_human_read(self) -> None:
        """Not the identifying triple. A digest that survived an edit to the
        evidence would let a ruling claim to answer something nobody saw."""
        document = a_docket()
        before = item_sha256(document["items"][0])
        document["items"][0]["evidence"] = ["a rewritten line"]
        self.assertNotEqual(before, item_sha256(document["items"][0]))


class DocketSubjectTests(unittest.TestCase):
    def test_subjects_derive_from_the_document(self) -> None:
        document = a_docket()
        subjects = docket_subjects(document)
        self.assertEqual(["block", "retirement"], [s.kind for s in subjects])
        self.assertEqual(
            [entry["item_id"] for entry in document["items"]],
            [s.item_id for s in subjects],
        )

    def test_an_item_id_that_does_not_derive_is_refused(self) -> None:
        document = a_docket()
        document["items"][0]["item_id"] = "block-0000000000000000"
        with self.assertRaises(ReversalGateError) as caught:
            docket_subjects(document)
        self.assertIn("does not derive", str(caught.exception))

    def test_a_subject_swapped_between_two_items_is_refused(self) -> None:
        """The id binds the triple, so exchanging the subjects breaks both."""
        document = a_docket()
        document["items"][0]["subject"] = "explore/topical_explore/"
        with self.assertRaises(ReversalGateError):
            docket_subjects(document)

    def test_two_items_sharing_an_id_are_refused_here_not_only_in_the_request(self) -> None:
        """`publish_admitted` builds `{item_id: entry}` and never builds a request.

        `ReversalGateRequestV1` refuses duplicates, but the consumer reads the
        document directly — so a collision there drops an item silently and
        attributes one decision's ruling to another. The id is a truncated digest
        and the width is one edit from being too short.
        """
        document = a_docket()
        document["items"][1] = dict(document["items"][0], evidence=["different prose"])
        with self.assertRaises(ReversalGateError) as caught:
            docket_subjects(document)
        self.assertIn("share an id", str(caught.exception))

    def test_an_empty_rulings_document_is_not_an_answer(self) -> None:
        with self.assertRaises(ReversalGateError) as caught:
            ReversalRulingsV1(1, "a" * 64, VERSION, POLICY, ())
        self.assertIn("not an answer", str(caught.exception))

    def test_a_malformed_docket_is_refused(self) -> None:
        for document in (
            {"items": "not a list"},
            {"items": [None]},
            {"items": [{"kind": "block", "subject": FEED}]},
            {},
        ):
            with self.subTest(document=document):
                with self.assertRaises(ReversalGateError):
                    docket_subjects(document)


class ContractRoundTripTests(unittest.TestCase):
    """Every wire type survives a round trip and refuses a sloppy one."""

    def test_each_type_round_trips(self) -> None:
        request = a_request()
        rulings = a_rulings(request)
        submission = a_submission(request, rulings)
        gate = derive_reversal_gate(request)
        for value, cls in (
            (gate, ReversalGateV1),
            (request, ReversalGateRequestV1),
            (request.items[0], ReversalSubjectV1),
            (rulings.rulings[0], ReversalRulingV1),
            (rulings, ReversalRulingsV1),
            (submission, ReversalGateSubmissionV1),
            (ReversalRunRequestV1(1, RUN_ID, 3600), ReversalRunRequestV1),
            (
                ReversalRunResultV1(1, RUN_ID, "completed", "decision-gate-1", submission.rulings),
                ReversalRunResultV1,
            ),
            (ReversalRulingsAdmissionV1(1, RUN_ID, submission), ReversalRulingsAdmissionV1),
        ):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(value, cls.from_dict(value.to_dict()))

    def test_a_missing_or_unknown_key_is_refused(self) -> None:
        """Strict both ways. A decoder that tolerates a missing key supplies a
        default, and the field most worth omitting is the one that binds a
        document to a subject."""
        body = a_request().to_dict()
        for mutate in (
            lambda d: d.pop("docket"),
            lambda d: d.update(note="hello"),
            lambda d: d.pop("allowed_actor"),
        ):
            with self.subTest(mutate=mutate):
                copy = dict(body)
                mutate(copy)
                with self.assertRaises(ReversalGateError):
                    ReversalGateRequestV1.from_dict(copy)

    def test_an_artifact_of_the_wrong_kind_is_refused(self) -> None:
        request = a_request()
        rulings = a_rulings(request)
        docket_ref = a_reference(DOCKET_ARTIFACT_KIND, rulings.to_dict())
        with self.assertRaises(ReversalGateError):
            ReversalGateSubmissionV1(1, a_decision(request), docket_ref)
        with self.assertRaises(ReversalGateError):
            a_request(docket=a_reference(RULINGS_ARTIFACT_KIND, a_docket()))

    def test_a_request_with_no_items_is_refused(self) -> None:
        with self.assertRaises(ReversalGateError) as caught:
            a_request(items=())
        self.assertIn("nothing to ask", str(caught.exception))

    def test_a_duplicate_item_is_refused(self) -> None:
        request = a_request()
        with self.assertRaises(ReversalGateError):
            a_request(items=(request.items[0], request.items[0]))

    def test_an_unknown_verdict_is_refused(self) -> None:
        request = a_request()
        with self.assertRaises(ReversalGateError):
            ReversalRulingV1(1, request.items[0].item_id, "unblock", "x", "a" * 64)

    def test_an_item_id_that_disagrees_with_its_kind_is_refused(self) -> None:
        with self.assertRaises(ReversalGateError):
            ReversalSubjectV1(1, "retirement-0000000000000000", "block", "a" * 64)


class DeriveTests(unittest.TestCase):
    def test_the_gate_takes_actor_and_policy_from_the_request(self) -> None:
        """A second parameter is a second chance to disagree, and the one that
        reached History would be the one the validator enforced."""
        request = a_request()
        gate = derive_reversal_gate(request)
        self.assertEqual(request.allowed_actor, gate.allowed_actor)
        self.assertEqual(request.policy_revision, gate.policy_revision)
        self.assertEqual(request.sha256, gate.request_sha256)
        self.assertEqual(GATE_ID, gate.gate_id)

    def test_the_gate_carries_no_prose(self) -> None:
        """Six scalars. The docket holds an agent's summary of every suspect
        decision, and History is permanent and replayable."""
        gate = derive_reversal_gate(a_request())
        self.assertEqual(6, len(dataclasses.fields(gate)))
        rendered = canonical_json(gate.to_dict())
        for prose in ("block cannot be doing anything", FEED, "block_inert"):
            self.assertNotIn(prose, rendered)

    def test_deriving_from_the_wrong_type_is_refused(self) -> None:
        with self.assertRaises(ReversalGateError):
            derive_reversal_gate(a_docket())


class ValidateSubmissionTests(unittest.TestCase):
    """The authority, clause by clause. Each starts from a valid submission."""

    def setUp(self) -> None:
        self.request = a_request()
        self.rulings = a_rulings(self.request)
        self.submission = a_submission(self.request, self.rulings)

    def check(self, **overrides) -> None:
        validate_submission(
            overrides.get("request", self.request),
            overrides.get("submission", self.submission),
            overrides.get("rulings", self.rulings),
        )

    def test_a_valid_submission_is_admitted(self) -> None:
        """The positive control. Every refusal below is worthless without it."""
        self.check()

    def test_a_reference_that_does_not_hold_this_document_is_refused(self) -> None:
        other = a_rulings(self.request, verdict="keep", rationale="it stands")
        with self.assertRaises(ReversalGateError) as caught:
            self.check(rulings=other)
        self.assertIn("does not hold this document", str(caught.exception))

    def test_a_reference_whose_size_disagrees_is_refused(self) -> None:
        reference = dataclasses.replace(self.submission.rulings, size=1)
        with self.assertRaises(ReversalGateError) as caught:
            self.check(submission=ReversalGateSubmissionV1(1, self.submission.decision, reference))
        self.assertIn("size does not match", str(caught.exception))

    def test_the_subject_digest_is_recomputed_and_never_adopted(self) -> None:
        """`a-client-must-not-assert-what-it-approves`, at the authority.

        `validate_submission` passes `request.sha256` — the digest it recomputed —
        into `bind_decision`. Changing that one argument to
        `submission.decision.subject_sha256` makes all three clauses tautological
        and every other test in this file still passes, because they each mutate
        *one* hash field and the remaining two then disagree with it.

        So this decision is bound self-consistently to a digest nobody derived:
        all three fields hold the same wrong value. Only a re-derivation can tell
        the difference, which is the entire premise of the gate.
        """
        decision = a_decision(self.request, subject="f" * 64)
        with self.assertRaises(ReversalGateError) as caught:
            self.check(
                submission=ReversalGateSubmissionV1(
                    1, decision, self.submission.rulings
                )
            )
        self.assertIn("does not bind the derived gate request", str(caught.exception))
        # The positive control: the same construction with the REAL digest is
        # admitted, so the refusal above is about the value and not the shape.
        self.check(
            submission=ReversalGateSubmissionV1(
                1, a_decision(self.request), self.submission.rulings
            )
        )

    def test_all_three_hash_fields_are_checked_not_only_the_subject(self) -> None:
        """The clause that was missing once, and the reason `gate_contract` exists.

        A decision bound correctly on `subject_sha256` and wrongly on either of
        the other two was admitted by any route that bypassed the sandbox filter —
        and an Activity is reachable independently of its Workflow.
        """
        for field in ("subject_sha256", "admission_sha256", "prepared_sha256"):
            with self.subTest(field=field):
                decision = dataclasses.replace(self.submission.decision, **{field: "f" * 64})
                with self.assertRaises(ReversalGateError):
                    self.check(
                        submission=ReversalGateSubmissionV1(
                            1, decision, self.submission.rulings
                        )
                    )

    def test_an_unauthorized_actor_is_refused_by_the_authority(self) -> None:
        """Not only by the sandbox validator. This is the clause whose absence
        put "who may answer" entirely in the filter."""
        decision = dataclasses.replace(self.submission.decision, actor="intruder")
        with self.assertRaises(ReversalGateError) as caught:
            self.check(
                submission=ReversalGateSubmissionV1(1, decision, self.submission.rulings)
            )
        self.assertIn("not authorized", str(caught.exception))

    def test_a_decision_bound_to_another_run_gate_or_policy_is_refused(self) -> None:
        for field, value in (
            ("run_id", "reconsider-440"),
            ("gate_id", "reconsider-440-reversal-gate"),
            ("policy_revision", "2026-01-01"),
        ):
            with self.subTest(field=field):
                decision = dataclasses.replace(self.submission.decision, **{field: value})
                with self.assertRaises(ReversalGateError):
                    self.check(
                        submission=ReversalGateSubmissionV1(
                            1, decision, self.submission.rulings
                        )
                    )

    def test_rulings_that_answer_another_docket_are_refused(self) -> None:
        other = a_rulings(self.request, docket_sha256="e" * 64)
        with self.assertRaises(ReversalGateError) as caught:
            self.check(
                rulings=other,
                submission=a_submission(self.request, other),
            )
        self.assertIn("different docket", str(caught.exception))

    def test_rulings_naming_another_version_or_policy_are_refused(self) -> None:
        for field, value, message in (
            ("version", "440", "different Instagram version"),
            ("policy_revision", "2026-01-01", "different policy revision"),
        ):
            with self.subTest(field=field):
                other = a_rulings(self.request, **{field: value})
                with self.assertRaises(ReversalGateError) as caught:
                    self.check(rulings=other, submission=a_submission(self.request, other))
                self.assertIn(message, str(caught.exception))

    def test_a_missing_ruling_is_refused_and_is_not_read_as_keep(self) -> None:
        """Silence is not an answer. Reading it as `keep` would leave a block in
        force on the strength of an answer no human gave."""
        other = a_rulings(self.request, items=self.request.items[:1])
        with self.assertRaises(ReversalGateError) as caught:
            self.check(rulings=other, submission=a_submission(self.request, other))
        message = str(caught.exception)
        self.assertIn(self.request.items[1].item_id, message)
        self.assertIn("not a `keep`", message)

    def test_a_ruling_for_something_not_in_the_docket_is_refused(self) -> None:
        stray = ReversalRulingV1(
            1, item_id("block", "decision-elsewhere", "other/"), "withdraw", "x", "a" * 64
        )
        other = ReversalRulingsV1(
            1,
            self.request.docket.sha256,
            VERSION,
            POLICY,
            self.rulings.rulings + (stray,),
        )
        with self.assertRaises(ReversalGateError) as caught:
            self.check(rulings=other, submission=a_submission(self.request, other))
        self.assertIn("not in this docket", str(caught.exception))

    def test_ruling_on_one_decision_twice_is_refused(self) -> None:
        other = ReversalRulingsV1(
            1,
            self.request.docket.sha256,
            VERSION,
            POLICY,
            self.rulings.rulings + (self.rulings.rulings[0],),
        )
        with self.assertRaises(ReversalGateError) as caught:
            self.check(rulings=other, submission=a_submission(self.request, other))
        self.assertIn("ruled on twice", str(caught.exception))

    def test_every_verdict_needs_a_rationale_including_keep(self) -> None:
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                other = a_rulings(self.request, verdict=verdict, rationale="   ")
                with self.assertRaises(ReversalGateError) as caught:
                    self.check(rulings=other, submission=a_submission(self.request, other))
                self.assertIn("needs a rationale", str(caught.exception))

    def test_a_ruling_that_answers_a_different_item_is_refused(self) -> None:
        """The clause the retirement gate does not have.

        `RetirementRulingV1.case_sha256` is filled in by the client from the
        recorded docket and nothing verifies it. This digest is what
        `publish_admitted` records as the evidence a human ruled against, and a
        permanent record naming evidence nobody saw is worse than one naming none.
        """
        swapped = ReversalRulingsV1(
            1,
            self.request.docket.sha256,
            VERSION,
            POLICY,
            (
                ReversalRulingV1(
                    1,
                    self.request.items[0].item_id,
                    "withdraw",
                    "reviewed",
                    self.request.items[1].item_sha256,
                ),
                self.rulings.rulings[1],
            ),
        )
        with self.assertRaises(ReversalGateError) as caught:
            self.check(rulings=swapped, submission=a_submission(self.request, swapped))
        self.assertIn("answers docket item", str(caught.exception))

    def test_the_verdict_itself_is_not_judged_here(self) -> None:
        """Deliberate. The Activity checks `decision == approve`; a gate contract
        that also refused `defer` would put the same rule in two places and let
        one of them be relaxed."""
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                other = a_rulings(self.request, verdict=verdict)
                self.check(rulings=other, submission=a_submission(self.request, other))

    def test_the_authority_reads_every_decision_field_the_filter_reads(self) -> None:
        """`the-authority-checked-less-than-the-filter`, made mechanical.

        The Workflow's update validator runs in the sandbox and is a *filter*;
        this module is the authority. When one gate last split a check that way,
        the authority checked less — it compared only `subject_sha256` and never
        the actor — so "who may answer" came to rest entirely on a validator that
        an Activity call bypasses.

        Derived from the source rather than listed, so a clause added to the
        filter alone fails here instead of being noticed by nobody. Three fields
        are deliberately filter-only and are named with their reasons; anything
        else that appears in the validator and not in the authority is the bug.

        **What this proves and what it does not.** It proves each field is *read*
        by the authority; it cannot prove the comparison is the right one — an
        authority that read `decision.actor` and compared it to itself would pass.
        The behavioural tests above are what cover that, clause by clause, and
        this exists for the case they cannot see: a *new* clause arriving in the
        filter with no counterpart at all. Widening it into a semantic check would
        mean reimplementing the authority in the test, which is the same trap as a
        second derivation of a gate subject.
        """
        import ast
        from pathlib import Path

        #: Checked by the filter alone, on purpose.
        #:
        #: * `issued_at` — the gate's validity window is a Workflow-clock fact.
        #:   The Activity has no `expires_at` to compare against, and re-deriving
        #:   one would need it to read a clock, which an Activity must not do.
        #: * `decision_id` / `idempotency_id` — replay dedup within one open gate.
        #:   The ledger enforces the durable half (`decisions` is append-only and
        #:   both ids are UNIQUE), so the authority's version of this check is a
        #:   constraint rather than a comparison.
        FILTER_ONLY = {"issued_at", "decision_id", "idempotency_id"}

        root = repository_root()
        source = (root / "src/dfinsta_pipeline/reversal_workflow.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        validator = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_submit_reversal_rulings"
        )
        read = {
            node.attr
            for node in ast.walk(validator)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "decision"
        }
        # A parse that found nothing would make every assertion below vacuous.
        self.assertGreaterEqual(len(read), 8, f"the validator parse found only {read}")

        # Parsed, NOT grepped, and this is the whole test. A substring search over
        # these two files can never fail: `ReversalGateSubmissionV1.to_dict`
        # writes `self.decision.<field>` for all thirteen `GateDecision` fields,
        # so `"decision.anything" in source` is true by construction. Three
        # deliberately-unchecked clauses were added to the filter to prove that,
        # and the grep version of this test passed all three — including
        # `schema_version`, chosen as a negative control precisely because the
        # authority does not check it. An absence assertion with no positive
        # control is this repository's most repeated defect, and it had reached
        # the test written to guard against a security regression.
        checked: set[str] = set()
        for path, function in (
            ("src/dfinsta_pipeline/gate_contract.py", "bind_decision"),
            ("src/dfinsta_pipeline/reversal_gate.py", "validate_submission"),
        ):
            body = next(
                node
                for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8")))
                if isinstance(node, ast.FunctionDef) and node.name == function
            )
            # Scoped to attributes read off *the decision* — `decision.x` in
            # `bind_decision`, `submission.decision.x` here — and not to every
            # attribute in the function. A whole-function sweep sees
            # `ruling.rationale` and would report `decision.rationale` as checked.
            checked |= {
                node.attr
                for node in ast.walk(body)
                if isinstance(node, ast.Attribute)
                and (
                    (isinstance(node.value, ast.Name) and node.value.id == "decision")
                    or (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr == "decision"
                    )
                )
            }

        # The positive control the grep version lacked: a field the authority
        # genuinely does not compare must be seen as unchecked. If this starts
        # failing because the authority gained a `schema_version` clause, replace
        # the control rather than deleting it.
        self.assertNotIn("schema_version", checked)

        for field in sorted(read - FILTER_ONLY):
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    checked,
                    f"the filter checks decision.{field} and the authority does not",
                )
        # And the allowlist must really be an allowlist, not a way to empty the
        # rule: every name in it has to be something the filter actually reads.
        self.assertEqual(FILTER_ONLY, FILTER_ONLY & read)

    def test_the_wrong_types_are_refused_before_anything_is_compared(self) -> None:
        for arguments in (
            (a_docket(), self.submission, self.rulings),
            (self.request, self.rulings, self.rulings),
            (self.request, self.submission, self.submission),
        ):
            with self.subTest(arguments=type(arguments[1]).__name__):
                with self.assertRaises(ReversalGateError):
                    validate_submission(*arguments)


if __name__ == "__main__":
    unittest.main()
