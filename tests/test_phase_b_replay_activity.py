import asyncio
import hashlib
import inspect
import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest import mock

from temporalio import activity

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_decode_checkpoint_activity,
    replay_install_frameworks_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.contracts import ArtifactRef, canonical_json, canonical_sha256
from dfinsta_pipeline.decoded_artifact import load_decoded_tree
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    CapabilityBinding,
    ReplayDecodeCheckpointResultV1,
    ReplayDecodedTreeReceiptV1,
    ReplayDecodedTreeReceiptV2,
    ReplayFrameworkCacheReceiptV1,
)
from tests.test_phase_b_replay_contracts import (
    admit_v3,
    bind_v3_fixture,
    capability_for_plan,
    fixture_v2,
    fixture_v3,
    profile_v3,
)


def activity_fixture(
    executable_sha256: str,
    *,
    with_framework: bool = False,
    framework_package_ids: tuple[int, ...] | None = None,
):
    base = fixture_v2(
        with_framework, framework_package_ids=framework_package_ids
    )
    profile = profile_v3(
        with_framework, framework_package_ids=framework_package_ids
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


class FakeProcess:
    def __init__(
        self, stdout: bytes = b"decoded", stderr: bytes = b"warning", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = returncode
        self.killed = False
        self.reaped = False
        self.cwd: Path | None = None
        self.output_builder = self._build_output

    @staticmethod
    def _build_output(output: Path) -> None:
        (output / "empty").mkdir(parents=True)
        (output / "smali").mkdir()
        (output / "smali" / "Example.smali").write_text(
            ".class public LExample;\n", encoding="utf-8"
        )
        (output / "resources.bin").write_bytes(bytes(range(256)))

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.returncode == 0:
            assert self.cwd is not None
            self.output_builder(self.cwd / "output")
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

    def test_decoded_tree_receipt_is_strict_and_binds_nested_lineage(self) -> None:
        input_apk = ArtifactRef(
            1,
            "stock-apk",
            "1" * 64,
            3,
            f"cas://sha256/{'1' * 64}",
            "stock-producer",
            (),
        )
        execution_inputs = (
            "2" * 64,
            canonical_sha256(input_apk),
            "3" * 64,
            "4" * 64,
            "5" * 64,
            "6" * 64,
            "7" * 64,
        )
        manifest = ArtifactRef(
            1,
            "decoded-tree-manifest-v1",
            "8" * 64,
            4,
            f"cas://sha256/{'8' * 64}",
            "9" * 64,
            execution_inputs,
        )
        receipt = ReplayDecodedTreeReceiptV1(
            1,
            "stock_input",
            "2" * 64,
            input_apk,
            "apktool-v1",
            "3" * 64,
            "decode",
            "4" * 64,
            "5" * 64,
            "6" * 64,
            "7" * 64,
            manifest,
            "a" * 64,
            "9" * 64,
            True,
        )
        self.assertEqual(ReplayDecodedTreeReceiptV1.from_dict(asdict(receipt)), receipt)
        self.assertEqual(receipt.execution_input_hashes, execution_inputs)
        self.assertEqual(
            receipt.receipt_input_hashes,
            (*execution_inputs, canonical_sha256(manifest), "a" * 64),
        )

        mutations = (
            {**asdict(receipt), "schema_version": True},
            {**asdict(receipt), "decoded_apk_role": "other"},
            {**asdict(receipt), "input_apk": {**asdict(input_apk), "kind": "final-apk"}},
            {**asdict(receipt), "toolchain_profile_id": "UPPER"},
            {**asdict(receipt), "role": "build"},
            {**asdict(receipt), "success": 1},
            {**asdict(receipt), "operation_key": "A" * 64},
            {
                **asdict(receipt),
                "decoded_tree_manifest": {
                    **asdict(manifest),
                    "input_hashes": execution_inputs[:-1],
                },
            },
            {**asdict(receipt), "extra": "field"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayDecodedTreeReceiptV1.from_dict(mutation)

    def test_activity_receipt_parser_rejects_duplicate_and_noncanonical_json(self) -> None:
        for payload in (
            b'{"schema_version":1,"schema_version":1}',
            b'{ "schema_version":1}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                activities._strict_replay_decoded_tree_receipt(payload)


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
            self.process.cwd = Path(kwargs["cwd"])
            if (self.process.cwd / "output").exists():
                raise AssertionError("output existed before launch")
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

    @staticmethod
    def receipt(output: ArtifactRef) -> ReplayDecodedTreeReceiptV1:
        return ReplayDecodedTreeReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def blob_path(reference: ArtifactRef | str) -> Path:
        digest = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / digest[:2] / digest

    def configure_fresh_runtime(self, name: str) -> None:
        root = self.root / name
        self.process = FakeProcess()
        configure_runtime(
            root / "state",
            attempts_root=root / "attempts",
            executor_paths={
                self.admitted.capability("decode").executable_sha256: self.executable
            },
            launcher=runtime().launcher,
        )
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture",
                data=payload,
                producer_operation_id="fixture-producer",
                input_hashes=(),
            )
        self.record_authority(self.admitted)

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
        self.assertTrue((workspace / "output").is_dir())
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
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
        pre_framework_operation_input = {
            "schema_version": 1,
            "decoded_apk_role": receipt.decoded_apk_role,
            "admitted_replay_sha256": receipt.admitted_replay_sha256,
            "input_apk": receipt.input_apk,
            "toolchain_profile_id": receipt.toolchain_profile_id,
            "toolchain_profile_sha256": receipt.toolchain_profile_sha256,
            "role": receipt.role,
            "execution_plan_sha256": receipt.execution_plan_sha256,
            "executor_capability_sha256": receipt.executor_capability_sha256,
            "tool_artifact_sha256": receipt.tool_artifact_sha256,
            "execution_request_sha256": receipt.execution_request_sha256,
        }
        self.assertEqual(
            key,
            activities.operation_key(
                "replay_decode_tree_v1", pre_framework_operation_input
            ),
        )
        self.assertEqual(
            activities._replay_decode_operation_identity(admitted),
            (
                key,
                canonical_sha256(pre_framework_operation_input),
                receipt.execution_request_sha256,
            ),
        )
        old_domain_key = activities.operation_key(
            "replay_decode_checkpoint",
            {
                "schema_version": 1,
                "admitted_replay_sha256": receipt.admitted_replay_sha256,
                "role": receipt.role,
                "execution_plan_sha256": receipt.execution_plan_sha256,
                "executor_capability_sha256": receipt.executor_capability_sha256,
                "tool_artifact_sha256": receipt.tool_artifact_sha256,
                "execution_request_sha256": receipt.execution_request_sha256,
            },
        )
        self.assertEqual(output.kind, "replay-decoded-tree-receipt-v1")
        self.assertIs(type(receipt), ReplayDecodedTreeReceiptV1)
        self.assertNotEqual(key, old_domain_key)
        self.assertEqual(receipt.admitted_replay_sha256, admitted.sha256)
        self.assertEqual(receipt.input_apk, admitted.request.stock_apk)
        self.assertEqual(receipt.execution_request_sha256, request.canonical_identity)
        self.assertEqual(receipt.tool_artifact_sha256, tool.artifact.sha256)
        self.assertEqual(receipt.decoded_tree_semantic_sha256, manifest.decoded_tree_sha256)
        self.assertEqual(receipt.decoded_tree_manifest.input_hashes, receipt.execution_input_hashes)
        self.assertEqual(output.input_hashes, receipt.receipt_input_hashes)
        self.assertEqual(
            tuple((entry.path, entry.kind) for entry in manifest.entries),
            (
                ("empty", "directory"),
                ("resources.bin", "file"),
                ("smali", "directory"),
                ("smali/Example.smali", "file"),
            ),
        )
        self.assertTrue(receipt.success)

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
        normalized = AdmittedReplayV3.from_dict(
            asdict(self.admitted)
        )
        self.assertIsNot(normalized, self.admitted)
        with mock.patch.object(
            activities.Ledger, "require_admitted_replay_v3", return_value=normalized
        ):
            output = await self.invoke(self.admitted)
        decoded = self.receipt(output)
        self.assertEqual(decoded.admitted_replay_sha256, normalized.sha256)
        self.assertEqual(decoded.admitted_replay_sha256, self.admitted.sha256)

    async def test_authority_lookup_cannot_be_shadowed_on_ledger_instance(self) -> None:
        shadow = mock.Mock(side_effect=AssertionError("instance shadow called"))
        runtime().ledger.require_admitted_replay_v3 = shadow  # type: ignore[method-assign]
        output = await self.invoke()
        shadow.assert_not_called()
        self.assertEqual(self.receipt(output).admitted_replay_sha256, self.admitted.sha256)

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
        self.assertEqual(
            self.receipt(second).decoded_tree_manifest,
            ReplayDecodedTreeReceiptV1.from_dict(json.loads(first_bytes)).decoded_tree_manifest,
        )

    async def test_empty_directory_and_file_changes_alter_tree_and_receipt_identity(self) -> None:
        first = await self.invoke(owner="first-owner")
        first_receipt = self.receipt(first)

        async def run_variant(
            name: str, output_builder: Any
        ) -> tuple[ArtifactRef, ReplayDecodedTreeReceiptV1]:
            variant_root = self.root / name
            variant_executable = variant_root / "executor.bin"
            variant_executable.parent.mkdir()
            variant_executable.write_bytes(self.executable.read_bytes())
            self.process = FakeProcess()
            self.process.output_builder = output_builder
            configure_runtime(
                variant_root / "state",
                attempts_root=variant_root / "attempts",
                executor_paths={
                    self.admitted.capability("decode").executable_sha256: variant_executable
                },
                launcher=runtime().launcher,
            )
            for payload in self.case.payloads.values():
                runtime().store.put_bytes(
                    kind="fixture",
                    data=payload,
                    producer_operation_id="fixture-producer",
                    input_hashes=(),
                )
            self.record_authority(self.admitted)
            output = await self.invoke(owner=f"owner-{name}")
            return output, self.receipt(output)

        def extra_empty_directory(output: Path) -> None:
            FakeProcess._build_output(output)
            (output / "another-empty").mkdir()

        directory_output, directory_receipt = await run_variant(
            "changed-directory", extra_empty_directory
        )
        self.assertEqual(directory_output.producer_operation_id, first.producer_operation_id)
        self.assertNotEqual(
            directory_receipt.decoded_tree_manifest.sha256,
            first_receipt.decoded_tree_manifest.sha256,
        )
        self.assertEqual(
            directory_receipt.decoded_tree_semantic_sha256,
            first_receipt.decoded_tree_semantic_sha256,
        )
        self.assertNotEqual(directory_output.sha256, first.sha256)

        def changed_file(output: Path) -> None:
            FakeProcess._build_output(output)
            (output / "resources.bin").write_bytes(b"changed")

        file_output, file_receipt = await run_variant("changed-file", changed_file)
        self.assertEqual(file_output.producer_operation_id, first.producer_operation_id)
        self.assertNotEqual(
            file_receipt.decoded_tree_manifest.sha256,
            first_receipt.decoded_tree_manifest.sha256,
        )
        self.assertNotEqual(
            file_receipt.decoded_tree_semantic_sha256,
            first_receipt.decoded_tree_semantic_sha256,
        )
        self.assertNotEqual(file_output.sha256, first.sha256)

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
        with mock.patch.object(
            runtime().store, "read_bytes", wraps=runtime().store.read_bytes
        ) as read_bytes:
            with self.assertRaisesRegex(ValueError, "runtime executable"):
                await self.invoke()
        read_bytes.assert_not_called()
        _, status = self.sole_operation()
        self.assertEqual(status, "pending")
        self.assertFalse(runtime().attempts_root.exists())
        self.assertEqual(self.launches, [])

    async def test_released_preworkspace_claim_can_retry_with_a_new_owner(self) -> None:
        state_root = self.root / "released-state"
        attempts_root = self.root / "released-attempts"
        configure_runtime(
            state_root,
            attempts_root=attempts_root,
            executor_paths={},
            launcher=runtime().launcher,
        )
        self.record_authority(self.admitted)
        with self.assertRaisesRegex(ValueError, "runtime executable"):
            await self.invoke(owner="first-owner")

        configure_runtime(
            state_root,
            attempts_root=attempts_root,
            executor_paths={
                self.admitted.capability("decode").executable_sha256: self.executable
            },
            launcher=runtime().launcher,
        )
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture",
                data=payload,
                producer_operation_id="fixture-producer",
                input_hashes=(),
            )
        self.record_authority(self.admitted)
        output = await self.invoke(owner="second-owner")
        self.assertEqual(runtime().ledger.operation_status(output.producer_operation_id), "completed")
        self.assertEqual(len(self.launches), 1)

    async def test_release_failure_does_not_mask_preworkspace_failure(self) -> None:
        configure_runtime(
            self.root / "release-failure-state",
            attempts_root=self.root / "release-failure-attempts",
            executor_paths={},
            launcher=runtime().launcher,
        )
        self.record_authority(self.admitted)
        with mock.patch.object(
            activities.Ledger,
            "release_pending_operation",
            side_effect=RuntimeError("release unavailable"),
        ):
            with self.assertRaisesRegex(ValueError, "runtime executable") as caught:
                await self.invoke()
        self.assertTrue(
            any(
                "Pending operation release failed: RuntimeError: release unavailable" in note
                for note in caught.exception.__notes__
            )
        )

    async def test_nonzero_exit_quarantines_without_effect(self) -> None:
        self.process = FakeProcess(returncode=7)
        with self.assertRaisesRegex(RuntimeError, "exit code 7"):
            await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_invalid_success_outputs_quarantine_without_effect(self) -> None:
        builders = {
            "missing": lambda output: None,
            "file": lambda output: output.write_bytes(b"not a directory"),
            "symlink": lambda output: output.symlink_to(self.root),
            "unsafe-path": lambda output: (
                output.mkdir(),
                (output / "bad:name").write_bytes(b"bad"),
            ),
            "hardlink": lambda output: (
                output.mkdir(),
                (output / "first").write_bytes(b"same"),
                (output / "second").hardlink_to(output / "first"),
            ),
            "special": lambda output: (
                output.mkdir(),
                os.mkfifo(output / "fifo"),
            ),
        }
        for index, (name, builder) in enumerate(builders.items()):
            with self.subTest(name=name):
                if index:
                    state = self.root / f"invalid-{name}"
                    configure_runtime(
                        state / "state",
                        attempts_root=state / "attempts",
                        executor_paths={
                            self.admitted.capability("decode").executable_sha256: self.executable
                        },
                        launcher=runtime().launcher,
                    )
                    for payload in self.case.payloads.values():
                        runtime().store.put_bytes(
                            kind="fixture",
                            data=payload,
                            producer_operation_id="fixture-producer",
                            input_hashes=(),
                        )
                    self.record_authority(self.admitted)
                self.process = FakeProcess()
                self.process.output_builder = builder
                with self.assertRaises((OSError, ValueError)):
                    await self.invoke(owner=f"owner-{name}")
                key, status = self.sole_operation()
                self.assertEqual(status, "quarantined")
                self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_capture_failure_after_workspace_quarantines_without_effect(self) -> None:
        with mock.patch.object(
            activities, "capture_decoded_tree_fd", side_effect=RuntimeError("capture failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_failure_after_receipt_publication_quarantines_without_effect(self) -> None:
        with mock.patch.object(
            activities.Ledger, "record_effect", side_effect=RuntimeError("effect unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "effect unavailable"):
                await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)
        receipt_blobs = []
        for prefix in (runtime().store.root / "sha256").iterdir():
            for blob in prefix.iterdir():
                try:
                    value = json.loads(blob.read_bytes())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and value.get("operation_key") == key:
                    receipt_blobs.append(blob)
        self.assertEqual(len(receipt_blobs), 1)

    async def test_tampered_executor_fails_before_launch_and_effect(self) -> None:
        self.executable.write_bytes(b"tampered executable bytes")
        with self.assertRaisesRegex(ValueError, "Runtime executable"):
            await self.invoke()
        key, status = self.sole_operation()
        self.assertEqual(self.launches, [])
        self.assertEqual(status, "pending")
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
            await self.invoke(owner="first-owner")
        self.assertEqual(self.launches, [])
        self.assertEqual(runtime().ledger.operation_status(key), "pending")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_overlapping_owner_is_rejected_without_workspace_or_launch(self) -> None:
        process = BlockingProcess()
        self.process = process
        first = asyncio.create_task(self.invoke(owner="first-owner"))
        await process.started.wait()
        key, status = self.sole_operation()
        self.assertEqual(status, "pending")
        with self.assertRaisesRegex(ValueError, "already claimed"):
            await self.invoke(owner="second-owner")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(runtime().ledger.operation_event_count(key, "pending"), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")

    async def test_workspace_path_replacement_cannot_redirect_descriptor_capture(self) -> None:
        capture = activities.capture_decoded_tree_fd

        def replace_workspace_path(*args: Any, **kwargs: Any) -> ArtifactRef:
            workspace = Path(self.launches[0]["cwd"])
            moved = workspace.with_name(f"{workspace.name}-moved")
            workspace.rename(moved)
            replacement = workspace / "output"
            replacement.mkdir(parents=True)
            (replacement / "attacker.txt").write_bytes(b"replacement")
            return capture(*args, **kwargs)

        with mock.patch.object(
            activities, "capture_decoded_tree_fd", side_effect=replace_workspace_path
        ):
            output = await self.invoke()
        manifest = load_decoded_tree(
            runtime().store, self.receipt(output).decoded_tree_manifest
        )
        self.assertEqual(
            tuple(entry.path for entry in manifest.entries),
            ("empty", "resources.bin", "smali", "smali/Example.smali"),
        )

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
        path.chmod(0o644)
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

    async def test_missing_manifest_fails_adoption_without_launch_or_completion(self) -> None:
        output = await self.invoke(owner="first-owner")
        receipt = self.receipt(output)
        self.blob_path(receipt.decoded_tree_manifest).unlink()
        launches = len(self.launches)
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.invoke(owner="retry-owner")
        self.assertEqual(len(self.launches), launches)
        self.assertEqual(runtime().ledger.operation_status(output.producer_operation_id), "completed")

    async def test_hardlinked_child_blob_fails_adoption_without_launch(self) -> None:
        output = await self.invoke(owner="first-owner")
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
        child = next(entry for entry in manifest.entries if entry.kind == "file")
        link = self.root / "child-link"
        link.hardlink_to(self.blob_path(child.sha256))
        launches = len(self.launches)
        with self.assertRaises(ValueError):
            await self.invoke(owner="retry-owner")
        self.assertEqual(len(self.launches), launches)

    async def test_receipt_manifest_and_child_blob_tampering_all_fail_closed(self) -> None:
        for layer in ("receipt", "manifest", "child"):
            for mutation in ("corrupt", "missing", "writable", "hardlinked"):
                with self.subTest(layer=layer, mutation=mutation):
                    self.configure_fresh_runtime(f"tamper-{layer}-{mutation}")
                    output = await self.invoke(owner="first-owner")
                    receipt = self.receipt(output)
                    manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
                    child = next(entry for entry in manifest.entries if entry.kind == "file")
                    references = {
                        "receipt": output.sha256,
                        "manifest": receipt.decoded_tree_manifest.sha256,
                        "child": child.sha256,
                    }
                    path = self.blob_path(references[layer])
                    if mutation == "corrupt":
                        path.chmod(0o644)
                        path.write_bytes(b"corrupt")
                    elif mutation == "missing":
                        path.unlink()
                    elif mutation == "writable":
                        path.chmod(0o644)
                    else:
                        (self.root / f"link-{layer}-{mutation}").hardlink_to(path)

                    launches = len(self.launches)
                    completion_events = runtime().ledger.operation_event_count(
                        output.producer_operation_id, "completed"
                    )
                    retry_workspace = (
                        runtime().attempts_root
                        / output.producer_operation_id
                        / hashlib.sha256(b"retry-owner").hexdigest()
                    )
                    with self.assertRaises((OSError, ValueError)):
                        await self.invoke(owner="retry-owner")
                    self.assertEqual(len(self.launches), launches)
                    self.assertFalse(retry_workspace.exists())
                    self.assertEqual(
                        runtime().ledger.operation_event_count(
                            output.producer_operation_id, "completed"
                        ),
                        completion_events,
                    )

    async def test_caller_cannot_substitute_plan_or_tool(self) -> None:
        substituted = admit_v3(fixture_v3(with_framework=True))
        with self.assertRaisesRegex(ValueError, "does not match candidate"):
            await self.invoke(substituted)
        self.assertEqual(self.launches, [])
        self.assertFalse(runtime().attempts_root.exists())


class FrameworkDecodeProcess:
    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        expected_frameworks: dict[str, bytes],
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.expected_frameworks = expected_frameworks
        self.returncode: int | None = 0
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.argv[1].startswith("framework-apks/"):
            source = self.cwd / self.argv[1]
            destination = self.cwd / self.argv[2] / source.name
            prior = {
                path.name: path.read_bytes()
                for path in destination.parent.iterdir()
            }
            destination.write_bytes(source.read_bytes())
            for name, payload in prior.items():
                assert (destination.parent / name).read_bytes() == payload
        else:
            framework = self.cwd / "framework"
            observed = {
                path.name: path.read_bytes() for path in framework.iterdir()
            }
            if observed != self.expected_frameworks:
                raise AssertionError("decode did not receive exact framework cache")
            FakeProcess._build_output(self.cwd / "output")
        self.reaped = True
        return b"ok", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1


class FrameworkReplayDecodeActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="framework-decode-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "executor"
        self.executable.write_bytes(b"combined framework and decode executor")
        executable_sha256 = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        self.case = activity_fixture(
            executable_sha256,
            with_framework=True,
            framework_package_ids=(2, 10),
        )
        self.admitted = admit_v3(self.case)
        self.launches: list[dict[str, Any]] = []
        expected = {
            f"{item.package_id}.apk": self.case.resolve(item.artifact)
            for item in self.admitted.request.frameworks
        }

        async def launcher(*argv: str, **kwargs: Any) -> FrameworkDecodeProcess:
            self.launches.append({"argv": argv, **kwargs})
            return FrameworkDecodeProcess(argv, Path(kwargs["cwd"]), expected)

        self.launcher = launcher
        self.configure(self.root / "run")

    def configure(self, root: Path) -> None:
        configure_runtime(
            root / "state",
            attempts_root=root / "attempts",
            executor_paths={
                capability.executable_sha256: self.executable
                for capability in self.admitted.executor_capabilities
            },
            launcher=self.launcher,
        )
        for payload in self.case.payloads.values():
            runtime().store.put_bytes(
                kind="fixture",
                data=payload,
                producer_operation_id="fixture-producer",
                input_hashes=(),
            )
        runtime().ledger.record_decision(self.admitted.decision)
        runtime().ledger.record_admitted_replay_v3(self.admitted)

    async def install(self, owner: str = "install-owner") -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_install_frameworks_checkpoint_activity(self.admitted)

    async def decode(self, owner: str = "decode-owner") -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_decode_checkpoint_activity(self.admitted)

    @staticmethod
    def framework_receipt(output: ArtifactRef) -> ReplayFrameworkCacheReceiptV1:
        return ReplayFrameworkCacheReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def decode_receipt(output: ArtifactRef) -> ReplayDecodedTreeReceiptV2:
        return ReplayDecodedTreeReceiptV2.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def blob(reference: ArtifactRef | str) -> Path:
        digest = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / digest[:2] / digest

    async def test_predecessor_must_be_completed_before_decode_claim_workspace_or_launch(self) -> None:
        with (
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(activities, "_open_or_create_directory") as workspace,
        ):
            with self.assertRaises(ValueError):
                await self.decode()
        begin.assert_not_called()
        workspace.assert_not_called()
        self.assertEqual(self.launches, [])

        for status in ("pending", "effect", "quarantined"):
            with self.subTest(status=status):
                self.configure(self.root / f"predecessor-{status}")
                framework_output = await self.install(owner=f"install-{status}")
                framework_key = framework_output.producer_operation_id
                with runtime().ledger._connection() as connection:
                    connection.execute(
                        "UPDATE operation_claims SET status = ? WHERE operation_key = ?",
                        (status, framework_key),
                    )
                launches = len(self.launches)
                with (
                    mock.patch.object(activities.Ledger, "begin_operation") as begin,
                    mock.patch.object(activities, "_open_or_create_directory") as workspace,
                ):
                    with self.assertRaises(ValueError):
                        await self.decode(owner=f"decode-{status}")
                begin.assert_not_called()
                workspace.assert_not_called()
                self.assertEqual(len(self.launches), launches)

    async def test_v2_happy_path_binds_and_revalidates_exact_materialized_cache(self) -> None:
        framework_output = await self.install()
        framework_receipt = self.framework_receipt(framework_output)
        with mock.patch.object(
            activities,
            "verify_materialized_decoded_tree",
            wraps=activities.verify_materialized_decoded_tree,
        ) as verify:
            output = await self.decode()
        receipt = self.decode_receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
        self.assertEqual(output.kind, "replay-decoded-tree-receipt-v2")
        self.assertEqual(
            runtime().ledger.operation_status(output.producer_operation_id), "completed"
        )
        self.assertEqual(receipt.completed_framework_cache_receipt, framework_output)
        self.assertEqual(
            receipt.framework_cache_manifest,
            framework_receipt.framework_cache_manifest,
        )
        self.assertEqual(
            receipt.framework_cache_semantic_sha256,
            framework_receipt.framework_cache_semantic_sha256,
        )
        self.assertEqual(receipt.decoded_tree_semantic_sha256, manifest.decoded_tree_sha256)
        self.assertEqual(
            receipt.decoded_tree_manifest.input_hashes,
            receipt.execution_input_hashes,
        )
        self.assertEqual(output.input_hashes, receipt.receipt_input_hashes)
        verify.assert_called_once()

        launches = len(self.launches)
        retry_workspace = (
            runtime().attempts_root
            / output.producer_operation_id
            / hashlib.sha256(b"retry-owner").hexdigest()
        )
        adopted = await self.decode(owner="retry-owner")
        self.assertEqual(adopted, output)
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(retry_workspace.exists())

    async def test_v2_receipt_manifest_and_child_tampering_fail_before_decode_work(self) -> None:
        for layer, mutation in (
            ("receipt", "corrupt"),
            ("manifest", "missing"),
            ("child", "hardlink"),
        ):
            with self.subTest(layer=layer):
                self.configure(self.root / f"tamper-v2-{layer}")
                await self.install(owner=f"install-{layer}")
                output = await self.decode(owner=f"decode-{layer}")
                receipt = self.decode_receipt(output)
                manifest = load_decoded_tree(
                    runtime().store, receipt.decoded_tree_manifest
                )
                child = next(entry for entry in manifest.entries if entry.kind == "file")
                target = {
                    "receipt": self.blob(output),
                    "manifest": self.blob(receipt.decoded_tree_manifest),
                    "child": self.blob(child.sha256),
                }[layer]
                if mutation == "corrupt":
                    target.chmod(0o644)
                    target.write_bytes(b"corrupt")
                elif mutation == "missing":
                    target.unlink()
                else:
                    (self.root / f"v2-hardlink-{layer}").hardlink_to(target)
                launches = len(self.launches)
                with mock.patch.object(
                    activities, "_open_or_create_directory"
                ) as workspace:
                    with self.assertRaises((OSError, ValueError)):
                        await self.decode(owner="retry-owner")
                workspace.assert_not_called()
                self.assertEqual(len(self.launches), launches)

    async def test_framework_receipt_changes_decode_identity_and_parser_is_strict(self) -> None:
        framework_output = await self.install()
        receipt = self.framework_receipt(framework_output)
        first = activities._replay_decode_operation_identity(
            self.admitted, framework_output, receipt
        )[0]
        changed_output = replace(framework_output, size=framework_output.size + 1)
        second = activities._replay_decode_operation_identity(
            self.admitted, changed_output, receipt
        )[0]
        self.assertNotEqual(first, second)
        self.assertNotEqual(
            first,
            activities._replay_decode_operation_identity(
                admit_v3(
                    activity_fixture(
                        hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                        with_framework=True,
                        framework_package_ids=(2,),
                    )
                ),
                framework_output,
                receipt,
            )[0],
        )
        for payload in (
            b'{"schema_version":2,"schema_version":2}',
            b" " + canonical_json(
                ReplayDecodedTreeReceiptV2(
                    2,
                    "stock_input",
                    "1" * 64,
                    self.admitted.request.stock_apk,
                    self.admitted.profile.profile_id,
                    "2" * 64,
                    "decode",
                    "3" * 64,
                    "4" * 64,
                    "5" * 64,
                    "6" * 64,
                    framework_output,
                    receipt.framework_cache_manifest,
                    receipt.framework_cache_semantic_sha256,
                    ArtifactRef(
                        1,
                        "decoded-tree-manifest-v1",
                        "7" * 64,
                        1,
                        f"cas://sha256/{'7' * 64}",
                        "8" * 64,
                        (
                            "1" * 64,
                            canonical_sha256(self.admitted.request.stock_apk),
                            "2" * 64,
                            "3" * 64,
                            "4" * 64,
                            "5" * 64,
                            "6" * 64,
                            canonical_sha256(framework_output),
                            canonical_sha256(receipt.framework_cache_manifest),
                            receipt.framework_cache_semantic_sha256,
                        ),
                    ),
                    "9" * 64,
                    "8" * 64,
                    True,
                )
            ).encode(),
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                activities._strict_replay_decoded_tree_receipt_v2(payload)


class ReplayDecodeRegistrationTests(unittest.TestCase):
    """The decode checkpoint runs through a wrapper, never registered directly.

    Registering the checkpoint itself would take a full AdmittedReplayV3 as the
    Activity argument, putting the whole port recipe and every source path into
    Temporal History. The wrapper takes a hash-pinned handle and loads the same
    authority from the ledger.
    """

    def test_checkpoint_keeps_its_metadata_and_proven_signature(self) -> None:
        definition = activity._Definition.from_callable(  # type: ignore[attr-defined]
            replay_decode_checkpoint_activity
        )
        self.assertIsNotNone(definition)
        self.assertEqual(definition.name, "replay_decode_checkpoint_activity")
        self.assertEqual(
            tuple(inspect.signature(replay_decode_checkpoint_activity).parameters),
            ("candidate",),
        )

    def test_the_wrapper_is_registered_and_the_checkpoint_is_not(self) -> None:
        from dfinsta_pipeline import worker

        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined]
            for fn in worker.REGISTERED_ACTIVITIES
        }
        self.assertIn("replay_decode_stage_activity", registered)
        self.assertNotIn("replay_decode_checkpoint_activity", registered)


if __name__ == "__main__":
    unittest.main()
