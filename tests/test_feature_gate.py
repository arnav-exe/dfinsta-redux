"""Tests for the stage-4 feature assessment gate — the durable human decision.

The gate exists because "addictive" is a judgement and this project's recurring
failure is a confident wrong answer. Stage 4 therefore decides nothing: it puts
evidence in front of a human and records what they ruled, per candidate. Two
properties make that recording worth anything, and almost every test here is
about one of them.

**Derivation is pure.** Two Activities derive the same subject independently —
one publishes only its hash so the Workflow can wait for days on a human, the
other re-derives it when the submission arrives — and neither may trust the
other's copy. A clock read, a ledger lookup or an environment variable anywhere
in `derive_feature_gate_request` makes the two disagree, and the run then fails
closed for a reason that has nothing to do with the human's decision.

**Absence is never a pass.** A candidate nobody ruled on blocks the run. It does
not default to `ignore`. That single check is the difference between a human
having decided and a human having scrolled past, and it is the one this file
attacks hardest.

`MutationTests` adds no coverage. It re-attacks four guards that already have
positive tests, from the direction a broken implementation would take, so that
"the guard is present" and "the guard bites" stay separate claims. Each of its
docstrings says what would ship if that guard were removed.
"""

import hashlib
import inspect
import json
import types
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest import mock

from dfinsta_pipeline import feature_gate
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
from dfinsta_pipeline.feature_gate import (
    ASSESSMENT_ARTIFACT_KIND,
    DISPOSITIONS_ARTIFACT_KIND,
    GATE_ID_SUFFIX,
    MAX_RATIONALE,
    SILENT_VERDICT,
    VERDICTS,
    FeatureAssessmentGateV1,
    FeatureDispositionsV1,
    FeatureDispositionV1,
    FeatureGateRequestV1,
    FeatureGateSubmissionV1,
    derive_assessment_gate,
    derive_feature_gate_request,
    derived_gate_id,
    validate_submission,
)


# --------------------------------------------------------------------- fixture

RUN_ID = "port-439-stage4"
ACTOR = "human-sam"
POLICY = "policy-2026-08"

# The exact candidate ids stage 4a mints for Instagram 439: the four surfaces
# `LX/03Ez` groups with the known feeds and DFInsta does not block, each named
# `gap:{literal}` by `assessment.assess_gap` with the literal spelled as the app
# writes it. Copied verbatim rather than imported, so this suite does not couple
# to the producer's module -- but it is why a candidate id has to hold a `:`, a
# `/` and a trailing `/`.
CANDIDATES = (
    "gap:feed/injected_reels_media/",
    "gap:feed/reels_media/",
    "gap:feed/reels_media_stream/",
    "gap:feed/timeline_stream/",
)

ASSESSMENT_BODY = b'{"schema_version":1,"grouping":"LX/03Ez","candidates":4}'


class Forbidden:
    """Any attribute access is a purity violation."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{self._label}.{name} accessed during pure derivation")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{self._label} called during pure derivation")


def cas_ref(
    kind: str, body: bytes, producer: str, input_hashes: tuple[str, ...] = ()
) -> ArtifactRef:
    """A content-addressed reference to `body`, with the uri `ArtifactRef` demands."""
    digest = hashlib.sha256(body).hexdigest()
    return ArtifactRef(1, kind, digest, len(body), f"cas://sha256/{digest}", producer, input_hashes)


def assessment_ref(body: bytes = ASSESSMENT_BODY) -> ArtifactRef:
    return cas_ref(ASSESSMENT_ARTIFACT_KIND, body, "stage4-assess-439")


def make_request(
    *,
    run_id: str = RUN_ID,
    assessment: ArtifactRef | None = None,
    policy_revision: str = POLICY,
    allowed_actor: str = ACTOR,
    candidate_ids: tuple[str, ...] = CANDIDATES,
) -> FeatureGateRequestV1:
    return derive_feature_gate_request(
        run_id,
        assessment_ref() if assessment is None else assessment,
        policy_revision,
        allowed_actor,
        candidate_ids,
    )


def ruling(
    candidate_id: str, verdict: str = "offer_toggle", rationale: str | None = None
) -> FeatureDispositionV1:
    if rationale is None:
        rationale = "" if verdict == SILENT_VERDICT else f"{verdict}: injected into the timeline"
    return FeatureDispositionV1(1, candidate_id, verdict, rationale)  # type: ignore[arg-type]


def dispositions_document(
    request: FeatureGateRequestV1,
    rulings: tuple[FeatureDispositionV1, ...] | None = None,
    *,
    assessment_sha256: str | None = None,
    policy_revision: str | None = None,
) -> FeatureDispositionsV1:
    """A document that rules on every candidate unless the caller says otherwise."""
    if rulings is None:
        rulings = tuple(ruling(candidate_id) for candidate_id in request.candidate_ids)
    return FeatureDispositionsV1(
        1,
        request.assessment.sha256 if assessment_sha256 is None else assessment_sha256,
        request.policy_revision if policy_revision is None else policy_revision,
        tuple(rulings),
    )


def dispositions_ref(document: FeatureDispositionsV1) -> ArtifactRef:
    """The CAS reference for exactly this document's canonical bytes."""
    return cas_ref(
        DISPOSITIONS_ARTIFACT_KIND,
        canonical_json(document).encode("utf-8"),
        "stage4-dispositions-439",
        (document.assessment_sha256,),
    )


