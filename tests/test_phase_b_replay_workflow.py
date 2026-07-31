"""Behavioural tests for the registered ReplayRunWorkflow.

Every Activity here is a stub registered under the *real* Activity name, so the
Workflow's `execute_activity` calls resolve without running apktool, touching a
workspace or opening the ledger. What is under test is therefore the Workflow
itself and nothing else: the stage sequence it derives from the plan, the
final-verification gate it hosts, and the shape of the History it leaves behind.

The History test is the one that justifies the handle design. The Workflow
carries `AdmittedReplayHandleV1` rather than `AdmittedReplayV3`, so the port
recipe and every source path must be absent from Temporal History; the test
checks the recorded bytes against the real 340 specs rather than against a
hand-written sample.
"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.api.history.v1 import History
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.common import PinnedVersioningOverride, VersioningBehavior, WorkerDeploymentVersion
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker, WorkerDeploymentConfig
from temporalio.workflow import NondeterminismError

from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, GateRequest
from dfinsta_pipeline.replay_contracts import (
    REPLAY_STAGE_ORDER,
    REPLAY_STAGES_WITHOUT_FRAMEWORK,
    AdmittedReplayHandleV1,
    ReplayExecutionPlanV1,
    ReplayRunRequestV1,
    ReplayRunResultV1,
    ReplayVerificationAdmissionV1,
    ReplayVerificationGateV1,
    ReplayVerificationGrantHandleV1,
)
from dfinsta_pipeline.replay_workflow import ReplayRunWorkflow


TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-pipeline-tests", "phase-b-replay-v1")

ADMITTED_REPLAY_SHA256 = "a" * 64
GATE_REQUEST_SHA256 = "b" * 64
GRANT_SHA256 = "c" * 64
STAGE_RECEIPT_SHA256 = "e" * 64
VERIFICATION_SHA256 = "f" * 64

ALLOWED_ACTOR = "operator"
POLICY_REVISION = "policy-1"
GATE_ID = "replay-final-verification"

# Large enough that no test accidentally races the gate timer, small enough to
# stay inside the contract's 30 day ceiling.
GATE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
STAGE_BUDGET_SECONDS = 3600

PLAN_ACTIVITY = "prepare_replay_plan_activity"
INSTALL_ACTIVITY = "replay_install_frameworks_stage_activity"
DECODE_ACTIVITY = "replay_decode_stage_activity"
APPLY_ACTIVITY = "replay_apply_tree_stage_activity"
BUILD_ACTIVITY = "replay_build_patched_apk_stage_activity"
GATE_ACTIVITY = "prepare_replay_verification_gate_activity"
GRANT_ACTIVITY = "admit_replay_verification_grant_activity"
VERIFY_ACTIVITY = "replay_verify_final_apk_stage_activity"

STAGE_ACTIVITY_NAMES = {
    "install_framework": INSTALL_ACTIVITY,
    "decode": DECODE_ACTIVITY,
    "apply": APPLY_ACTIVITY,
    "build": BUILD_ACTIVITY,
}


def artifact(kind: str, digest: str, operation_id: str) -> ArtifactRef:
    return ArtifactRef(1, kind, digest, 64, f"cas://sha256/{digest}", operation_id, ())


@temporal_workflow.defn(name="ReplayRunWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class IncompatibleReplayRunWorkflow:
    """Same Workflow type name, an entirely different command stream."""

    @temporal_workflow.run
    async def run(self, request: ReplayRunRequestV1) -> str:
        return request.handle.run_id


class ReplayStubs:
    """Stubs registered under the real Activity names, recording call order.

    Name-for-name registration is what makes this a Workflow test rather than an
    Activity test: `ReplayRunWorkflow` passes the real Activity callables to
    `execute_activity`, the Worker resolves them by name, and these run instead.
    """

    def __init__(self, stages: tuple[str, ...] = REPLAY_STAGES_WITHOUT_FRAMEWORK) -> None:
        self.stages = stages
        self.calls: list[str] = []
        self.admissions: list[ReplayVerificationAdmissionV1] = []
        # Set by default: only the duplicate-decision test needs the Workflow to
        # stay alive past approval, and it clears this to hold the verify stage.
        self.verify_released = asyncio.Event()
        self.verify_released.set()
        recorder = self

        @activity.defn(name=PLAN_ACTIVITY)
        async def plan_stub(handle: AdmittedReplayHandleV1) -> ReplayExecutionPlanV1:
            recorder.calls.append(PLAN_ACTIVITY)
            return ReplayExecutionPlanV1(
                1,
                handle.run_id,
                handle.admitted_replay_sha256,
                recorder.stages,
                tuple(STAGE_BUDGET_SECONDS for _ in recorder.stages),
            )

        @activity.defn(name=GATE_ACTIVITY)
        async def gate_stub(handle: AdmittedReplayHandleV1) -> ReplayVerificationGateV1:
            recorder.calls.append(GATE_ACTIVITY)
            return ReplayVerificationGateV1(
                1, handle.run_id, GATE_ID, GATE_REQUEST_SHA256, ALLOWED_ACTOR, POLICY_REVISION
            )

        @activity.defn(name=GRANT_ACTIVITY)
        async def grant_stub(
            admission: ReplayVerificationAdmissionV1,
        ) -> ReplayVerificationGrantHandleV1:
            recorder.calls.append(GRANT_ACTIVITY)
            recorder.admissions.append(admission)
            return ReplayVerificationGrantHandleV1(1, "grant-1", GRANT_SHA256)

        @activity.defn(name=VERIFY_ACTIVITY)
        async def verify_stub(handle: ReplayVerificationGrantHandleV1) -> ArtifactRef:
            recorder.calls.append(VERIFY_ACTIVITY)
            await recorder.verify_released.wait()
            return artifact(
                "replay-final-apk-verification-receipt-v1", VERIFICATION_SHA256, "op-verify"
            )

        self.activities = [
            plan_stub,
            gate_stub,
            grant_stub,
            verify_stub,
            *(self._stage_stub(name) for name in STAGE_ACTIVITY_NAMES.values()),
        ]

    def _stage_stub(self, name: str):
        recorder = self

        @activity.defn(name=name)
        async def stage_stub(handle: AdmittedReplayHandleV1) -> ArtifactRef:
            recorder.calls.append(name)
            return artifact("replay-stage-receipt-v1", STAGE_RECEIPT_SHA256, "op-stage")

        return stage_stub

    @property
    def registered_names(self) -> set[str]:
        return {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined,union-attr]
            for fn in self.activities
        }


def replay_request(
    run_id: str, *, gate_timeout_seconds: int = GATE_TIMEOUT_SECONDS
) -> ReplayRunRequestV1:
    return ReplayRunRequestV1(
        1, AdmittedReplayHandleV1(1, run_id, ADMITTED_REPLAY_SHA256), gate_timeout_seconds
    )


def verification_decision(
    gate: GateRequest,
    *,
    decision_id: str = "verify-approval-1",
    idempotency_id: str | None = None,
    actor: str = ALLOWED_ACTOR,
    run_id: str | None = None,
    gate_id: str | None = None,
    subject: str | None = None,
    admission: str | None = None,
    prepared: str | None = None,
    policy_revision: str | None = None,
    decision: str = "approve",
    issued_at: str | None = None,
) -> GateDecision:
    """Build a decision that binds the published gate unless a field is overridden.

    `issued_at` defaults to the gate's own issue time rather than wall-clock
    time: the Workflow clock is the test server's, and a skipped-time run must
    not depend on the two agreeing.
    """
    return GateDecision(
        1,
        decision_id,
        idempotency_id or f"request-{decision_id}",
        actor,
        run_id or gate.run_id,
        gate_id or gate.gate_id,
        subject or gate.subject_sha256,
        admission or gate.admission_sha256,
        prepared or gate.prepared_sha256,
        policy_revision or gate.policy_revision,
        decision,
        "verified against the recorded build receipt",
        issued_at or gate.issued_at,
    )


def _json_values(value: object, key: str):
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key and isinstance(item, str):
                yield item
            yield from _json_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _json_values(item, key)


def _spec(*parts: str) -> object:
    root = Path(__file__).resolve().parents[1]
    return json.loads(root.joinpath(*parts).read_text(encoding="utf-8"))


class ReplayRunWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.task_queue = "phase-b-replay-tests"

    async def asyncTearDown(self) -> None:
        await self.environment.shutdown()

    def worker(self, stubs: ReplayStubs) -> Worker:
        return Worker(
            self.environment.client,
            task_queue=self.task_queue,
            workflows=[ReplayRunWorkflow],
            activities=stubs.activities,
            max_cached_workflows=0,
            deployment_config=WorkerDeploymentConfig(
                version=TEST_DEPLOYMENT_VERSION,
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
            ),
        )

    async def start(self, request: ReplayRunRequestV1):
        return await self.environment.client.start_workflow(
            ReplayRunWorkflow.run,
            request,
            id=request.handle.run_id,
            task_queue=self.task_queue,
            versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
        )

    async def wait_for_gate(self, handle) -> GateRequest:
        for _ in range(200):
            status = await handle.query(ReplayRunWorkflow.status)
            if status.state == "awaiting-verification-approval":
                assert status.gate is not None
                return status.gate
            await self.environment.sleep(0.01)
        self.fail("Workflow did not reach the verification gate")

    async def run_to_completion(self, stubs: ReplayStubs, run_id: str):
        async with self.worker(stubs):
            handle = await self.start(replay_request(run_id))
            gate = await self.wait_for_gate(handle)
            receipt = await handle.execute_update(
                ReplayRunWorkflow.submit_verification_decision, verification_decision(gate)
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()
            history = await handle.fetch_history()
        return result, history, gate

    async def test_stage_sequence_without_frameworks(self) -> None:
        stubs = ReplayStubs(REPLAY_STAGES_WITHOUT_FRAMEWORK)
        result, _, _ = await self.run_to_completion(stubs, "replay-no-frameworks")

        self.assertEqual(
            stubs.calls,
            [
                PLAN_ACTIVITY,
                DECODE_ACTIVITY,
                APPLY_ACTIVITY,
                BUILD_ACTIVITY,
                GATE_ACTIVITY,
                GRANT_ACTIVITY,
                VERIFY_ACTIVITY,
            ],
        )
        # The stub is registered, so its absence is the Workflow's decision and
        # not a missing Activity.
        self.assertIn(INSTALL_ACTIVITY, stubs.registered_names)
        self.assertNotIn(INSTALL_ACTIVITY, stubs.calls)
        self.assertIsInstance(result, ReplayRunResultV1)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.stages_completed, REPLAY_STAGES_WITHOUT_FRAMEWORK)
        self.assertEqual(result.verification_decision_id, "verify-approval-1")
        assert result.final_verification is not None
        self.assertEqual(result.final_verification.sha256, VERIFICATION_SHA256)

    async def test_stage_sequence_with_frameworks(self) -> None:
        stubs = ReplayStubs(REPLAY_STAGE_ORDER)
        result, _, _ = await self.run_to_completion(stubs, "replay-with-frameworks")

        self.assertEqual(
            stubs.calls,
            [
                PLAN_ACTIVITY,
                INSTALL_ACTIVITY,
                DECODE_ACTIVITY,
                APPLY_ACTIVITY,
                BUILD_ACTIVITY,
                GATE_ACTIVITY,
                GRANT_ACTIVITY,
                VERIFY_ACTIVITY,
            ],
        )
        self.assertEqual(stubs.calls[1], INSTALL_ACTIVITY)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.stages_completed, REPLAY_STAGE_ORDER)

    async def test_verification_gate_rejects_decisions_that_do_not_bind_the_request(self) -> None:
        stubs = ReplayStubs()
        async with self.worker(stubs):
            handle = await self.start(replay_request("replay-gate-binding"))
            gate = await self.wait_for_gate(handle)
            other_hash = "9" * 64
            expiry = datetime.fromisoformat(gate.expires_at)
            unbound = "Decision does not match the pending verification gate"
            stale = "Decision timestamp is outside the gate validity period"
            # Each case is paired with the refusal it must produce, so a
            # validator that stopped distinguishing them could not still pass.
            invalid = (
                (
                    verification_decision(
                        gate, decision_id="wrong-gate", gate_id="some-other-gate"
                    ),
                    unbound,
                ),
                (
                    verification_decision(gate, decision_id="wrong-run", run_id="some-other-run"),
                    unbound,
                ),
                (
                    verification_decision(gate, decision_id="wrong-actor", actor="intruder"),
                    "Decision actor is not authorized",
                ),
                (
                    verification_decision(
                        gate, decision_id="wrong-policy", policy_revision="policy-2"
                    ),
                    unbound,
                ),
                (
                    verification_decision(gate, decision_id="wrong-subject", subject=other_hash),
                    unbound,
                ),
                (
                    verification_decision(
                        gate, decision_id="wrong-admission", admission=other_hash
                    ),
                    unbound,
                ),
                (
                    verification_decision(gate, decision_id="wrong-prepared", prepared=other_hash),
                    unbound,
                ),
                (
                    verification_decision(
                        gate, decision_id="before-issue", issued_at="2000-01-01T00:00:00+00:00"
                    ),
                    stale,
                ),
                (
                    verification_decision(
                        gate, decision_id="after-expiry", issued_at=gate.expires_at
                    ),
                    stale,
                ),
                (
                    verification_decision(
                        gate,
                        decision_id="naive-timestamp",
                        issued_at=expiry.replace(tzinfo=None).isoformat(),
                    ),
                    "Decision timestamp requires a UTC offset",
                ),
            )
            for candidate, reason in invalid:
                with self.subTest(decision_id=candidate.decision_id):
                    with self.assertRaises(WorkflowUpdateFailedError) as raised:
                        await handle.execute_update(
                            ReplayRunWorkflow.submit_verification_decision, candidate
                        )
                    self.assertIn(reason, str(raised.exception.cause))
            self.assertNotIn(GRANT_ACTIVITY, stubs.calls)

            receipt = await handle.execute_update(
                ReplayRunWorkflow.submit_verification_decision, verification_decision(gate)
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(len(stubs.admissions), 1)
        admitted = stubs.admissions[0].decision
        self.assertEqual(admitted.decision_id, "verify-approval-1")
        # The grant contract needs all three to bind the same request hash.
        self.assertEqual(
            {admitted.subject_sha256, admitted.admission_sha256, admitted.prepared_sha256},
            {GATE_REQUEST_SHA256},
        )

    async def test_duplicate_verification_decision_is_rejected(self) -> None:
        stubs = ReplayStubs()
        stubs.verify_released.clear()
        with self.environment.auto_time_skipping_disabled():
            async with self.worker(stubs):
                handle = await self.start(replay_request("replay-duplicate-decision"))
                gate = await self.wait_for_gate(handle)
                accepted = verification_decision(gate)
                receipt = await handle.execute_update(
                    ReplayRunWorkflow.submit_verification_decision, accepted
                )
                self.assertTrue(receipt.accepted)

                replayed_decision_id = verification_decision(
                    gate, decision_id=accepted.decision_id, idempotency_id="request-replay"
                )
                replayed_idempotency_id = verification_decision(
                    gate,
                    decision_id="verify-approval-2",
                    idempotency_id=accepted.idempotency_id,
                )
                for candidate in (replayed_decision_id, replayed_idempotency_id):
                    with self.subTest(
                        decision_id=candidate.decision_id,
                        idempotency_id=candidate.idempotency_id,
                    ), self.assertRaises(WorkflowUpdateFailedError):
                        await handle.execute_update(
                            ReplayRunWorkflow.submit_verification_decision, candidate
                        )

                stubs.verify_released.set()
                result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.verification_decision_id, accepted.decision_id)
        # One decision reached the grant Activity, not three.
        self.assertEqual(len(stubs.admissions), 1)
        self.assertEqual(stubs.calls.count(GRANT_ACTIVITY), 1)
        self.assertEqual(stubs.calls.count(VERIFY_ACTIVITY), 1)

    async def test_verification_gate_timeout_blocks_and_skips_verify(self) -> None:
        timeout = 3 * 24 * 60 * 60
        stubs = ReplayStubs()
        with self.environment.auto_time_skipping_disabled():
            async with self.worker(stubs):
                handle = await self.start(
                    replay_request("replay-gate-timeout", gate_timeout_seconds=timeout)
                )
                await self.wait_for_gate(handle)
                await self.environment.sleep(timeout - 1)
                status = await handle.query(ReplayRunWorkflow.status)
                self.assertEqual(status.state, "awaiting-verification-approval")
                await self.environment.sleep(2)
                result = await handle.result()

        self.assertEqual(result.state, "blocked")
        self.assertIsNone(result.final_verification)
        self.assertIsNone(result.verification_decision_id)
        # The stages that ran are still reported: a blocked run is stopped, not
        # erased, and the ledger holds the receipts they produced.
        self.assertEqual(result.stages_completed, ("decode", "apply", "build"))
        self.assertEqual(
            stubs.calls,
            [PLAN_ACTIVITY, DECODE_ACTIVITY, APPLY_ACTIVITY, BUILD_ACTIVITY, GATE_ACTIVITY],
        )
        self.assertNotIn(GRANT_ACTIVITY, stubs.calls)
        self.assertNotIn(VERIFY_ACTIVITY, stubs.calls)

    async def test_rejected_decision_ends_run_without_verifying(self) -> None:
        for verdict, expected_state in (("reject", "rejected"), ("defer", "deferred")):
            with self.subTest(decision=verdict):
                stubs = ReplayStubs()
                async with self.worker(stubs):
                    handle = await self.start(replay_request(f"replay-{verdict}"))
                    gate = await self.wait_for_gate(handle)
                    receipt = await handle.execute_update(
                        ReplayRunWorkflow.submit_verification_decision,
                        verification_decision(
                            gate, decision_id=f"verify-{verdict}-1", decision=verdict
                        ),
                    )
                    self.assertTrue(receipt.accepted)
                    result = await handle.result()

                self.assertEqual(result.state, expected_state)
                self.assertIsNone(result.final_verification)
                self.assertEqual(result.verification_decision_id, f"verify-{verdict}-1")
                self.assertEqual(result.stages_completed, ("decode", "apply", "build"))
                self.assertNotIn(GRANT_ACTIVITY, stubs.calls)
                self.assertNotIn(VERIFY_ACTIVITY, stubs.calls)
                self.assertEqual(stubs.admissions, [])

    async def test_history_stays_small_and_carries_no_recipe(self) -> None:
        """The reason the Workflow carries a handle instead of AdmittedReplayV3.

        AdmittedReplayV3 embeds the intent, resolution and source manifest by
        value: over 100 KB for the 340 target, including every source path and
        every smali descriptor the port touches. Passing it through Temporal
        would copy that into History on every stage. The forbidden strings below
        are read straight from the real specs, so this cannot pass by checking a
        sample that happens not to be recorded.
        """
        stubs = ReplayStubs(REPLAY_STAGE_ORDER)
        result, history, _ = await self.run_to_completion(stubs, "replay-history-shape")
        self.assertEqual(result.state, "completed")

        history_json = history.to_json()
        size = len(history_json.encode("utf-8"))
        self.assertLess(size, 128 * 1024, f"History grew to {size} bytes")

        # Payload bodies are base64 inside the JSON rendering, so searching the
        # JSON alone would pass even if the whole recipe were recorded. The
        # recorded protobuf holds payload bytes verbatim, so that is the surface
        # that actually proves absence; the handle assertion below is the
        # positive control that the surface contains payload content at all.
        recorded = History(events=list(history.events)).SerializeToString()

        resolution = _spec("pipeline_specs", "resolutions", "instagram_340.json")
        manifest = _spec("pipeline_specs", "source_manifests", "instagram_340.json")
        descriptors = sorted(
            {value for value in _json_values(resolution, "descriptor") if len(value) >= 6}
        )
        source_paths = sorted(
            {value for value in _json_values(manifest, "relative_path") if len(value) >= 6}
        )
        # Guard against the corpus silently collapsing to nothing.
        self.assertGreaterEqual(len(descriptors), 20)
        self.assertGreaterEqual(len(source_paths), 50)
        self.assertIn("LX/15J;", descriptors)
        self.assertIn("dfinsta_source_1.4.1/appendRes/values/arrays.xml", source_paths)

        for forbidden in (*descriptors, *source_paths):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, history_json)
                self.assertNotIn(forbidden.encode("utf-8"), recorded)

        # What the Workflow does carry is the handle, and nothing larger.
        self.assertIn(ADMITTED_REPLAY_SHA256.encode("utf-8"), recorded)

    async def test_replay_workflow_history_replays_deterministically(self) -> None:
        stubs = ReplayStubs(REPLAY_STAGE_ORDER)
        result, history, _ = await self.run_to_completion(stubs, "replay-determinism")
        self.assertEqual(result.state, "completed")

        saved = WorkflowHistory.from_json("replay-determinism", history.to_json())
        await Replayer(workflows=[ReplayRunWorkflow]).replay_workflow(saved)
        with self.assertRaises(NondeterminismError):
            await Replayer(workflows=[IncompatibleReplayRunWorkflow]).replay_workflow(saved)

    async def test_workflow_status_query_reports_progress(self) -> None:
        stubs = ReplayStubs()
        async with self.worker(stubs):
            handle = await self.start(replay_request("replay-status-query"))
            gate = await self.wait_for_gate(handle)
            status = await handle.query(ReplayRunWorkflow.status)

            self.assertEqual(status.state, "awaiting-verification-approval")
            self.assertIsNone(status.decision_id)
            assert status.gate is not None
            self.assertEqual(status.gate.run_id, "replay-status-query")
            self.assertEqual(status.gate.gate_id, GATE_ID)
            self.assertEqual(status.gate.policy_revision, POLICY_REVISION)
            # The gate publishes only the request hash, three times over, because
            # the grant contract binds subject, admission and prepared alike.
            self.assertEqual(
                {
                    status.gate.subject_sha256,
                    status.gate.admission_sha256,
                    status.gate.prepared_sha256,
                },
                {GATE_REQUEST_SHA256},
            )
            issued = datetime.fromisoformat(status.gate.issued_at)
            expires = datetime.fromisoformat(status.gate.expires_at)
            self.assertIsNotNone(issued.tzinfo)
            self.assertEqual(expires - issued, timedelta(seconds=GATE_TIMEOUT_SECONDS))
            self.assertEqual(issued.tzinfo.utcoffset(issued), timezone.utc.utcoffset(None))

            await handle.execute_update(
                ReplayRunWorkflow.submit_verification_decision, verification_decision(gate)
            )
            result = await handle.result()

        self.assertEqual(result.state, "completed")


if __name__ == "__main__":
    unittest.main()
