"""Contracts for the reversal gate. Pure: no I/O, no clock, no ledger.

`reversal.py` gives a recorded decision a way back and `reconsider.py` says which
recorded decisions no longer match the evidence. Between them there is a hole the
shape of a human: `reconsider` proposes and exits 0, and somebody has to notice
the proposal, open a terminal and type `reversal withdraw-block` with the right
`--original-decision-id`. For a pipeline meant to run with minimal human effort
that is the same "complete and reached by nothing" shape the retirement gate
closed, and it fails in the *dangerous* direction: an unread proposal leaves a
block in force that the evidence says is doing nothing.

This module is what `retirement_gate.py` is for the retirement gate — the shapes
that cross between a Workflow, an Activity and a client, and the one function
that decides whether an answer may be admitted. It is deliberately separate from
`reversal.py`, which owns what a *recorded reversal* is and knows nothing about
gates, and from `reconsider.py`, which owns what a *reconsideration* is and knows
nothing about either.

===============================================================================
  THE DOCKET GROUPS BY DECISION, NOT BY TRIGGER
===============================================================================

`reconsider` emits one `Reconsideration` per *rule that fired*, so a block can be
reported twice — once because its hook never executed and once because its
endpoint has vanished from the app. Those are two pieces of evidence about **one**
decision, and there is only one thing a human can do about them: withdraw it, or
not.

So a docket item is a `(kind, original_decision_id, subject)` — one item per
decision-to-withdraw, carrying every trigger that fired and every line of their
evidence. That is not a tidying-up: `reversal.withdrawn` is keyed on
`(original_decision_id, subject)`, and a docket that asked per trigger would let a
human answer `withdraw` to one and `keep` to the other on the same key, and then
`reversal.append` would refuse the second as a duplicate — after the first had
already rewritten the manifest. The gate's unit of question has to be the unit of
record, and this is it.

`item_id` is derived from exactly that triple, so it is stable across
re-derivations and cannot collide between two different decisions.

===============================================================================
  WHY THE RULES THAT DID NOT RUN ARE IN THE SIGNED BYTES
===============================================================================

`reconsiderations()` returns `(found, not_run)` and its docstring says **both
halves are the result**: a rule that needed a decode index and did not get one
found nothing for a reason that has nothing to do with the evidence. A docket
carrying only the findings would present "3 decisions look wrong" to a human who
has no way to see that the fourth rule never ran — which is this repository's most
repeated defect wearing gate clothes.

So `rules_not_run` is part of the docket document, and therefore part of what the
human's signature covers. A gate raised with an incomplete sweep is answerable;
one that hides that it was incomplete is not.

===============================================================================
  WHAT CROSSES INTO HISTORY, AND WHAT DOES NOT
===============================================================================

The Workflow carries `ReversalGateV1` — six scalars — and the *hash* of the
subject, while the docket and the rulings live in the content-addressed store. The
docket holds an agent's prose summary of each suspect decision and every line of
evidence behind it, and Temporal History is permanent and replayable.

The consequence is the division every gate here draws: **the Workflow's update
validator is a filter and the admitting Activity is the authority.** The validator
runs in the sandbox with no store and no ledger, so it can check that a submission
binds this gate and arrived in the window; it cannot read the document being
approved. Everything the filter checks, the authority checks again — see
`gate_contract.py` for what happened the one time that was not true.

===============================================================================
  ONE CLAUSE THIS AUTHORITY HAS THAT THE RETIREMENT GATE'S DOES NOT
===============================================================================

Each ruling carries `item_sha256`, the digest of the docket item it answers, and
`validate_submission` **checks it against the subject**. `RetirementRulingV1`
carries the equivalent `case_sha256` and nothing verifies it: the client fills it
in from the recorded docket, so on the supported path it is right, and an
Activity is reachable independently of the client. The digest is what
`publish_admitted` writes into the permanent record as "the evidence this was
ruled against", and a permanent record that names the wrong evidence is worse
than one that names none.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .contracts import (
    ID_PATTERN,
    SHA256_PATTERN,
    ArtifactRef,
    GateDecision,
    canonical_sha256,
)
from .gate_contract import bind_decision, bind_document

__all__ = [
    "DOCKET_ARTIFACT_KIND",
    "RULINGS_ARTIFACT_KIND",
    "GATE_ID_SUFFIX",
    "KINDS",
    "VERDICTS",
    "WITHDRAWING_VERDICT",
    "MAX_RATIONALE",
    "ReversalGateError",
    "derived_gate_id",
    "item_id",
    "item_sha256",
    "docket_subjects",
    "ReversalGateV1",
    "ReversalSubjectV1",
    "ReversalGateRequestV1",
    "ReversalRulingV1",
    "ReversalRulingsV1",
    "ReversalGateSubmissionV1",
    "ReversalRunRequestV1",
    "ReversalRunResultV1",
    "ReversalRulingsAdmissionV1",
    "derive_reversal_gate_request",
    "derive_reversal_gate",
    "validate_submission",
]


DOCKET_ARTIFACT_KIND = "reversal-docket-v1"
RULINGS_ARTIFACT_KIND = "reversal-rulings-v1"
GATE_ID_SUFFIX = "-reversal-gate"

#: What may be withdrawn. Identical to `reversal.KINDS` and cross-checked by a
#: test rather than imported, exactly as `retirement_gate.VERDICTS` is: this layer
#: is the wire contract and that one is the local record, and a change to either
#: that silently changed the other is the coupling worth refusing.
KINDS = ("block", "retirement")

#: What a human may answer per item.
#:
#: `keep` is not "do nothing": it is a decision that the evidence has been read
#: and the original decision stands, and it is why `defer` exists separately. The
#: `block_endpoint_absent` evidence tells a human in as many words to CONFIRM
#: AGAINST THE DECODE before withdrawing, and `defer` is the honest answer for
#: somebody who has not done that yet.
VERDICTS = ("withdraw", "keep", "defer")

#: The one verdict that changes anything. Named rather than spelled `"withdraw"`
#: at each use, because the consumer branches on it and a typo there would record
#: a human's decision and apply nothing.
WITHDRAWING_VERDICT = "withdraw"

MAX_RATIONALE = 2048

#: No silent verdict. Every answer needs a rationale, including `keep` — going on
#: enforcing a block the evidence says is inert is also a decision, and a docket
#: here is a handful of items rather than the hundred candidates that made the
#: feature gate's `ignore` unanswerable without one.


class ReversalGateError(ValueError):
    """Raised when a reversal gate contract is malformed or unadmissible."""


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ReversalGateError(f"Invalid {label}")


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ReversalGateError(f"Invalid {label}")


def _artifact(value: object, kind: str, label: str) -> None:
    if type(value) is not ArtifactRef:
        raise ReversalGateError(f"{label} must be an ArtifactRef")
    if value.kind != kind:
        raise ReversalGateError(f"{label} must be of kind {kind}")


def _strict(data: object, expected: set[str], label: str) -> dict[str, Any]:
    """Refuse unknown *and* missing keys, following `contracts._strict_keys`.

    A decoder that tolerates a missing key silently supplies a default, and the
    one field most worth omitting from a reversal document is the one that binds
    it to a subject.
    """

    if not isinstance(data, dict):
        raise ReversalGateError(f"{label} must be an object")
    if set(data) != expected:
        missing = sorted(expected - set(data))
        unknown = sorted(set(data) - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise ReversalGateError(f"{label} has {'; '.join(detail)}")
    return data


def derived_gate_id(run_id: str) -> str:
    """`<run_id>-reversal-gate`, or a refusal.

    Never truncated to fit `ID_PATTERN`. A gate id silently shortened would still
    look plausible and would stop matching the client's `matches` predicate, which
    is the failure mode where a gate is answerable in a test and unanswerable in
    production.
    """

    _identifier(run_id, "run id")
    gate_id = f"{run_id}{GATE_ID_SUFFIX}"
    if not ID_PATTERN.fullmatch(gate_id):
        raise ReversalGateError(
            f"run id {run_id!r} makes a gate id that is not a valid identifier"
        )
    return gate_id


def item_id(kind: str, original_decision_id: str, subject: str) -> str:
    """A docket item's identity, from the triple a withdrawal is recorded against.

    Not from the trigger, and not from the summary. Two rules firing on one
    decision must produce one item — see the module docstring — and a summary an
    agent rewrote must not turn a pending question into a different one.

    An endpoint path is not an `ID_PATTERN` identifier (`feed/timeline_stream/`
    has slashes), so the subject cannot be the id. A digest can be, and it also
    cannot collide with another decision's item.
    """

    if kind not in KINDS:
        raise ReversalGateError(
            f"unknown reversal kind {kind!r}; expected one of {', '.join(KINDS)}"
        )
    for value, label in (
        (original_decision_id, "original decision id"),
        (subject, "subject"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ReversalGateError(f"a docket item is missing its {label}")
    digest = canonical_sha256(
        {
            "kind": kind,
            "original_decision_id": original_decision_id,
            "subject": subject,
        }
    )
    return f"{kind}-{digest[:16]}"


def item_sha256(item: Mapping[str, Any]) -> str:
    """The digest of one docket item, as the ruling that answers it must carry.

    Over the WHOLE item — triggers, summary and evidence included — because that
    is what a human read. A digest over the identifying triple alone would still
    match after an agent rewrote the evidence, and the ruling would then claim to
    have been made against something nobody saw.
    """

    return canonical_sha256(dict(item))


def docket_subjects(document: Mapping[str, Any]) -> tuple["ReversalSubjectV1", ...]:
    """The subject's item list, read out of a docket document. One derivation.

    Called by the producer when it records a docket, by `resolve_with` when it
    re-derives one from the ledger, and by the submission client. Three callers
    and one implementation, for the reason `activities._reversal_request` is one
    function: two derivations agree only until one of them is edited.
    """

    items = document.get("items")
    if not isinstance(items, (list, tuple)):
        raise ReversalGateError("a reversal docket must carry an items array")
    out: list[ReversalSubjectV1] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise ReversalGateError("every docket item must be an object")
        for field in ("item_id", "kind", "original_decision_id", "subject"):
            if field not in entry:
                raise ReversalGateError(f"a docket item has no {field}")
        expected = item_id(
            str(entry["kind"]),
            str(entry["original_decision_id"]),
            str(entry["subject"]),
        )
        if entry["item_id"] != expected:
            # A recorded document whose ids do not derive from its own contents is
            # a document somebody edited. Refused here rather than adopted,
            # because the id is what a human's ruling names.
            raise ReversalGateError(
                f"docket item {entry['item_id']!r} does not derive from its own "
                f"kind, decision and subject (expected {expected!r})"
            )
        out.append(
            ReversalSubjectV1(
                schema_version=1,
                item_id=expected,
                kind=str(entry["kind"]),
                item_sha256=item_sha256(entry),
            )
        )
    ids = [item.item_id for item in out]
    if len(set(ids)) != len(ids):
        # Checked HERE and not only in `ReversalGateRequestV1`. That dataclass
        # refuses duplicates, but `reversal_record.publish_admitted` builds a
        # `{item_id: entry}` dictionary straight from the document and never
        # constructs a request — so a collision there silently drops an item and
        # attributes one decision's ruling to another. Two ids collide only if
        # their digests do, which is why this reads as paranoia and is not: the
        # id is a *truncation*, and the width is one edit from being too short.
        raise ReversalGateError(
            "two docket items share an id; nothing downstream could tell their "
            "rulings apart"
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ReversalGateV1:
    """The history-safe envelope. Six scalars and nothing a human wrote."""

    schema_version: int
    run_id: str
    gate_id: str
    request_sha256: str
    allowed_actor: str
    policy_revision: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported reversal gate schema")
        _identifier(self.run_id, "run id")
        _identifier(self.gate_id, "gate id")
        _sha256(self.request_sha256, "request digest")
        _identifier(self.allowed_actor, "allowed actor")
        _identifier(self.policy_revision, "policy revision")
        if self.gate_id != f"{self.run_id}{GATE_ID_SUFFIX}":
            raise ReversalGateError("Gate id does not derive from the run id")

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
    def from_dict(cls, data: dict[str, Any]) -> "ReversalGateV1":
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
            "reversal gate",
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
class ReversalSubjectV1:
    """One decision under reconsideration, as the signed subject names it.

    `kind` is carried so the subject a human signs says *what sort of decisions*
    are in front of them without fetching the docket; nothing validates against
    it, because the docket is bound by hash and is the authority on its own
    contents.
    """

    schema_version: int
    item_id: str
    kind: str
    item_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported reversal subject schema")
        _identifier(self.item_id, "docket item id")
        if self.kind not in KINDS:
            raise ReversalGateError(f"Unknown reversal kind {self.kind!r}")
        _sha256(self.item_sha256, "docket item digest")
        if not self.item_id.startswith(f"{self.kind}-"):
            raise ReversalGateError("Docket item id does not derive from its kind")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "kind": self.kind,
            "item_sha256": self.item_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalSubjectV1":
        row = _strict(
            data,
            {"schema_version", "item_id", "kind", "item_sha256"},
            "reversal subject",
        )
        return cls(
            schema_version=row["schema_version"],
            item_id=row["item_id"],
            kind=row["kind"],
            item_sha256=row["item_sha256"],
        )


@dataclass(frozen=True, slots=True)
class ReversalGateRequestV1:
    """The derived subject. Hashed into the gate; never itself in History.

    `allowed_actor` is inside the hashed bytes deliberately, so the admitting
    Activity can verify who was permitted to answer without trusting anything the
    Workflow carried — the filter is not the only place that requirement lives.
    """

    schema_version: int
    run_id: str
    gate_id: str
    #: The docket in CAS: every decision under reconsideration at this version,
    #: with the evidence against it and the rules that could not be run.
    docket: ArtifactRef
    #: The Instagram version the evidence was read at. Carried in the subject so a
    #: human can see, in the bytes they sign, which port's evidence this is about.
    version: str
    policy_revision: str
    allowed_actor: str
    #: Every decision the docket asks about, in the docket's own order.
    items: tuple[ReversalSubjectV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported reversal request schema")
        _identifier(self.run_id, "run id")
        _identifier(self.gate_id, "gate id")
        _artifact(self.docket, DOCKET_ARTIFACT_KIND, "docket")
        _identifier(self.version, "version")
        _identifier(self.policy_revision, "policy revision")
        _identifier(self.allowed_actor, "allowed actor")
        if not isinstance(self.items, tuple) or not self.items:
            raise ReversalGateError(
                "a reversal gate with no decisions to reconsider has nothing to ask. "
                "Do not raise one"
            )
        for item in self.items:
            if type(item) is not ReversalSubjectV1:
                raise ReversalGateError("Every docket item must be a ReversalSubjectV1")
        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ReversalGateError("Duplicate docket item id")
        if self.gate_id != f"{self.run_id}{GATE_ID_SUFFIX}":
            raise ReversalGateError("Gate id does not derive from the run id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_id": self.gate_id,
            # `dataclasses.asdict`, because `ArtifactRef` has `from_dict` and no
            # `to_dict` — matching how `feature_gate` and `retirement_gate`
            # serialise the same type.
            "docket": dataclasses.asdict(self.docket),
            "version": self.version,
            "policy_revision": self.policy_revision,
            "allowed_actor": self.allowed_actor,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalGateRequestV1":
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
                "items",
            },
            "reversal request",
        )
        items = row["items"]
        if not isinstance(items, (list, tuple)):
            raise ReversalGateError("items must be an array")
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            gate_id=row["gate_id"],
            docket=ArtifactRef.from_dict(row["docket"]),
            version=row["version"],
            policy_revision=row["policy_revision"],
            allowed_actor=row["allowed_actor"],
            items=tuple(ReversalSubjectV1.from_dict(item) for item in items),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ReversalRulingV1:
    """One decision's answer, as it travels to the gate."""

    schema_version: int
    item_id: str
    verdict: Literal["withdraw", "keep", "defer"]
    rationale: str
    #: The docket item bytes this answers. Checked against the subject by
    #: `validate_submission`, unlike the retirement gate's `case_sha256` — see the
    #: module docstring for why that difference is deliberate.
    item_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported ruling schema")
        _identifier(self.item_id, "docket item id")
        if self.verdict not in VERDICTS:
            raise ReversalGateError(f"Unknown verdict {self.verdict!r}")
        if not isinstance(self.rationale, str) or len(self.rationale) > MAX_RATIONALE:
            raise ReversalGateError("Invalid rationale")
        _sha256(self.item_sha256, "docket item digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "item_id": self.item_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "item_sha256": self.item_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalRulingV1":
        row = _strict(
            data,
            {"schema_version", "item_id", "verdict", "rationale", "item_sha256"},
            "ruling",
        )
        return cls(
            schema_version=row["schema_version"],
            item_id=row["item_id"],
            verdict=row["verdict"],
            rationale=row["rationale"],
            item_sha256=row["item_sha256"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ReversalRulingsV1:
    """The document a human signs: one ruling per item in the docket."""

    schema_version: int
    docket_sha256: str
    version: str
    policy_revision: str
    rulings: tuple[ReversalRulingV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported rulings schema")
        _sha256(self.docket_sha256, "docket digest")
        _identifier(self.version, "version")
        _identifier(self.policy_revision, "policy revision")
        if not isinstance(self.rulings, tuple) or not self.rulings:
            raise ReversalGateError("A rulings document with no rulings is not an answer")
        for ruling in self.rulings:
            if type(ruling) is not ReversalRulingV1:
                raise ReversalGateError("Every ruling must be a ReversalRulingV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "docket_sha256": self.docket_sha256,
            "version": self.version,
            "policy_revision": self.policy_revision,
            "rulings": [ruling.to_dict() for ruling in self.rulings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalRulingsV1":
        row = _strict(
            data,
            {"schema_version", "docket_sha256", "version", "policy_revision", "rulings"},
            "rulings document",
        )
        items = row["rulings"]
        if not isinstance(items, (list, tuple)):
            raise ReversalGateError("rulings must be an array")
        return cls(
            schema_version=row["schema_version"],
            docket_sha256=row["docket_sha256"],
            version=row["version"],
            policy_revision=row["policy_revision"],
            rulings=tuple(ReversalRulingV1.from_dict(item) for item in items),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ReversalGateSubmissionV1:
    """A `GateDecision` plus the rulings it approves, by reference."""

    schema_version: int
    decision: GateDecision
    rulings: ArtifactRef

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported submission schema")
        if type(self.decision) is not GateDecision:
            raise ReversalGateError("Submission must carry a GateDecision")
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
            "rulings": dataclasses.asdict(self.rulings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalGateSubmissionV1":
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
class ReversalRunRequestV1:
    """Workflow input."""

    schema_version: int
    run_id: str
    gate_timeout_seconds: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported run request schema")
        _identifier(self.run_id, "run id")
        if not isinstance(self.gate_timeout_seconds, int) or self.gate_timeout_seconds <= 0:
            raise ReversalGateError("Gate timeout must be a positive number of seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "gate_timeout_seconds": self.gate_timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalRunRequestV1":
        row = _strict(
            data, {"schema_version", "run_id", "gate_timeout_seconds"}, "run request"
        )
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            gate_timeout_seconds=row["gate_timeout_seconds"],
        )


@dataclass(frozen=True, slots=True)
class ReversalRunResultV1:
    """Workflow output."""

    schema_version: int
    run_id: str
    state: str
    decision_id: str | None
    rulings: ArtifactRef | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported run result schema")
        _identifier(self.run_id, "run id")
        if not isinstance(self.state, str) or not self.state.strip():
            raise ReversalGateError("Run result needs a state")
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
            "rulings": dataclasses.asdict(self.rulings) if self.rulings else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalRunResultV1":
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
class ReversalRulingsAdmissionV1:
    """Workflow → Activity."""

    schema_version: int
    run_id: str
    submission: ReversalGateSubmissionV1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ReversalGateError("Unsupported admission schema")
        _identifier(self.run_id, "run id")
        if type(self.submission) is not ReversalGateSubmissionV1:
            raise ReversalGateError("Admission must carry a ReversalGateSubmissionV1")
        if self.submission.decision.run_id != self.run_id:
            raise ReversalGateError("Admission and decision name different runs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "submission": self.submission.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReversalRulingsAdmissionV1":
        row = _strict(data, {"schema_version", "run_id", "submission"}, "admission")
        return cls(
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            submission=ReversalGateSubmissionV1.from_dict(row["submission"]),
        )


def derive_reversal_gate_request(
    run_id: str,
    docket: ArtifactRef,
    version: str,
    policy_revision: str,
    allowed_actor: str,
    items: Sequence[ReversalSubjectV1],
) -> ReversalGateRequestV1:
    """The subject, from recorded state alone. Pure, and the same anywhere."""

    return ReversalGateRequestV1(
        schema_version=1,
        run_id=run_id,
        gate_id=derived_gate_id(run_id),
        docket=docket,
        version=version,
        policy_revision=policy_revision,
        allowed_actor=allowed_actor,
        items=tuple(items),
    )


def derive_reversal_gate(request: ReversalGateRequestV1) -> ReversalGateV1:
    """The history-safe envelope for a subject.

    Actor and policy are taken **from the request** rather than passed again. A
    second parameter is a second chance to disagree, and the one that reached
    History would be the one the validator enforced.
    """

    if type(request) is not ReversalGateRequestV1:
        raise ReversalGateError("Expected a ReversalGateRequestV1")
    return ReversalGateV1(
        schema_version=1,
        run_id=request.run_id,
        gate_id=request.gate_id,
        request_sha256=request.sha256,
        allowed_actor=request.allowed_actor,
        policy_revision=request.policy_revision,
    )


def validate_submission(
    request: ReversalGateRequestV1,
    submission: ReversalGateSubmissionV1,
    rulings: ReversalRulingsV1,
) -> None:
    """The authority. Everything the sandbox validator checks, and more.

    Takes no `request_sha256` argument: the digest is recomputed from the request
    in hand, so a caller cannot supply the number it wants to be compared against.

    The order below is deliberate — document integrity first, then the binding to
    this gate, then who answered, then that every decision was actually ruled on,
    and last that each ruling names the item it claims to answer. A refusal from
    an early clause means the later ones were never reached, and the message says
    which.
    """

    if type(request) is not ReversalGateRequestV1:
        raise ReversalGateError("Expected a ReversalGateRequestV1")
    if type(submission) is not ReversalGateSubmissionV1:
        raise ReversalGateError("Expected a ReversalGateSubmissionV1")
    if type(rulings) is not ReversalRulingsV1:
        raise ReversalGateError("Expected a ReversalRulingsV1")

    # The six clauses every gate's authority shares live in `gate_contract`, so a
    # fix reaches all of them. See that module for why three copies of a security
    # check is how one gets fixed and two do not.
    try:
        bind_document(rulings.to_dict(), submission.rulings, label="reversal rulings")
        bind_decision(
            submission.decision,
            subject_sha256=request.sha256,
            run_id=request.run_id,
            gate_id=request.gate_id,
            policy_revision=request.policy_revision,
            allowed_actor=request.allowed_actor,
        )
    except ValueError as error:
        raise ReversalGateError(str(error)) from error

    if rulings.docket_sha256 != request.docket.sha256:
        raise ReversalGateError("Rulings answer a different docket")
    if rulings.version != request.version:
        raise ReversalGateError("Rulings name a different Instagram version")
    if rulings.policy_revision != request.policy_revision:
        raise ReversalGateError("Rulings were written under a different policy revision")

    seen = [ruling.item_id for ruling in rulings.rulings]
    if len(set(seen)) != len(seen):
        raise ReversalGateError("A decision was ruled on twice")
    expected = {item.item_id: item for item in request.items}
    missing = sorted(set(expected) - set(seen))
    unknown = sorted(set(seen) - set(expected))
    if missing:
        # Refused, never defaulted. An item nobody mentioned is a decision nobody
        # looked at, and reading that silence as `keep` would leave a block in
        # force on the strength of an answer no human gave.
        raise ReversalGateError(
            f"No ruling for {', '.join(missing)}. Every decision in the docket needs "
            "an answer; a missing one is not a `keep`"
        )
    if unknown:
        raise ReversalGateError(
            f"Ruling for {', '.join(unknown)}, which is not in this docket"
        )

    for ruling in rulings.rulings:
        if not ruling.rationale.strip():
            raise ReversalGateError(
                f"{ruling.item_id}: every verdict needs a rationale, including `keep` — "
                "going on enforcing a decision the evidence questions is also a decision"
            )
        if ruling.item_sha256 != expected[ruling.item_id].item_sha256:
            # The clause the retirement gate does not have. This digest is what
            # the permanent record names as the evidence a human ruled against,
            # and a record naming evidence nobody saw is worse than one naming
            # none.
            raise ReversalGateError(
                f"{ruling.item_id}: the ruling answers docket item "
                f"{ruling.item_sha256}, but this docket's item is "
                f"{expected[ruling.item_id].item_sha256}"
            )