def decision_for(request: FeatureGateRequestV1, **overrides: Any) -> GateDecision:
    """An approval bound to `request`, in the shape the Workflow validator builds.

    `subject`, `admission` and `prepared` all carry the request hash, which is
    what `replay_workflow` already does for its hash-pinned gate: there is one
    derived subject, so there is one hash to bind.
    """
    fields: dict[str, Any] = {
        "schema_version": 1,
        "decision_id": f"{request.run_id}-feature-decision",
        "idempotency_id": f"{request.run_id}-feature-decision-attempt",
        "actor": ACTOR,
        "run_id": request.run_id,
        "gate_id": request.gate_id,
        "subject_sha256": request.sha256,
        "admission_sha256": request.sha256,
        "prepared_sha256": request.sha256,
        "policy_revision": request.policy_revision,
        "decision": "approve",
        "rationale": "Ruled on every uncovered consumption endpoint",
        "issued_at": "2026-08-01T12:00:00+00:00",
    }
    fields.update(overrides)
    return GateDecision(**fields)


def submission_for(
    request: FeatureGateRequestV1,
    document: FeatureDispositionsV1,
    *,
    decision: GateDecision | None = None,
) -> FeatureGateSubmissionV1:
    """A submission whose artifact reference is rebuilt from `document`.

    Rebuilding rather than reusing matters: a test that mutates the document
    must otherwise trip the reference-binding clause before reaching the clause
    it is about.
    """
    return FeatureGateSubmissionV1(
        1, decision_for(request) if decision is None else decision, dispositions_ref(document)
    )


class GateTestCase(unittest.TestCase):
    """One derived request, one complete document, one submission binding both."""

    def setUp(self) -> None:
        self.request = make_request()
        self.document = dispositions_document(self.request)
        self.submission = submission_for(self.request, self.document)

    def admit(
        self,
        document: FeatureDispositionsV1 | None = None,
        *,
        request: FeatureGateRequestV1 | None = None,
        decision: GateDecision | None = None,
    ) -> None:
        request = self.request if request is None else request
        document = self.document if document is None else document
        validate_submission(
            request, submission_for(request, document, decision=decision), document
        )


# ------------------------------------------------------------------ derivation


