from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.workflow import ActivityCancellationType
from temporalio.common import RetryPolicy, VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from .activities import admit_activity, apply_activity, prepare_activity, record_decision_activity
    from .contracts import (
        GateDecision,
        GateReceipt,
        GateRequest,
        RunResult,
        RunSpec,
        StageInput,
        WorkflowStatus,
        canonical_sha256,
    )


@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class PortRunWorkflow:
    def __init__(self) -> None:
        self._spec: RunSpec | None = None
        self._gate: GateRequest | None = None
        self._decision: GateDecision | None = None
        self._decision_ids: set[str] = set()
        self._idempotency_ids: set[str] = set()
        self._state = "created"

    @workflow.run
    async def run(self, spec: RunSpec) -> RunResult:
        self._spec = spec
        self._state = "admitting"
        admitted = await workflow.execute_activity(
            admit_activity,
            spec,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        self._state = "preparing"
        prepared = await workflow.execute_activity(
            prepare_activity,
            StageInput(1, spec, (admitted,)),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        issued_at = workflow.now()
        expires_at = issued_at + timedelta(seconds=spec.gate_timeout_seconds)
        self._gate = GateRequest(
            schema_version=1,
            run_id=spec.run_id,
            gate_id="phase-a-approval",
            subject_sha256=canonical_sha256(spec),
            admission_sha256=admitted.sha256,
            prepared_sha256=prepared.sha256,
            policy_revision=spec.policy_revision,
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        self._state = "awaiting-approval"
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=spec.gate_timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._state = "blocked"
            return RunResult(1, spec.run_id, "blocked", prepared, None, None)

        assert self._decision is not None
        await workflow.execute_activity(
            record_decision_activity,
            self._decision,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if self._decision.decision != "approve":
            state = "rejected" if self._decision.decision == "reject" else "deferred"
            self._state = state
            return RunResult(1, spec.run_id, state, prepared, None, self._decision.decision_id)

        self._state = "applying"
        output = await workflow.execute_activity(
            apply_activity,
            StageInput(1, spec, (admitted, prepared), self._decision),
            start_to_close_timeout=timedelta(seconds=max(30, spec.apply_delay_seconds + 10)),
            heartbeat_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._state = "completed"
        return RunResult(1, spec.run_id, "completed", prepared, output, self._decision.decision_id)

    @workflow.update
    def submit_decision(self, decision: GateDecision) -> GateReceipt:
        self._decision = decision
        self._decision_ids.add(decision.decision_id)
        self._idempotency_ids.add(decision.idempotency_id)
        return GateReceipt(decision.decision_id, True)

    @submit_decision.validator
    def validate_submit_decision(self, decision: GateDecision) -> None:
        if self._spec is None or self._gate is None or self._state != "awaiting-approval":
            raise ValueError("Workflow is not awaiting a decision")
        if decision.actor != self._spec.allowed_actor:
            raise ValueError("Decision actor is not authorized")
        if (
            decision.run_id != self._gate.run_id
            or decision.gate_id != self._gate.gate_id
            or decision.subject_sha256 != self._gate.subject_sha256
            or decision.admission_sha256 != self._gate.admission_sha256
            or decision.prepared_sha256 != self._gate.prepared_sha256
            or decision.policy_revision != self._gate.policy_revision
        ):
            raise ValueError("Decision does not match the pending gate")
        try:
            decision_time = datetime.fromisoformat(decision.issued_at)
            gate_time = datetime.fromisoformat(self._gate.issued_at)
            expiry_time = datetime.fromisoformat(self._gate.expires_at)
        except (TypeError, ValueError) as error:
            raise ValueError("Decision timestamp is invalid") from error
        if decision_time.tzinfo is None or gate_time.tzinfo is None or expiry_time.tzinfo is None:
            raise ValueError("Decision timestamp requires a UTC offset")
        current_time = workflow.now()
        if current_time >= expiry_time:
            raise ValueError("Gate has expired")
        if decision_time < gate_time or decision_time > min(
            expiry_time, current_time + timedelta(minutes=5)
        ):
            raise ValueError("Decision timestamp is outside the gate validity period")
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
            gate=self._gate,
            decision_id=self._decision.decision_id if self._decision else None,
        )
