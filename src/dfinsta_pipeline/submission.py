"""The trusted submission client: how a human actually answers a durable gate.

Until now nothing could answer a gate. `execute_update` appeared only in tests,
so every human decision in this pipeline was hypothetical, and stage 4 could not
run end to end no matter what its contracts said.

The obvious way to close that is a script that reads the pending
:class:`~.contracts.GateRequest` off the Workflow, asks a human yes or no, and
submits a decision carrying the hashes the Workflow published. **That is the
shape this module deliberately refuses.** A standalone CLI was designed once
before in this project, reviewed, and deleted unexecuted, because it
self-asserted the hashes it should have been verifying. A client that copies the
Workflow's hashes into a decision has a human signing whatever the Workflow said
-- their signature adds a name to a number nobody checked.

So the rule here is narrow and total:

    **The client re-derives the gate subject from recorded state, and refuses to
    let a human sign a hash it cannot reproduce.**

Everything else follows from it.

*Re-derivation is the same code the Activity ran.* `_resolve_replay_verification`
is `prepare_replay_verification_gate_activity`'s body, reached through a runtime
whose ledger is open read-only. Reimplementing the derivation would defeat the
purpose -- the client's entire claim is that it computes what the preparing
Activity computed, and two implementations can agree while both being wrong.

*The ledger cannot be written.* `configure_runtime(..., read_only=True)` opens
SQLite through `mode=ro`, so the client is structurally unable to create the
state it is checking. This is not a promise in a docstring; it is a database
handle that refuses.

*A gate with no registered resolver is refused, not trusted.* `PortRunWorkflow`'s
`phase-a-approval` gate binds three hashes whose operations the ledger indexes by
content rather than by run, so this client cannot reproduce them, and therefore
will not sign them. Refusing to answer is the correct behaviour and the honest
one; falling back to the published hashes is the failure this module exists to
prevent.

*The actor is the OS principal, not an argument.* An `--actor` flag would make
authorization a matter of typing. The actor is read from a 0600 file owned by the
invoking uid, which is the same trust boundary the ledger and the content store
already rely on. This is not remote authentication -- the plan still lists that
as open -- but it is no longer a string the caller chooses.

*The decision's identity is a function of its content.* Identical answers produce
identical ids, so a resubmission after a dropped connection is the same decision
and Temporal returns the original receipt; different answers produce different
ids, so nothing collides. The journal exists to make `issued_at` stable across
process restarts, which is what makes that property reach past a crash.

What this module does not do: it does not enforce the plan's "at most three
invalid responses" budget. That cannot live in a client, or in a validator --
an Update rejected by its validator never reaches History, so any counter resets
on worker restart. It is recorded as unimplemented in `docs/STAGE_4_DESIGN.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .contracts import (
    ID_PATTERN,
    SHA256_PATTERN,
    GateDecision,
    GateRequest,
    canonical_json,
    canonical_sha256,
)

#: Every refusal is one category: the client will not submit this. The message
#: says which clause refused. A caller that wants to distinguish them reads the
#: message; a caller that wants to proceed anyway has to write different code,
#: which is the intent.
class SubmissionRefused(RuntimeError):
    """The client declines to submit. The message names the failing clause."""


VERDICTS = ("approve", "reject", "defer")

#: Prefixes for the two derived identifiers. They are part of the derived bytes
#: in the sense that changing one changes every id this client has ever minted,
#: so an in-flight resubmission after such a change is correctly treated as a
#: new decision rather than silently deduplicated against the old one.
DECISION_ID_PREFIX = "decision-"
IDEMPOTENCY_ID_PREFIX = "idempotency-"

#: The minimum number of leading hex characters of the *derived* subject hash a
#: human must quote back to submit. Twelve is not a security parameter -- the
#: client already verified the subject -- it is evidence that a human read the
#: thing the client verified rather than approving whatever was pending.
CONFIRMATION_LENGTH = 12

#: `GateDecision` fields that are NOT part of the content the identifiers are
#: derived from: the schema tag, and the two identifiers themselves. Everything
#: else must feed the digest, or two materially different decisions could share
#: an id and the ledger would reject the second as an identity collision at the
#: worst possible moment. `test_submission` binds this to the dataclass so a new
#: field cannot quietly fall out of the identity.
IDENTITY_EXCLUDED_FIELDS = frozenset({"schema_version", "decision_id", "idempotency_id"})

PRINCIPAL_FIELDS = frozenset({"schema_version", "uid", "actor"})
JOURNAL_FIELDS = frozenset(
    {"schema_version", "workflow_id", "gate_id", "subject_sha256", "decision"}
)

#: Entries written since gates gained kind-specific payloads. Read strictly as
#: either shape: an entry written before this existed is a valid v1 entry with no
#: payload, and refusing it would strand a human mid-answer on an upgrade.
JOURNAL_FIELDS_WITH_PAYLOAD = JOURNAL_FIELDS | {"payload_sha256"}


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or not ID_PATTERN.fullmatch(value):
        raise SubmissionRefused(f"Invalid {label}")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or not SHA256_PATTERN.fullmatch(value):
        raise SubmissionRefused(f"Invalid {label}")
    return value


def _strict(data: object, expected: frozenset[str], label: str) -> dict[str, Any]:
    if type(data) is not dict or any(type(key) is not str for key in data):
        raise SubmissionRefused(f"{label} must be an object with string keys")
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise SubmissionRefused(f"Unknown {label} field: {unknown[0]}")
    if missing:
        raise SubmissionRefused(f"Missing {label} field: {missing[0]}")
    return data


# ---------------------------------------------------------------- the principal


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is answering, bound to the OS user rather than to a flag.

    The plan's requirement is "authentication occurs in a trusted client before
    submission", and separately records that identity is still a test string.
    This does not close that gap -- a same-uid process can still write this file
    -- but it moves the actor from something the caller types to something the
    operating system already decided, at exactly the trust boundary the ledger
    (a same-uid SQLite file) and the content store (same-uid 0444 blobs) already
    depend on. Claiming more than that would be the more dangerous error.
    """

    schema_version: int
    uid: int
    actor: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SubmissionRefused("Unsupported principal schema")
        if type(self.uid) is not int or self.uid < 0:
            raise SubmissionRefused("Principal uid must be a non-negative integer")
        _identifier(self.actor, "principal actor")


