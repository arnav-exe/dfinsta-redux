"""The Workflow that raises the feature-assessment gate.

Everything around this gate existed before it did. The assessment is recorded as
a ledger operation under a run-keyed authority row, the subject re-derives from a
run id alone, and `submission.py` can answer it with a ruling per candidate — and
**nothing raised it**, so the whole chain was reachable only by hand.

A separate `@workflow.defn` rather than another branch of `PortRunWorkflow`, for
the reason `ReplayRunWorkflow` is separate: extending an existing definition
inserts commands into a command stream that saved Histories already recorded, so
every completed history would fail replay. Keeping this in its own definition
makes that compatibility trivially true rather than argued.

===============================================================================
  WHY THE VALIDATOR IS NOT WHERE THE ANSWER IS CHECKED
===============================================================================

A Workflow update validator runs in the sandbox: no I/O, no clock but
`workflow.now()`, no ledger, no content store. It can check the *shape* of what
arrived — that the decision binds this gate, that the actor is the one the gate
allows, that the timestamps sit inside the window, that nothing was submitted
twice, that the dispositions reference is a dispositions reference. It cannot
read the document those rulings live in, so it cannot check that they rule on the
candidates the assessment actually names.

So the validator is a **filter** and `admit_feature_dispositions_activity` is the
**authority**. The Activity re-derives the request from the ledger rather than
taking the Workflow's copy, fetches the document by the reference the human
signed (which re-verifies its digest and size on read), and runs
`feature_gate.validate_submission` over the three together. Putting that check in
the validator would mean either doing I/O in the sandbox or trusting a body
carried through History — and a body in History is human rationales in a
replayable log forever.

One consequence worth stating plainly: **a submission that passes the validator
can still be refused by the Activity**, and that is the design rather than a gap.
The client runs the same validator over its own submission before sending, so the
case should not arise from the supported path; when it does, it fails closed and
non-retryably rather than admitting rulings nobody checked.
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
        admit_feature_dispositions_activity,
        prepare_feature_gate_activity,
    )
    from .contracts import ArtifactRef, GateReceipt, GateRequest, WorkflowStatus
    from .feature_gate import (
        DISPOSITIONS_ARTIFACT_KIND,
        FeatureAssessmentGateV1,
        FeatureDispositionsAdmissionV1,
        FeatureGateSubmissionV1,
        FeatureRunRequestV1,
        FeatureRunResultV1,
    )


# Ledger-and-CAS work only: no subprocess, no workspace, no decode. The same
# budget the replay chain gives its ledger-only Activities.
_GATE_RETRY = RetryPolicy(maximum_attempts=3)
_GATE_TIMEOUT = timedelta(seconds=120)

#: How far a decision's own timestamp may sit ahead of the Workflow's clock. Not
#: zero, because the client stamps `issued_at` before the round trip and the two
#: clocks are different machines; not large, because the point of the check is
#: that a decision cannot be pre-dated into a window it was not issued in.
_CLOCK_SKEW = timedelta(minutes=5)


@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class FeatureAssessmentRunWorkflow:
    """Raises the feature-assessment gate and admits the rulings it receives.

    Holds no authority. The assessment was recorded before this started, the
    preparing Activity derives the subject from the ledger, and the admitting
    Activity derives it again. What this contributes is *ordering and human
    consent* — the same division `ReplayRunWorkflow` draws.

    No heartbeats: neither Activity here heartbeats, and both are ledger-and-CAS
    work bounded at two minutes, so worker loss is detected at `start_to_close`
    rather than three hours later. That is a real limitation and a small one at
    this budget; it is the same open item (F4) the replay chain carries.
    """

    def __init__(self) -> None:
        self._gate: FeatureAssessmentGateV1 | None = None
        self._gate_request: GateRequest | None = None
        self._submission: FeatureGateSubmissionV1 | None = None
        self._decision_ids: set[str] = set()
        self._idempotency_ids: set[str] = set()
        self._state = "created"

    @workflow.run
    async def run(self, request: FeatureRunRequestV1) -> FeatureRunResultV1:
        run_id = request.run_id

        self._state = "preparing-feature-gate"
        self._gate = await workflow.execute_activity(
            prepare_feature_gate_activity,
            run_id,
            start_to_close_timeout=_GATE_TIMEOUT,
            retry_policy=_GATE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        # The gate Activity is reached by run id and derives from a row keyed by
        # it, so a mismatch here would mean the ledger disagrees with itself.
        # Checked anyway, and outside the validator: constructing a contract that
        # raises inside Workflow code is a workflow *task* failure, which Temporal
        # retries forever — the run would wedge rather than fail.
        if self._gate.run_id != run_id:
            raise ApplicationError(
                "Feature gate does not bind the requested run",
                type="FeatureGateRunMismatch",
                non_retryable=True,
            )

        issued_at = workflow.now()
        expires_at = issued_at + timedelta(seconds=request.gate_timeout_seconds)
        # All three hash fields are the request hash. This gate's subject is one
        # derived object, `submission.py` compares all three against what it
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

        self._state = "awaiting-feature-dispositions"
        try:
            await workflow.wait_condition(
                lambda: self._submission is not None,
                timeout=timedelta(seconds=request.gate_timeout_seconds),
            )
        except asyncio.TimeoutError:
            # `blocked`, not `rejected`. Nobody decided anything; the gate simply
            # went unanswered, and a run recorded as rejected would say a human
            # looked at this and said no.
            self._state = "blocked"
            return self._result(run_id, "blocked", None, None)

        assert self._submission is not None
        decision = self._submission.decision
        if decision.decision != "approve":
            state = "rejected" if decision.decision == "reject" else "deferred"
            self._state = state
            return self._result(run_id, state, decision.decision_id, None)

        self._state = "admitting-feature-dispositions"
        dispositions = await workflow.execute_activity(
            admit_feature_dispositions_activity,
            FeatureDispositionsAdmissionV1(1, run_id, self._submission),
            start_to_close_timeout=_GATE_TIMEOUT,
            retry_policy=_GATE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._state = "completed"
        return self._result(run_id, "completed", decision.decision_id, dispositions)

    def _result(
        self,
        run_id: str,
        state: str,
        decision_id: str | None,
        dispositions: ArtifactRef | None,
    ) -> FeatureRunResultV1:
        return FeatureRunResultV1(1, run_id, state, decision_id, dispositions)

    @workflow.update
    def submit_feature_dispositions(
        self, submission: FeatureGateSubmissionV1
    ) -> GateReceipt:
        self._submission = submission
        self._decision_ids.add(submission.decision.decision_id)
        self._idempotency_ids.add(submission.decision.idempotency_id)
        return GateReceipt(submission.decision.decision_id, True)

    @submit_feature_dispositions.validator
    def validate_submit_feature_dispositions(
        self, submission: FeatureGateSubmissionV1
    ) -> None:
        """Everything checkable without I/O. The Activity checks the rest.

        Deliberately does **not** try to check the rulings themselves. Reading the
        dispositions document needs the content store, which the sandbox does not
        have, and accepting the document as an argument instead would put human
        rationales into History permanently and make this the place that decides
        what was approved.
        """
        if (
            self._gate is None
            or self._gate_request is None
            or self._state != "awaiting-feature-dispositions"
        ):
            raise ValueError("Workflow is not awaiting feature dispositions")
        if type(submission) is not FeatureGateSubmissionV1:
            raise ValueError("Submission must be an exact FeatureGateSubmissionV1")
        # Checked here as well as in the contract: the contract's own message
        # names no gate, and a reference of the wrong kind is the one confusion
        # that would let an *assessment* be presented as a human's answer to it.
        if submission.dispositions.kind != DISPOSITIONS_ARTIFACT_KIND:
            raise ValueError("Submitted artifact is not a feature dispositions document")
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
            raise ValueError("Decision does not match the pending feature gate")
        try:
            decision_time = datetime.fromisoformat(decision.issued_at)
            gate_time = datetime.fromisoformat(self._gate_request.issued_at)
            expiry_time = datetime.fromisoformat(self._gate_request.expires_at)
        except (TypeError, ValueError) as error:
            raise ValueError("Decision timestamp is invalid") from error
        if decision_time.tzinfo is None or gate_time.tzinfo is None or expiry_time.tzinfo is None:
            raise ValueError("Decision timestamp requires a UTC offset")
        current_time = workflow.now()
        if current_time >= expiry_time:
            raise ValueError("Feature gate has expired")
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

        It queries `"status"` by name and requires `{state, gate, decision_id}`
        with `gate` a plain `GateRequest`. A gate whose Workflow published a
        different shape would be unanswerable by the one client that can answer
        it — answerable in a test and unanswerable in practice, which is the trap
        this project has now hit twice.
        """
        return WorkflowStatus(
            state=self._state,
            gate=self._gate_request,
            decision_id=(
                self._submission.decision.decision_id if self._submission else None
            ),
        )
