from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from .activities import (
        admit_replay_verification_grant_activity,
        prepare_replay_plan_activity,
        prepare_replay_verification_gate_activity,
        resolve_replay_verification_grant_activity,
        replay_apply_tree_stage_activity,
        replay_build_patched_apk_stage_activity,
        replay_decode_stage_activity,
        replay_install_frameworks_stage_activity,
        replay_verify_final_apk_stage_activity,
    )
    from .contracts import ArtifactRef, GateDecision, GateReceipt, GateRequest, WorkflowStatus
    from .replay_contracts import (
        AdmittedReplayHandleV1,
        ReplayExecutionPlanV1,
        ReplayRunRequestV1,
        ReplayRunResultV1,
        ReplayVerificationAdmissionV1,
        ReplayVerificationGateV1,
        ReplayVerificationGrantHandleV1,
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
# path is unreachable.
#
# **Corrected 2026-08-05.** This block used to conclude "every other
# second-attempt outcome fails closed, so no path can produce two effects", and
# argued it FROM quarantine being what stops attempt two. Cancellation now
# releases the claim instead, so attempt two re-runs the stage rather than
# meeting a terminal refusal — which is the intent, not a regression. Two effects
# remain impossible for a different and better reason: `record_effect` is
# owner-fenced and the transition to `effect` requires `status == 'pending'` under
# `BEGIN IMMEDIATE`, so the release itself fences the attempt it replaced.
#
# The non-retryable list still keeps a genuine fault visible: a deterministic
# failure fails on attempt one instead of being re-run to fail identically.
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

# How long the server waits for a stage to say it is alive. Until 2026-08-05 no
# replay stage heartbeated, so worker loss was invisible until `start_to_close`
# expired -- three hours for verify. The stage wrappers now heartbeat every 30 s
# from the event loop.
#
# Ten minutes is twenty intervals, and an order of magnitude above the worst
# measured loop unavailability on a real port (3 consecutive samples, ~60 s,
# after the decoded-tree walks moved into a thread). Deliberately generous: a
# missed heartbeat cancels the stage, and while that is no longer destructive --
# it releases the claim for a later attempt to adopt -- it still throws away that
# stage's work. Better to detect a dead worker in ten minutes than to re-run a
# 25-minute build because the loop stalled for eleven.
#
# The wrappers report `worst_gap_seconds` in their heartbeat details, so this can
# be tightened against evidence rather than re-guessed.
_STAGE_HEARTBEAT_TIMEOUT = timedelta(seconds=600)


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

    The stage wrappers heartbeat every 30 s from the event loop, so worker loss is
    detected in ten minutes rather than at `start_to_close` expiry — three hours
    for verify. That became possible only once the decoded-tree walks moved into
    a thread: a heartbeater is a loop task, and the loop used to be blocked for
    nine minutes at a stretch.
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
        # Set from the human's decision on the ordinary path and from the recorded
        # grant on a resumed one, so a resumed run's result still names the
        # decision that authorised it rather than reporting an unapproved success.
        self._decision_id: str | None = None
        self._resumed = False

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
                heartbeat_timeout=_STAGE_HEARTBEAT_TIMEOUT,
                retry_policy=_STAGE_RETRY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            self._stages_completed.append(stage)

        # A gate already answered is not asked again. The grant is single-shot by
        # construction -- a different decision for the same run collides in the
        # ledger, and a decision issued before this gate is refused by the
        # validator -- so a run re-driven after admission could satisfy neither
        # door. Reading the recorded answer is the only exit that weakens no check.
        self._state = "resolving-verification-grant"
        resumption = await workflow.execute_activity(
            resolve_replay_verification_grant_activity,
            request.handle,
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_LEDGER_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        if resumption is not None:
            self._resumed = True
            self._decision_id = resumption.decision_id
            return await self._verify(run_id, plan, resumption.grant)

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

        # ReplayVerificationAdmissionV1 requires the decision to bind the handle's
        # run. If the gate Activity ever returned a different run, constructing it
        # would raise ValueError inside Workflow code, which Temporal treats as a
        # workflow *task* failure and retries forever -- the run would wedge
        # instead of failing. Check it here and fail the execution outright.
        if self._gate.run_id != request.handle.run_id:
            raise ApplicationError(
                "Verification gate does not bind the admitted replay run",
                type="ReplayVerificationGateMismatch",
                non_retryable=True,
            )

        self._state = "admitting-verification-grant"
        grant_handle = await workflow.execute_activity(
            admit_replay_verification_grant_activity,
            ReplayVerificationAdmissionV1(1, request.handle, self._decision),
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_LEDGER_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

        return await self._verify(run_id, plan, grant_handle)

    async def _verify(
        self,
        run_id: str,
        plan: ReplayExecutionPlanV1,
        grant_handle: ReplayVerificationGrantHandleV1,
    ) -> ReplayRunResultV1:
        """The last stage, reached either through the gate or past an answered one."""

        self._state = "running-verify"
        verify_budget = plan.stage_budget_seconds[plan.stages.index("verify")]
        verification = await workflow.execute_activity(
            replay_verify_final_apk_stage_activity,
            grant_handle,
            start_to_close_timeout=timedelta(seconds=verify_budget),
            heartbeat_timeout=_STAGE_HEARTBEAT_TIMEOUT,
            retry_policy=_STAGE_RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        self._stages_completed.append("verify")
        self._state = "completed"
        return self._result(run_id, "completed", verification, self._decision_id)

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
        # Published by `status` the moment the decision is accepted, exactly as
        # before this handler existed alongside a resumed path: the submission
        # client reads it to refuse answering a gate that already has an answer,
        # so it must not become visible one workflow task later than the decision.
        self._decision_id = decision.decision_id
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
        # `self._decision is not None` is what actually rejects a duplicate: only
        # one decision is ever accepted, so the id sets below can only hold that
        # one. They are kept as defence in depth against a future second gate in
        # this Workflow, not because the admission gate seeds them -- that gate
        # closed before this Workflow started and its ids are not visible here.
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
            decision_id=self._decision_id,
        )
