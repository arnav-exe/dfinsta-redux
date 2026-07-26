import asyncio
import hashlib
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest import mock

from dfinsta_pipeline.contracts import ArtifactRef, RunSpec
from dfinsta_pipeline.executor import (
    ExecutionMetadata,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapability,
    execute,
)


class FakeProcess:
    def __init__(self, stdout: bytes = b"ok", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1


def artifact(kind: str = "apk") -> ArtifactRef:
    digest = "a" * 64
    return ArtifactRef(1, kind, digest, 1, f"cas://sha256/{digest}", "producer-1", ())


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.executable = self.root / "tool.bin"
        self.executable.write_bytes(b"approved executable")
        self.digest = hashlib.sha256(self.executable.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def capability(self, **changes: Any) -> ExecutorCapability:
        values: dict[str, Any] = {
            "schema_version": 1,
            "capability_id": "decode-apk",
            "executable_sha256": self.digest,
            "argv_template": ("decode", "{input}", "--output", "{output}"),
            "path_arguments": ("input", "output"),
            "input_kinds": ("apk",),
            "output_kind": "decoded-tree",
            "allowed_environment": ("LANG",),
            "fixed_environment": (("MODE", "strict"),),
            "allowed_mutation_paths": ("out",),
        }
        values.update(changes)
        return ExecutorCapability(**values)

    def request(self, **changes: Any) -> ExecutionRequest:
        values: dict[str, Any] = {
            "schema_version": 1,
            "capability_id": "decode-apk",
            "executor_capability_sha256": self.capability().canonical_identity,
            "input_artifact": artifact(),
            "output_kind": "decoded-tree",
            "arguments": (("input", "input.apk"), ("output", "out")),
            "environment": (("LANG", "C.UTF-8"),),
            "apk_composition": "monolithic",
        }
        values.update(changes)
        return ExecutionRequest(**values)

    def metadata(self, **changes: Any) -> ExecutionMetadata:
        values: dict[str, Any] = {
            "executable_path": self.executable,
            "workspace_root": self.workspace,
            "cwd": self.workspace,
        }
        values.update(changes)
        return ExecutionMetadata(**values)

    async def admitted_execute(
        self,
        capability: ExecutorCapability,
        request: ExecutionRequest,
        metadata: ExecutionMetadata,
        *,
        admitted_capability_sha256: str | None = None,
        timeout_seconds: float,
        launcher: Any = None,
    ) -> ExecutionResult:
        admitted_hash = admitted_capability_sha256 or capability.canonical_identity
        admitted_spec = RunSpec(
            1,
            "executor-test",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            admitted_hash,
            "policy-1",
            "operator",
            60,
            "monolithic",
        )
        return await execute(
            capability,
            request,
            metadata,
            admitted_spec=admitted_spec,
            timeout_seconds=timeout_seconds,
            launcher=launcher,
        )

    async def test_valid_admission_uses_exec_and_excludes_ambient_secret(self) -> None:
        captured: dict[str, Any] = {}

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            captured.update(argv=argv, **kwargs)
            return FakeProcess(stdout="success".encode("utf-8"))

        os.environ["PHASE_A_SECRET"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "PHASE_A_SECRET", None)
        result = await self.admitted_execute(
            self.capability(), self.request(), self.metadata(), timeout_seconds=1, launcher=launcher
        )

        self.assertEqual(result.stdout, "success")
        self.assertEqual(captured["argv"][0], str(self.executable.resolve()))
        self.assertEqual(captured["argv"][1:], ("decode", "input.apk", "--output", "out"))
        self.assertEqual(captured["env"], {"MODE": "strict", "LANG": "C.UTF-8"})
        self.assertNotIn("PHASE_A_SECRET", captured["env"])

    async def test_wrong_digest_fails_before_launcher(self) -> None:
        launched = False

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            nonlocal launched
            launched = True
            return FakeProcess()

        capability = self.capability(executable_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "Executable SHA-256"):
            await self.admitted_execute(
                capability,
                self.request(executor_capability_sha256=capability.canonical_identity),
                self.metadata(),
                timeout_seconds=1,
                launcher=launcher,
            )
        self.assertFalse(launched)

    async def test_substituted_capability_hash_fails_before_filesystem_or_launcher(self) -> None:
        approved = self.capability()
        substituted = self.capability(allowed_mutation_paths=("other",))
        launched = False

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            nonlocal launched
            launched = True
            return FakeProcess()

        with self.assertRaisesRegex(ValueError, "admitted SHA-256"):
            await self.admitted_execute(
                substituted,
                self.request(executor_capability_sha256=substituted.canonical_identity),
                self.metadata(
                    executable_path=self.root / "missing-tool",
                    workspace_root=self.root / "missing-workspace",
                    cwd=self.root / "missing-cwd",
                ),
                admitted_capability_sha256=approved.canonical_identity,
                timeout_seconds=1,
                launcher=launcher,
            )
        self.assertFalse(launched)

    async def test_template_rejects_missing_and_extra_arguments(self) -> None:
        for arguments in (
            (("input", "input.apk"),),
            (("extra", "value"), ("input", "input.apk"), ("output", "out")),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "exactly"):
                await self.admitted_execute(
                    self.capability(),
                    self.request(arguments=arguments),
                    self.metadata(),
                    timeout_seconds=1,
                    launcher=lambda *args, **kwargs: None,
                )

    async def test_workspace_and_symlink_escapes_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "escapes"):
            await self.admitted_execute(
                self.capability(),
                self.request(),
                self.metadata(cwd=outside),
                timeout_seconds=1,
            )

        link = self.workspace / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "escapes"):
            await self.admitted_execute(
                self.capability(),
                self.request(arguments=(("input", "input.apk"), ("output", "link/out"))),
                self.metadata(),
                timeout_seconds=1,
            )

    async def test_artifact_and_output_kinds_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "Input artifact kind"):
            await self.admitted_execute(
                self.capability(), self.request(input_artifact=artifact("report")), self.metadata(), timeout_seconds=1
            )
        with self.assertRaisesRegex(ValueError, "Output artifact kind"):
            await self.admitted_execute(
                self.capability(), self.request(output_kind="report"), self.metadata(), timeout_seconds=1
            )

    async def test_undeclared_mutation_is_rejected(self) -> None:
        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            (self.workspace / "unexpected.txt").write_text("changed", encoding="utf-8")
            return FakeProcess()

        with self.assertRaisesRegex(PermissionError, "unexpected.txt"):
            await self.admitted_execute(
                self.capability(), self.request(), self.metadata(), timeout_seconds=1, launcher=launcher
            )

        async def allowed_launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            output = self.workspace / "out"
            output.mkdir()
            (output / "result.txt").write_text("allowed", encoding="utf-8")
            return FakeProcess()

        result = await self.admitted_execute(
            self.capability(), self.request(), self.metadata(), timeout_seconds=1, launcher=allowed_launcher
        )
        self.assertEqual(result.returncode, 0)

    def test_split_apk_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Split APK"):
            self.request(apk_composition="split")

    def test_from_dict_rejects_malformed_containers_scalars_and_artifact(self) -> None:
        capability = self.capability()
        capability_data: dict[str, Any] = {
            "schema_version": capability.schema_version,
            "capability_id": capability.capability_id,
            "executable_sha256": capability.executable_sha256,
            "argv_template": list(capability.argv_template),
            "path_arguments": list(capability.path_arguments),
            "input_kinds": list(capability.input_kinds),
            "output_kind": capability.output_kind,
            "allowed_environment": list(capability.allowed_environment),
            "fixed_environment": [list(pair) for pair in capability.fixed_environment],
            "allowed_mutation_paths": list(capability.allowed_mutation_paths),
        }
        input_artifact = artifact()
        artifact_data: dict[str, Any] = {
            "schema_version": input_artifact.schema_version,
            "kind": input_artifact.kind,
            "sha256": input_artifact.sha256,
            "size": input_artifact.size,
            "uri": input_artifact.uri,
            "producer_operation_id": input_artifact.producer_operation_id,
            "input_hashes": list(input_artifact.input_hashes),
        }
        request = self.request()
        request_data: dict[str, Any] = {
            "schema_version": request.schema_version,
            "capability_id": request.capability_id,
            "executor_capability_sha256": request.executor_capability_sha256,
            "input_artifact": artifact_data,
            "output_kind": request.output_kind,
            "arguments": [list(pair) for pair in request.arguments],
            "environment": [list(pair) for pair in request.environment],
            "apk_composition": request.apk_composition,
        }

        self.assertEqual(ExecutorCapability.from_dict(capability_data), capability)
        self.assertEqual(ExecutionRequest.from_dict(request_data), request)
        malformed = (
            (ExecutorCapability, {**capability_data, "argv_template": "decode"}),
            (ExecutorCapability, {**capability_data, "fixed_environment": "MODE=strict"}),
            (ExecutorCapability, {**capability_data, "schema_version": True}),
            (ExecutionRequest, {**request_data, "arguments": "input=input.apk"}),
            (ExecutionRequest, {**request_data, "apk_composition": ["monolithic"]}),
            (
                ExecutionRequest,
                {**request_data, "input_artifact": {**artifact_data, "size": False}},
            ),
            (
                ExecutionRequest,
                {**request_data, "input_artifact": {**artifact_data, "input_hashes": "a" * 64}},
            ),
            (
                ExecutionRequest,
                {**request_data, "input_artifact": {key: value for key, value in artifact_data.items() if key != "uri"}},
            ),
        )
        for contract, data in malformed:
            with self.subTest(contract=contract.__name__, data=data), self.assertRaises(
                (TypeError, ValueError)
            ):
                contract.from_dict(data)

    def test_environment_names_reject_case_collisions(self) -> None:
        with self.assertRaisesRegex(ValueError, "case-insensitively"):
            self.capability(allowed_environment=("LANG", "lang"))
        with self.assertRaisesRegex(ValueError, "case-insensitively"):
            self.capability(fixed_environment=(("MODE", "strict"), ("mode", "other")))
        with self.assertRaisesRegex(ValueError, "case-insensitively"):
            self.request(environment=(("LANG", "C"), ("lang", "other")))

    async def test_windows_replacement_environment_requires_systemroot(self) -> None:
        os.environ["SystemRoot"] = "C:/ambient-secret-bearing-environment"
        self.addCleanup(os.environ.pop, "SystemRoot", None)
        launched = False

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            nonlocal launched
            launched = True
            return FakeProcess()

        with mock.patch("dfinsta_pipeline.executor.sys.platform", "win32"):
            with self.assertRaisesRegex(ValueError, "requires SystemRoot"):
                await self.admitted_execute(
                    self.capability(),
                    self.request(),
                    self.metadata(),
                    timeout_seconds=1,
                    launcher=launcher,
                )
        self.assertFalse(launched)

    def test_canonical_identity_is_location_independent_and_contracts_are_frozen(self) -> None:
        capability = self.capability()
        request = self.request()
        other_root = self.root / "other"
        other_root.mkdir()
        other_executable = self.root / "other-tool.bin"
        other_executable.write_bytes(self.executable.read_bytes())
        first = self.metadata()
        second = self.metadata(
            executable_path=other_executable, workspace_root=other_root, cwd=other_root
        )

        self.assertEqual(capability.canonical_identity, self.capability().canonical_identity)
        self.assertEqual(request.canonical_identity, self.request().canonical_identity)
        self.assertNotEqual(first, second)
        with self.assertRaises(FrozenInstanceError):
            request.output_kind = "other"  # type: ignore[misc]

    async def test_timeout_kills_and_reaps_process(self) -> None:
        class HangingProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.returncode = None

            async def communicate(self) -> tuple[bytes, bytes]:
                if not self.killed:
                    await asyncio.Event().wait()
                return b"", b""

        process = HangingProcess()

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            return process

        with self.assertRaises(TimeoutError):
            await self.admitted_execute(
                self.capability(),
                self.request(),
                self.metadata(),
                timeout_seconds=0.01,
                launcher=launcher,
            )
        self.assertTrue(process.killed)

    async def test_cancellation_kills_and_reaps_process(self) -> None:
        communicating = asyncio.Event()

        class HangingProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.returncode = None

            async def communicate(self) -> tuple[bytes, bytes]:
                communicating.set()
                if not self.killed:
                    await asyncio.Event().wait()
                return b"", b""

        process = HangingProcess()

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            return process

        task = asyncio.create_task(
            self.admitted_execute(
                self.capability(),
                self.request(),
                self.metadata(),
                timeout_seconds=60,
                launcher=launcher,
            )
        )
        await communicating.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(process.killed)

    async def test_cancellation_during_launch_cleans_up_returned_process(self) -> None:
        launch_started = asyncio.Event()
        return_process = asyncio.Event()
        process = FakeProcess()
        process.returncode = None

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            launch_started.set()
            await return_process.wait()
            return process

        task = asyncio.create_task(
            self.admitted_execute(
                self.capability(),
                self.request(),
                self.metadata(),
                timeout_seconds=60,
                launcher=launcher,
            )
        )
        await launch_started.wait()
        task.cancel()
        return_process.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(process.killed)

    async def test_slow_cancelled_launch_is_supervised_until_handle_arrives(self) -> None:
        launch_started = asyncio.Event()
        return_process = asyncio.Event()
        process = FakeProcess()
        process.returncode = None

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            launch_started.set()
            await return_process.wait()
            return process

        with mock.patch("dfinsta_pipeline.executor._CLEANUP_TIMEOUT_SECONDS", 0.01):
            task = asyncio.create_task(
                self.admitted_execute(
                    self.capability(),
                    self.request(),
                    self.metadata(),
                    timeout_seconds=60,
                    launcher=launcher,
                )
            )
            await launch_started.wait()
            task.cancel()
            with self.assertRaisesRegex(RuntimeError, "process handle"):
                await task
            return_process.set()
            for _ in range(100):
                if process.killed:
                    break
                await asyncio.sleep(0.001)
            else:
                self.fail("Late process handle was not cleaned up")

    async def test_cleanup_is_bounded_when_reap_hangs(self) -> None:
        class UnreapableProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.returncode = None

            async def communicate(self) -> tuple[bytes, bytes]:
                await asyncio.Event().wait()

        process = UnreapableProcess()

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            return process

        with mock.patch("dfinsta_pipeline.executor._CLEANUP_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(RuntimeError, "did not exit"):
                await asyncio.wait_for(
                    self.admitted_execute(
                        self.capability(),
                        self.request(),
                        self.metadata(),
                        timeout_seconds=0.01,
                        launcher=launcher,
                    ),
                    timeout=0.2,
                )
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