class DeriveFeatureGateRequestTests(GateTestCase):
    def test_derivation_is_deterministic_and_byte_identical(self) -> None:
        """Two Activities derive this separately; disagreement fails the run closed."""
        first = make_request()
        second = make_request()
        self.assertIsNot(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first, second)

    def test_derivation_touches_no_ledger_store_or_clock(self) -> None:
        """Purity is structural, not a promise: the module imports nothing that could.

        `re`, `dataclasses` and `hashlib` are the whole import surface. A ledger
        handle, a content store or a clock in that set would make two
        derivations of the same subject disagree for reasons the human never
        touched.
        """
        from dfinsta_pipeline import activities, ledger, store

        modules = {
            name for name, value in vars(feature_gate).items() if isinstance(value, types.ModuleType)
        }
        self.assertEqual(modules, {"dataclasses", "hashlib", "re"})

        expected = canonical_json(make_request())
        with mock.patch.object(ledger, "Ledger", Forbidden("Ledger")), mock.patch.object(
            store, "ContentStore", Forbidden("ContentStore")
        ), mock.patch.object(activities, "runtime", Forbidden("runtime")):
            observed = canonical_json(make_request())
            derive_assessment_gate(make_request())
        self.assertEqual(observed, expected)

    def test_module_depends_only_on_the_shared_contracts(self) -> None:
        """Every in-package import is a pure contract module, transitively.

        `.gate_contract` joined on 2026-08-08, when the six clauses every gate's
        authority shares were extracted so a fix reaches all of them instead of
        one. It is allowed here **only because it is pure itself** — which this
        test now checks rather than assumes, because a shared module that grew a
        filesystem read would smuggle state into three gates at once instead of
        one.
        """
        source = Path(feature_gate.__file__).read_text(encoding="utf-8")
        relative = [line for line in source.splitlines() if line.startswith("from .")]
        self.assertEqual(
            relative,
            [
                "from .gate_contract import bind_decision, bind_document",
                "from .contracts import (",
            ],
        )

        from dfinsta_pipeline import gate_contract

        shared = Path(gate_contract.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in shared.splitlines() if line.startswith("from .")],
            ["from .contracts import ArtifactRef, GateDecision, canonical_json"],
        )
        for forbidden in ("open(", "Path(", "datetime", "os.", "random"):
            self.assertNotIn(forbidden, shared, f"gate_contract reached for {forbidden}")

    def test_nothing_in_the_module_reads_a_clock_or_a_filesystem(self) -> None:
        """Timestamps arrive from the caller; a clock read breaks Temporal replay."""
        source = Path(feature_gate.__file__).read_text(encoding="utf-8")
        for token in (
            "import os",
            "import time",
            "import random",
            "import datetime",
            "workflow.now",
            "open(",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_changing_any_single_input_changes_the_request_hash(self) -> None:
        """Every argument is load-bearing; none is decoration the hash ignores."""
        baseline = self.request.sha256
        variants = {
            "run_id": make_request(run_id="port-440-stage4"),
            "assessment_bytes": make_request(assessment=assessment_ref(b'{"other":true}')),
            "assessment_metadata": derive_feature_gate_request(
                RUN_ID, replace(assessment_ref(), size=999), POLICY, ACTOR, CANDIDATES
            ),
            "allowed_actor": make_request(allowed_actor="someone-else"),
            "policy_revision": make_request(policy_revision="policy-2026-09"),
            "candidate_membership": make_request(candidate_ids=CANDIDATES[:3]),
            "candidate_order": make_request(candidate_ids=tuple(reversed(CANDIDATES))),
        }
        for label, variant in variants.items():
            with self.subTest(changed=label):
                self.assertNotEqual(variant.sha256, baseline)

    def test_candidate_order_is_preserved_rather_than_sorted(self) -> None:
        """The assessment's own order is what a human was shown; re-sorting loses it."""
        unsorted = tuple(reversed(CANDIDATES))
        self.assertNotEqual(list(unsorted), sorted(unsorted))
        self.assertEqual(make_request(candidate_ids=unsorted).candidate_ids, unsorted)
        self.assertNotEqual(make_request(candidate_ids=unsorted).sha256, self.request.sha256)

    def test_derived_gate_id_is_run_scoped(self) -> None:
        self.assertEqual(self.request.gate_id, f"{RUN_ID}{GATE_ID_SUFFIX}")
        self.assertEqual(derived_gate_id(RUN_ID), self.request.gate_id)

    def test_derived_gate_id_fails_loudly_rather_than_truncating(self) -> None:
        """A truncated gate id could collide with another run's gate.

        Two runs whose ids share a prefix would then raise gates with the same
        name, and a decision meant for one could be admitted against the other.
        """
        with self.assertRaises(ValueError):
            derived_gate_id("r" * 128)
        with self.assertRaises(ValueError):
            derived_gate_id("-leading-dash")
        with self.assertRaises(TypeError):
            derived_gate_id(None)

    def test_arguments_must_be_exact_types(self) -> None:
        reference = assessment_ref()
        for arguments in (
            (None, reference, POLICY, CANDIDATES),
            (RUN_ID, asdict(reference), POLICY, CANDIDATES),
            (RUN_ID, reference, None, CANDIDATES),
            (RUN_ID, reference, POLICY, list(CANDIDATES)),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                derive_feature_gate_request(*arguments)

    def test_request_binds_the_assessment_document_and_nothing_else(self) -> None:
        self.assertEqual(self.request.run_id, RUN_ID)
        self.assertEqual(self.request.policy_revision, POLICY)
        self.assertEqual(self.request.candidate_ids, CANDIDATES)
        self.assertEqual(self.request.assessment.kind, ASSESSMENT_ARTIFACT_KIND)
        self.assertEqual(
            self.request.assessment.uri, f"cas://sha256/{self.request.assessment.sha256}"
        )

    def test_assessment_must_be_an_assessment(self) -> None:
        """A dispositions reference presented as the assessment is a swapped subject."""
        with self.assertRaises(ValueError):
            make_request(assessment=dispositions_ref(self.document))

    def test_request_refuses_an_empty_candidate_list(self) -> None:
        """A gate covering nothing is a human approving nothing.

        Completeness would hold vacuously, so the run would proceed on a
        decision that ruled on no candidate at all. Stage 4 finding no grouping
        is a report, not a gate.
        """
        with self.assertRaises(ValueError):
            make_request(candidate_ids=())

    def test_request_refuses_duplicate_candidate_ids(self) -> None:
        """A repeated candidate makes "every candidate was ruled on" ambiguous."""
        with self.assertRaises(ValueError):
            make_request(candidate_ids=CANDIDATES + CANDIDATES[:1])

    def test_candidate_ids_hold_the_ids_stage_4a_actually_mints(self) -> None:
        """The gate is unusable if it refuses its own producer's ids.

        `assessment.assess_gap` names candidates `gap:{literal}`, and the literal
        keeps the trailing slash the index holds. A pattern borrowed from
        `contracts.ID_PATTERN` would reject every one of them, and the failure
        would surface only once a real assessment reached a real gate.
        """
        for accepted in (
            *CANDIDATES,
            "gap:clips/homecoming/",
            "feed/timeline",
            "com/instagram/clips",
            "hook.reels",
        ):
            with self.subTest(accepted=accepted):
                self.assertEqual(make_request(candidate_ids=(accepted,)).candidate_ids, (accepted,))
        for rejected in ("/feed/timeline", "feed//timeline", "gap:", "a:b:c", "", "feed timeline"):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                make_request(candidate_ids=(rejected,))
        with self.assertRaises(TypeError):
            make_request(candidate_ids=(None,))

    def test_a_candidate_id_is_never_normalised(self) -> None:
        """Two spellings are two candidates; folding them could only hide one."""
        both = ("gap:feed/reels_media/", "gap:feed/reels_media")
        self.assertEqual(make_request(candidate_ids=both).candidate_ids, both)

    def test_request_round_trips_and_decodes_strictly(self) -> None:
        data = json.loads(json.dumps(self.request.to_dict()))
        self.assertEqual(FeatureGateRequestV1.from_dict(data), self.request)
        self.assertEqual(canonical_json(self.request), canonical_json(self.request.to_dict()))
        self.assertEqual(self.request.sha256, canonical_sha256(self.request))
        with self.assertRaises(ValueError):
            FeatureGateRequestV1.from_dict({**data, "reviewer": "sam"})
        with self.assertRaises(ValueError):
            FeatureGateRequestV1.from_dict(
                {k: v for k, v in data.items() if k != "candidate_ids"}
            )

    def test_a_candidate_list_that_is_a_string_is_not_a_candidate_list(self) -> None:
        """A JSON string would otherwise decode as one candidate per character.

        `tuple("feedX")` is five ids, every one of which matches the pattern and
        none of which any human ruled on. The gate would then block on five
        candidates that do not exist, for a reason nobody reading the error
        could act on — and a corrupted candidate list is the one input that must
        never be silently reinterpreted.
        """
        data = self.request.to_dict()
        for value in ("feedX", CANDIDATES[0]):
            with self.subTest(value=value), self.assertRaises(TypeError):
                FeatureGateRequestV1.from_dict({**data, "candidate_ids": value})


# -------------------------------------------------------------------- envelope


class FeatureAssessmentGateTests(GateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.gate = derive_assessment_gate(self.request)

    def test_envelope_carries_no_assessment_body(self) -> None:
        """The whole point: the body never enters Temporal History.

        An assessment covering ~100 candidates with their evidence attached
        would sit in History forever and be replayed on every worker restart.
        """
        self.assertEqual(
            set(self.gate.to_dict()),
            {
                "schema_version",
                "run_id",
                "gate_id",
                "request_sha256",
                "allowed_actor",
                "policy_revision",
            },
        )
        serialised = canonical_json(self.gate)
        for candidate_id in CANDIDATES:
            self.assertNotIn(candidate_id, serialised)
        self.assertNotIn(self.request.assessment.sha256, serialised)

    def test_envelope_is_derived_from_the_request(self) -> None:
        self.assertEqual(self.gate.run_id, self.request.run_id)
        self.assertEqual(self.gate.gate_id, self.request.gate_id)
        self.assertEqual(self.gate.policy_revision, self.request.policy_revision)
        self.assertEqual(self.gate.request_sha256, self.request.sha256)
        self.assertEqual(self.gate.allowed_actor, ACTOR)

    def test_envelope_derivation_requires_an_exact_request(self) -> None:
        for arguments in ((self.request.to_dict(), ACTOR), (self.request, None)):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                derive_assessment_gate(*arguments)

    def test_envelope_round_trips_and_decodes_strictly(self) -> None:
        data = json.loads(json.dumps(self.gate.to_dict()))
        self.assertEqual(FeatureAssessmentGateV1.from_dict(data), self.gate)
        self.assertEqual(canonical_json(self.gate), canonical_json(self.gate.to_dict()))
        self.assertEqual(self.gate.sha256, canonical_sha256(self.gate))
        with self.assertRaises(ValueError):
            FeatureAssessmentGateV1.from_dict({**data, "unexpected": 1})
        with self.assertRaises(ValueError):
            FeatureAssessmentGateV1.from_dict({k: v for k, v in data.items() if k != "gate_id"})

    def test_envelope_validates_its_own_fields(self) -> None:
        data = self.gate.to_dict()
        for field, value in (
            ("schema_version", 2),
            ("run_id", "-nope"),
            ("gate_id", ""),
            ("request_sha256", "not-a-digest"),
            ("allowed_actor", "sam@dfinsta"),
            ("policy_revision", "policy 2026"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                FeatureAssessmentGateV1.from_dict({**data, field: value})


# ----------------------------------------------------------- response document


class FeatureDispositionsTests(GateTestCase):
    def test_every_verdict_the_design_names_is_accepted(self) -> None:
        self.assertEqual(VERDICTS, ("block", "offer_toggle", "ignore", "defer"))
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertEqual(ruling(CANDIDATES[0], verdict).verdict, verdict)

    def test_an_invented_verdict_is_refused(self) -> None:
        """`approve` is not a disposition; the wrapper exists because it is not enough."""
        with self.assertRaises(ValueError):
            FeatureDispositionV1(1, CANDIDATES[0], "approve", "why")  # type: ignore[arg-type]

    def test_rationale_is_bounded_like_the_decision_rationale(self) -> None:
        self.assertEqual(MAX_RATIONALE, 2048)
        FeatureDispositionV1(1, CANDIDATES[0], "block", "x" * MAX_RATIONALE)
        with self.assertRaises(ValueError):
            FeatureDispositionV1(1, CANDIDATES[0], "block", "x" * (MAX_RATIONALE + 1))

    def test_document_round_trips_and_decodes_strictly(self) -> None:
        data = json.loads(json.dumps(self.document.to_dict()))
        self.assertEqual(FeatureDispositionsV1.from_dict(data), self.document)
        self.assertEqual(canonical_json(self.document), canonical_json(self.document.to_dict()))
        self.assertEqual(self.document.sha256, canonical_sha256(self.document))
        with self.assertRaises(ValueError):
            FeatureDispositionsV1.from_dict({**data, "signed_by": "sam"})
        with self.assertRaises(ValueError):
            FeatureDispositionsV1.from_dict(
                {k: v for k, v in data.items() if k != "assessment_sha256"}
            )

    def test_nested_dispositions_decode_strictly_too(self) -> None:
        data = self.document.to_dict()
        broken = dict(data)
        broken["dispositions"] = [{**data["dispositions"][0], "confidence": 0.9}]
        with self.assertRaises(ValueError):
            FeatureDispositionsV1.from_dict(broken)

    def test_document_requires_exact_disposition_objects(self) -> None:
        with self.assertRaises(TypeError):
            FeatureDispositionsV1(1, self.request.assessment.sha256, POLICY, ({"a": 1},))
        with self.assertRaises(TypeError):
            FeatureDispositionsV1(
                1, self.request.assessment.sha256, POLICY, [ruling(CANDIDATES[0])]
            )

    def test_document_alone_does_not_judge_completeness(self) -> None:
        """Incoherent documents are constructible on purpose.

        The bytes are fetched from CAS before anything has been checked against
        a request, so the document type cannot know which candidates were
        covered. Keeping the judgement in `validate_submission` keeps the gate
        to a single authority check instead of two that could disagree.
        """
        repeated = dispositions_document(
            self.request, (ruling(CANDIDATES[0]), ruling(CANDIDATES[0]))
        )
        self.assertEqual(len(repeated.dispositions), 2)
        with self.assertRaises(ValueError):
            self.admit(repeated)


# ------------------------------------------------------------------ submission


class FeatureGateSubmissionTests(GateTestCase):
    def test_submission_wraps_rather_than_extends_the_decision(self) -> None:
        """A new schema, not new fields: `GateDecision` is unchanged and unaware."""
        self.assertEqual(
            set(self.submission.to_dict()), {"schema_version", "decision", "dispositions"}
        )
        self.assertNotIn("dispositions", set(asdict(self.submission.decision)))

    def test_submission_requires_exact_members(self) -> None:
        for arguments in (
            (1, asdict(self.submission.decision), self.submission.dispositions),
            (1, self.submission.decision, asdict(self.submission.dispositions)),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                FeatureGateSubmissionV1(*arguments)

    def test_submission_refuses_an_artifact_of_the_wrong_kind(self) -> None:
        """The assessment is not the answer to itself."""
        with self.assertRaises(ValueError):
            FeatureGateSubmissionV1(1, self.submission.decision, self.request.assessment)

    def test_submission_round_trips_and_decodes_strictly(self) -> None:
        data = json.loads(json.dumps(self.submission.to_dict()))
        self.assertEqual(FeatureGateSubmissionV1.from_dict(data), self.submission)
        self.assertEqual(canonical_json(self.submission), canonical_json(self.submission.to_dict()))
        self.assertEqual(self.submission.sha256, canonical_sha256(self.submission))
        with self.assertRaises(ValueError):
            FeatureGateSubmissionV1.from_dict({**data, "attempts": 1})
        with self.assertRaises(ValueError):
            FeatureGateSubmissionV1.from_dict({k: v for k, v in data.items() if k != "decision"})


# ------------------------------------------------------------ authority checks


class ValidateSubmissionTests(GateTestCase):
    def test_a_complete_well_bound_submission_is_admitted(self) -> None:
        self.assertIsNone(validate_submission(self.request, self.submission, self.document))

    def test_arguments_must_be_exact_types(self) -> None:
        for arguments in (
            (self.request.to_dict(), self.submission, self.document),
            (self.request, self.submission.to_dict(), self.document),
            (self.request, self.submission, self.document.to_dict()),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                validate_submission(*arguments)

    def test_dispositions_artifact_must_hold_the_submitted_document(self) -> None:
        """The decoded document and the reference the human signed must be one thing."""
        other = dispositions_document(
            self.request, tuple(ruling(name, "block") for name in CANDIDATES)
        )
        mismatched = FeatureGateSubmissionV1(
            1, decision_for(self.request), dispositions_ref(other)
        )
        with self.assertRaises(ValueError):
            validate_submission(self.request, mismatched, self.document)

    def test_a_same_size_substitution_is_still_refused(self) -> None:
        """Length is a weak witness; the digest is the clause that binds.

        `block` and `defer` are both five characters, so a document that defers
        every candidate serialises to exactly as many bytes as one that blocks
        them all. A size comparison sees two identical documents; the meaning is
        opposite.
        """
        blocked = dispositions_document(
            self.request, tuple(ruling(name, "block", "blocked") for name in CANDIDATES)
        )
        deferred = dispositions_document(
            self.request, tuple(ruling(name, "defer", "blocked") for name in CANDIDATES)
        )
        self.assertEqual(dispositions_ref(blocked).size, dispositions_ref(deferred).size)
        self.assertNotEqual(canonical_json(blocked), canonical_json(deferred))

        signed = FeatureGateSubmissionV1(
            1, decision_for(self.request), dispositions_ref(blocked)
        )
        with self.assertRaises(ValueError):
            validate_submission(self.request, signed, deferred)
        validate_submission(self.request, signed, blocked)

    def test_dispositions_artifact_size_must_match_the_document(self) -> None:
        """A size that disagrees with the bytes means the reference was hand-built."""
        reference = dispositions_ref(self.document)
        resized = FeatureGateSubmissionV1(
            1, decision_for(self.request), replace(reference, size=reference.size + 1)
        )
        with self.assertRaises(ValueError):
            validate_submission(self.request, resized, self.document)

    def test_decision_subject_must_be_the_derived_request_hash(self) -> None:
        other = make_request(candidate_ids=CANDIDATES[:2])
        self.assertNotEqual(other.sha256, self.request.sha256)
        with self.assertRaises(ValueError):
            self.admit(decision=decision_for(self.request, subject_sha256=other.sha256))

    def test_decision_run_must_match_the_request(self) -> None:
        with self.assertRaises(ValueError):
            self.admit(decision=decision_for(self.request, run_id="port-440-stage4"))

    def test_decision_gate_must_match_the_request(self) -> None:
        with self.assertRaises(ValueError):
            self.admit(decision=decision_for(self.request, gate_id=derived_gate_id("port-440")))

    def test_decision_policy_must_match_the_request(self) -> None:
        """A decision taken under a superseded policy does not authorise this run."""
        with self.assertRaises(ValueError):
            self.admit(decision=decision_for(self.request, policy_revision="policy-2026-09"))

    def test_dispositions_must_bind_the_assessed_document(self) -> None:
        """Verdicts belong to the assessment they were formed against, or to nothing."""
        stale = dispositions_document(self.request, assessment_sha256="a" * 64)
        with self.assertRaises(ValueError):
            self.admit(stale)

    def test_dispositions_policy_must_match_the_request(self) -> None:
        divergent = dispositions_document(self.request, policy_revision="policy-2026-09")
        with self.assertRaises(ValueError):
            self.admit(divergent)

    def test_duplicate_candidate_dispositions_are_refused(self) -> None:
        """Two rulings on one candidate is two answers; the gate takes one."""
        rulings = tuple(ruling(name) for name in CANDIDATES) + (ruling(CANDIDATES[0], "ignore"),)
        with self.assertRaises(ValueError):
            self.admit(dispositions_document(self.request, rulings))

    def test_a_candidate_with_no_disposition_blocks(self) -> None:
        rulings = tuple(ruling(name) for name in CANDIDATES[:-1])
        with self.assertRaises(ValueError) as raised:
            self.admit(dispositions_document(self.request, rulings))
        self.assertIn(CANDIDATES[-1], str(raised.exception))

    def test_a_disposition_naming_an_unknown_candidate_blocks(self) -> None:
        rulings = tuple(ruling(name) for name in CANDIDATES) + (ruling("gap:clips/homecoming/"),)
        with self.assertRaises(ValueError) as raised:
            self.admit(dispositions_document(self.request, rulings))
        self.assertIn("gap:clips/homecoming/", str(raised.exception))

    def test_a_blank_rationale_is_refused_for_every_verdict_but_ignore(self) -> None:
        """Removing a candidate from consideration costs a sentence."""
        for verdict in VERDICTS:
            if verdict == SILENT_VERDICT:
                continue
            rulings = tuple(
                ruling(name, verdict, "  ")
                if name == CANDIDATES[0]
                else ruling(name, "block", "blocked")
                for name in CANDIDATES
            )
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                self.admit(dispositions_document(self.request, rulings))

    def test_ignore_may_be_silent_because_it_is_the_no_op(self) -> None:
        rulings = tuple(ruling(name, SILENT_VERDICT, "") for name in CANDIDATES)
        self.admit(dispositions_document(self.request, rulings))

    def test_a_rejection_still_owes_a_ruling_on_every_candidate(self) -> None:
        """The whole-gate verb never excuses the per-candidate record.

        The wrapper exists because one verb cannot express a hundred rulings, so
        one verb cannot dismiss them either. A human who wants to punt says so
        candidate by candidate with `defer`, which is a ruling and satisfies
        completeness; scrolling past is not.
        """
        rejection = decision_for(self.request, decision="reject", rationale="Not this release")
        with self.assertRaises(ValueError):
            validate_submission(
                self.request,
                submission_for(
                    self.request,
                    dispositions_document(self.request, (ruling(CANDIDATES[0], "block"),)),
                    decision=rejection,
                ),
                dispositions_document(self.request, (ruling(CANDIDATES[0], "block"),)),
            )
        deferred = dispositions_document(
            self.request, tuple(ruling(name, "defer", "revisit next version") for name in CANDIDATES)
        )
        self.admit(deferred, decision=rejection)


# --------------------------------------------------------------------- mutants


class MutationTests(GateTestCase):
    """Each guard, re-attacked from the direction a broken implementation takes.

    A positive test proves the guard exists. These prove it bites: every one
    constructs the input a specific plausible mutation would wave through, and
    asserts the outcome that mutation could not produce.
    """

    def test_a_candidate_nobody_ruled_on_cannot_default_to_ignore(self) -> None:
        """Mutation: drop the completeness check in `validate_submission`.

        In production `feed/reels_media_stream` reaches the human at the bottom
        of a hundred-row list, they approve the ones they read, and the
        unreviewed rows are treated as `ignore` — the most permissive verdict
        available. Injecting Reels into the timeline then ships unblocked, and
        the run records that a human approved it. Nobody did; they scrolled
        past. Absence is never a pass.
        """
        reviewed = tuple(ruling(name) for name in CANDIDATES[:-1])
        with self.assertRaises(ValueError):
            self.admit(dispositions_document(self.request, reviewed))

        # The mutant's own arithmetic, spelled out: the *only* difference
        # between blocked and admitted is a row saying `ignore`, and under the
        # mutation that row is what the missing candidate silently becomes.
        defaulted = reviewed + (ruling(CANDIDATES[-1], SILENT_VERDICT, ""),)
        self.admit(dispositions_document(self.request, defaulted))

        # And the candidate list the check reads is the approved subject's, not
        # a list the submitter supplies, so it cannot be trimmed to fit.
        self.assertEqual(self.request.candidate_ids, CANDIDATES)
        self.assertNotIn("candidate_ids", set(self.document.to_dict()))

    def test_verdicts_cannot_be_applied_to_an_assessment_the_human_never_saw(self) -> None:
        """Mutation: stop comparing `dispositions.assessment_sha256`.

        In production the assessment is regenerated — a new decode, one more
        candidate found, a corrected delivery branch — while a human is midway
        through reviewing the old one. Their `ignore` on a surface that used to
        look like a task endpoint is then applied to whatever now sits at that
        id. The human ruled on one document and authorised another, which is
        exactly the stale-approval failure the request-side hash pinning
        prevents in the other direction.
        """
        revised = assessment_ref(b'{"schema_version":1,"grouping":"LX/03Ez","candidates":5}')
        revised_request = make_request(assessment=revised)
        self.assertNotEqual(revised.sha256, self.request.assessment.sha256)

        stale = dispositions_document(
            revised_request, assessment_sha256=self.request.assessment.sha256
        )
        with self.assertRaises(ValueError):
            self.admit(stale, request=revised_request)

        # Everything else about the submission is already correct: the one field
        # between these verdicts and the wrong assessment is `assessment_sha256`.
        self.admit(dispositions_document(revised_request), request=revised_request)

    def test_the_subject_hash_cannot_be_supplied_by_the_caller(self) -> None:
        """Mutation: take `request_sha256` as an argument instead of deriving it.

        In production the Activity that admits the submission is handed both the
        decision and "the hash it was for". A caller — a retried Activity
        holding a stale subject, a submission client with its own idea of the
        request, a bug that reuses the previous run's hash — then asserts what
        was approved, and the gate agrees with whatever it is told. The check
        becomes a comparison of a value with itself.
        """
        self.assertNotIn(
            "request_sha256", inspect.signature(validate_submission).parameters
        )
        self.assertEqual(
            tuple(inspect.signature(validate_submission).parameters),
            ("request", "submission", "dispositions"),
        )

        # A well-formed hash of a *different* derived request is still refused,
        # so the value has to come from the subject in hand.
        other = make_request(candidate_ids=CANDIDATES[:2])
        forged = decision_for(self.request, subject_sha256=other.sha256)
        with self.assertRaises(ValueError):
            self.admit(decision=forged)
        self.assertEqual(self.request.sha256, canonical_sha256(self.request))

    def test_a_submitted_document_cannot_differ_from_the_reference_it_names(self) -> None:
        """Mutation: trust the decoded dispositions instead of hashing them.

        In production the admitting Activity resolves the CAS object the human
        signed, then validates a document it was handed alongside it. A caller
        that resolves one and passes another gets every clause checked against
        rulings nobody submitted — the decision binds a hash that describes
        different bytes entirely.

        The substitution here is same-size on purpose. `block` and `defer` are
        both five characters, so every other guard the submission has — the
        size, the completeness, the rationales — reads the two documents as
        identical. Only the digest distinguishes "blocked" from "deferred".
        """
        signed = dispositions_document(
            self.request, tuple(ruling(name, "block", "blocked") for name in CANDIDATES)
        )
        substituted = dispositions_document(
            self.request, tuple(ruling(name, "defer", "blocked") for name in CANDIDATES)
        )
        reference = dispositions_ref(signed)
        self.assertEqual(reference.size, dispositions_ref(substituted).size)
        self.assertNotEqual(canonical_json(signed), canonical_json(substituted))

        submission = FeatureGateSubmissionV1(1, decision_for(self.request), reference)
        with self.assertRaises(ValueError):
            validate_submission(self.request, submission, substituted)

        # Both documents are individually admissible; the binding is what says
        # which one the human actually signed.
        validate_submission(self.request, submission, signed)


if __name__ == "__main__":
    unittest.main()