def load_principal(path: Path, *, effective_uid: int | None = None) -> Principal:
    """Read the actor from a file only its owner can have written.

    `effective_uid` is injectable for tests alone; production always passes the
    real one. It is a parameter rather than a patch target because a test that
    monkeypatches `os.geteuid` proves nothing about the code path that runs.
    """

    if not isinstance(path, Path):
        raise SubmissionRefused("Principal path must be a Path")
    uid = os.geteuid() if effective_uid is None else effective_uid
    try:
        status = path.lstat()
    except OSError as error:
        raise SubmissionRefused(
            f"No principal file at {path}. Create it, mode 0600, holding "
            '{"schema_version": 1, "uid": <your uid>, "actor": "<actor>"}'
        ) from error
    if not stat.S_ISREG(status.st_mode):
        raise SubmissionRefused(f"Principal path is not a regular file: {path}")
    if status.st_uid != uid:
        raise SubmissionRefused(f"Principal file is not owned by uid {uid}: {path}")
    # Any group or other bit at all. A world-readable actor file is a different
    # problem from a world-writable one, but neither belongs to an identity. The
    # owner's execute bit is not a disclosure, so 0700 and 0400 pass -- the
    # message says what is actually checked rather than naming one mode.
    if status.st_mode & 0o077:
        raise SubmissionRefused(
            f"Principal file must have no group or other permissions, found "
            f"{status.st_mode & 0o777:04o}: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionRefused(f"Principal file is not readable JSON: {path}") from error
    fields = _strict(data, PRINCIPAL_FIELDS, "principal")
    principal = Principal(fields["schema_version"], fields["uid"], fields["actor"])
    if principal.uid != uid:
        raise SubmissionRefused(
            f"Principal file names uid {principal.uid} but this process runs as {uid}"
        )
    return principal


# ------------------------------------------------------------- the derived view


@dataclass(frozen=True, slots=True)
class DerivedSubject:
    """What the client recomputed, independently of anything the Workflow said.

    Every hash a decision carries comes from here rather than from the published
    request. That is not belt and braces: it means a code path that signs an
    unverified value does not exist, instead of existing and being avoided.
    """

    run_id: str
    gate_id: str
    subject_sha256: str
    admission_sha256: str
    prepared_sha256: str
    policy_revision: str
    allowed_actor: str

    def __post_init__(self) -> None:
        _identifier(self.run_id, "derived run id")
        _identifier(self.gate_id, "derived gate id")
        _sha256(self.subject_sha256, "derived subject SHA-256")
        _sha256(self.admission_sha256, "derived admission SHA-256")
        _sha256(self.prepared_sha256, "derived prepared SHA-256")
        _identifier(self.policy_revision, "derived policy revision")
        _identifier(self.allowed_actor, "derived allowed actor")


def gate_request_from_dict(data: object) -> GateRequest:
    """Decode a `GateRequest` from a query result, strictly.

    The query returns plain JSON because the client submits and queries by name
    and holds no workflow class. Decoding it field by field is the point: the
    published request is the one input here that a compromised or simply older
    Workflow controls, so it gets parsed rather than trusted.
    """

    expected = frozenset(field.name for field in dataclasses.fields(GateRequest))
    fields = _strict(data, expected, "published gate request")
    try:
        return GateRequest(**fields)
    except (TypeError, ValueError) as error:
        raise SubmissionRefused(f"Published gate request is invalid: {error}") from error


def verify_published_gate(
    published: GateRequest, derived: DerivedSubject, *, now: datetime
) -> None:
    """Refuse unless the published gate is the subject the client reproduced.

    THIS IS THE CHECK THE MODULE EXISTS FOR.

    A mismatch is never a reason to prefer one side. It means the Workflow is
    gated on something this client cannot account for, and the only safe move is
    to stop and show a human both values. Nothing here repairs, prefers or
    normalises: it names the first field that disagreed and raises.
    """

    if type(published) is not GateRequest:
        raise SubmissionRefused("Published gate must be an exact GateRequest")
    if type(derived) is not DerivedSubject:
        raise SubmissionRefused("Derived subject must be an exact DerivedSubject")
    if type(now) is not datetime or now.tzinfo is None:
        raise SubmissionRefused("Current time must be an aware datetime")

    for label, published_value, derived_value in (
        ("run", published.run_id, derived.run_id),
        ("gate", published.gate_id, derived.gate_id),
        ("subject hash", published.subject_sha256, derived.subject_sha256),
        ("admission hash", published.admission_sha256, derived.admission_sha256),
        ("prepared hash", published.prepared_sha256, derived.prepared_sha256),
        ("policy revision", published.policy_revision, derived.policy_revision),
    ):
        if published_value != derived_value:
            raise SubmissionRefused(
                f"Published {label} {published_value!r} is not the derived "
                f"{label} {derived_value!r}; refusing to sign an unverified subject"
            )

    try:
        issued_at = datetime.fromisoformat(published.issued_at)
        expires_at = datetime.fromisoformat(published.expires_at)
    except (TypeError, ValueError) as error:
        raise SubmissionRefused("Published gate timestamps are invalid") from error
    if issued_at.tzinfo is None or expires_at.tzinfo is None:
        raise SubmissionRefused("Published gate timestamps require a UTC offset")
    if expires_at <= issued_at:
        raise SubmissionRefused("Published gate expires before it was issued")
    # Refusing here rather than letting the validator refuse is not politeness.
    # A submission that reaches an expired gate is rejected with a message about
    # the update, and the human is left unsure whether their decision landed.
    if now >= expires_at:
        raise SubmissionRefused(f"Gate expired at {published.expires_at}")
    if now < issued_at - timedelta(minutes=5):
        raise SubmissionRefused(
            f"Gate was issued at {published.issued_at}, which is in this client's future; "
            "check the clock before deciding anything"
        )


# ------------------------------------------------------------------- the answer


@dataclass(frozen=True, slots=True)
class Answer:
    """The only part of a decision a human supplies.

    ``detail`` carries whatever a *particular* gate needs beyond a verdict and a
    rationale — the feature gate wants a ruling per candidate, and this is the
    only place a human's own content belongs. It is deliberately untyped here:
    the gate kind that understands it validates it, and this dataclass would
    otherwise have to know about every gate that will ever exist.

    **A kind that does not understand a detail must refuse it, never drop it.**
    That rule lives in :func:`_decision_payload`, and it is the point of the
    field rather than a nicety: a human who supplies rulings and gets a bare
    `approve` submitted is the exact failure this module exists to prevent,
    arriving through an argument nobody read.
    """

    verdict: Literal["approve", "reject", "defer"]
    rationale: str
    detail: object | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise SubmissionRefused(f"Verdict must be one of {', '.join(VERDICTS)}")
        if type(self.rationale) is not str or not self.rationale.strip():
            raise SubmissionRefused("A decision requires a rationale")
        if len(self.rationale) > 2048:
            raise SubmissionRefused("Rationale is longer than 2048 characters")


def decision_identity(content: dict[str, Any]) -> tuple[str, str]:
    """Derive `(decision_id, idempotency_id)` from the decision's own content.

    Both come from one digest, so they are perfectly correlated, and that is
    correct rather than a shortcut: `idempotency_id` means "the same intent" and
    `decision_id` means "this decision record", and for a client that submits
    one assembled decision per answer those are the same thing. The property
    that matters is the invariant -- identical content yields identical ids, so
    a resubmission deduplicates; different content yields different ids, so
    nothing collides with a decision the ledger already holds.
    """

    digest = canonical_sha256(content)
    return f"{DECISION_ID_PREFIX}{digest}", f"{IDEMPOTENCY_ID_PREFIX}{digest}"


def assemble_decision(
    derived: DerivedSubject, principal: Principal, answer: Answer, issued_at: datetime
) -> GateDecision:
    """Build the decision from the DERIVED subject and the OS principal.

    Note what is not a parameter: the published gate request. It has already
    served its purpose by being checked against `derived`, and giving it a route
    into the decision would reintroduce exactly the copy-the-Workflow's-hashes
    behaviour this module refuses.
    """

    if type(derived) is not DerivedSubject:
        raise SubmissionRefused("Derived subject must be an exact DerivedSubject")
    if type(principal) is not Principal:
        raise SubmissionRefused("Principal must be an exact Principal")
    if type(answer) is not Answer:
        raise SubmissionRefused("Answer must be an exact Answer")
    if type(issued_at) is not datetime or issued_at.tzinfo is None:
        raise SubmissionRefused("Decision timestamp must be an aware datetime")
    if principal.actor != derived.allowed_actor:
        raise SubmissionRefused(
            f"This gate may only be answered by {derived.allowed_actor!r}, "
            f"and this process is {principal.actor!r}"
        )

    content = {
        "actor": principal.actor,
        "run_id": derived.run_id,
        "gate_id": derived.gate_id,
        "subject_sha256": derived.subject_sha256,
        "admission_sha256": derived.admission_sha256,
        "prepared_sha256": derived.prepared_sha256,
        "policy_revision": derived.policy_revision,
        "decision": answer.verdict,
        "rationale": answer.rationale,
        "issued_at": issued_at.isoformat(),
    }
    decision_id, idempotency_id = decision_identity(content)
    return GateDecision(
        1,
        decision_id,
        idempotency_id,
        content["actor"],
        content["run_id"],
        content["gate_id"],
        content["subject_sha256"],
        content["admission_sha256"],
        content["prepared_sha256"],
        content["policy_revision"],
        content["decision"],
        content["rationale"],
        content["issued_at"],
    )


# ------------------------------------------------------------------ the journal


@dataclass(frozen=True, slots=True)
class JournalEntry:
    schema_version: int
    workflow_id: str
    gate_id: str
    subject_sha256: str
    decision: GateDecision
    payload_sha256: str | None = None


def journal_path(journal_root: Path, workflow_id: str, gate_id: str) -> Path:
    """Name the journal file for one gate of one workflow.

    Both components are re-validated as identifiers before they reach a path.
    `ID_PATTERN` already forbids `/` and requires an alphanumeric first
    character, so no traversal is expressible; checking anyway costs nothing and
    keeps the guarantee local to the function that builds the path.
    """

    if not isinstance(journal_root, Path):
        raise SubmissionRefused("Journal root must be a Path")
    _identifier(workflow_id, "journal workflow id")
    _identifier(gate_id, "journal gate id")
    return journal_root / f"{workflow_id}.{gate_id}.json"


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """What was recorded before submitting: the decision, and what went with it."""

    decision: GateDecision
    #: `None` for an entry written before gates had kind-specific payloads, and
    #: for every gate whose payload is the decision itself.
    payload_sha256: str | None = None


def read_journal(
    journal_root: Path, workflow_id: str, gate_id: str, subject_sha256: str
) -> JournalRecord | None:
    """Return a previously assembled decision for exactly this subject.

    A recorded decision for a *different* subject is ignored rather than
    reused. The gate may have been re-raised over changed bytes, and reusing the
    old answer would be a stale approval authorising something the human never
    saw -- the same failure the whole hash chain exists to prevent, arriving
    through the client's own cache.

    The payload digest rides along because a `GateDecision` does not mention it.
    Without it, a resubmission would pair the recorded decision with a *freshly
    built* payload, and a gate whose answer is more than a verdict -- the feature
    gate rules on every candidate -- would submit rulings the human never made,
    or replace the ones they did, with nothing anywhere saying so.
    """

    path = journal_path(journal_root, workflow_id, gate_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionRefused(f"Journal entry is not readable JSON: {path}") from error
    allowed = (
        JOURNAL_FIELDS_WITH_PAYLOAD
        if isinstance(data, dict) and "payload_sha256" in data
        else JOURNAL_FIELDS
    )
    fields = _strict(data, allowed, "journal entry")
    if fields["schema_version"] != 1:
        raise SubmissionRefused("Unsupported journal entry schema")
    try:
        decision = GateDecision.from_dict(fields["decision"])
    except (TypeError, ValueError) as error:
        raise SubmissionRefused(f"Journal entry holds an invalid decision: {error}") from error
    if fields["workflow_id"] != workflow_id or fields["gate_id"] != gate_id:
        raise SubmissionRefused(f"Journal entry names a different gate: {path}")
    payload_sha256 = fields.get("payload_sha256")
    if payload_sha256 is not None:
        _sha256(payload_sha256, "journal payload hash")
    if fields["subject_sha256"] != subject_sha256:
        return None
    if decision.subject_sha256 != subject_sha256:
        raise SubmissionRefused(f"Journal entry decision does not bind its subject: {path}")
    return JournalRecord(decision, payload_sha256)


def write_journal(
    journal_root: Path,
    workflow_id: str,
    gate_id: str,
    decision: GateDecision,
    payload_sha256: str | None = None,
) -> Path:
    """Record the assembled decision BEFORE submitting it.

    This is what makes a retry byte-identical rather than merely similar.
    `issued_at` is part of the decision and part of its derived identity, so a
    second invocation that re-timestamps produces different ids, and Temporal
    sees a new update instead of the same one -- the workflow then refuses it as
    a duplicate and the human cannot tell a dropped connection from a rejected
    decision. Writing first turns that into "resubmit exactly what was recorded".

    Written to a temporary file and renamed, rather than truncated in place. A
    crash mid-write would otherwise leave truncated JSON that `read_journal`
    refuses **permanently**: the human could not answer the gate at all until
    they worked out for themselves that the fix was to delete a file the client
    never told them about. `os.replace` is atomic within a directory, so the
    journal is either the previous decision or the new one.
    """

    if type(decision) is not GateDecision:
        raise SubmissionRefused("Journal decision must be an exact GateDecision")
    path = journal_path(journal_root, workflow_id, gate_id)
    journal_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A journal entry that anyone else can write is a decision anyone else can
    # put words in: `submit_answer` resubmits what it finds here. Refusing beats
    # silently tightening a directory the caller chose.
    mode = journal_root.stat().st_mode
    if mode & 0o022:
        raise SubmissionRefused(
            f"Journal directory is group- or world-writable ({mode & 0o777:04o}): {journal_root}"
        )
    if payload_sha256 is not None:
        _sha256(payload_sha256, "journal payload hash")
    entry = JournalEntry(
        1, workflow_id, gate_id, decision.subject_sha256, decision, payload_sha256
    )
    written = dataclasses.asdict(entry)
    if payload_sha256 is None:
        # A gate whose payload IS the decision writes no `payload_sha256` key at
        # all, rather than an explicit null. Entries then stay byte-identical to
        # the ones written before this field existed, so upgrading the client
        # mid-answer does not invalidate a journal a human is relying on.
        written.pop("payload_sha256")
    body = canonical_json(written).encode("utf-8")
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    return path


# ------------------------------------------------------------------- gate kinds


def _payload_digest(payload: object, decision: GateDecision) -> str | None:
    """The digest a journal records and an update id covers, or `None`.

    `None` exactly when the payload *is* the decision, which keeps every existing
    gate byte-identical: same journal shape, same update id, same deduplication
    behaviour. A gate that sends more than the decision gets a digest over the
    whole payload, so two answers that differ only in their attachments are two
    different updates rather than one update and a silently discarded document.
    """
    if payload is decision:
        return None
    return canonical_sha256(payload)


def _require_recorded_payload_matches(
    recorded: "JournalRecord", payload: object, pending: "PendingGate", journal_root: Path
) -> None:
    """Refuse to resubmit a journalled decision alongside a different payload.

    The gap this closes: a `GateDecision` says nothing about what rode with it,
    so a resubmission pairs the recorded decision with a *freshly built* payload.
    For the feature gate that payload carries every per-candidate ruling. If the
    human edited their rulings between attempts, the first attempt's decision
    would be resubmitted carrying the second attempt's rulings — or the reverse,
    depending on which side moved — and nothing would say so.
    """
    expected = _payload_digest(payload, recorded.decision)
    if recorded.payload_sha256 == expected:
        return
    path = journal_path(journal_root, pending.workflow_id, pending.derived.gate_id)
    raise SubmissionRefused(
        "The recorded answer was submitted with different content than this one "
        f"({recorded.payload_sha256 or 'none'} vs {expected or 'none'}). Delete "
        f"{path} only if you are certain the recorded answer never reached the "
        "workflow; otherwise the two answers must be reconciled by hand."
    )


def _decision_payload(pending: "PendingGate", decision: GateDecision, answer: Answer) -> object:
    """What a gate's update carries. For most gates, the decision itself.

    Refusing a detail it does not understand is the whole job. Silently dropping
    it would submit a bare verdict on a human's behalf while they believe they
    ruled on something specific — and the receipt would say `accepted`.
    """
    if answer.detail is not None:
        raise SubmissionRefused(
            f"The {pending.kind.name} gate takes a verdict and a rationale and nothing "
            "else; this answer carries additional detail that nothing here would send."
        )
    return decision


@dataclass(frozen=True, slots=True)
class GateKind:
    """How to recognise a gate and how to reproduce its subject.

    ``resolve`` takes **one** argument, the run id, and must keep taking exactly
    one. That signature is what makes "a subject unreachable from a run id is
    unregisterable" a structural property rather than a convention, and it is the
    entire reason `PortRunWorkflow`'s `phase-a-approval` is refused below.
    Widening it to accept a path, a published request or a candidate list would
    re-open that trap for every gate at once.
    """

    name: str
    update_name: str
    matches: Callable[[str, str], bool]
    resolve: Callable[[str], DerivedSubject]
    #: Builds the object the Workflow update receives. Defaults to the decision
    #: alone, which is what every gate needed until one needed more.
    payload: Callable[["PendingGate", GateDecision, Answer], object] = _decision_payload


def _resolve_replay_verification(run_id: str) -> DerivedSubject:
    """Reproduce the final-verification gate subject from the ledger.

    This is `prepare_replay_verification_gate_activity`'s body, called against a
    read-only runtime. Deliberately not a reimplementation: two derivations that
    agree prove something only if they are the same derivation reading the same
    recorded state, and a parallel implementation would drift silently the first
    time either changed.
    """

    from . import activities, replay_gate
    from .ledger import Ledger

    configured = activities.runtime()
    handle = Ledger.admitted_replay_handle_for_run(configured.ledger, run_id)
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    completed_build, build_receipt = replay_gate.resolve_admitted_build(admitted)
    request = replay_gate.derive_verification_request(admitted, completed_build, build_receipt)
    # All three hashes are the request hash: this gate's subject is one derived
    # object, and `ReplayRunWorkflow` binds the decision to it three times over.
    return DerivedSubject(
        run_id=admitted.run_spec.run_id,
        gate_id=request.gate_id,
        subject_sha256=request.sha256,
        admission_sha256=request.sha256,
        prepared_sha256=request.sha256,
        policy_revision=request.policy_revision,
        allowed_actor=request.allowed_actor,
    )


def _resolve_feature_assessment(run_id: str) -> DerivedSubject:
    """Reproduce the feature gate's subject from the ledger.

    Same discipline as the replay resolver: not a reimplementation. It calls
    `assessment_record.resolve_with` against the read-only runtime, which reaches
    the `ArtifactRef` through `require_completed_operation` and reads the
    candidate ids out of the recorded bytes with the single decoder in
    `assessment.py`. A parallel implementation could agree while both were wrong.
    """

    from . import activities, assessment_record
    from .feature_gate import derive_feature_gate_request

    configured = activities.runtime()
    recorded = assessment_record.resolve_with(configured.ledger, configured.store, run_id)
    request = derive_feature_gate_request(
        recorded.run_id,
        recorded.assessment,
        recorded.policy_revision,
        recorded.allowed_actor,
        recorded.candidate_ids,
    )
    # All three hashes are the request hash, as for the replay gate: this gate's
    # subject is one derived object.
    return DerivedSubject(
        run_id=request.run_id,
        gate_id=request.gate_id,
        subject_sha256=request.sha256,
        admission_sha256=request.sha256,
        prepared_sha256=request.sha256,
        policy_revision=request.policy_revision,
        allowed_actor=request.allowed_actor,
    )


def _feature_rulings(detail: object, candidates: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Read the human's rulings, keyed by candidate, refusing anything unexpected.

    **Iterates the derived candidates and looks each one up**, never the other
    way round. That direction is the whole safety property: a file that renames,
    drops or invents a candidate is refused *by name* before anything is signed,
    and the emission order is the request's rather than the file's, so the
    document's digest cannot depend on how a human happened to order their
    editor. `feature_gate.validate_submission` never re-reads the assessment
    blob, so this is where a ruling about a document nobody read gets stopped.
    """
    from .feature_gate import VERDICTS as CANDIDATE_VERDICTS

    if not isinstance(detail, dict):
        raise SubmissionRefused(
            "This gate needs a ruling for every candidate: pass --rulings with a JSON "
            "object mapping each candidate id to {\"verdict\": …, \"rationale\": …}"
        )
    unknown = sorted(set(detail) - set(candidates))
    if unknown:
        raise SubmissionRefused(
            f"Rulings name a candidate this gate does not cover: {unknown[0]}"
        )
    out: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        entry = detail.get(candidate)
        if entry is None:
            raise SubmissionRefused(f"No ruling for candidate {candidate}")
        if not isinstance(entry, dict):
            raise SubmissionRefused(f"Ruling for {candidate} must be an object")
        verdict = entry.get("verdict")
        rationale = entry.get("rationale", "")
        if type(verdict) is not str or type(rationale) is not str:
            raise SubmissionRefused(f"Ruling for {candidate} needs a verdict and a rationale")
        # Checked here, by name, rather than left to the contract. A candidate
        # verdict is NOT the gate's own vocabulary — `approve/reject/defer` is
        # what `--verdict` takes and `block/offer_toggle/ignore/defer` is what a
        # candidate takes — and the two appear on the same command line, so
        # reaching for the wrong one is the ordinary mistake rather than an
        # exotic one. `FeatureDispositionV1` would refuse it too, with a message
        # naming no candidate, through an exception the CLI does not catch.
        if verdict not in CANDIDATE_VERDICTS:
            raise SubmissionRefused(
                f"Ruling for {candidate} has verdict {verdict!r}; a candidate verdict is "
                f"one of {', '.join(CANDIDATE_VERDICTS)}. Note these are not the gate's own "
                f"{', '.join(VERDICTS)} — the gate says whether you answered, each candidate "
                "says what to do about it."
            )
        if verdict != "ignore" and not rationale.strip():
            # The template ships blank rationales on purpose, so this is the
            # refusal a human meets most often. It has to say what to type.
            raise SubmissionRefused(
                f"Ruling for {candidate} has verdict {verdict!r} and no rationale. Only "
                "'ignore' may be silent; every other verdict changes what the app does and "
                "has to say why."
            )
        out[candidate] = (verdict, rationale)
    return out


def _feature_assessment_payload(
    pending: "PendingGate", decision: GateDecision, answer: Answer
) -> object:
    """Build the dispositions document, publish it, and wrap it with the decision.

    The client writes bytes to CAS here, and that is deliberate rather than
    incidental: CAS is not authority. `put_blob` touches no ledger table, an
    `ArtifactRef` acquires provenance only when `record_effect` binds it to an
    operation key, and every read re-verifies digest, size, mode, owner and
    inode. So the write grants availability and never meaning — and the client
    still makes no ledger write at all.

    The last line is the point: the client runs the *admitting side's own*
    validator over its own submission. If it cannot admit its own answer it
    refuses here, rather than making a human's decision fail at a worker where
    they cannot see why.
    """

    from . import activities, assessment_record
    from .feature_gate import (
        DISPOSITIONS_ARTIFACT_KIND,
        FeatureDispositionsV1,
        FeatureDispositionV1,
        FeatureGateSubmissionV1,
        derive_feature_gate_request,
        validate_submission,
    )

    configured = activities.runtime()
    recorded = assessment_record.resolve_with(
        configured.ledger, configured.store, pending.derived.run_id
    )
    request = derive_feature_gate_request(
        recorded.run_id,
        recorded.assessment,
        recorded.policy_revision,
        recorded.allowed_actor,
        recorded.candidate_ids,
    )
    rulings = _feature_rulings(answer.detail, recorded.candidate_ids)
    try:
        document = FeatureDispositionsV1(
            1,
            recorded.assessment.sha256,
            recorded.policy_revision,
            tuple(
                FeatureDispositionV1(1, candidate, *rulings[candidate])
                for candidate in recorded.candidate_ids
            ),
        )
    except (TypeError, ValueError) as error:
        # Every refusal out of this client is a `SubmissionRefused`, because the
        # CLI turns that into "refused: …" and exit 2 while anything else is a
        # traceback — and a gate client that teaches whoever is on call to skim
        # tracebacks has undone the reason it exists. `_feature_rulings` catches
        # the reachable cases by name; this is the backstop for the rest.
        raise SubmissionRefused(f"These rulings are not a valid answer: {error}") from error
    body = canonical_json(document).encode("utf-8")
    reference = configured.store.put_bytes(
        kind=DISPOSITIONS_ARTIFACT_KIND,
        data=body,
        # Prefixed, so it structurally cannot collide with a real operation key
        # (which is a bare 64-hex digest), and a pure function of the document,
        # so a resubmission of the same rulings mints the same ref.
        producer_operation_id=f"client-{document.sha256}",
        input_hashes=(recorded.assessment.sha256,),
    )
    try:
        submission = FeatureGateSubmissionV1(1, decision, reference)
        validate_submission(request, submission, document)
    except (TypeError, ValueError) as error:
        raise SubmissionRefused(
            f"This client cannot admit its own answer, so it will not send it: {error}"
        ) from error
    return submission


FEATURE_ASSESSMENT_GATE = GateKind(
    name="feature-assessment",
    update_name="submit_feature_dispositions",
    matches=lambda gate_id, run_id: gate_id == f"{run_id}-feature-assessment-gate",
    resolve=_resolve_feature_assessment,
    payload=_feature_assessment_payload,
)


REPLAY_VERIFICATION_GATE = GateKind(
    name="replay-final-verification",
    update_name="submit_verification_decision",
    matches=lambda gate_id, run_id: gate_id == f"{run_id}-final-verification-gate",
    resolve=_resolve_replay_verification,
)

#: Registered gate kinds, in match order.
#:
#: `PortRunWorkflow`'s `phase-a-approval` is deliberately absent. Its subject is
#: `canonical_sha256(spec)` plus two operation outputs, and the ledger indexes
#: operations by content hash rather than by run, so a client holding only a run
#: id cannot reach them. Registering it with a weaker check -- or with none --
#: would mean a human signing hashes nobody reproduced, which is the one thing
#: this module refuses. It is answerable once its subject is reproducible.
#:
#: The feature gate joined this tuple only once its subject *became* reproducible:
#: `recorded_assessments_v1` keys a recorded assessment by run, so a client
#: holding a run id can reach the operation, load the exact `ArtifactRef` and
#: read the candidate ids out of the pinned bytes. Registering it before that
#: existed would have been the `phase-a-approval` mistake with a different name.
#:
#: The hook-retirement and reversal gates were the third and fourth members, and
#: both were removed on 2026-08-08 with the whole decision-correction layer.
#: Neither had ever been answered. Their absence is not the `phase-a-approval`
#: case — there is nothing left to make reproducible.
GATE_KINDS: tuple[GateKind, ...] = (
    REPLAY_VERIFICATION_GATE,
    FEATURE_ASSESSMENT_GATE,
)


def select_gate_kind(
    gate_id: str, run_id: str, kinds: tuple[GateKind, ...] = GATE_KINDS
) -> GateKind:
    """Pick the resolver for a gate, or refuse.

    `kinds` is a parameter rather than a fixed lookup so a caller can register a
    gate kind without editing this module, and so tests can drive the client
    against a Workflow whose subject is not reproducible from a test ledger. It
    grants nothing: a caller who can supply a resolver could equally construct a
    decision by hand, and the resolver *is* the derivation.
    """

    for kind in kinds:
        if kind.matches(gate_id, run_id):
            return kind
    raise SubmissionRefused(
        f"No resolver is registered for gate {gate_id!r}. This client will not "
        "submit a decision whose subject it cannot independently reproduce."
    )


# ------------------------------------------------------------------- the client


@dataclass(frozen=True, slots=True)
class PendingGate:
    """A gate that has been read, reproduced and checked. Ready to be answered."""

    workflow_id: str
    kind: GateKind
    published: GateRequest
    derived: DerivedSubject


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    decision_id: str
    accepted: bool
    resubmitted: bool
    journal: Path


async def read_pending_gate(
    client: Any,
    workflow_id: str,
    *,
    now: datetime,
    kinds: tuple[GateKind, ...] = GATE_KINDS,
) -> PendingGate:
    """Query the Workflow, reproduce its subject, and refuse on any disagreement.

    Queried by name, with no workflow class imported: the client is a different
    program from the worker and should not have to hold the Workflow definitions
    to answer a gate. That also means the reply arrives as plain JSON and is
    decoded field by field, which is what `gate_request_from_dict` is for.
    """

    _identifier(workflow_id, "workflow id")
    handle = client.get_workflow_handle(workflow_id)
    status = await handle.query("status")
    if type(status) is not dict:
        raise SubmissionRefused("Workflow status query did not return an object")
    if status.get("gate") is None:
        state = status.get("state")
        raise SubmissionRefused(f"Workflow {workflow_id} has no open gate (state {state!r})")
    published = gate_request_from_dict(status["gate"])
    if status.get("decision_id") is not None:
        raise SubmissionRefused(
            f"Workflow {workflow_id} already recorded decision {status['decision_id']!r}"
        )
    kind = select_gate_kind(published.gate_id, published.run_id, kinds)
    derived = kind.resolve(published.run_id)
    verify_published_gate(published, derived, now=now)
    return PendingGate(workflow_id, kind, published, derived)


def check_confirmation(pending: PendingGate, confirmation: str) -> None:
    """Require the human to quote the subject hash the CLIENT derived.

    Not a security control -- the subject is already verified. It is the
    difference between a human having read what they are approving and a human
    having approved whatever was pending, which is the same distinction the
    feature gate draws when it refuses to let a missing disposition mean
    `ignore`. It quotes the derived hash rather than the published one so that
    what a human confirms is what the client vouched for.
    """

    if type(confirmation) is not str:
        raise SubmissionRefused("Confirmation must be a string")
    confirmation = confirmation.strip().lower()
    if len(confirmation) < CONFIRMATION_LENGTH:
        raise SubmissionRefused(
            f"Confirmation must be at least {CONFIRMATION_LENGTH} characters of the "
            "derived subject hash"
        )
    if not pending.derived.subject_sha256.startswith(confirmation):
        raise SubmissionRefused(
            "Confirmation does not match the derived subject hash "
            f"{pending.derived.subject_sha256}"
        )


async def submit_answer(
    client: Any,
    pending: PendingGate,
    principal: Principal,
    answer: Answer,
    *,
    journal_root: Path,
    issued_at: datetime,
) -> SubmissionOutcome:
    """Assemble, journal and submit. The only method that changes anything.

    The update id is the decision's own idempotency id, which makes a retry a
    server-side no-op that returns the original receipt rather than a second
    decision the validator has to reject.
    """

    if type(pending) is not PendingGate:
        raise SubmissionRefused("Pending gate must be an exact PendingGate")
    recorded = read_journal(
        journal_root, pending.workflow_id, pending.derived.gate_id, pending.derived.subject_sha256
    )
    resubmitted = recorded is not None
    if recorded is not None:
        # Resubmit the recorded bytes verbatim. Re-assembling would re-timestamp,
        # and a decision whose `issued_at` moved is a different decision.
        #
        # But verbatim only after the recorded decision is re-authorized against
        # the gate that is actually pending. `read_journal` decodes; it does not
        # decide. Every field the gate binds is compared here, so the guarantee
        # is local rather than an argument about which hash happens to cover
        # which field in one gate kind -- an argument that would silently stop
        # holding the first time a second kind is registered.
        _require_recorded_decision_matches(
            recorded.decision, pending, principal, answer, journal_root
        )
        decision = recorded.decision
        payload = pending.kind.payload(pending, decision, answer)
        _require_recorded_payload_matches(recorded, payload, pending, journal_root)
        journal = journal_path(journal_root, pending.workflow_id, pending.derived.gate_id)
    else:
        decision = assemble_decision(pending.derived, principal, answer, issued_at)
        payload = pending.kind.payload(pending, decision, answer)
        journal = write_journal(
            journal_root,
            pending.workflow_id,
            pending.derived.gate_id,
            decision,
            _payload_digest(payload, decision),
        )

    handle = client.get_workflow_handle(pending.workflow_id)
    # The update id covers the PAYLOAD, not just the decision. Two different
    # dispositions documents under one decision would otherwise share an id:
    # Temporal returns the first receipt, the second document is dropped, and
    # this client prints `accepted True`. For a gate whose payload is the
    # decision the digest is the decision's own, so the replay gate's dedupe
    # property is unchanged.
    digest = _payload_digest(payload, decision)
    update_id = (
        decision.idempotency_id
        if digest is None
        else f"{IDEMPOTENCY_ID_PREFIX}{digest}"
    )
    receipt = await handle.execute_update(pending.kind.update_name, payload, id=update_id)
    # Submitted by name, so the receipt arrives as plain JSON. `bool(receipt)` on
    # an object would be True for any object at all, including a rejection.
    if type(receipt) is dict:
        accepted = receipt.get("accepted") is True
    else:
        accepted = getattr(receipt, "accepted", False) is True
    return SubmissionOutcome(decision.decision_id, accepted, resubmitted, journal)


def _require_recorded_decision_matches(
    recorded: GateDecision,
    pending: PendingGate,
    principal: Principal,
    answer: Answer,
    journal_root: Path,
) -> None:
    """Refuse to resubmit a journalled decision that is not the one being asked for.

    Two failures this closes, and both are reachable.

    A human who answered, crashed before the update landed, and re-runs with a
    *different* answer would otherwise have their first answer submitted
    silently. Their second answer is the one they meant.

    And a journal file is an ordinary file. If anything else could write it --
    which `write_journal` now refuses to allow, but only for the directory it
    creates -- a planted entry naming the right actor and the right subject
    would be resubmitted with words nobody typed. Comparing the verdict and the
    rationale against what the caller actually supplied makes the journal a
    cache, never a source.
    """

    derived = pending.derived
    for label, recorded_value, expected in (
        ("actor", recorded.actor, principal.actor),
        ("run", recorded.run_id, derived.run_id),
        ("gate", recorded.gate_id, derived.gate_id),
        ("subject hash", recorded.subject_sha256, derived.subject_sha256),
        ("admission hash", recorded.admission_sha256, derived.admission_sha256),
        ("prepared hash", recorded.prepared_sha256, derived.prepared_sha256),
        ("policy revision", recorded.policy_revision, derived.policy_revision),
        ("verdict", recorded.decision, answer.verdict),
        ("rationale", recorded.rationale, answer.rationale),
    ):
        if recorded_value != expected:
            path = journal_path(journal_root, pending.workflow_id, derived.gate_id)
            raise SubmissionRefused(
                f"Journalled decision {label} {recorded_value!r} is not the {label} "
                f"{expected!r} being submitted. Delete {path} only if you are sure "
                "the recorded decision never reached the workflow."
            )


# ----------------------------------------------------------------------- the CLI


def describe(pending: PendingGate) -> str:
    """Render what the client verified, for a human to read before deciding."""

    derived = pending.derived
    return "\n".join(
        (
            f"workflow        {pending.workflow_id}",
            f"gate            {derived.gate_id}  ({pending.kind.name})",
            f"run             {derived.run_id}",
            f"policy          {derived.policy_revision}",
            f"answerable by   {derived.allowed_actor}",
            f"issued          {pending.published.issued_at}",
            f"expires         {pending.published.expires_at}",
            "",
            "subject hash, independently derived from the ledger and matching",
            "the hash the workflow published:",
            f"    {derived.subject_sha256}",
            "",
            f"to answer, pass --confirm {derived.subject_sha256[:CONFIRMATION_LENGTH]}",
        )
    )


def _recorded_document(pending: PendingGate) -> str:
    """The assessment bytes this gate pins, fetched by the ref the client derived.

    Fetched by ref rather than by a path the caller names: a `--assessment-file`
    flag would let a human read one document and rule on another, and nothing
    downstream would catch it because `validate_submission` never re-reads the
    blob.
    """
    from . import activities, assessment_record

    configured = activities.runtime()
    recorded = assessment_record.resolve_with(
        configured.ledger, configured.store, pending.derived.run_id
    )
    return json.dumps(recorded.document, indent=2, sort_keys=True)


def _rulings_template(pending: PendingGate) -> str:
    """A skeleton whose candidate ids are the derived ones.

    So a hand-edited file that renames, drops or invents a candidate is refused
    by name before anything is signed, rather than quietly ruling on a set the
    pinned bytes do not contain.

    **Invalid as emitted, deliberately.** Every verdict but `ignore` needs a
    rationale, so the unedited template is refused and a human cannot answer this
    gate without typing something for each candidate. A template that submitted
    cleanly as-is would let someone approve four rulings they never made — which
    is the same failure as a client copying the Workflow's hashes, one level down.
    """
    from . import activities, assessment_record

    configured = activities.runtime()
    recorded = assessment_record.resolve_with(
        configured.ledger, configured.store, pending.derived.run_id
    )
    return json.dumps(
        {
            candidate: {"verdict": "defer", "rationale": ""}
            for candidate in recorded.candidate_ids
        },
        indent=2,
    )


def _read_rulings(path: Path | None) -> object | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionRefused(f"Rulings file is not readable JSON: {path}") from error
    if not isinstance(data, dict):
        raise SubmissionRefused(f"Rulings file must hold a JSON object: {path}")
    return data


async def _run(arguments: argparse.Namespace) -> int:
    from temporalio.client import Client

    from . import activities

    principal = load_principal(arguments.principal)
    # Read-only from the first line: the client reaches the ledger only to check
    # what is already recorded there.
    #
    # Pointing `--state-root` at the wrong directory is the most ordinary
    # mistake there is, and a traceback for it teaches whoever is on call to
    # skim tracebacks -- which is the last habit anyone answering a gate should
    # have.
    try:
        activities.configure_runtime(arguments.state_root, read_only=True)
    except FileNotFoundError as error:
        raise SubmissionRefused(
            f"No ledger under --state-root {arguments.state_root}. This client reads "
            "the ledger to reproduce the gate subject, so it cannot answer anything "
            "without one."
        ) from error
    try:
        client = await Client.connect(
            arguments.endpoint, identity=f"dfinsta-submission:{principal.actor}"
        )
    except (OSError, RuntimeError) as error:
        raise SubmissionRefused(
            f"Cannot reach Temporal at {arguments.endpoint}: {error}"
        ) from error
    now = datetime.now(timezone.utc)
    pending = await read_pending_gate(client, arguments.workflow_id, now=now)
    print(describe(pending))
    if arguments.command == "show":
        if getattr(arguments, "assessment", False):
            print()
            print(_recorded_document(pending))
        if getattr(arguments, "rulings_template", False):
            print()
            print(_rulings_template(pending))
        return 0

    # After `describe`, so a human who typed the wrong confirmation sees the
    # right one in the same output rather than having to run `show` again.
    check_confirmation(pending, arguments.confirm)
    answer = Answer(arguments.verdict, arguments.rationale, _read_rulings(arguments.rulings))
    outcome = await submit_answer(
        client,
        pending,
        principal,
        answer,
        journal_root=arguments.journal or arguments.state_root / "submissions",
        issued_at=now,
    )
    print()
    print(f"decision        {outcome.decision_id}")
    print(f"accepted        {outcome.accepted}")
    print(f"journal         {outcome.journal}")
    if outcome.resubmitted:
        print("note            resubmitted the decision already recorded in the journal")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="localhost:7233")
    parser.add_argument("--state-root", type=Path, default=Path(".pipeline-state"))
    parser.add_argument(
        "--principal",
        type=Path,
        default=Path.home() / ".config" / "dfinsta" / "principal.json",
        help="0600 file, owned by this uid, naming the actor",
    )
    parser.add_argument(
        "--journal", type=Path, default=None, help="default: <state-root>/submissions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="reproduce and display the pending gate")
    show.add_argument("workflow_id")
    show.add_argument(
        "--assessment",
        action="store_true",
        help="print the recorded assessment document this gate pins. Confirming a "
        "hash over evidence you have not read is not a decision.",
    )
    show.add_argument(
        "--rulings-template",
        action="store_true",
        help="print a rulings skeleton whose candidate ids are the DERIVED ones",
    )

    submit = subparsers.add_parser("submit", help="answer the pending gate")
    submit.add_argument("workflow_id")
    submit.add_argument("--verdict", choices=VERDICTS, required=True)
    submit.add_argument("--rationale", required=True)
    submit.add_argument(
        "--rulings",
        type=Path,
        help="JSON object mapping each candidate id to {verdict, rationale}. "
        "Required by gates that rule per candidate; refused by gates that do not.",
    )
    submit.add_argument(
        "--confirm",
        required=True,
        help=f"at least {CONFIRMATION_LENGTH} characters of the derived subject hash",
    )

    from temporalio.client import WorkflowUpdateFailedError

    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(_run(arguments))
    except SubmissionRefused as error:
        parser.exit(2, f"refused: {error}\n")
    except WorkflowUpdateFailedError as error:
        # The workflow validator refused. Distinguished from the client's own
        # refusal because the two mean different things: this one says the
        # decision reached the workflow and was rejected there, which a human
        # deciding what to do next needs to know.
        parser.exit(3, f"the workflow rejected the decision: {error.cause}\n")


if __name__ == "__main__":
    raise SystemExit(main())
