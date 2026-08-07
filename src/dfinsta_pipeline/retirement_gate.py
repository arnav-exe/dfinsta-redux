"""Contracts for the hook-retirement gate. Pure: no I/O, no clock, no ledger.

`retirement.py` builds the case for one hook and takes a ruling on it at a
command line. That works and it does not *wait* — the case is a file somebody has
to remember to open. The feature gate is a Temporal Workflow for exactly the
reason this needs to be one: a human decision takes hours or days, and the thing
holding the question open must survive a worker restart, a laptop closing and a
weekend.

This module is the layer `feature_gate.py` is for that gate — the shapes that
cross between a Workflow, an Activity and a client, and the one function that
decides whether an answer may be admitted. It is deliberately separate from
`retirement.py`, which owns what a *case* is and knows nothing about gates.

===============================================================================
  ONE GATE, MANY HOOKS
===============================================================================

The subject is a **docket** — every retirement case open at one version — and not
a single case, for the same reason the feature gate rules on every candidate at
once. A human working through retirement candidates is working through a list,
and a gate per hook would mean N workflows, N waits and N chances for one to be
answered and the rest forgotten. `validate_submission` therefore requires a
ruling for **every** hook in the docket: a missing ruling is refused rather than
read as `keep`, because silence that under-requires is how a bar quietly moves.

===============================================================================
  WHAT CROSSES INTO HISTORY, AND WHAT DOES NOT
===============================================================================

Temporal History is permanent and replayable. So the Workflow carries
`RetirementGateV1` — six scalars — and the *hash* of the subject, while the
docket and the rulings live in the content-addressed store. A human's rationale
for retiring a hook is free text they wrote, which is precisely the kind of thing
that must not be pinned into a replayable log forever, and the docket is unbounded
in the number of hooks it can carry.

The consequence is the same division the feature gate draws, and it is worth
stating rather than inferring: **the Workflow's update validator is a filter and
the admitting Activity is the authority.** The validator runs in the sandbox with
no store and no ledger, so it can check that a submission binds this gate and
arrived in the window; it cannot read the document being approved. Everything the
filter checks, the authority checks again — when this project last split a check
that way the authority checked *less*, and "who may answer" ended up resting
entirely on the sandbox.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .contracts import (
    ID_PATTERN,
    SHA256_PATTERN,
    ArtifactRef,
    GateDecision,
    canonical_json,
    canonical_sha256,
)

__all__ = [
    "DOCKET_ARTIFACT_KIND",
    "RULINGS_ARTIFACT_KIND",
    "GATE_ID_SUFFIX",
    "VERDICTS",
    "MAX_RATIONALE",
    "RetirementGateError",
    "derived_gate_id",
    "HookRetirementGateV1",
    "RetirementGateRequestV1",
    "RetirementRulingV1",
    "RetirementRulingsV1",
    "RetirementGateSubmissionV1",
    "RetirementRunRequestV1",
    "RetirementRunResultV1",
    "RetirementRulingsAdmissionV1",
    "derive_retirement_gate_request",
    "derive_retirement_gate",
    "validate_submission",
]


DOCKET_ARTIFACT_KIND = "hook-retirement-docket-v1"
RULINGS_ARTIFACT_KIND = "hook-retirement-rulings-v1"
GATE_ID_SUFFIX = "-hook-retirement-gate"

#: What a human may answer per hook. Identical to `retirement.VERDICTS` and
#: cross-checked by a test rather than imported: this layer is the wire contract
#: and that one is the local workflow, and a change to either that silently
#: changed the other is exactly the coupling worth refusing.
VERDICTS = ("retire", "keep", "defer")

MAX_RATIONALE = 2048

#: No `SILENT_VERDICT`, unlike the feature gate. There, `ignore` is the common
#: answer and demanding prose for each one would make a hundred-candidate docket
#: unanswerable. Here every verdict changes what the project expects of itself —
#: including `keep`, which is a decision to go on carrying a hook that is not
#: working — so all three require a rationale.


class RetirementGateError(ValueError):
    """Raised when a retirement gate contract is malformed or unadmissible."""


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise RetirementGateError(f"Invalid {label}")


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise RetirementGateError(f"Invalid {label}")


def _artifact(value: object, kind: str, label: str) -> None:
    if type(value) is not ArtifactRef:
        raise RetirementGateError(f"{label} must be an ArtifactRef")
    if value.kind != kind:
        raise RetirementGateError(f"{label} must be of kind {kind}")


def _strict(data: object, expected: set[str], label: str) -> dict[str, Any]:
    """Refuse unknown *and* missing keys, following `contracts._strict_keys`.

    A decoder that tolerates a missing key silently supplies a default, and the
    one field most worth omitting from a retirement document is the one that
    binds it to a subject.
    """

    if not isinstance(data, dict):
        raise RetirementGateError(f"{label} must be an object")
    if set(data) != expected:
        missing = sorted(expected - set(data))
        unknown = sorted(set(data) - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise RetirementGateError(f"{label} has {'; '.join(detail)}")
    return data


def derived_gate_id(run_id: str) -> str:
    """`<run_id>-hook-retirement-gate`, or a refusal.

    Never truncated to fit `ID_PATTERN`. A gate id silently shortened would still
    look plausible and would stop matching the client's `matches` predicate, which
    is the failure mode where a gate is answerable in a test and unanswerable in
    production.
    """

    _identifier(run_id, "run id")
    gate_id = f"{run_id}{GATE_ID_SUFFIX}"
    if not ID_PATTERN.fullmatch(gate_id):
        raise RetirementGateError(
            f"run id {run_id!r} makes a gate id that is not a valid identifier"
        )
    return gate_id


@dataclass(frozen=True, slots=True)
class HookRetirementGateV1:
    """The history-safe envelope. Six scalars and nothing a human wrote."""

    schema_version: int
    run_id: str
    gate_id: str
    request_sha256: str
    allowed_actor: str
    policy_revision: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported retirement gate schema")
        _identifier(self.run_id, "run id")
        _identifier(self.gate_id, "gate id")
        _sha256(self.request_sha256, "request digest")
        _identifier(self.allowed_actor, "allowed actor")
        _identifier(self.policy_revision, "policy revision")
        if self.gate_id != f"{self.run_id}{GATE_ID_SUFFIX}":
            raise RetirementGateError("Gate id does not derive from the run id")

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
    def from_dict(cls, data: dict[str, Any]) -> "HookRetirementGateV1":
        row = _strict(
            data,
            {
                "schema_version",
                "run_id",
                "gate_id",
                "request_sha256",
                "allowed_actor",
                "policy_revision",
            },
            "retirement gate",
        )
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            gate_id=row["gate_id"],
            request_sha256=row["request_sha256"],
            allowed_actor=row["allowed_actor"],
            policy_revision=row["policy_revision"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetirementGateRequestV1:
    """The derived subject. Hashed into the gate; never itself in History.

    `allowed_actor` is inside the hashed bytes deliberately, so the admitting
    Activity can verify who was permitted to answer without trusting anything the
    Workflow carried — the filter is not the only place that requirement lives.
    """

    schema_version: int
    run_id: str
    gate_id: str
    #: The docket in CAS: every open retirement case at this version.
    docket: ArtifactRef
    #: The Instagram version the docket was built from. Carried in the subject so
    #: a human can see, in the bytes they sign, which port's evidence this is
    #: about — and so a docket cannot be answered as though it were another
    #: version's.
    version: str
    policy_revision: str
    allowed_actor: str
    #: Every hook the docket holds a case for, in the docket's own order.
    hook_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported retirement request schema")
        _identifier(self.run_id, "run id")
        _identifier(self.gate_id, "gate id")
        _artifact(self.docket, DOCKET_ARTIFACT_KIND, "docket")
        _identifier(self.version, "version")
        _identifier(self.policy_revision, "policy revision")
        _identifier(self.allowed_actor, "allowed actor")
        if not isinstance(self.hook_ids, tuple) or not self.hook_ids:
            raise RetirementGateError(
                "a retirement gate with no hooks has nothing to ask. Do not raise one"
            )
        for hook in self.hook_ids:
            _identifier(hook, "hook id")
        if len(set(self.hook_ids)) != len(self.hook_ids):
            raise RetirementGateError("Duplicate hook id in the docket")
        if self.gate_id != f"{self.run_id}{GATE_ID_SUFFIX}":
            raise RetirementGateError("Gate id does not derive from the run id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_id": self.gate_id,
            "docket": self.docket.to_dict(),
            "version": self.version,
            "policy_revision": self.policy_revision,
            "allowed_actor": self.allowed_actor,
            "hook_ids": list(self.hook_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementGateRequestV1":
        row = _strict(
            data,
            {
                "schema_version",
                "run_id",
                "gate_id",
                "docket",
                "version",
                "policy_revision",
                "allowed_actor",
                "hook_ids",
            },
            "retirement request",
        )
        hooks = row["hook_ids"]
        if not isinstance(hooks, (list, tuple)):
            raise RetirementGateError("hook_ids must be an array")
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            gate_id=row["gate_id"],
            docket=ArtifactRef.from_dict(row["docket"]),
            version=row["version"],
            policy_revision=row["policy_revision"],
            allowed_actor=row["allowed_actor"],
            hook_ids=tuple(hooks),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetirementRulingV1:
    """One hook's answer, as it travels to the gate."""

    schema_version: int
    hook_id: str
    verdict: Literal["retire", "keep", "defer"]
    rationale: str
    #: The case bytes this answers. `retirement.validate_ruling` binds a ruling to
    #: a single case's hash, and that binding is carried through the gate rather
    #: than re-established at the far end: a docket can hold two cases for
    #: different hooks, and a ruling that named only its verdict could be applied
    #: to whichever one happened to be iterated.
    case_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported ruling schema")
        _identifier(self.hook_id, "hook id")
        if self.verdict not in VERDICTS:
            raise RetirementGateError(f"Unknown verdict {self.verdict!r}")
        if not isinstance(self.rationale, str) or len(self.rationale) > MAX_RATIONALE:
            raise RetirementGateError("Invalid rationale")
        _sha256(self.case_sha256, "case digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hook_id": self.hook_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "case_sha256": self.case_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementRulingV1":
        row = _strict(
            data,
            {"schema_version", "hook_id", "verdict", "rationale", "case_sha256"},
            "ruling",
        )
        return cls(
            schema_version=row["schema_version"],
            hook_id=row["hook_id"],
            verdict=row["verdict"],
            rationale=row["rationale"],
            case_sha256=row["case_sha256"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetirementRulingsV1:
    """The document a human signs: one ruling per hook in the docket."""

    schema_version: int
    docket_sha256: str
    version: str
    policy_revision: str
    rulings: tuple[RetirementRulingV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported rulings schema")
        _sha256(self.docket_sha256, "docket digest")
        _identifier(self.version, "version")
        _identifier(self.policy_revision, "policy revision")
        if not isinstance(self.rulings, tuple) or not self.rulings:
            raise RetirementGateError("A rulings document with no rulings is not an answer")
        for ruling in self.rulings:
            if type(ruling) is not RetirementRulingV1:
                raise RetirementGateError("Every ruling must be a RetirementRulingV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "docket_sha256": self.docket_sha256,
            "version": self.version,
            "policy_revision": self.policy_revision,
            "rulings": [ruling.to_dict() for ruling in self.rulings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementRulingsV1":
        row = _strict(
            data,
            {"schema_version", "docket_sha256", "version", "policy_revision", "rulings"},
            "rulings document",
        )
        items = row["rulings"]
        if not isinstance(items, (list, tuple)):
            raise RetirementGateError("rulings must be an array")
        return cls(
            schema_version=row["schema_version"],
            docket_sha256=row["docket_sha256"],
            version=row["version"],
            policy_revision=row["policy_revision"],
            rulings=tuple(RetirementRulingV1.from_dict(item) for item in items),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetirementGateSubmissionV1:
    """A `GateDecision` plus the rulings it approves, by reference."""

    schema_version: int
    decision: GateDecision
    rulings: ArtifactRef

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported submission schema")
        if type(self.decision) is not GateDecision:
            raise RetirementGateError("Submission must carry a GateDecision")
        _artifact(self.rulings, RULINGS_ARTIFACT_KIND, "rulings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision": {
                "schema_version": self.decision.schema_version,
                "decision_id": self.decision.decision_id,
                "idempotency_id": self.decision.idempotency_id,
                "actor": self.decision.actor,
                "run_id": self.decision.run_id,
                "gate_id": self.decision.gate_id,
                "subject_sha256": self.decision.subject_sha256,
                "admission_sha256": self.decision.admission_sha256,
                "prepared_sha256": self.decision.prepared_sha256,
                "policy_revision": self.decision.policy_revision,
                "decision": self.decision.decision,
                "rationale": self.decision.rationale,
                "issued_at": self.decision.issued_at,
            },
            "rulings": self.rulings.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementGateSubmissionV1":
        row = _strict(data, {"schema_version", "decision", "rulings"}, "submission")
        return cls(
            schema_version=row["schema_version"],
            decision=GateDecision.from_dict(row["decision"]),
            rulings=ArtifactRef.from_dict(row["rulings"]),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RetirementRunRequestV1:
    """Workflow input."""

    schema_version: int
    run_id: str
    gate_timeout_seconds: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported run request schema")
        _identifier(self.run_id, "run id")
        if not isinstance(self.gate_timeout_seconds, int) or self.gate_timeout_seconds <= 0:
            raise RetirementGateError("Gate timeout must be a positive number of seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_timeout_seconds": self.gate_timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementRunRequestV1":
        row = _strict(
            data, {"schema_version", "run_id", "gate_timeout_seconds"}, "run request"
        )
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            gate_timeout_seconds=row["gate_timeout_seconds"],
        )


@dataclass(frozen=True, slots=True)
class RetirementRunResultV1:
    """Workflow output."""

    schema_version: int
    run_id: str
    state: str
    decision_id: str | None
    rulings: ArtifactRef | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported run result schema")
        _identifier(self.run_id, "run id")
        if not isinstance(self.state, str) or not self.state.strip():
            raise RetirementGateError("Run result needs a state")
        if self.decision_id is not None:
            _identifier(self.decision_id, "decision id")
        if self.rulings is not None:
            _artifact(self.rulings, RULINGS_ARTIFACT_KIND, "rulings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "decision_id": self.decision_id,
            "rulings": self.rulings.to_dict() if self.rulings else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementRunResultV1":
        row = _strict(
            data,
            {"schema_version", "run_id", "state", "decision_id", "rulings"},
            "run result",
        )
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            state=row["state"],
            decision_id=row["decision_id"],
            rulings=ArtifactRef.from_dict(row["rulings"]) if row["rulings"] else None,
        )


@dataclass(frozen=True, slots=True)
class RetirementRulingsAdmissionV1:
    """Workflow → Activity."""

    schema_version: int
    run_id: str
    submission: RetirementGateSubmissionV1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RetirementGateError("Unsupported admission schema")
        _identifier(self.run_id, "run id")
        if type(self.submission) is not RetirementGateSubmissionV1:
            raise RetirementGateError("Admission must carry a RetirementGateSubmissionV1")
        if self.submission.decision.run_id != self.run_id:
            raise RetirementGateError("Admission and decision name different runs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "submission": self.submission.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetirementRulingsAdmissionV1":
        row = _strict(data, {"schema_version", "run_id", "submission"}, "admission")
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            submission=RetirementGateSubmissionV1.from_dict(row["submission"]),
        )


def derive_retirement_gate_request(
    run_id: str,
    docket: ArtifactRef,
    version: str,
    policy_revision: str,
    allowed_actor: str,
    hook_ids: tuple[str, ...],
) -> RetirementGateRequestV1:
    """The subject, from recorded state alone. Pure, and the same anywhere."""

    return RetirementGateRequestV1(
        schema_version=1,
        run_id=run_id,
        gate_id=derived_gate_id(run_id),
        docket=docket,
        version=version,
        policy_revision=policy_revision,
        allowed_actor=allowed_actor,
        hook_ids=tuple(hook_ids),
    )


def derive_retirement_gate(request: RetirementGateRequestV1) -> HookRetirementGateV1:
    """The history-safe envelope for a subject.

    Actor and policy are taken **from the request** rather than passed again. A
    second parameter is a second chance to disagree, and the one that reached
    History would be the one the validator enforced.
    """

    if type(request) is not RetirementGateRequestV1:
        raise RetirementGateError("Expected a RetirementGateRequestV1")
    return HookRetirementGateV1(
        schema_version=1,
        run_id=request.run_id,
        gate_id=request.gate_id,
        request_sha256=request.sha256,
        allowed_actor=request.allowed_actor,
        policy_revision=request.policy_revision,
    )


def validate_submission(
    request: RetirementGateRequestV1,
    submission: RetirementGateSubmissionV1,
    rulings: RetirementRulingsV1,
) -> None:
    """The authority. Everything the sandbox validator checks, and more.

    Takes no `request_sha256` argument: the digest is recomputed from the request
    in hand, so a caller cannot supply the number it wants to be compared against.

    The order below is deliberate — document integrity first, then the binding to
    this gate, then who answered, then that every hook was actually ruled on.
    A refusal from an early clause means the later ones were never reached, and
    the message says which.
    """

    if type(request) is not RetirementGateRequestV1:
        raise RetirementGateError("Expected a RetirementGateRequestV1")
    if type(submission) is not RetirementGateSubmissionV1:
        raise RetirementGateError("Expected a RetirementGateSubmissionV1")
    if type(rulings) is not RetirementRulingsV1:
        raise RetirementGateError("Expected a RetirementRulingsV1")

    body = canonical_json(rulings.to_dict()).encode("utf-8")
    if hashlib.sha256(body).hexdigest() != submission.rulings.sha256:
        raise RetirementGateError("Rulings document does not match its reference digest")
    if len(body) != submission.rulings.size:
        raise RetirementGateError("Rulings document does not match its reference size")

    decision = submission.decision
    subject = request.sha256
    # All three, not just `subject_sha256`. The Workflow sets all three to the
    # request hash and its validator checks all three; when this project last
    # left two of them to the filter alone, a decision with the right subject and
    # a wrong admission hash was admitted.
    if (
        decision.subject_sha256 != subject
        or decision.admission_sha256 != subject
        or decision.prepared_sha256 != subject
    ):
        raise RetirementGateError("Decision does not bind this retirement subject")
    if decision.run_id != request.run_id or decision.gate_id != request.gate_id:
        raise RetirementGateError("Decision does not bind this gate")
    if decision.policy_revision != request.policy_revision:
        raise RetirementGateError("Decision was made under a different policy revision")
    # Checked HERE and not only in the sandbox. `allowed_actor` is inside the
    # derived bytes precisely so this layer can verify it without trusting
    # anything carried through History.
    if decision.actor != request.allowed_actor:
        raise RetirementGateError("Decision actor is not authorized for this gate")

    if rulings.docket_sha256 != request.docket.sha256:
        raise RetirementGateError("Rulings answer a different docket")
    if rulings.version != request.version:
        raise RetirementGateError("Rulings name a different Instagram version")
    if rulings.policy_revision != request.policy_revision:
        raise RetirementGateError("Rulings were written under a different policy revision")

    seen = [ruling.hook_id for ruling in rulings.rulings]
    if len(set(seen)) != len(seen):
        raise RetirementGateError("A hook was ruled on twice")
    expected = set(request.hook_ids)
    missing = sorted(expected - set(seen))
    unknown = sorted(set(seen) - expected)
    if missing:
        # Refused, never defaulted. A hook nobody mentioned is a hook nobody
        # looked at, and reading that silence as `keep` would be a decision the
        # rest of this design exists to prevent being made by accident.
        raise RetirementGateError(
            f"No ruling for {', '.join(missing)}. Every hook in the docket needs an "
            "answer; a missing one is not a `keep`"
        )
    if unknown:
        raise RetirementGateError(
            f"Ruling for {', '.join(unknown)}, which is not in this docket"
        )

    for ruling in rulings.rulings:
        if not ruling.rationale.strip():
            raise RetirementGateError(
                f"{ruling.hook_id}: every verdict needs a rationale, including `keep` — "
                "going on carrying a hook that does not work is also a decision"
            )
