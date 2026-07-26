import asyncio
import hashlib
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.executor import (
    ExecutionMetadata,
    ExecutionRequest,
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

    async def test_valid_admission_uses_exec_and_excludes_ambient_secret(self) -> None:
        captured: dict[str, Any] = {}

        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            captured.update(argv=argv, **kwargs)
            return FakeProcess(stdout="success".encode("utf-8"))

        os.environ["PHASE_A_SECRET"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "PHASE_A_SECRET", None)
        result = await execute(
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

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            await execute(
                self.capability(executable_sha256="0" * 64),
                self.request(),
                self.metadata(),
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
                await execute(
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
            await execute(
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
            await execute(
                self.capability(),
                self.request(arguments=(("input", "input.apk"), ("output", "link/out"))),
                self.metadata(),
                timeout_seconds=1,
            )

    async def test_artifact_and_output_kinds_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "Input artifact kind"):
            await execute(
                self.capability(), self.request(input_artifact=artifact("report")), self.metadata(), timeout_seconds=1
            )
        with self.assertRaisesRegex(ValueError, "Output artifact kind"):
            await execute(
                self.capability(), self.request(output_kind="report"), self.metadata(), timeout_seconds=1
            )

    async def test_undeclared_mutation_is_rejected(self) -> None:
        async def launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            (self.workspace / "unexpected.txt").write_text("changed", encoding="utf-8")
            return FakeProcess()

        with self.assertRaisesRegex(PermissionError, "unexpected.txt"):
            await execute(
                self.capability(), self.request(), self.metadata(), timeout_seconds=1, launcher=launcher
            )

        async def allowed_launcher(*argv: str, **kwargs: Any) -> FakeProcess:
            output = self.workspace / "out"
            output.mkdir()
            (output / "result.txt").write_text("allowed", encoding="utf-8")
            return FakeProcess()

        result = await execute(
            self.capability(), self.request(), self.metadata(), timeout_seconds=1, launcher=allowed_launcher
        )
        self.assertEqual(result.returncode, 0)

    def test_split_apk_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Split APK"):
            self.request(apk_composition="split")

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
            await execute(
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
            execute(
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


if __name__ == "__main__":
    unittest.main()
