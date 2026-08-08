"""The Workflow that raises the reversal gate and waits, for days.

`reconsider.py` can already say which recorded decisions no longer match the
evidence, and `reversal.py` can already record a withdrawal at a command line.
What sits between them is nothing at all: the report is text somebody has to
remember to read, and the withdrawal is a command somebody has to remember to
type with the right `--original-decision-id`. That is the "complete and reached
by nothing" shape one step along from the gap the retirement gate closed, and
here it fails in the *dangerous* direction — an unread proposal leaves a block in
force that the evidence says is inert.

A separate `@workflow.defn` rather than a branch of an existing one, for the
reason `HookRetirementRunWorkflow` is separate: extending a definition inserts
commands into a command stream that saved Histories already recorded, so every
completed history would fail replay.

===============================================================================
  THIS IS NOT A GATE INSIDE A PORT
===============================================================================

Nothing about a port waits on this and no port is unblocked by it, exactly as
with the retirement gate. `replay_workflow` never references this class.

That is an incentive decision. If withdrawing a block could turn a red build
green, withdrawing one becomes the cheapest thing a tired person can do at the
end of a long port — and this gate's whole subject matter is the protections the
project has already decided it wants. A gate that can be answered *to make a
build pass* is a gate that will be.

===============================================================================
  THE VALIDATOR IS A FILTER; THE ACTIVITY IS THE AUTHORITY
===============================================================================

A Workflow update validator runs in the sandbox: no I/O, no ledger, no content
store, no clock but `workflow.now()`. It can check that a submission binds this
gate, that the actor is the one the gate allows, that the timestamps sit inside
the window and that nothing was submitted twice. It cannot read the rulings
document, so it cannot check that a human ruled on the decisions the docket
actually names, nor that each ruling answers the item it claims to.

So `admit_reversal_rulings_activity` re-derives the subject from the ledger,
fetches the document by the reference the human signed, and runs
`reversal_gate.validate_submission` over the three together — and that function
checks **everything this validator checks, and more**. When this project last
split a check into a cheap filter and a real authority, the authority checked
less, and "who may answer" came to rest entirely on the sandbox.

A submission that passes the validator can therefore still be refused by the
Activity. That is the design and not a gap: the client runs the same validator
over its own answer before sending, so the case should not arise from the
supported path, and when it does it fails closed and non-retryably.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from .activities import (
        admit_reversal_rulings_activity,
        prepare_reversal_gate_activity,
    )
    from .contracts import ArtifactRef, GateReceipt, GateRequest, WorkflowStatus
    from .reversal_gate import (
        RULINGS_ARTIFACT_KIND,
        ReversalGateSubmissionV1,
        ReversalGateV1,
        ReversalRulingsAdmissionV1,
        ReversalRunRequestV1,
        ReversalRunResultV1,
    )


# Ledger-and-CAS work only: no subprocess, no workspace, no decode.
_GATE_RETRY = RetryPolicy(maximum_attempts=3)
_GATE_TIMEOUT = timedelta(seconds=120)

#: How far a decision's own timestamp may sit ahead of this Workflow's clock. Not
#: zero, because the client stamps `issued_at` before the round trip and the two
#: clocks are different machines; not large, because the point is that a decision
#: cannot be pre-dated into a window it was not issued in.
_CLOCK_SKEW = timedelta(minutes=5)


@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class ReversalRunWorkflow:
    """Raises the reversal gate and admits the rulings it receives.

    Holds no authority. The docket was recorded before this started, the
    preparing Activity derives the subject from the ledger and the admitting
    Activity derives it again. What this contributes is *ordering and human
    consent*, and the ability to still be waiting on Thursday.

    `PINNED` is required rather than stylistic: the worker runs with
    `use_worker_versioning=True` and `default_versioning_behavior=UNSPECIFIED`,
    so it refuses to register an unpinned workflow.
    """

    def __init__(self) -> None:
        self._gate: ReversalGateV1 | None = None
        self._gate_request: GateRequest | None = None
        self._submission: ReversalGateSubmissionV1 | None = None
        self._decision_ids: set[str] = set()
        self._idempotency_ids: set[str] = set()
        self._state = "created"

    @workflow.run
    async def run(self, request: ReversalRunRequestV1) -> ReversalRunResultV1:
        run_id = request.run_id

        self._state = "preparing-reversal-gate"
        self._gate = await workflow.execute_activity(
            prepare_reversal_gate_activity,
            run_id,
            start_to_close_timeout=_GATE_TIMEOUT,
            retry_policy=_GATE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        # The Activity is reached by run id and derives from a row keyed by it, so
        # a mismatch here would mean the ledger disagrees with itself. Checked
        # anyway, and as an `ApplicationError`: a bare exception raised in Workflow
        # code is a workflow *task* failure, which Temporal retries forever, so the
        # run would wedge rather than fail.
        if self._gate.run_id != run_id:
            raise ApplicationError(
                "Reversal gate does not bind the requested run",
                type="ReversalGateRunMismatch",
                non_retryable=True,
            )

        issued_at = workflow.now()
        expires_at = issued_at + timedelta(seconds=request.gate_timeout_seconds)
        # All three hash fields carry the request hash. This gate's subject is one
        # derived object; `submission.py` compares all three against what it
        # re-derived, and `GateDecision` requires all three to be present.
        self._gate_request = GateRequest(
            schema_version=1,
            run_id=self._gate.run_id,
            gate_id=self._gate.gate_id,
            subject_sha256=self._gate.request_sha256,
            admission_sha256=self._gate.request_sha256,
            prepared_sha256=self._gate.request_sha256,
            policy_revision=self._gate.policy_revision,
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )

        self._state = "awaiting-reversal-rulings"
        try:
            await workflow.wait_condition(
                lambda: self._submission is not None,
                timeout=timedelta(seconds=request.gate_timeout_seconds),
            )
        except asyncio.TimeoutError:
            # `blocked`, never an implicit approval. Nobody decided anything, and
            # a run recorded as rejected would say a human looked at this and said
            # no. Here the distinction decides which way an unanswered question
            # fails: every questioned decision stays in force, which is the safe
            # direction for a block and the honest one for a retirement.
            self._state = "blocked"
            return self._result(run_id, "blocked", None, None)

        assert self._submission is not None
        decision = self._submission.decision
        if decision.decision != "approve":
            state = "rejected" if decision.decision == "reject" else "deferred"
            self._state = state
            return self._result(run_id, state, decision.decision_id, None)

        self._state = "admitting-reversal-rulings"
        rulings = await workflow.execute_activity(
            admit_reversal_rulings_activity,
            ReversalRulingsAdmissionV1(1, run_id, self._submission),
            start_to_close_timeout=_GATE_TIMEOUT,
            retry_policy=_GATE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._state = "completed"
        return self._result(run_id, "completed", decision.decision_id, rulings)

    def _result(
        self,
        run_id: str,
        state: str,
        decision_id: str | None,
        rulings: ArtifactRef | None,
    ) -> ReversalRunResultV1:
        return ReversalRunResultV1(1, run_id, state, decision_id, rulings)

    @workflow.update
    def submit_reversal_rulings(
        self, submission: ReversalGateSubmissionV1
    ) -> GateReceipt:
        self._submission = submission
        self._decision_ids.add(submission.decision.decision_id)
        self._idempotency_ids.add(submission.decision.idempotency_id)
        return GateReceipt(submission.decision.decision_id, True)

    @submit_reversal_rulings.validator
    def validate_submit_reversal_rulings(
        self, submission: ReversalGateSubmissionV1
    ) -> None:
        """Everything checkable without I/O. The Activity checks the rest, and more.

        Deliberately does not try to read the rulings. That needs the content
        store, which the sandbox does not have, and accepting the document as an
        argument instead would pin every human rationale into History for ever and
        make this the place that decides what was approved.
        """

        if (
            self._gate is None
            or self._gate_request is None
            or self._state != "awaiting-reversal-rulings"
        ):
            raise ValueError("Workflow is not awaiting reversal rulings")
        if type(submission) is not ReversalGateSubmissionV1:
            raise ValueError("Submission must be an exact ReversalGateSubmissionV1")
        # Checked here as well as in the contract: a reference of the wrong kind
        # is the one confusion that would let a *docket* be presented as a human's
        # answer to it.
        if submission.rulings.kind != RULINGS_ARTIFACT_KIND:
            raise ValueError("Submitted artifact is not a reversal rulings document")
        decision = submission.decision
        if decision.actor != self._gate.allowed_actor:
            raise ValueError("Decision actor is not authorized")
        if (
            decision.run_id != self._gate_request.run_id
            or decision.gate_id != self._gate_request.gate_id
            or decision.subject_sha256 != self._gate_request.subject_sha256
            or decision.admission_sha256 != self._gate_request.subject_sha256
            or decision.prepared_sha256 != self._gate_request.subject_sha256
            or decision.policy_revision != self._gate_request.policy_revision
        ):
            raise ValueError("Decision does not match the pending reversal gate")
        try:
            decision_time = datetime.fromisoformat(decision.issued_at)
            gate_time = datetime.fromisoformat(self._gate_request.issued_at)
            expiry_time = datetime.fromisoformat(self._gate_request.expires_at)
        except (TypeError, ValueError) as error:
            raise ValueError("Decision timestamp is invalid") from error
        if (
            decision_time.tzinfo is None
            or gate_time.tzinfo is None
            or expiry_time.tzinfo is None
        ):
            raise ValueError("Decision timestamp requires a UTC offset")
        current_time = workflow.now()
        if current_time >= expiry_time:
            raise ValueError("Reversal gate has expired")
        if (
            decision_time < gate_time
            or decision_time >= expiry_time
            or decision_time > current_time + _CLOCK_SKEW
        ):
            raise ValueError("Decision timestamp is outside the gate validity period")
        if (
            decision.decision_id in self._decision_ids
            or decision.idempotency_id in self._idempotency_ids
            or self._submission is not None
        ):
            raise ValueError("Decision was already submitted")

    @workflow.query
    def status(self) -> WorkflowStatus:
        """Exactly the shape `submission.read_pending_gate` expects.

        It queries `"status"` **by name** and requires `{state, gate, decision_id}`
        with `gate` a plain `GateRequest`. A gate whose Workflow published a
        different shape would be answerable in a unit test and unanswerable in
        production, which is the trap this project has hit twice.
        """

        return WorkflowStatus(
            state=self._state,
            gate=self._gate_request,
            decision_id=(
                self._submission.decision.decision_id if self._submission else None
            ),
        )
