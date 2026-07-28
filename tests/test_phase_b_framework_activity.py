import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from temporalio import activity

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_install_frameworks_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.contracts import ArtifactRef, canonical_json, canonical_sha256
from dfinsta_pipeline.decoded_artifact import load_decoded_tree
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    CapabilityBinding,
    ReplayFrameworkCacheReceiptV1,
)
from tests.test_phase_b_replay_contracts import (
    admit_v3,
    bind_v3_fixture,
    capability_for_plan,
    fixture_v2,
    fixture_v3,
    framework_contract_receipt,
    profile_v3,
)


def framework_activity_fixture(
    executable_sha256: str,
    package_ids: tuple[int, ...] = (2, 10),
    *,
    framework_payload_suffix: bytes = b"",
):
    base = fixture_v2(
        True,
        framework_package_ids=package_ids,
        framework_payload_suffix=framework_payload_suffix,
    )
    profile = profile_v3(
        True,
        framework_package_ids=package_ids,
        framework_payload_suffix=framework_payload_suffix,
    )
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


class InstallProcess:
    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        returncode: int = 0,
        output_package_id: int | None = None,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.returncode: int | None = returncode
        self.output_package_id = output_package_id
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.returncode == 0:
            source = self.cwd / self.argv[1]
            framework = self.cwd / self.argv[2]
            package_id = self.output_package_id or int(source.stem)
            (framework / f"{package_id}.apk").write_bytes(source.read_bytes())
        self.reaped = True
        return b"installed", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1


class BlockingInstallProcess(InstallProcess):
    def __init__(self, argv: tuple[str, ...], cwd: Path) -> None:
        super().__init__(argv, cwd)
        self.returncode = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if not self.killed:
            await self.release.wait()
        self.reaped = True
        return b"", b""


class ReplayFrameworkActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="framework-activity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "installer"
        self.executable.write_bytes(b"synthetic framework installer")
        executable_sha256 = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        self.case = framework_activity_fixture(executable_sha256)
        self.admitted = admit_v3(self.case)
        self.launches: list[dict[str, Any]] = []
        self.returncodes: list[int] = []
        self.output_package_ids: list[int] = []
        self.blocking: BlockingInstallProcess | None = None

        async def launcher(*argv: str, **kwargs: Any) -> InstallProcess:
            call = {"argv": argv, **kwargs}
            self.launches.append(call)
            cwd = Path(kwargs["cwd"])
            if self.blocking is not None:
                self.blocking.argv = argv
                self.blocking.cwd = cwd
                return self.blocking
            returncode = self.returncodes.pop(0) if self.returncodes else 0
            output_package_id = (
                self.output_package_ids.pop(0) if self.output_package_ids else None
            )
            return InstallProcess(argv, cwd, returncode, output_package_id)

        self.launcher = launcher
        self.configure(self.root / "run")

    def configure(self, root: Path, *, executors: bool = True) -> None:
        capability = self.admitted.capability("install_framework")
        configure_runtime(
            root / "state",
            attempts_root=root / "attempts",
            executor_paths=(
                {capability.executable_sha256: self.executable} if executors else {}
            ),
            launcher=self.launcher,
        )
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

    async def invoke(
        self, candidate: AdmittedReplayV3 | None = None, owner: str = "framework-owner"
    ) -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_install_frameworks_checkpoint_activity(
                candidate or self.admitted
            )

    @staticmethod
    def receipt(output: ArtifactRef) -> ReplayFrameworkCacheReceiptV1:
        return ReplayFrameworkCacheReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def blob(reference: ArtifactRef | str) -> Path:
        digest = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / digest[:2] / digest

    def sole_operation(self) -> tuple[str, str]:
        with runtime().ledger._connection() as connection:
            row = connection.execute(
                "SELECT operation_key, status FROM operation_claims"
            ).fetchone()
        assert row is not None
        return row

    async def test_multiple_framework_happy_path_is_sequential_and_fully_bound(self) -> None:
        _, _, expected_installations, expected_requests = (
            activities._replay_framework_operation_identity(self.admitted)
        )
        output = await self.invoke()
        key, status = self.sole_operation()
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.framework_cache_manifest)
        workspace = (
            runtime().attempts_root
            / key
            / hashlib.sha256(b"framework-owner").hexdigest()
        )

        self.assertEqual(status, "completed")
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(
            tuple(call["argv"][1:] for call in self.launches),
            (
                ("framework-apks/2.apk", "framework", "tool"),
                ("framework-apks/10.apk", "framework", "tool"),
            ),
        )
        self.assertEqual(
            tuple(item.package_id for item in receipt.installations), (2, 10)
        )
        self.assertEqual(receipt.installations, expected_installations)
        self.assertEqual(
            tuple(item.framework_apk for item in receipt.installations),
            tuple(item.input_artifact for item in expected_requests),
        )
        self.assertEqual(
            tuple(item.execution_request_sha256 for item in receipt.installations),
            tuple(item.canonical_identity for item in expected_requests),
        )
        self.assertEqual(
            tuple(item.arguments for item in expected_requests),
            (
                (
                    ("framework_apk", "framework-apks/2.apk"),
                    ("framework_dir", "framework"),
                    ("tool", "tool"),
                ),
                (
                    ("framework_apk", "framework-apks/10.apk"),
                    ("framework_dir", "framework"),
                    ("tool", "tool"),
                ),
            ),
        )
        self.assertEqual(
            tuple(entry.path for entry in manifest.entries), ("10.apk", "2.apk")
        )
        self.assertEqual(
            (workspace / "framework/2.apk").read_bytes(),
            self.case.resolve(self.admitted.request.frameworks[0].artifact),
        )
        self.assertEqual(
            (workspace / "framework/10.apk").read_bytes(),
            self.case.resolve(self.admitted.request.frameworks[1].artifact),
        )
        self.assertEqual(
            (workspace / "tool").read_bytes(),
            self.case.resolve(
                next(
                    item.artifact
                    for item in self.admitted.request.tools
                    if item.tool_id
                    == self.admitted.profile.tool_for_role("install_framework").tool_id
                )
            ),
        )
        self.assertEqual(
            receipt.framework_cache_manifest.input_hashes,
            receipt.execution_input_hashes,
        )
        self.assertEqual(output.input_hashes, receipt.receipt_input_hashes)
        self.assertEqual(output.kind, "replay-framework-cache-receipt-v1")
        self.assertEqual(output.producer_operation_id, receipt.operation_key)
        self.assertEqual(runtime().ledger.operation_event_count(key, "pending"), 1)
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 1)
        self.assertEqual(runtime().ledger.operation_event_count(key, "completed"), 1)

    async def test_one_framework_and_second_run_adoption_do_no_repeated_work(self) -> None:
        single_case = framework_activity_fixture(
            hashlib.sha256(self.executable.read_bytes()).hexdigest(), (2,)
        )
        single = admit_v3(single_case)
        self.case = single_case
        self.admitted = single
        self.configure(self.root / "single")
        output = await self.invoke()
        launches = len(self.launches)
        retry_workspace = (
            runtime().attempts_root
            / output.producer_operation_id
            / hashlib.sha256(b"retry-owner").hexdigest()
        )
        with mock.patch.object(activities, "execute") as execute:
            adopted = await self.invoke(owner="retry-owner")
        execute.assert_not_called()
        self.assertEqual(adopted, output)
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(retry_workspace.exists())

    async def test_authority_first_exact_dispatch_and_no_framework_rejection(self) -> None:
        unrecorded = admit_v3(fixture_v3(True, framework_package_ids=(2, 10)))
        with (
            mock.patch.object(runtime().ledger, "begin_operation") as begin,
            mock.patch.object(runtime().store, "read_bytes") as cas,
            mock.patch.object(activities, "_open_or_create_directory") as workspace,
        ):
            with self.assertRaisesRegex(ValueError, "authority"):
                await self.invoke(unrecorded)
        begin.assert_not_called()
        cas.assert_not_called()
        workspace.assert_not_called()

        shadow = mock.Mock(side_effect=AssertionError("instance shadow called"))
        runtime().ledger.require_admitted_replay_v3 = shadow  # type: ignore[method-assign]
        await self.invoke()
        shadow.assert_not_called()

        no_framework = admit_v3(fixture_v3())
        other = self.root / "no-framework"
        configure_runtime(other / "state", attempts_root=other / "attempts")
        self.record_authority(no_framework)
        with (
            mock.patch.object(runtime().ledger, "begin_operation") as begin,
            mock.patch.object(runtime().store, "read_bytes") as cas,
            mock.patch.object(activities, "_open_or_create_directory") as workspace,
        ):
            with self.assertRaisesRegex(ValueError, "nonempty frameworks"):
                await self.invoke(no_framework)
        begin.assert_not_called()
        cas.assert_not_called()
        workspace.assert_not_called()

    async def test_missing_executable_and_cas_release_before_workspace(self) -> None:
        for name, missing_cas in (("executable", False), ("cas", True)):
            with self.subTest(name=name):
                self.configure(self.root / f"missing-{name}", executors=missing_cas)
                if missing_cas:
                    tool = next(
                        item.artifact
                        for item in self.admitted.request.tools
                        if item.tool_id
                        == self.admitted.profile.tool_for_role("install_framework").tool_id
                    )
                    self.blob(tool).unlink()
                with mock.patch.object(
                    activities, "_open_or_create_directory"
                ) as workspace:
                    with self.assertRaises((FileNotFoundError, ValueError)):
                        await self.invoke(owner=f"owner-{name}")
                workspace.assert_not_called()
                _, status = self.sole_operation()
                self.assertEqual(status, "pending")

    async def test_nonzero_cancellation_and_capture_failure_quarantine(self) -> None:
        self.returncodes = [0, 9]
        with self.assertRaisesRegex(RuntimeError, "10 failed with exit code 9"):
            await self.invoke(owner="nonzero-owner")
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

        self.configure(self.root / "capture-failure")
        with mock.patch.object(
            activities, "capture_decoded_tree_fd", side_effect=RuntimeError("capture")
        ):
            with self.assertRaisesRegex(RuntimeError, "capture"):
                await self.invoke(owner="capture-owner")
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

        self.configure(self.root / "cancel")
        self.blocking = BlockingInstallProcess((), self.root)
        task = asyncio.create_task(self.invoke(owner="cancel-owner"))
        await self.blocking.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        key, status = self.sole_operation()
        self.assertTrue(self.blocking.killed)
        self.assertTrue(self.blocking.reaped)
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_each_install_must_add_its_declared_id_without_overwrite(self) -> None:
        for name, output_ids in (
            ("swapped", [10, 2]),
            ("overwrite", [2, 2]),
        ):
            with self.subTest(name=name):
                self.configure(self.root / f"wrong-id-{name}")
                self.output_package_ids = output_ids
                with self.assertRaisesRegex(ValueError, "declared package id"):
                    await self.invoke(owner=f"owner-{name}")
                key, status = self.sole_operation()
                self.assertEqual(status, "quarantined")
                self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_post_effect_completion_failure_adopts_without_launch_or_workspace(self) -> None:
        with mock.patch.object(
            activities.Ledger,
            "complete_operation",
            side_effect=RuntimeError("completion unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "completion unavailable"):
                await self.invoke(owner="first-owner")
        key, status = self.sole_operation()
        self.assertEqual(status, "effect")
        launches = len(self.launches)
        retry_workspace = (
            runtime().attempts_root
            / key
            / hashlib.sha256(b"retry-owner").hexdigest()
        )
        output = await self.invoke(owner="retry-owner")
        self.assertEqual(runtime().ledger.operation_status(key), "completed")
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(retry_workspace.exists())
        self.assertEqual(output.producer_operation_id, key)

    async def test_receipt_manifest_and_child_tampering_fail_before_repeated_work(self) -> None:
        for layer, mutation in (
            ("receipt", "corrupt"),
            ("manifest", "missing"),
            ("child", "hardlink"),
        ):
            with self.subTest(layer=layer):
                self.configure(self.root / f"tamper-{layer}")
                output = await self.invoke(owner="first-owner")
                receipt = self.receipt(output)
                manifest = load_decoded_tree(
                    runtime().store, receipt.framework_cache_manifest
                )
                child = next(entry for entry in manifest.entries if entry.kind == "file")
                target = {
                    "receipt": self.blob(output),
                    "manifest": self.blob(receipt.framework_cache_manifest),
                    "child": self.blob(child.sha256),
                }[layer]
                if mutation == "corrupt":
                    target.chmod(0o644)
                    target.write_bytes(b"corrupt")
                elif mutation == "missing":
                    target.unlink()
                else:
                    (self.root / f"hardlink-{layer}").hardlink_to(target)
                launches = len(self.launches)
                with mock.patch.object(
                    activities, "_open_or_create_directory"
                ) as workspace:
                    with self.assertRaises((OSError, ValueError)):
                        await self.invoke(owner="retry-owner")
                workspace.assert_not_called()
                self.assertEqual(len(self.launches), launches)

    def test_framework_and_decode_identities_bind_content_not_physical_roots(self) -> None:
        first = self.admitted
        changed_artifact = admit_v3(
            framework_activity_fixture(
                hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                (2, 10),
                framework_payload_suffix=b" changed",
            )
        )
        changed_package = admit_v3(
            framework_activity_fixture(
                hashlib.sha256(self.executable.read_bytes()).hexdigest(), (3,)
            )
        )
        first_key = activities._replay_framework_operation_identity(first)[0]
        self.assertNotEqual(
            first_key, activities._replay_framework_operation_identity(changed_artifact)[0]
        )
        self.assertNotEqual(
            activities._replay_framework_operation_identity(changed_artifact)[0],
            activities._replay_framework_operation_identity(changed_package)[0],
        )

        self.assertEqual(first_key, activities._replay_framework_operation_identity(first)[0])

    async def test_physical_roots_and_owners_do_not_change_canonical_output(self) -> None:
        first = await self.invoke(owner="owner-a")
        first_bytes = runtime().store.read_bytes(first)
        second_root = self.root / "physical-second"
        second_executable = second_root / "installer"
        second_executable.parent.mkdir()
        second_executable.write_bytes(self.executable.read_bytes())
        self.executable = second_executable
        self.configure(second_root)
        second = await self.invoke(owner="owner-b")
        self.assertEqual(second.producer_operation_id, first.producer_operation_id)
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(runtime().store.read_bytes(second), first_bytes)

    def test_strict_framework_receipt_parser_rejects_duplicates_and_noncanonical_json(self) -> None:
        receipt = framework_contract_receipt()
        for payload in (
            b'{"schema_version":1,"schema_version":1}',
            b" " + canonical_json(receipt).encode(),
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                activities._strict_replay_framework_cache_receipt(payload)

    def test_temporal_metadata_exists_but_worker_and_workflow_do_not_register(self) -> None:
        self.assertIsNotNone(
            activity._Definition.from_callable(  # type: ignore[attr-defined]
                replay_install_frameworks_checkpoint_activity
            )
        )
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "src/dfinsta_pipeline/worker.py",
            "src/dfinsta_pipeline/workflow.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("replay_install_frameworks_checkpoint_activity", source)


if __name__ == "__main__":
    unittest.main()
