import asyncio
import socket
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from temporalio import workflow as temporal_workflow
from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory, WorkflowUpdateFailedError
from temporalio.common import PinnedVersioningOverride, VersioningBehavior, WorkerDeploymentVersion
from temporalio.exceptions import CancelledError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker, WorkerDeploymentConfig
from temporalio.workflow import NondeterminismError

from dfinsta_pipeline.activities import (
    admit_activity,
    apply_activity,
    configure_runtime,
    prepare_activity,
    record_decision_activity,
    runtime,
)
from dfinsta_pipeline.contracts import GateDecision, GateRequest, RunSpec, canonical_sha256
from dfinsta_pipeline.workflow import PortRunWorkflow


TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-pipeline-tests", "phase-a-v1")


@temporal_workflow.defn(name="PortRunWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class IncompatiblePortRunWorkflow:
    @temporal_workflow.run
    async def run(self, spec: RunSpec) -> str:
        return spec.run_id


def worker_for(client: Client, task_queue: str) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[PortRunWorkflow],
        activities=[admit_activity, prepare_activity, record_decision_activity, apply_activity],
        max_cached_workflows=0,
        deployment_config=WorkerDeploymentConfig(
            version=TEST_DEPLOYMENT_VERSION,
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
        ),
    )


def run_spec(
    run_id: str,
    subject: str,
    *,
    gate_timeout_seconds: int = 60,
    crash_after_effect: bool = False,
    apply_delay_seconds: int = 0,
) -> RunSpec:
    return RunSpec(
        1,
        run_id,
        subject,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        "policy-1",
        "operator",
        gate_timeout_seconds,
        "monolithic",
        crash_after_effect,
        apply_delay_seconds,
    )


def decision(
    spec: RunSpec,
    gate: GateRequest,
    *,
    decision_id: str = "decision-1",
    idempotency_id: str | None = None,
    subject: str | None = None,
    admission: str | None = None,
    prepared: str | None = None,
    issued_at: str | None = None,
) -> GateDecision:
    return GateDecision(
        1,
        decision_id,
        idempotency_id or f"request-{decision_id}",
        spec.allowed_actor,
        spec.run_id,
        "phase-a-approval",
        subject or gate.subject_sha256,
        admission or gate.admission_sha256,
        prepared or gate.prepared_sha256,
        spec.policy_revision,
        "approve",
        "approved for phase A test",
        issued_at or datetime.now(timezone.utc).isoformat(),
    )


class TemporalPhaseATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        configure_runtime(Path(self.directory.name))
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.task_queue = "phase-a-tests"

    async def asyncTearDown(self) -> None:
        await self.environment.shutdown()
        self.directory.cleanup()

    def worker(self) -> Worker:
        return worker_for(self.environment.client, self.task_queue)

    async def wait_for_gate(self, handle) -> GateRequest:
        for _ in range(100):
            status = await handle.query(PortRunWorkflow.status)
            if status.state == "awaiting-approval":
                assert status.gate is not None
                return status.gate
            await self.environment.sleep(0.01)
        self.fail("Workflow did not reach approval gate")

    async def wait_for_state(self, handle, expected: str) -> None:
        for _ in range(100):
            status = await handle.query(PortRunWorkflow.status)
            if status.state == expected:
                return
            await asyncio.sleep(0.01)
        self.fail(f"Workflow did not reach {expected}")

    async def test_worker_restart_at_gate_and_post_effect_adoption(self) -> None:
        spec = run_spec("run-restart", "a" * 64, crash_after_effect=True)
        with self.environment.auto_time_skipping_disabled():
            async with self.worker():
                handle = await asyncio.wait_for(
                    self.environment.client.start_workflow(
                        PortRunWorkflow.run,
                        spec,
                        id=spec.run_id,
                        task_queue=self.task_queue,
                        versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
                    ),
                    timeout=10,
                )
                gate = await self.wait_for_gate(handle)

            async with self.worker():
                handle = self.environment.client.get_workflow_handle_for(
                    PortRunWorkflow.run,
                    spec.run_id,
                )
                status = await asyncio.wait_for(handle.query(PortRunWorkflow.status), timeout=10)
                self.assertEqual(status.state, "awaiting-approval")
                approved = decision(spec, gate)
                receipt = await asyncio.wait_for(
                    handle.execute_update(PortRunWorkflow.submit_decision, approved),
                    timeout=30,
                )
                self.assertTrue(receipt.accepted)
                result = await asyncio.wait_for(handle.result(), timeout=20)

        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)
        key = runtime().ledger.operation_key_for_kind("phase_a_apply")
        self.assertEqual(runtime().ledger.operation_status(key), "completed")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 1)
        self.assertIsNotNone(result.output)
        assert result.output is not None
        self.assertEqual(
            result.output.input_hashes,
            (
                canonical_sha256(spec),
                gate.admission_sha256,
                gate.prepared_sha256,
                canonical_sha256(approved),
            ),
        )

    async def test_stale_decision_is_rejected(self) -> None:
        spec = run_spec("run-stale", "b" * 64, gate_timeout_seconds=86400)
        async with self.worker():
            handle = await self.environment.client.start_workflow(
                PortRunWorkflow.run,
                spec,
                id=spec.run_id,
                task_queue=self.task_queue,
                versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
            )
            gate = await self.wait_for_gate(handle)
            invalid_decisions = (
                decision(spec, gate, decision_id="wrong-subject", subject="c" * 64),
                decision(spec, gate, decision_id="wrong-admission", admission="d" * 64),
                decision(spec, gate, decision_id="wrong-prepared", prepared="e" * 64),
                decision(spec, gate, decision_id="wrong-actor"),
                decision(spec, gate, decision_id="stale-time", issued_at="2000-01-01T00:00:00+00:00"),
            )
            invalid_decisions = (
                *invalid_decisions[:3],
                replace(invalid_decisions[3], actor="intruder"),
                invalid_decisions[4],
            )
            for invalid in invalid_decisions:
                with self.subTest(decision_id=invalid.decision_id), self.assertRaises(
                    WorkflowUpdateFailedError
                ):
                    await handle.execute_update(PortRunWorkflow.submit_decision, invalid)
            await handle.execute_update(PortRunWorkflow.submit_decision, decision(spec, gate))
            result = await handle.result()
        self.assertEqual(result.state, "completed")

    async def test_duplicate_update_is_rejected(self) -> None:
        spec = run_spec("run-duplicate", "e" * 64, apply_delay_seconds=2)
        with self.environment.auto_time_skipping_disabled():
            async with self.worker():
                handle = await self.environment.client.start_workflow(
                    PortRunWorkflow.run,
                    spec,
                    id=spec.run_id,
                    task_queue=self.task_queue,
                    versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
                )
                gate = await self.wait_for_gate(handle)
                accepted = decision(spec, gate)
                first = await handle.execute_update(
                    PortRunWorkflow.submit_decision, accepted, id=accepted.idempotency_id
                )
                retried = await handle.execute_update(
                    PortRunWorkflow.submit_decision, accepted, id=accepted.idempotency_id
                )
                self.assertEqual(first, retried)
                with self.assertRaises(WorkflowUpdateFailedError):
                    await handle.execute_update(
                        PortRunWorkflow.submit_decision,
                        decision(
                            spec,
                            gate,
                            decision_id="decision-2",
                            idempotency_id=accepted.idempotency_id,
                        ),
                    )
                result = await handle.result()
        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)

    async def test_cancel_after_effect_quarantines_operation(self) -> None:
        spec = run_spec("run-cancel", "f" * 64, apply_delay_seconds=30)
        with self.environment.auto_time_skipping_disabled():
            async with self.worker():
                handle = await self.environment.client.start_workflow(
                    PortRunWorkflow.run,
                    spec,
                    id=spec.run_id,
                    task_queue=self.task_queue,
                    versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
                )
                gate = await self.wait_for_gate(handle)
                await handle.execute_update(PortRunWorkflow.submit_decision, decision(spec, gate))
                await self.wait_for_state(handle, "applying")
                for _ in range(100):
                    try:
                        key = runtime().ledger.operation_key_for_kind("phase_a_apply")
                    except ValueError:
                        await asyncio.sleep(0.01)
                        continue
                    if runtime().ledger.operation_status(key) == "effect":
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("Apply operation did not start")
                await handle.cancel()
                with self.assertRaises(WorkflowFailureError) as raised:
                    await handle.result()
        self.assertIsInstance(raised.exception.cause, CancelledError)
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 1)

    async def test_history_is_compact_private_and_replay_safe(self) -> None:
        spec = run_spec("run-history", "1" * 64)
        async with self.worker():
            handle = await self.environment.client.start_workflow(
                PortRunWorkflow.run,
                spec,
                id=spec.run_id,
                task_queue=self.task_queue,
                versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
            )
            gate = await self.wait_for_gate(handle)
            await handle.execute_update(PortRunWorkflow.submit_decision, decision(spec, gate))
            await handle.result()
            history = await handle.fetch_history()

        history_json = history.to_json()
        self.assertLess(len(history_json.encode("utf-8")), 256 * 1024)
        for forbidden in (str(Path(self.directory.name)), "PRIVATE_KEY", "PASSWORD", "SECRET"):
            self.assertNotIn(forbidden, history_json)

        saved_history = WorkflowHistory.from_json(spec.run_id, history_json)
        await Replayer(workflows=[PortRunWorkflow]).replay_workflow(saved_history)
        with self.assertRaises(NondeterminismError):
            await Replayer(workflows=[IncompatiblePortRunWorkflow]).replay_workflow(saved_history)

    async def test_gate_timeout_is_blocked(self) -> None:
        timeout = 3 * 24 * 60 * 60
        spec = run_spec("run-timeout", "d" * 64, gate_timeout_seconds=timeout)
        with self.environment.auto_time_skipping_disabled():
            async with self.worker():
                handle = await self.environment.client.start_workflow(
                    PortRunWorkflow.run,
                    spec,
                    id=spec.run_id,
                    task_queue=self.task_queue,
                    versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
                )
                await self.wait_for_gate(handle)
                await self.environment.sleep(timeout - 1)
                status = await handle.query(PortRunWorkflow.status)
                self.assertEqual(status.state, "awaiting-approval")
                await self.environment.sleep(2)
                result = await handle.result()
        self.assertEqual(result.state, "blocked")


class TemporalPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_and_fresh_clients_resume_gate_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "temporal.sqlite3"
            configure_runtime(root / "pipeline-state")
            task_queue = "phase-a-persistence"
            spec = run_spec("run-persistence", "9" * 64, gate_timeout_seconds=3600)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            target_host = f"127.0.0.1:{port}"

            first_environment = await WorkflowEnvironment.start_local(
                port=port,
                dev_server_database_filename=str(database),
                dev_server_log_level="error",
            )
            try:
                async with worker_for(first_environment.client, task_queue):
                    await asyncio.sleep(2)
                    request = SetWorkerDeploymentCurrentVersionRequest(
                        namespace="default",
                        deployment_name=TEST_DEPLOYMENT_VERSION.deployment_name,
                        build_id=TEST_DEPLOYMENT_VERSION.build_id,
                        identity="phase-a-deployment-test",
                    )
                    last_error: RPCError | None = None
                    for _ in range(100):
                        try:
                            await first_environment.client.workflow_service.set_worker_deployment_current_version(
                                request
                            )
                            break
                        except RPCError as error:
                            last_error = error
                            await asyncio.sleep(0.05)
                    else:
                        self.fail(f"Worker Deployment version did not become ready: {last_error}")
                    handle = await first_environment.client.start_workflow(
                        PortRunWorkflow.run,
                        spec,
                        id=spec.run_id,
                        task_queue=task_queue,
                    )
                    for _ in range(100):
                        status = await handle.query(PortRunWorkflow.status)
                        if status.state == "awaiting-approval":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Workflow did not reach approval gate before server restart")
            finally:
                await first_environment.shutdown()

            second_environment = await WorkflowEnvironment.start_local(
                port=port,
                dev_server_database_filename=str(database),
                dev_server_log_level="error",
            )
            try:
                trusted_client = await Client.connect(target_host, identity="phase-a-trusted-client-2")
                worker_client = await Client.connect(target_host, identity="phase-a-worker-2")
                async with worker_for(worker_client, task_queue):
                    await asyncio.sleep(2)
                    handle = trusted_client.get_workflow_handle_for(PortRunWorkflow.run, spec.run_id)
                    for _ in range(100):
                        status = await handle.query(PortRunWorkflow.status)
                        if status.state == "awaiting-approval":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Workflow did not resume at approval gate")
                    assert status.gate is not None
                    await handle.execute_update(
                        PortRunWorkflow.submit_decision,
                        decision(spec, status.gate),
                    )
                    result = await handle.result()
                self.assertEqual(result.state, "completed")
                self.assertEqual(runtime().ledger.decision_count(), 1)
            finally:
                await second_environment.shutdown()


if __name__ == "__main__":
    unittest.main()
