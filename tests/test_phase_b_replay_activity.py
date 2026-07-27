import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest import mock

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_decode_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.contracts import canonical_json
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    CapabilityBinding,
    ReplayDecodeCheckpointResultV1,
)
from tests.test_phase_b_replay_contracts import (
    admit_v3,
    bind_v3_fixture,
    capability_for_plan,
    fixture_v2,
    fixture_v3,
    profile_v3,
)


def activity_fixture(executable_sha256: str, *, with_framework: bool = False):
    base = fixture_v2(with_framework)
    profile = profile_v3(with_framework)
    capabilities = tuple(
        capability_for_plan(
            profile,
            binding.role,
            executable_sha256=executable_sha256,
        )
        for binding in profile.capability_bindings
    )
    profile = replace(
        profile,
        capability_bindings=tuple(
            CapabilityBinding(binding.role, capability.canonical_identity)
            for binding, capability in zip(
                profile.capability_bindings, capabilities, strict=True
            )
        ),
    )
    return bind_v3_fixture(base, profile, capabilities)


class FakeProcess:
    def __init__(
        self, stdout: bytes = b"decoded", stderr: bytes = b"warning", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = returncode
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.reaped = True
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1


class BlockingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.returncode = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if not self.killed:
            await self.release.wait()
        self.reaped = True
        return self.stdout, self.stderr


class ReplayDecodeCheckpointContractTests(unittest.TestCase):
    def test_result_is_strict_versioned_and_canonically_hashed(self) -> None:
        result = ReplayDecodeCheckpointResultV1(
            1,
            "a" * 64,
            "decode",
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            0,
        )
        self.assertEqual(ReplayDecodeCheckpointResultV1.from_dict(asdict(result)), result)
        self.assertEqual(result.sha256, hashlib.sha256(canonical_json(result).encode()).hexdigest())
        for mutation in (
            {**asdict(result), "schema_version": True},
            {**asdict(result), "role": "build"},
            {**asdict(result), "returncode": False},
            {**asdict(result), "execution_plan_sha256": "A" * 64},
            {**asdict(result), "stdout": "/physical/workspace"},
            {**asdict(result), "extra": "field"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayDecodeCheckpointResultV1.from_dict(mutation)


class ReplayDecodeCheckpointActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.executable = self.root / "executor.bin"
        self.executable.write_bytes(b"trusted synthetic executor")
        executable_sha256 = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        self.case = activity_fixture(executable_sha256)
        self.admitted = admit_v3(self.case)
        self.launches: list[dict[str, Any]] = []
        self.process: FakeProcess = FakeProcess(
            stdout=f"decoded in {self.root}/attempts by owner-1".encode(),
            stderr=f"warning from {self.root}".encode(),
        )

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            self.launches.append({"argv": argv, **kwargs})
            return self.process

        capability_hash = self.admitted.capability("decode").executable_sha256
        executor_paths = {capability_hash: self.executable}
        configure_runtime(
            self.root / "state",
            attempts_root=self.root / "attempts",
            executor_paths=executor_paths,
            launcher=launcher,
        )
        executor_paths.clear()
        self.assertEqual(runtime().executor_paths[capability_hash], self.executable)
        with self.assertRaises(TypeError):
            runtime().executor_paths[capability_hash] = self.root / "substituted"  # type: ignore[index]
        self.assertFalse(runtime().attempts_root.exists())
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture",
                data=payload,
                producer_operation_id="fixture-producer",
                input_hashes=(),
            )
        self.record_authority(self.admitted)

    @staticmethod
    def record_authority(admitted: AdmittedReplayV3) -> None:
        runtime().ledger.record_decision(admitted.decision)
        runtime().ledger.record_admitted_replay_v3(admitted)

    async def invoke(self, candidate: AdmittedReplayV3 | None = None, owner: str = "owner-1"):
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_decode_checkpoint_activity(candidate or self.admitted)

    def sole_operation(self) -> tuple[str, str]:
        with runtime().ledger._connection() as connection:
            row = connection.execute(
                "SELECT operation_key, status FROM operation_claims"
            ).fetchone()
        assert row is not None
        return row

    async def test_happy_path_uses_exact_admitted_inputs_and_completes_effect(self) -> None:
        real_execute = activities.execute
        calls: list[dict[str, Any]] = []

        async def observed_execute(*args: Any, **kwargs: Any):
            calls.append({"args": args, "kwargs": kwargs})
            return await real_execute(*args, **kwargs)

        with mock.patch.object(activities, "execute", side_effect=observed_execute):
            output = await self.invoke()

        key, status = self.sole_operation()
        admitted = runtime().ledger.require_admitted_replay_v3(self.admitted)
        plan = admitted.plan("decode")
        tool = next(tool for tool in admitted.request.tools if tool.tool_id == plan.tool_id)
        workspace = runtime().attempts_root / key / hashlib.sha256(b"owner-1").hexdigest()
        self.assertEqual(status, "completed")
        self.assertEqual(runtime().ledger.operation_event_count(key, "pending"), 1)
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 1)
        self.assertEqual(runtime().ledger.operation_event_count(key, "completed"), 1)
        self.assertEqual((workspace / "input.apk").read_bytes(), self.case.resolve(admitted.request.stock_apk))
        self.assertEqual((workspace / "tool").read_bytes(), self.case.resolve(tool.artifact))
        self.assertTrue((workspace / "framework").is_dir())
        self.assertFalse((workspace / "output").exists())
        self.assertEqual(self.launches[0]["argv"][1:], ("output", "framework", "input.apk", "tool"))
        self.assertEqual(calls[0]["kwargs"]["timeout_seconds"], plan.timeout_seconds)
        self.assertIs(calls[0]["kwargs"]["launcher"], runtime().launcher)
        request = calls[0]["args"][1]
        self.assertEqual(
            request.arguments,
            tuple(
                (name, {"decoded_tree": "output", "framework_dir": "framework", "input_apk": "input.apk", "tool": "tool"}[slot])
                for name, slot in plan.arguments
            ),
        )
        result = ReplayDecodeCheckpointResultV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )
        self.assertEqual(result.admitted_replay_sha256, admitted.sha256)
        self.assertEqual(result.execution_request_sha256, request.canonical_identity)
        self.assertEqual(result.tool_artifact_sha256, tool.artifact.sha256)
        self.assertEqual(output.input_hashes[3], tool.artifact.sha256)
        self.assertNotEqual(output.input_hashes[3], tool.sha256)
        self.assertEqual(result.returncode, 0)

    async def test_unrecorded_authority_has_no_operation_cas_workspace_registry_or_launch(self) -> None:
        other_root = self.root / "unrecorded"
        candidate = admit_v3(fixture_v3(with_framework=True))
        configure_runtime(
            other_root / "state",
            attempts_root=other_root / "attempts",
            executor_paths={},
            launcher=runtime().launcher,
        )
        with (
            mock.patch.object(runtime().ledger, "begin_operation", wraps=runtime().ledger.begin_operation) as begin,
            mock.patch.object(runtime().store, "read_bytes", wraps=runtime().store.read_bytes) as read_bytes,
        ):
            with self.assertRaisesRegex(ValueError, "authority"):
                await self.invoke(candidate)
        begin.assert_not_called()
        read_bytes.assert_not_called()
        self.assertFalse(runtime().attempts_root.exists())
        self.assertEqual(self.launches, [])

    async def test_uses_returned_normalized_authority_not_caller_object(self) -> None:
        normalized_case = activity_fixture(
            hashlib.sha256(self.executable.read_bytes()).hexdigest(),
            with_framework=True,
        )
        normalized = admit_v3(normalized_case)
        for payload in normalized_case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture", data=payload, producer_operation_id="fixture-producer", input_hashes=()
            )
        with mock.patch.object(
            runtime().ledger, "require_admitted_replay_v3", return_value=normalized
        ):
            output = await self.invoke(self.admitted)
        decoded = ReplayDecodeCheckpointResultV1.from_dict(json.loads(runtime().store.read_bytes(output)))
        self.assertEqual(decoded.admitted_replay_sha256, normalized.sha256)
        self.assertNotEqual(decoded.admitted_replay_sha256, self.admitted.sha256)

    async def test_roots_and_owners_do_not_change_operation_or_result_bytes(self) -> None:
        first_process = self.process
        first = await self.invoke(owner="physical-owner-a")
        first_bytes = runtime().store.read_bytes(first)
        first_key = first.producer_operation_id

        second_root = self.root / "second"
        second_executable = second_root / "executor.bin"
        second_executable.parent.mkdir()
        second_executable.write_bytes(self.executable.read_bytes())
        self.process = FakeProcess(
            stdout=f"decoded in {second_root}/physical-attempts by physical-owner-b".encode(),
            stderr=f"warning from {second_root}".encode(),
        )
        configure_runtime(
            second_root / "state",
            attempts_root=second_root / "physical-attempts",
            executor_paths={self.admitted.capability("decode").executable_sha256: second_executable},
            launcher=runtime().launcher,
        )
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture", data=payload, producer_operation_id="fixture-producer", input_hashes=()
            )
        self.record_authority(self.admitted)
        second = await self.invoke(owner="physical-owner-b")
        self.assertNotEqual(first_process.stdout, self.process.stdout)
        self.assertNotEqual(first_process.stderr, self.process.stderr)
        self.assertEqual(second.producer_operation_id, first_key)
        self.assertEqual(runtime().store.read_bytes(second), first_bytes)
        self.assertEqual(second.sha256, first.sha256)

    async def test_attempts_root_overlap_is_rejected_without_creation(self) -> None:
        state_root = self.root / "overlap-state"
        attempts_root = state_root / "cas" / "attempts"
        with self.assertRaisesRegex(ValueError, "content store"):
            configure_runtime(
                state_root,
                attempts_root=attempts_root,
                executor_paths={
                    self.admitted.capability("decode").executable_sha256: self.executable
                },
            )
        self.assertFalse(attempts_root.exists())

    async def test_configured_attempts_symlink_is_canonicalized_before_use(self) -> None:
        outside = self.root / "outside-attempts"
        outside.mkdir()
        linked = self.root / "linked-attempts"
        linked.symlink_to(outside, target_is_directory=True)
        configure_runtime(
            self.root / "linked-state",
            attempts_root=linked,
            executor_paths={
                self.admitted.capability("decode").executable_sha256: self.executable
            },
            launcher=runtime().launcher,
        )
        self.assertEqual(runtime().attempts_root, outside.resolve())
        linked.unlink()
        linked.symlink_to(runtime().store.root, target_is_directory=True)
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture",
                data=payload,
                producer_operation_id="fixture-producer",
                input_hashes=(),
            )
        self.record_authority(self.admitted)
        output = await self.invoke()
        self.assertTrue((outside / output.producer_operation_id).is_dir())
        self.assertFalse((runtime().store.root / output.producer_operation_id).exists())

    async def test_missing_executable_leaves_pending_before_workspace_and_launch(self) -> None:
        configure_runtime(
            self.root / "missing-state",
            attempts_root=self.root / "missing-attempts",
            executor_paths={},
            launcher=runtime().launcher,
        )
        self.record_authority(self.admitted)
        with self.assertRaisesRegex(ValueError, "runtime executable"):
            await self.invoke()
        _, status = self.sole_operation()
        self.assertEqual(status, "pending")
        self.assertFalse(runtime().attempts_root.exists())
        self.assertEqual(self.launches, [])

    async def test_nonzero_exit_quarantines_without_effect(self) -> None:
        self.process = FakeProcess(returncode=7)
        with self.assertRaisesRegex(RuntimeError, "exit code 7"):
            await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_tampered_executor_fails_before_launch_and_effect(self) -> None:
        self.executable.write_bytes(b"tampered executable bytes")
        with self.assertRaisesRegex(ValueError, "Executable SHA-256"):
            await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(self.launches, [])
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_preexisting_operation_root_symlink_fails_before_launch_and_effect(self) -> None:
        with mock.patch.object(
            activities,
            "_open_or_create_directory",
            side_effect=RuntimeError("stop before attempts creation"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop before attempts"):
                await self.invoke(owner="first-owner")
        key, status = self.sole_operation()
        self.assertEqual(status, "pending")
        runtime().attempts_root.mkdir()
        target = self.root / "symlink-target"
        target.mkdir()
        (runtime().attempts_root / key).symlink_to(target, target_is_directory=True)

        with self.assertRaises(OSError):
            await self.invoke(owner="retry-owner")
        self.assertEqual(self.launches, [])
        self.assertEqual(runtime().ledger.operation_status(key), "pending")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_cancellation_kills_reaps_and_quarantines(self) -> None:
        process = BlockingProcess()
        self.process = process
        task = asyncio.create_task(self.invoke())
        await process.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        key, status = self.sole_operation()
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_post_effect_completion_failure_retries_by_adoption_without_work(self) -> None:
        complete = runtime().ledger.complete_operation
        with mock.patch.object(
            runtime().ledger,
            "complete_operation",
            side_effect=RuntimeError("completion unavailable"),
            wraps=complete,
        ):
            with self.assertRaisesRegex(RuntimeError, "completion unavailable"):
                await self.invoke(owner="first-owner")
        key, status = self.sole_operation()
        self.assertEqual(status, "effect")
        launches = len(self.launches)
        retry_workspace = runtime().attempts_root / key / hashlib.sha256(b"retry-owner").hexdigest()
        output = await self.invoke(owner="retry-owner")
        self.assertEqual(runtime().ledger.operation_status(key), "completed")
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(retry_workspace.exists())
        self.assertEqual(output.producer_operation_id, key)

    async def test_corrupt_adopted_cas_fails_closed_without_launch_or_workspace(self) -> None:
        output = await self.invoke(owner="first-owner")
        launches = len(self.launches)
        path = runtime().store.root / "sha256" / output.sha256[:2] / output.sha256
        path.write_bytes(b"corrupt")
        retry_workspace = (
            runtime().attempts_root
            / output.producer_operation_id
            / hashlib.sha256(b"retry-owner").hexdigest()
        )
        with self.assertRaises(ValueError):
            await self.invoke(owner="retry-owner")
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(retry_workspace.exists())

    async def test_caller_cannot_substitute_plan_or_tool(self) -> None:
        substituted = admit_v3(fixture_v3(with_framework=True))
        with self.assertRaisesRegex(ValueError, "does not match candidate"):
            await self.invoke(substituted)
        self.assertEqual(self.launches, [])
        self.assertFalse(runtime().attempts_root.exists())


if __name__ == "__main__":
    unittest.main()
