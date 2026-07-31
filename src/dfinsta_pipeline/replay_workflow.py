from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from .activities import (
        admit_replay_verification_grant_activity,
        prepare_replay_plan_activity,
        prepare_replay_verification_gate_activity,
        replay_apply_tree_stage_activity,
        replay_build_patched_apk_stage_activity,
        replay_decode_stage_activity,
        replay_install_frameworks_stage_activity,
        replay_verify_final_apk_stage_activity,
    )
    from .contracts import ArtifactRef, GateDecision, GateReceipt, GateRequest, WorkflowStatus
    from .replay_contracts import (
        AdmittedReplayHandleV1,
        ReplayRunRequestV1,
        ReplayRunResultV1,
        ReplayVerificationAdmissionV1,
        ReplayVerificationGateV1,
    )


# Keyed by capability role, never by target. The Workflow does not decide which
# stages run: prepare_replay_plan_activity derives that from admitted authority.
_STAGE_ACTIVITIES = {
    "install_framework": replay_install_frameworks_stage_activity,
    "decode": replay_decode_stage_activity,
    "apply": replay_apply_tree_stage_activity,
    "build": replay_build_patched_apk_stage_activity,
}

# maximum_attempts=2 is deliberate and 1 would be wrong. Ledger adoption only
# fires on a second attempt: begin_operation returns an existing artifact just
# when a prior attempt already reached effect or completed. With one attempt a
# worker lost after record_effect fails the run outright and the proven adoption
# path is unreachable. Every other second-attempt outcome fails closed, so no
# path can produce two effects.
#
# The non-retryable list keeps a genuine fault visible: without it, attempt two
# reports "Operation is quarantined" and hides the real error from attempt one.
_STAGE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=1.0,
    maximum_attempts=2,
    non_retryable_error_types=[
        "ValueError",
        "TypeError",
        "RuntimeError",
        "OSError",
        "FileExistsError",
        "AssertionError",
    ],
)

# Ledger-only work: no subprocess, no workspace, safe to retry more freely.
_LEDGER_RETRY = RetryPolicy(maximum_attempts=3)
_LEDGER_TIMEOUT = timedelta(seconds=120)


@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class ReplayRunWorkflow:
    """Sequences the admitted replay chain and hosts the final-verification gate.

    This is a separate definition from PortRunWorkflow on purpose. Extending that
    workflow would insert commands into a command stream that saved Phase A
    Histories already recorded, so every completed history would fail replay.
    Keeping the chain in its own definition makes Phase A compatibility trivially
    true rather than argued.

    The Workflow holds no authority. Admission already happened before it starts,
    the ledger validates every stage, and each stage re-derives its predecessors.
    What this contributes is ordering and human consent.

    No heartbeats: none of the underlying checkpoint Activities heartbeat, so
    worker loss is only detected at start_to_close expiry.
    """

    def __init__(self) -> None:
        self._handle: AdmittedReplayHandleV1 | None = None
        self._gate: ReplayVerificationGateV1 | None = None
        self._gate_request: GateRequest | None = None
        self._decision: GateDecision | None = None
        self._decision_ids: set[str] = set()
        self._idempotency_ids: set[str] = set()
        self._stages_completed: list[str] = []
        self._state = "created"

    @workflow.run
    async def run(self, request: ReplayRunRequestV1) -> ReplayRunResultV1:
        self._handle = request.handle
        run_id = request.handle.run_id

        self._state = "planning"
        plan = await workflow.execute_activity(
            prepare_replay_plan_activity,
            request.handle,
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_LEDGER_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        for stage, budget in zip(plan.stages, plan.stage_budget_seconds):
            if stage not in _STAGE_ACTIVITIES:
                continue
            self._state = f"running-{stage}"
            await workflow.execute_activity(
                _STAGE_ACTIVITIES[stage],
                request.handle,
                start_to_close_timeout=timedelta(seconds=budget),
                retry_policy=_STAGE_RETRY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            self._stages_completed.append(stage)

        self._state = "preparing-verification-gate"
        self._gate = await workflow.execute_activity(
            prepare_replay_verification_gate_activity,
            request.handle,
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_LEDGER_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        issued_at = workflow.now()
        expires_at = issued_at + timedelta(seconds=request.verification_gate_timeout_seconds)
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

        self._state = "awaiting-verification-approval"
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=request.verification_gate_timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._state = "blocked"
            return self._result(run_id, "blocked", None, None)

        assert self._decision is not None
        if self._decision.decision != "approve":
            state = "rejected" if self._decision.decision == "reject" else "deferred"
            self._state = state
            return self._result(run_id, state, None, self._decision.decision_id)

        self._state = "admitting-verification-grant"
        grant_handle = await workflow.execute_activity(
            admit_replay_verification_grant_activity,
            ReplayVerificationAdmissionV1(1, request.handle, self._decision),
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_LEDGER_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        self._state = "running-verify"
        verify_budget = plan.stage_budget_seconds[plan.stages.index("verify")]
        verification = await workflow.execute_activity(
            replay_verify_final_apk_stage_activity,
            grant_handle,
            start_to_close_timeout=timedelta(seconds=verify_budget),
            retry_policy=_STAGE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._stages_completed.append("verify")
        self._state = "completed"
        return self._result(run_id, "completed", verification, self._decision.decision_id)

    def _result(
        self,
        run_id: str,
        state: str,
        verification: ArtifactRef | None,
        decision_id: str | None,
    ) -> ReplayRunResultV1:
        return ReplayRunResultV1(
            1, run_id, state, tuple(self._stages_completed), verification, decision_id
        )

    @workflow.update
    def submit_verification_decision(self, decision: GateDecision) -> GateReceipt:
        self._decision = decision
        self._decision_ids.add(decision.decision_id)
        self._idempotency_ids.add(decision.idempotency_id)
        return GateReceipt(decision.decision_id, True)

    @submit_verification_decision.validator
    def validate_submit_verification_decision(self, decision: GateDecision) -> None:
        if (
            self._gate is None
            or self._gate_request is None
            or self._state != "awaiting-verification-approval"
        ):
            raise ValueError("Workflow is not awaiting a verification decision")
        if decision.actor != self._gate.allowed_actor:
            raise ValueError("Decision actor is not authorized")
        # The grant contract requires all three hash fields to bind the request,
        # so the validator enforces the same shape before the Activity sees it.
        if (
            decision.run_id != self._gate_request.run_id
            or decision.gate_id != self._gate_request.gate_id
            or decision.subject_sha256 != self._gate_request.subject_sha256
            or decision.admission_sha256 != self._gate_request.subject_sha256
            or decision.prepared_sha256 != self._gate_request.subject_sha256
            or decision.policy_revision != self._gate_request.policy_revision
        ):
            raise ValueError("Decision does not match the pending verification gate")
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
            raise ValueError("Verification gate has expired")
        if (
            decision_time < gate_time
            or decision_time >= expiry_time
            or decision_time > current_time + timedelta(minutes=5)
        ):
            raise ValueError("Decision timestamp is outside the gate validity period")
        # Shared across both gates in this run so an admission decision id can
        # never be replayed into the verification gate.
        if (
            decision.decision_id in self._decision_ids
            or decision.idempotency_id in self._idempotency_ids
            or self._decision is not None
        ):
            raise ValueError("Decision was already submitted")

    @workflow.query
    def status(self) -> WorkflowStatus:
        return WorkflowStatus(
            state=self._state,
            gate=self._gate_request,
            decision_id=self._decision.decision_id if self._decision else None,
        )
