"""Contracts and pure derivation for the stage-4 feature assessment gate.

Stage 3 (`surface_diff`) reports what changed in the API surface. Stage 4 asks a
human what to do about each new candidate surface, and the answer cannot be one
verdict: an assessment covers on the order of a hundred candidates and the human
owes a ruling on every one of them.

Two consequences shape everything in this module.

**Neither document travels through Temporal.** History must stay compact and
free of large bytes and private paths, so the assessment document goes to
content-addressed storage and the Workflow carries only
:class:`FeatureAssessmentGateV1` -- six scalars, one of which is the hash of a
request the Workflow never sees. The request itself is *derived*:
:func:`derive_feature_gate_request` is a pure function of already-recorded
state, so the Activity that raises the gate and the Activity that admits the
submission each compute it independently and refuse if the hashes differ.
Neither trusts the other's copy, and no caller ever gets to assert what was
approved. This mirrors `replay_gate.derive_verification_request`, down to
failing loudly on an over-long derived identifier rather than truncating one:
a truncated gate id could collide with another run's gate.

**The response is a document too.** :class:`~.contracts.GateDecision` offers one
of `{approve, reject, defer}` plus a 2048-character rationale, which cannot hold
a per-candidate verdict. Per the project's rule the sanctioned move is a new
wrapper schema rather than new fields on an existing contract, so the human's
per-candidate rulings go to CAS as :class:`FeatureDispositionsV1` and
:class:`FeatureGateSubmissionV1` binds that document's hash.

:func:`validate_submission` is where the response side earns its keep. Its
central clause is completeness: a candidate nobody ruled on blocks the run
rather than defaulting to `ignore`. That is stage 4's form of the evidence
ledger's central rule that absence is never a pass, and it is the difference
between a human having decided and a human having scrolled past.

Nothing here reads a clock, a ledger, a content store, the environment or a
random source. Every timestamp arrives from the caller inside `GateDecision`,
which is what keeps the gate deterministic under Temporal replay.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from .contracts import (
    ID_PATTERN,
    SHA256_PATTERN,
    ArtifactRef,
    GateDecision,
    _strict_keys,
    canonical_json,
    canonical_sha256,
)

#: Artifact kinds for the two CAS documents this gate binds. Both are checked by
#: the contracts below, so a dispositions reference structurally cannot be
#: presented as the assessment a human was shown, or the other way round.
ASSESSMENT_ARTIFACT_KIND = "feature-assessment-v1"
DISPOSITIONS_ARTIFACT_KIND = "feature-dispositions-v1"

#: Suffix appended to `run_id` to name the gate. It is part of the derived
#: bytes, so changing it changes every request hash.
GATE_ID_SUFFIX = "-feature-assessment-gate"

#: `offer_toggle` is the default shape for anything judged addictive: the
#: product rule is that an addictive feature gets a switch, not a silent
#: removal. `defer` is an explicit "not decided yet" -- it is a ruling, and
#: therefore satisfies completeness, which a missing disposition never does.
VERDICTS = ("block", "offer_toggle", "ignore", "defer")

#: The one verdict a human may leave unexplained, because it is the no-op.
#: Removing a candidate from consideration any other way costs a sentence.
SILENT_VERDICT = "ignore"

#: Matched to `GateDecision.rationale`, so a per-candidate rationale can say no
#: less and no more than a whole-gate one.
MAX_RATIONALE = 2048

MAX_CANDIDATE_ID = 256

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"

#: An optional `namespace:` prefix followed by slash-separated segments.
#:
#: This is the shape stage 4a actually mints: `assessment.assess_gap` names a
#: candidate `gap:{literal}` where the literal is the endpoint as the app writes
#: it, so the ids reaching this gate on Instagram 439 are
#: `gap:feed/timeline_stream/` and its three siblings.
#:
#: `contracts.ID_PATTERN` cannot be reused -- it forbids both `:` and `/` --
#: and assigning synthetic ids instead would need a mapping table the request
#: does not carry, whose collisions are exactly the failure `derived_gate_id`
#: refuses to risk. The trailing slash is significant and is deliberately kept:
#: it is part of the endpoint as indexed, and this module never normalises a
#: candidate id. Both sides of the completeness check come from the same
#: assessment, so exact equality is the right comparison and folding two
#: spellings together could only ever hide a candidate. What is refused is
#: ambiguity that no producer emits: a leading slash, a doubled slash, a second
#: `:`, an empty segment.
CANDIDATE_ID_PATTERN = re.compile(rf"^(?:{_SEGMENT}:)?{_SEGMENT}(?:/{_SEGMENT})*/?$")


def _identifier(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _sha256(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _candidate_id(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if len(value) > MAX_CANDIDATE_ID or not CANDIDATE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _artifact(value: object, kind: str, label: str) -> None:
    if type(value) is not ArtifactRef:
        raise TypeError(f"{label} must be an exact ArtifactRef")
    if value.kind != kind:
        raise ValueError(f"Invalid {label} kind")


def _array(value: object, label: str) -> tuple[Any, ...]:
    if type(value) not in {list, tuple}:
        raise TypeError(f"{label} must be an array")
    return tuple(value)


def _expected_fields(cls: type[object]) -> set[str]:
    return {field.name for field in dataclasses.fields(cls)}


def derived_gate_id(run_id: str) -> str:
    """Name this run's gate after `run_id` without inventing run-scoped state.

    `gate_id` is validated with the same length-bounded identifier pattern as
    `run_id`, so a long run id can push the suffixed name out of range. Failing
    loudly beats truncating: a truncated gate id could collide with a different
    run's gate, and the whole authority model is "this decision is about exactly
    this subject".
    """

    if type(run_id) is not str:
        raise TypeError("Feature gate run id must be a string")
    value = f"{run_id}{GATE_ID_SUFFIX}"
    if not ID_PATTERN.fullmatch(value):
        raise ValueError("Derived feature gate id is not a valid identifier")
    return value


@dataclass(frozen=True, slots=True)
class FeatureAssessmentGateV1:
    """Everything about this gate that is allowed into Temporal History.

    Six scalars and no assessment body. The assessment covers ~100 candidates
    with their evidence attached; putting it here would put large bytes into
    History and make replay carry them forever. The Workflow only needs enough
    to raise a `GateRequest` and authorise an actor, and the one field that
    binds the subject is `request_sha256` -- the hash of a
    :class:`FeatureGateRequestV1` the Workflow never holds.
    """

    schema_version: int
    run_id: str
    gate_id: str
    request_sha256: str
    allowed_actor: str
    policy_revision: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature assessment gate schema")
        _identifier(self.run_id, "feature assessment gate run id")
        _identifier(self.gate_id, "feature assessment gate id")
        _sha256(self.request_sha256, "feature assessment gate request SHA-256")
        _identifier(self.allowed_actor, "feature assessment gate allowed actor")
        _identifier(self.policy_revision, "feature assessment gate policy revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_id": self.gate_id,
            "request_sha256": self.request_sha256,
            "allowed_actor": self.allowed_actor,
            "policy_revision": self.policy_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureAssessmentGateV1:
        _strict_keys(data, _expected_fields(cls), "feature assessment gate")
        return cls(
            data["schema_version"],
            data["run_id"],
            data["gate_id"],
            data["request_sha256"],
            data["allowed_actor"],
            data["policy_revision"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FeatureGateRequestV1:
    """The derived subject: what a human is being asked to rule on.

    It binds the run, the gate, the assessment document in CAS, the policy
    revision in force, the actor permitted to answer, and the ordered candidate
    ids the assessment covers. The
    candidate list is part of the subject rather than a convenience: it is what
    makes "every candidate carries a disposition" a checkable claim about the
    *approved* subject instead of about whatever list a submitter supplies.

    ``allowed_actor`` is inside the derived bytes on purpose. Carried only on the
    envelope, "who may answer" would be checked solely by the Workflow validator
    against History-resident state and never by the hash chain, so the admitting
    Activity could not independently verify it — breaking the symmetry the rest
    of this design rests on. `ReplayVerificationGrantRequestV1` carries its actor
    for the same reason.

    Order is caller-supplied and is part of the derived bytes. Both derivations
    read the same CAS document, so the assessment's own order is the authority;
    re-sorting here would make two orders hash alike and quietly discard the
    order a human was shown.
    """

    schema_version: int
    run_id: str
    gate_id: str
    assessment: ArtifactRef
    policy_revision: str
    allowed_actor: str
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature gate request schema")
        _identifier(self.run_id, "feature gate request run id")
        _identifier(self.gate_id, "feature gate request gate id")
        _identifier(self.allowed_actor, "feature gate request allowed actor")
        _artifact(self.assessment, ASSESSMENT_ARTIFACT_KIND, "feature gate assessment")
        _identifier(self.policy_revision, "feature gate request policy revision")
        if type(self.candidate_ids) is not tuple:
            raise TypeError("Feature gate candidate ids must be a tuple")
        # A gate covering nothing is a human approving nothing: completeness
        # would hold vacuously and the run would proceed on an empty ruling.
        # Stage 4 reporting "no grouping found" is a report, not a gate.
        if not self.candidate_ids:
            raise ValueError("Feature gate request covers no candidates")
        seen: set[str] = set()
        for candidate_id in self.candidate_ids:
            _candidate_id(candidate_id, "feature gate candidate id")
            if candidate_id in seen:
                raise ValueError(f"Duplicate feature gate candidate id: {candidate_id}")
            seen.add(candidate_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_id": self.gate_id,
            "assessment": dataclasses.asdict(self.assessment),
            "policy_revision": self.policy_revision,
            "allowed_actor": self.allowed_actor,
            "candidate_ids": list(self.candidate_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureGateRequestV1:
        _strict_keys(data, _expected_fields(cls), "feature gate request")
        return cls(
            data["schema_version"],
            data["run_id"],
            data["gate_id"],
            ArtifactRef.from_dict(data["assessment"]),
            data["policy_revision"],
            data["allowed_actor"],
            _array(data["candidate_ids"], "feature gate candidate ids"),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FeatureDispositionV1:
    """One human ruling about one candidate.

    The rationale is not validated here. Whether a blank one is acceptable
    depends on the verdict, and that judgement belongs to
    :func:`validate_submission`, which is the single place that decides whether
    a submitted document may authorise anything.
    """

    schema_version: int
    candidate_id: str
    verdict: Literal["block", "offer_toggle", "ignore", "defer"]
    rationale: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature disposition schema")
        _candidate_id(self.candidate_id, "feature disposition candidate id")
        if self.verdict not in VERDICTS:
            raise ValueError("Invalid feature disposition verdict")
        if type(self.rationale) is not str:
            raise TypeError("Feature disposition rationale must be a string")
        if len(self.rationale) > MAX_RATIONALE:
            raise ValueError("Feature disposition rationale is too long")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureDispositionV1:
        _strict_keys(data, _expected_fields(cls), "feature disposition")
        return cls(
            data["schema_version"],
            data["candidate_id"],
            data["verdict"],
            data["rationale"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FeatureDispositionsV1:
    """The human's answer, which lives in CAS rather than in the decision.

    `assessment_sha256` is the load-bearing field: it records which assessment
    the human was actually shown. Without it a human could rule on one
    assessment and have the verdicts applied to another -- the response-side
    form of "a stale approval cannot authorise changed bytes".

    Duplicate and unknown candidate ids are *not* rejected here. This object is
    decoded from bytes that were fetched before anything was checked against a
    request, and the gate has exactly one authority check;
    :func:`validate_submission` is it.
    """

    schema_version: int
    assessment_sha256: str
    policy_revision: str
    dispositions: tuple[FeatureDispositionV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature dispositions schema")
        _sha256(self.assessment_sha256, "feature dispositions assessment SHA-256")
        _identifier(self.policy_revision, "feature dispositions policy revision")
        if type(self.dispositions) is not tuple:
            raise TypeError("Feature dispositions must be a tuple")
        for disposition in self.dispositions:
            if type(disposition) is not FeatureDispositionV1:
                raise TypeError("Feature disposition must be an exact FeatureDispositionV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assessment_sha256": self.assessment_sha256,
            "policy_revision": self.policy_revision,
            "dispositions": [item.to_dict() for item in self.dispositions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureDispositionsV1:
        _strict_keys(data, _expected_fields(cls), "feature dispositions")
        return cls(
            data["schema_version"],
            data["assessment_sha256"],
            data["policy_revision"],
            tuple(
                FeatureDispositionV1.from_dict(item)
                for item in _array(data["dispositions"], "feature dispositions")
            ),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FeatureGateSubmissionV1:
    """What the human actually submits: a decision plus a handle to the rulings.

    A wrapper, not new fields on `GateDecision`. One verdict and a 2048-character
    rationale cannot express a hundred per-candidate rulings, and the project's
    rule is that a contract that no longer fits gets a new schema wrapping it
    rather than fields bolted on.
    """

    schema_version: int
    decision: GateDecision
    dispositions: ArtifactRef

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature gate submission schema")
        if type(self.decision) is not GateDecision:
            raise TypeError("Feature gate decision must be an exact GateDecision")
        _artifact(
            self.dispositions, DISPOSITIONS_ARTIFACT_KIND, "feature gate dispositions"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision": dataclasses.asdict(self.decision),
            "dispositions": dataclasses.asdict(self.dispositions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureGateSubmissionV1:
        _strict_keys(data, _expected_fields(cls), "feature gate submission")
        return cls(
            data["schema_version"],
            GateDecision.from_dict(data["decision"]),
            ArtifactRef.from_dict(data["dispositions"]),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def derive_feature_gate_request(
    run_id: str,
    assessment: ArtifactRef,
    policy_revision: str,
    allowed_actor: str,
    candidate_ids: tuple[str, ...],
) -> FeatureGateRequestV1:
    """Derive the gate subject from already-recorded state alone.

    Pure: no ledger, no content store, no clock, no environment, no filesystem,
    no randomness. Identical arguments must always produce byte-identical
    canonical JSON, because two Activities derive this independently -- one
    publishes only its hash so the Workflow can wait on a human, the other
    re-derives it when the submission arrives -- and neither may trust the
    other's copy. Any impurity here makes the run fail closed for a spurious
    reason, which is as bad as failing open for a real one: it trains whoever is
    on call to re-run until the hashes agree.

    `assessment` must be the exact `ArtifactRef` recorded for the assessment
    document, and `candidate_ids` the candidates that document covers, in its
    own order.
    """

    if type(run_id) is not str:
        raise TypeError("Feature gate run id must be a string")
    if type(assessment) is not ArtifactRef:
        raise TypeError("Feature assessment must be an exact ArtifactRef")
    if type(policy_revision) is not str:
        raise TypeError("Feature gate policy revision must be a string")
    if type(allowed_actor) is not str:
        raise TypeError("Feature gate allowed actor must be a string")
    if type(candidate_ids) is not tuple:
        raise TypeError("Feature gate candidate ids must be a tuple")
    return FeatureGateRequestV1(
        1,
        run_id,
        derived_gate_id(run_id),
        assessment,
        policy_revision,
        allowed_actor,
        candidate_ids,
    )


def derive_assessment_gate(request: FeatureGateRequestV1) -> FeatureAssessmentGateV1:
    """Derive the History-safe envelope for an already-derived request.

    Also pure. It exists so the run, gate, policy AND actor the Workflow enforces
    are the request's by construction rather than by an Activity restating them,
    and so the only way to obtain the envelope is to hold the request it names.

    The actor is taken from the request rather than passed in, because a second
    parameter is a second chance to disagree: an envelope naming one actor while
    the hashed subject names another would let the Workflow admit a submitter the
    re-derivation then rejects, and that failure would look like corruption
    rather than like the mistake it is.
    """

    if type(request) is not FeatureGateRequestV1:
        raise TypeError("Feature gate request must be an exact FeatureGateRequestV1")
    return FeatureAssessmentGateV1(
        1,
        request.run_id,
        request.gate_id,
        request.sha256,
        request.allowed_actor,
        request.policy_revision,
    )


def _require_every_candidate_ruled(
    request: FeatureGateRequestV1, dispositions: FeatureDispositionsV1
) -> None:
    """Refuse unless the human ruled on exactly the candidates the gate covers.

    THIS IS THE MOST IMPORTANT CHECK IN THIS FILE.

    A candidate nobody ruled on must BLOCK the run. It must never default to
    `ignore`. Stage 4's entire claim is that a human decided, and a missing
    disposition is precisely the case where they did not -- they scrolled past.
    Defaulting the gap to `ignore` would turn silence into the most permissive
    verdict available and ship an addictive surface on nobody's authority. This
    is stage 4's form of the evidence ledger's central rule: absence is never a
    pass.

    The converse is refused for the same reason. A disposition naming a
    candidate this request does not cover means the human was working from a
    different candidate list than the one the gate binds, so no ruling in the
    document can be trusted to be about this run.
    """

    covered = set(request.candidate_ids)
    ruled = {item.candidate_id for item in dispositions.dispositions}
    missing = sorted(covered - ruled)
    if missing:
        raise ValueError(f"Candidate carries no disposition: {missing[0]}")
    unknown = sorted(ruled - covered)
    if unknown:
        raise ValueError(f"Disposition names an unknown candidate: {unknown[0]}")


def validate_submission(
    request: FeatureGateRequestV1,
    submission: FeatureGateSubmissionV1,
    dispositions: FeatureDispositionsV1,
) -> None:
    """Decide whether a submission may authorise anything. Raise if it may not.

    Returns `None` when the submission is admissible and raises `TypeError` or
    `ValueError` with a precise reason otherwise. Every clause is a binding
    between things that were recorded separately:

    1. the submitted dispositions artifact holds exactly this document;
    2. **all three** of the decision's hash fields are the *derived* request hash;
    3. the decision's run, gate, policy and **actor** are the request's;
    4. the document rules on the assessment the request pins;
    5. the document's policy revision is the request's;
    6. no candidate is ruled on twice;
    7. every candidate is ruled on and no unknown candidate is;
    8. every non-`ignore` verdict carries a rationale.

    There is deliberately no `request_sha256` parameter. The subject hash is
    recomputed from `request`, which the caller obtained from
    :func:`derive_feature_gate_request`; accepting a hash as an argument would
    let a caller assert what was approved, which is the one thing this gate
    exists to prevent.
    """

    if type(request) is not FeatureGateRequestV1:
        raise TypeError("Feature gate request must be an exact FeatureGateRequestV1")
    if type(submission) is not FeatureGateSubmissionV1:
        raise TypeError("Feature gate submission must be an exact FeatureGateSubmissionV1")
    if type(dispositions) is not FeatureDispositionsV1:
        raise TypeError("Feature dispositions must be an exact FeatureDispositionsV1")

    # The caller fetched and decoded the dispositions document; nothing so far
    # ties it to the reference the human signed. Canonical bytes make that a
    # hash comparison rather than a matter of trust.
    body = canonical_json(dispositions).encode("utf-8")
    artifact = submission.dispositions
    if hashlib.sha256(body).hexdigest() != artifact.sha256:
        raise ValueError("Submitted dispositions artifact does not hold this document")
    if len(body) != artifact.size:
        raise ValueError("Submitted dispositions artifact size does not match this document")

    decision = submission.decision
    # ALL THREE hash fields, not just the subject. The Workflow binds all three
    # to the request hash and its validator checks all three -- but that
    # validator runs in a sandbox and is a filter, not the authority. Checking
    # only the subject here meant a wrongly-bound decision that reached this
    # function by any other route was admitted, and the replay gate's admitting
    # side has always required all three.
    if decision.subject_sha256 != request.sha256:
        raise ValueError("Decision subject does not bind the derived gate request")
    if decision.admission_sha256 != request.sha256:
        raise ValueError("Decision admission hash does not bind the derived gate request")
    if decision.prepared_sha256 != request.sha256:
        raise ValueError("Decision prepared hash does not bind the derived gate request")
    if decision.run_id != request.run_id:
        raise ValueError("Decision run does not bind the gate request")
    if decision.gate_id != request.gate_id:
        raise ValueError("Decision gate does not bind the gate request")
    # `allowed_actor` is inside the derived bytes precisely so that the admitting
    # side can verify it independently -- and nothing here was doing so, which
    # left the sandbox validator as the *only* enforcement of who may answer.
    # `_validate_decision` and the verification grant both compare it; this gate
    # was the exception, against its own request's docstring.
    if decision.actor != request.allowed_actor:
        raise ValueError("Decision actor is not the gate's allowed actor")
    if decision.policy_revision != request.policy_revision:
        raise ValueError("Decision policy does not bind the gate request")

    # A stale approval cannot authorise changed bytes, said from the response
    # side: these verdicts are about the assessment the request pins, or they
    # are about something the human never saw.
    if dispositions.assessment_sha256 != request.assessment.sha256:
        raise ValueError("Dispositions do not bind the assessed document")
    if dispositions.policy_revision != request.policy_revision:
        raise ValueError("Dispositions policy does not bind the gate request")

    ruled: set[str] = set()
    for item in dispositions.dispositions:
        if item.candidate_id in ruled:
            raise ValueError(f"Duplicate disposition for candidate: {item.candidate_id}")
        ruled.add(item.candidate_id)

    _require_every_candidate_ruled(request, dispositions)

    for item in dispositions.dispositions:
        if item.verdict != SILENT_VERDICT and not item.rationale.strip():
            raise ValueError(f"Disposition for {item.candidate_id} has no rationale")


# ------------------------------------------------- the orchestration contracts


@dataclass(frozen=True, slots=True)
class FeatureRunRequestV1:
    """What starts a feature-assessment run: a run id and how long to wait.

    The assessment is *not* here. It was recorded before this Workflow starts and
    the preparing Activity reaches it by run id, so the only thing History
    carries is a name — which is also why the gate is answerable at all: a client
    holding the same run id can reach the same recorded state.
    """

    schema_version: int
    run_id: str
    gate_timeout_seconds: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature run request schema")
        _identifier(self.run_id, "feature run request run id")
        if type(self.gate_timeout_seconds) is not int or self.gate_timeout_seconds <= 0:
            raise ValueError("Feature gate timeout must be a positive number of seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_timeout_seconds": self.gate_timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureRunRequestV1:
        _strict_keys(data, _expected_fields(cls), "feature run request")
        return cls(data["schema_version"], data["run_id"], data["gate_timeout_seconds"])


@dataclass(frozen=True, slots=True)
class FeatureRunResultV1:
    """How a feature-assessment run ended.

    A new result type rather than fields on `RunResult`: `PortRunWorkflow`'s
    Histories already record that shape, and adding to it would change what every
    completed history replays into. The project's rule is that a contract which no
    longer fits gets a new schema beside it.
    """

    schema_version: int
    run_id: str
    state: str
    decision_id: str | None
    dispositions: ArtifactRef | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature run result schema")
        _identifier(self.run_id, "feature run result run id")
        if type(self.state) is not str or not self.state:
            raise ValueError("Feature run result needs a state")
        if self.decision_id is not None:
            _identifier(self.decision_id, "feature run result decision id")
        if self.dispositions is not None:
            _artifact(self.dispositions, DISPOSITIONS_ARTIFACT_KIND, "feature run dispositions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "decision_id": self.decision_id,
            "dispositions": (
                dataclasses.asdict(self.dispositions) if self.dispositions else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureRunResultV1:
        _strict_keys(data, _expected_fields(cls), "feature run result")
        reference = data["dispositions"]
        return cls(
            data["schema_version"],
            data["run_id"],
            data["state"],
            data["decision_id"],
            ArtifactRef.from_dict(reference) if reference is not None else None,
        )


@dataclass(frozen=True, slots=True)
class FeatureDispositionsAdmissionV1:
    """Workflow-to-Activity input that admits a human's rulings.

    Carries the *submission*, not the document: the dispositions body is in CAS
    and the Activity fetches it by the reference the human signed. Passing the
    body through History would put free-text human rationales into a replayable
    log forever, and would let the Workflow — which cannot read CAS — become the
    place that decides what the human said.
    """

    schema_version: int
    run_id: str
    submission: FeatureGateSubmissionV1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported feature dispositions admission schema")
        _identifier(self.run_id, "feature dispositions admission run id")
        if type(self.submission) is not FeatureGateSubmissionV1:
            raise TypeError("Admission submission must be an exact FeatureGateSubmissionV1")
        if self.submission.decision.run_id != self.run_id:
            raise ValueError("Admission submission does not bind the run")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "submission": self.submission.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureDispositionsAdmissionV1:
        _strict_keys(data, _expected_fields(cls), "feature dispositions admission")
        return cls(
            data["schema_version"],
            data["run_id"],
            FeatureGateSubmissionV1.from_dict(data["submission"]),
        )
