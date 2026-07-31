import asyncio
import contextlib
import hashlib
import io
import inspect
import json
import os
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from temporalio import activity

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_build_patched_apk_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.apply import ApplyReport, OperationResult
from dfinsta_pipeline.backend import BackendReport
from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.contracts import ArtifactRef, canonical_json, canonical_sha256
from dfinsta_pipeline.decoded_artifact import capture_decoded_tree, load_decoded_tree
from dfinsta_pipeline.executor import ExecutionResult
from dfinsta_pipeline.port_contracts import (
    ArchiveEntriesAbsent,
    ArchiveEntryNamesAndBytesPreservedExcept,
    IntentResolution,
    OverlayTree,
    StockDexGraftBackend,
)
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    CapabilityBinding,
    ReplayBackendCompositionV1,
    ReplayApplyOperationResultV1,
    ReplayDecodedTreeReceiptV1,
    ReplayDecodedTreeReceiptV2,
    ReplayFrameworkCacheReceiptV1,
    ReplayPatchedApkReceiptV1,
    ReplayPatchedTreeReceiptV1,
    ReplaySourceAdmissionEvidenceV1,
)
from tests.test_phase_b_replay_contracts import (
    admit_v3,
    admit_v2,
    artifact_ref,
    bind_v3_fixture,
    capability_for_plan,
    fixture_v2,
    profile_v3,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_info(name: str, marker: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (2026, 1, marker, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = marker << 16
    info.comment = f"entry-{marker}".encode()
    return info


def zip_bytes(
    entries: tuple[tuple[zipfile.ZipInfo | str, bytes], ...],
    *,
    comment: bytes = b"archive-comment",
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.comment = comment
        for name, payload in entries:
            archive.writestr(name, payload)
    return stream.getvalue()


def build_case(
    executable_sha256: str,
    with_framework: bool = False,
    *,
    stock_payload: bytes | None = None,
    backend: StockDexGraftBackend | None = None,
    allowed_mutation_paths: tuple[str, ...] | None = None,
    uses_tool_path: bool = True,
):
    base = fixture_v2(with_framework)
    if allowed_mutation_paths is None:
        allowed_mutation_paths = (
            ("intermediate.apk", "patched-tree/build")
            if with_framework
            else ("framework/1.apk", "intermediate.apk", "patched-tree/build")
        )
    admitted_v2 = admit_v2(base)
    stock_ref = (
        base.request.stock_apk
        if stock_payload is None
        else artifact_ref("stock-apk", stock_payload)
    )
    selected_backend = backend or admitted_v2.resolution.backend
    selected_intent = admitted_v2.intent
    statuses = (IntentResolution("retain-hook", "omitted", "Synthetic build fixture"),)
    operations = ()
    assertions = ()
    if isinstance(selected_backend, StockDexGraftBackend) and selected_backend.add_dex_entries:
        selected_intent = replace(
            selected_intent,
            hooks=tuple(
                replace(
                    hook,
                    allowed_strategies=tuple(
                        sorted({*hook.allowed_strategies, "overlay_tree"})
                    ),
                )
                if hook.hook_id == "retain-hook"
                else hook
                for hook in selected_intent.hooks
            ),
        )
        source_file = admitted_v2.source_manifest.records[0]
        operations = (
            OverlayTree(
                "add-graft-dex",
                "overlay_tree",
                ("retain-hook",),
                "code",
                "smali_classes3",
                (source_file,),
                canonical_sha256((source_file,)),
                "forbid",
            ),
        )
        signature_entries: tuple[str, ...] = ()
        if stock_payload is not None:
            with zipfile.ZipFile(io.BytesIO(stock_payload)) as archive:
                signature_entries = tuple(
                    info.filename
                    for info in archive.infolist()
                    if len(info.filename.upper().split("/")) == 2
                    and info.filename.upper().split("/")[0] == "META-INF"
                    and (
                        info.filename.upper().split("/")[1] == "MANIFEST.MF"
                        or info.filename.upper().split("/")[1].startswith("SIG-")
                        or info.filename.upper().split("/")[1].endswith(
                            (".SF", ".RSA", ".DSA", ".EC")
                        )
                    )
                )
        preservation = ArchiveEntryNamesAndBytesPreservedExcept(
            "graft-stock-preserved",
            "archive_preservation_except",
            tuple(
                sorted(
                    {
                        *selected_backend.replace_dex_entries,
                        *selected_backend.add_dex_entries,
                        *signature_entries,
                    }
                )
            ),
        )
        assertions = (
            (
                ArchiveEntriesAbsent(
                    "graft-signatures-absent",
                    "archive_entries_absent",
                    tuple(sorted(signature_entries)),
                ),
                preservation,
            )
            if signature_entries
            else (preservation,)
        )
        statuses = (IntentResolution("retain-hook", "implemented", None),)
    resolution = replace(
        admitted_v2.resolution,
        intent_sha256=selected_intent.sha256,
        target=replace(admitted_v2.resolution.target, apk_sha256=stock_ref.sha256),
        backend=selected_backend,
        intent_statuses=statuses,
        operations=operations,
        additional_assertions=assertions,
    )
    intent_payload = canonical_json(selected_intent).encode()
    intent_ref = artifact_ref(
        "intent-spec", intent_payload, inputs=(stock_ref.sha256,)
    )
    resolution_payload = canonical_json(resolution).encode()
    resolution_ref = artifact_ref("resolution-spec", resolution_payload)
    gate = replace(
        admitted_v2.gate_prepared,
        stock_apk=stock_ref,
        intent=intent_ref,
        resolution=resolution_ref,
    )
    gate_payload = canonical_json(gate).encode()
    gate_ref = artifact_ref(
        "replay-gate-prepared-v2",
        gate_payload,
        inputs=(
            gate.stock_apk.sha256,
            gate.intent.sha256,
            gate.resolution.sha256,
            gate.source_manifest.sha256,
            gate.toolchain_profile.sha256,
            *(item.artifact.sha256 for item in gate.frameworks),
            *(item.artifact.sha256 for item in gate.tools),
        ),
    )
    run_spec = replace(
        base.run_spec,
        subject_sha256=stock_ref.sha256,
        intent_sha256=selected_intent.sha256,
        resolution_sha256=resolution.sha256,
        gate_prepared_sha256=gate_ref.sha256,
        gate_prepared_ref_sha256=canonical_sha256(gate_ref),
    )
    request = replace(
        base.request,
        run_spec_sha256=run_spec.sha256,
        gate_prepared=gate_ref,
        stock_apk=stock_ref,
        intent=intent_ref,
        resolution=resolution_ref,
    )
    decision = replace(
        base.decision,
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_ref.sha256,
    )
    payloads = dict(base.payloads)
    payloads.pop(canonical_sha256(base.request.intent))
    payloads[canonical_sha256(intent_ref)] = intent_payload
    if stock_payload is not None:
        payloads.pop(canonical_sha256(base.request.stock_apk))
        payloads[canonical_sha256(stock_ref)] = stock_payload
    payloads.pop(canonical_sha256(base.request.resolution))
    payloads.pop(canonical_sha256(base.request.gate_prepared))
    payloads[canonical_sha256(resolution_ref)] = resolution_payload
    payloads[canonical_sha256(gate_ref)] = gate_payload
    base = replace(
        base,
        run_spec=run_spec,
        request=request,
        decision=decision,
        payloads=payloads,
    )
    profile = profile_v3(with_framework)
    profile = replace(profile, backend_kind=selected_backend.kind)
    if not uses_tool_path:
        build_plan = profile.plan("build")
        build_plan = replace(
            build_plan,
            arguments=tuple(
                pair for pair in build_plan.arguments if pair[1] != "tool"
            ),
        )
        profile = replace(
            profile,
            execution_plans=tuple(
                build_plan if plan.role == "build" else plan
                for plan in profile.execution_plans
            ),
        )
    capabilities = []
    for binding in profile.capability_bindings:
        capability = capability_for_plan(
            profile, binding.role, executable_sha256=executable_sha256
        )
        if binding.role == "build":
            capability = replace(
                capability,
                input_kinds=("decoded-tree-manifest-v1",),
                output_kind="intermediate-apk",
                allowed_mutation_paths=allowed_mutation_paths,
            )
        capabilities.append(capability)
    profile = replace(
        profile,
        capability_bindings=tuple(
            CapabilityBinding(binding.role, capability.canonical_identity)
            for binding, capability in zip(
                profile.capability_bindings, capabilities, strict=True
            )
        ),
    )
    return bind_v3_fixture(base, profile, tuple(capabilities))


class FakeBuildProcess:
    def __init__(
        self,
        returncode: int = 0,
        *,
        block: bool = False,
        intermediate_entries: tuple[tuple[zipfile.ZipInfo | str, bytes], ...] | None = None,
    ) -> None:
        self.returncode: int | None = None if block else returncode
        self.requested_returncode = returncode
        self.cwd: Path | None = None
        self.killed = False
        self.reaped = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.intermediate_entries = intermediate_entries

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if self.returncode is None and not self.killed:
            await self.release.wait()
            self.returncode = self.requested_returncode
        if self.returncode == 0:
            assert self.cwd is not None
            with zipfile.ZipFile(self.cwd / "intermediate.apk", "w") as archive:
                entries = self.intermediate_entries or (
                    ("classes.dex", b"rebuilt-dex"),
                    ("resources.arsc", b"rebuilt-resources"),
                )
                for name, payload in entries:
                    info = (
                        name
                        if isinstance(name, zipfile.ZipInfo)
                        else zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                    )
                    archive.writestr(info, payload)
            build = self.cwd / "patched-tree/build/generated/nested"
            build.mkdir(parents=True)
            (build / "artifact.bin").write_bytes(b"generated")
            framework = self.cwd / "framework"
            if framework.is_dir() and not any(framework.iterdir()):
                (framework / "1.apk").write_bytes(b"generated framework")
        self.reaped = True
        return b"built", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1


class ReplayBuildActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="build-activity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "executor.bin"
        self.executable.write_bytes(b"trusted build executor")
        self.case = build_case(digest(self.executable.read_bytes()))
        self.admitted = admit_v3(self.case)
        self.launches: list[dict[str, Any]] = []
        self.process = FakeBuildProcess()

        async def launcher(*argv: str, **kwargs: Any) -> FakeBuildProcess:
            self.launches.append({"argv": argv, **kwargs})
            self.process.cwd = Path(kwargs["cwd"])
            self.assertFalse((self.process.cwd / "intermediate.apk").exists())
            self.assertFalse((self.process.cwd / "patched.apk").exists())
            return self.process

        configure_runtime(
            self.root / "state",
            attempts_root=self.root / "attempts",
            executor_paths={
                self.admitted.capability("build").executable_sha256: self.executable
            },
            launcher=launcher,
        )
        for reference in self.admitted.request.direct_artifacts:
            runtime().store.put_bytes(
                kind=reference.kind,
                data=self.case.resolve(reference),
                producer_operation_id="fixture",
                input_hashes=(),
            )
        runtime().ledger.record_decision(self.admitted.decision)
        runtime().ledger.record_admitted_replay_v3(self.admitted)
        self.fixture_suffix = "initial"
        self.framework_output, self.framework_receipt = self.record_framework()
        self.decode_output, self.decode_receipt = self.record_decode()
        self.apply_output, self.apply_receipt = self.record_apply()

    def complete(self, key: str, kind: str, input_sha256: str, output: ArtifactRef) -> None:
        runtime().ledger.begin_operation(key, kind, input_sha256, "predecessor", retry_safe=False)
        runtime().ledger.record_effect(key, "predecessor", output)
        runtime().ledger.complete_operation(key, output)

    def record_framework(
        self,
    ) -> tuple[ArtifactRef | None, ReplayFrameworkCacheReceiptV1 | None]:
        if not self.admitted.request.frameworks:
            return None, None
        key, input_sha256, installations, _ = activities._replay_framework_operation_identity(
            self.admitted
        )
        tree = self.root / f"framework-tree-{self.fixture_suffix}"
        tree.mkdir()
        for installation in installations:
            (tree / f"{installation.package_id}.apk").write_bytes(
                self.case.resolve(installation.framework_apk)
            )
        plan = self.admitted.plan("install_framework")
        capability = self.admitted.capability("install_framework")
        tool = next(
            item
            for item in self.admitted.request.tools
            if item.tool_id == self.admitted.profile.tool_for_role("install_framework").tool_id
        )
        inputs = (
            self.admitted.sha256,
            self.admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            *(canonical_sha256(item) for item in installations),
        )
        manifest_ref = capture_decoded_tree(runtime().store, tree, key, inputs)
        manifest = load_decoded_tree(runtime().store, manifest_ref)
        receipt = ReplayFrameworkCacheReceiptV1(
            1,
            self.admitted.sha256,
            self.admitted.profile.profile_id,
            self.admitted.profile.sha256,
            "install_framework",
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            installations,
            manifest_ref,
            manifest.decoded_tree_sha256,
            key,
            True,
        )
        output = runtime().store.put_bytes(
            kind="replay-framework-cache-receipt-v1",
            data=canonical_json(receipt).encode(),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        self.complete(key, "replay_install_frameworks_v1", input_sha256, output)
        return output, receipt

    def record_decode(
        self,
    ) -> tuple[ArtifactRef, ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2]:
        key, input_sha256, request_sha256 = activities._replay_decode_operation_identity(
            self.admitted, self.framework_output, self.framework_receipt
        )
        tree = self.root / f"decoded-tree-{self.fixture_suffix}"
        (tree / "smali").mkdir(parents=True)
        (tree / "smali/Example.smali").write_text(
            ".class public LExample;\n", encoding="utf-8"
        )
        plan = self.admitted.plan("decode")
        capability = self.admitted.capability("decode")
        tool = next(
            item
            for item in self.admitted.request.tools
            if item.tool_id == self.admitted.profile.tool_for_role("decode").tool_id
        )
        inputs = (
            self.admitted.sha256,
            canonical_sha256(self.admitted.request.stock_apk),
            self.admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request_sha256,
        )
        if self.framework_output is not None and self.framework_receipt is not None:
            inputs = (
                *inputs,
                canonical_sha256(self.framework_output),
                canonical_sha256(self.framework_receipt.framework_cache_manifest),
                self.framework_receipt.framework_cache_semantic_sha256,
            )
        manifest_ref = capture_decoded_tree(runtime().store, tree, key, inputs)
        manifest = load_decoded_tree(runtime().store, manifest_ref)
        if self.framework_output is None or self.framework_receipt is None:
            receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2 = ReplayDecodedTreeReceiptV1(
                1, "stock_input", self.admitted.sha256, self.admitted.request.stock_apk,
                self.admitted.profile.profile_id, self.admitted.profile.sha256, "decode",
                plan.sha256, capability.canonical_identity, tool.artifact.sha256,
                request_sha256, manifest_ref, manifest.decoded_tree_sha256, key, True,
            )
            kind = "replay_decode_tree_v1"
            receipt_kind = "replay-decoded-tree-receipt-v1"
        else:
            receipt = ReplayDecodedTreeReceiptV2(
                2, "stock_input", self.admitted.sha256, self.admitted.request.stock_apk,
                self.admitted.profile.profile_id, self.admitted.profile.sha256, "decode",
                plan.sha256, capability.canonical_identity, tool.artifact.sha256,
                request_sha256, self.framework_output,
                self.framework_receipt.framework_cache_manifest,
                self.framework_receipt.framework_cache_semantic_sha256,
                manifest_ref, manifest.decoded_tree_sha256, key, True,
            )
            kind = "replay_decode_tree_v2"
            receipt_kind = "replay-decoded-tree-receipt-v2"
        output = runtime().store.put_bytes(
            kind=receipt_kind,
            data=canonical_json(receipt).encode(),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        self.complete(key, kind, input_sha256, output)
        return output, receipt

    def record_apply(self) -> tuple[ArtifactRef, ReplayPatchedTreeReceiptV1]:
        compiled = compile_port(self.admitted.intent, self.admitted.resolution)
        apply_input = {
            "schema_version": 1,
            "admitted_replay_sha256": self.admitted.sha256,
            "completed_decode_receipt": self.decode_output,
            "input_decoded_tree_manifest": self.decode_receipt.decoded_tree_manifest,
            "input_decoded_tree_semantic_sha256": self.decode_receipt.decoded_tree_semantic_sha256,
            "intent_sha256": self.admitted.intent.sha256,
            "resolution_sha256": self.admitted.resolution.sha256,
            "source_manifest_sha256": self.admitted.source_manifest.sha256,
            "target_port_spec_sha256": compiled.sha256,
        }
        key = activities.operation_key("replay_apply_tree_v1", apply_input)
        source = ReplaySourceAdmissionEvidenceV1(
            2,
            self.admitted.sha256,
            self.admitted.source_manifest.sha256,
            "a" * 64,
            len(self.admitted.source_manifest.records),
            "admitted-source",
            True,
        )
        operation_results = tuple(
            ReplayApplyOperationResultV1(operation.operation_id, "applied")
            for operation in compiled.operations
        )
        report = ApplyReport(
            tuple(
                OperationResult(operation.operation_id, "applied")
                for operation in compiled.operations
            )
        )
        inputs = (
            self.admitted.sha256,
            canonical_sha256(self.decode_output),
            canonical_sha256(self.decode_receipt.decoded_tree_manifest),
            self.decode_receipt.decoded_tree_semantic_sha256,
            self.admitted.intent.sha256,
            self.admitted.resolution.sha256,
            self.admitted.source_manifest.sha256,
            compiled.sha256,
            source.sha256,
            report.sha256,
        )
        patched_tree = self.root / f"patched-tree-{self.fixture_suffix}"
        (patched_tree / "smali").mkdir(parents=True)
        (patched_tree / "smali/Example.smali").write_text(
            ".class public LExample;\n", encoding="utf-8"
        )
        if getattr(self, "preexisting_build", False):
            (patched_tree / "build").mkdir()
            (patched_tree / "build/preexisting.bin").write_bytes(b"preexisting")
        manifest_ref = capture_decoded_tree(runtime().store, patched_tree, key, inputs)
        manifest = load_decoded_tree(runtime().store, manifest_ref)
        receipt = ReplayPatchedTreeReceiptV1(
            1, self.admitted.sha256, self.decode_output,
            self.decode_receipt.decoded_tree_manifest,
            self.decode_receipt.decoded_tree_semantic_sha256,
            self.admitted.intent.sha256, self.admitted.resolution.sha256,
            self.admitted.source_manifest.sha256, compiled.sha256, source, operation_results,
            report.sha256, manifest_ref, manifest.decoded_tree_sha256, key, True,
        )
        output = runtime().store.put_bytes(
            kind="replay-patched-tree-receipt-v1",
            data=canonical_json(receipt).encode(),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        self.complete(key, "replay_apply_tree_v1", canonical_sha256(apply_input), output)
        return output, receipt

    async def invoke(self, owner: str = "build-owner") -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_build_patched_apk_checkpoint_activity(self.admitted)

    @staticmethod
    def blob(reference: ArtifactRef | str) -> Path:
        value = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / value[:2] / value

    def configure_fresh(self, name: str) -> None:
        root = self.root / name
        self.process = FakeBuildProcess()
        configure_runtime(
            root / "state",
            attempts_root=root / "attempts",
            executor_paths={
                self.admitted.capability("build").executable_sha256: self.executable
            },
            launcher=runtime().launcher,
        )
        for reference in self.admitted.request.direct_artifacts:
            runtime().store.put_bytes(
                kind=reference.kind,
                data=self.case.resolve(reference),
                producer_operation_id="fixture",
                input_hashes=(),
            )
        runtime().ledger.record_decision(self.admitted.decision)
        runtime().ledger.record_admitted_replay_v3(self.admitted)
        self.fixture_suffix = name.replace("/", "_")
        self.framework_output, self.framework_receipt = self.record_framework()
        self.decode_output, self.decode_receipt = self.record_decode()
        self.apply_output, self.apply_receipt = self.record_apply()

    @staticmethod
    def receipt(output: ArtifactRef) -> ReplayPatchedApkReceiptV1:
        return ReplayPatchedApkReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    async def test_no_framework_happy_path_exact_request_materialization_and_canonical_parser(self) -> None:
        calls: list[dict[str, Any]] = []
        real_execute = activities.execute

        async def observed(*args: Any, **kwargs: Any):
            calls.append({"args": args, "kwargs": kwargs})
            return await real_execute(*args, **kwargs)

        with mock.patch.object(activities, "execute", side_effect=observed):
            output = await self.invoke()
        receipt = self.receipt(output)
        self.assertEqual(receipt.operation_key, receipt.expected_operation_key)
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT input_sha256 FROM operation_claims WHERE operation_key = ?",
                    (receipt.operation_key,),
                ).fetchone()[0],
                receipt.expected_operation_input_sha256,
            )
        workspace = runtime().attempts_root / output.producer_operation_id / digest(b"build-owner")
        self.assertEqual(self.launches[0]["argv"][1:], ("patched-tree", "framework", "intermediate.apk", "tool"))
        self.assertEqual(calls[0]["kwargs"]["timeout_seconds"], self.admitted.plan("build").timeout_seconds)
        request = calls[0]["args"][1]
        self.assertEqual(request.input_artifact, self.apply_receipt.patched_tree_manifest)
        self.assertEqual(request.environment, ())
        self.assertEqual(request.output_kind, "intermediate-apk")
        self.assertEqual((workspace / "stock.apk").read_bytes(), self.case.resolve(self.admitted.request.stock_apk))
        self.assertTrue((workspace / "framework").is_dir())
        self.assertEqual(tuple((workspace / "framework").iterdir()), ())
        self.assertFalse((workspace / "patched-tree/build").exists())
        self.assertEqual(
            (workspace / "patched-tree/smali/Example.smali").read_text(encoding="utf-8"),
            ".class public LExample;\n",
        )
        self.assertEqual(receipt.composition.backend_kind, "apktool_full_rebuild")
        self.assertEqual(runtime().store.read_bytes(receipt.intermediate_apk), runtime().store.read_bytes(receipt.patched_apk))
        canonical = canonical_json(receipt).encode()
        self.assertEqual(activities._strict_replay_patched_apk_receipt(canonical), receipt)
        for payload in (b" " + canonical, b'{"schema_version":1,"schema_version":1}'):
            with self.assertRaises(ValueError):
                activities._strict_replay_patched_apk_receipt(payload)

    async def test_framework_happy_path_materializes_exact_cache(self) -> None:
        self.case = build_case(digest(self.executable.read_bytes()), True)
        self.admitted = admit_v3(self.case)
        root = self.root / "framework-run"
        configure_runtime(
            root / "state", attempts_root=root / "attempts",
            executor_paths={self.admitted.capability("build").executable_sha256: self.executable},
            launcher=runtime().launcher,
        )
        for reference in self.admitted.request.direct_artifacts:
            runtime().store.put_bytes(kind=reference.kind, data=self.case.resolve(reference), producer_operation_id="fixture", input_hashes=())
        runtime().ledger.record_decision(self.admitted.decision)
        runtime().ledger.record_admitted_replay_v3(self.admitted)
        self.fixture_suffix = "framework-run"
        self.framework_output, self.framework_receipt = self.record_framework()
        self.decode_output, self.decode_receipt = self.record_decode()
        self.apply_output, self.apply_receipt = self.record_apply()
        output = await self.invoke("framework-owner")
        receipt = self.receipt(output)
        self.assertEqual(receipt.operation_key, receipt.expected_operation_key)
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT input_sha256 FROM operation_claims WHERE operation_key = ?",
                    (receipt.operation_key,),
                ).fetchone()[0],
                receipt.expected_operation_input_sha256,
            )
        workspace = runtime().attempts_root / output.producer_operation_id / digest(b"framework-owner")
        assert self.framework_receipt is not None
        self.assertEqual(receipt.completed_framework_cache_receipt, self.framework_output)
        self.assertEqual(
            sorted(path.name for path in (workspace / "framework").iterdir()),
            [f"{item.package_id}.apk" for item in self.admitted.request.frameworks],
        )
        self.assertFalse((workspace / "patched-tree/build").exists())
        for framework in self.admitted.request.frameworks:
            self.assertEqual(
                (workspace / "framework" / f"{framework.package_id}.apk").read_bytes(),
                self.case.resolve(framework.artifact),
            )

    async def test_graft_execution_and_post_effect_adoption_preserve_archive_contract(self) -> None:
        stock_entries = (
            (zip_info("classes.dex", 2), b"stock-one"),
            (zip_info("res/raw/value", 4), b"retained"),
            ("META-INF/MANIFEST.MF", b"manifest"),
            ("meta-inf/CERT.sf", b"signature"),
            ("META-INF/SIG-CUSTOM", b"signature-block"),
            ("META-INF/services/provider", b"preserve-meta"),
            (zip_info("classes2.dex", 6), b"stock-two"),
        )
        stock_payload = zip_bytes(stock_entries)
        backend = StockDexGraftBackend(
            "stock_dex_graft",
            profile_v3().profile_id,
            ("classes.dex", "classes2.dex"),
            ("classes2.dex",),
            ("classes3.dex",),
        )
        self.case = build_case(
            digest(self.executable.read_bytes()),
            stock_payload=stock_payload,
            backend=backend,
        )
        self.admitted = admit_v3(self.case)
        self.configure_fresh("graft")
        self.process = FakeBuildProcess(
            intermediate_entries=(
                (zip_info("classes2.dex", 8), b"replacement"),
                (zip_info("classes3.dex", 10), b"addition"),
            )
        )
        complete = activities.Ledger.complete_operation
        with mock.patch.object(
            activities.Ledger,
            "complete_operation",
            side_effect=RuntimeError("post-effect"),
        ):
            with self.assertRaisesRegex(RuntimeError, "post-effect"):
                await self.invoke("graft-first")
        with runtime().ledger._connection() as connection:
            output = ArtifactRef.from_dict(
                json.loads(
                    connection.execute(
                        "SELECT output_json FROM operation_claims "
                        "WHERE kind = 'replay_build_patched_apk_v1'"
                    ).fetchone()[0]
                )
            )
        receipt = self.receipt(output)
        final_bytes = runtime().store.read_bytes(receipt.patched_apk)
        intermediate_bytes = runtime().store.read_bytes(receipt.intermediate_apk)
        with (
            zipfile.ZipFile(io.BytesIO(final_bytes)) as archive,
            zipfile.ZipFile(io.BytesIO(stock_payload)) as stock_archive,
            zipfile.ZipFile(io.BytesIO(intermediate_bytes)) as intermediate_archive,
        ):
            names = tuple(info.filename for info in archive.infolist())
            payloads = {name: archive.read(name) for name in names}
            self.assertEqual(archive.comment, b"archive-comment")
            def metadata(info: zipfile.ZipInfo) -> tuple[object, ...]:
                return (
                    info.date_time,
                    info.compress_type,
                    info.comment,
                    info.extra,
                    info.internal_attr,
                    info.external_attr,
                    info.create_system,
                    info.create_version,
                    info.extract_version,
                    info.flag_bits,
                    info.volume,
                )
            for name in (
                "classes.dex",
                "res/raw/value",
                "META-INF/services/provider",
                "classes2.dex",
            ):
                self.assertEqual(
                    metadata(archive.getinfo(name)), metadata(stock_archive.getinfo(name))
                )
            self.assertEqual(
                metadata(archive.getinfo("classes3.dex")),
                metadata(intermediate_archive.getinfo("classes3.dex")),
            )
        self.assertEqual(
            names,
            (
                "classes.dex",
                "res/raw/value",
                "META-INF/services/provider",
                "classes2.dex",
                "classes3.dex",
            ),
        )
        self.assertEqual(payloads["res/raw/value"], b"retained")
        self.assertEqual(payloads["classes2.dex"], b"replacement")
        self.assertEqual(payloads["classes3.dex"], b"addition")
        self.assertEqual(receipt.composition.replaced_entries, ("classes2.dex",))
        self.assertEqual(receipt.composition.added_entries, ("classes3.dex",))
        self.assertEqual(receipt.composition.retained_entry_count, 3)
        self.assertEqual(
            receipt.composition.stripped_signature_entries,
            ("META-INF/MANIFEST.MF", "meta-inf/CERT.sf", "META-INF/SIG-CUSTOM"),
        )
        self.assertEqual(receipt.intermediate_apk.input_hashes, receipt.execution_input_hashes)
        self.assertEqual(receipt.patched_apk.input_hashes, receipt.patched_apk_input_hashes)
        graft_workspace = (
            runtime().attempts_root
            / receipt.operation_key
            / digest(b"graft-first")
        )
        self.assertFalse((graft_workspace / "patched-tree/build").exists())
        self.assertEqual(tuple((graft_workspace / "framework").iterdir()), ())
        launches = len(self.launches)
        with (
            mock.patch.object(activities, "execute", side_effect=AssertionError("launch")),
            mock.patch.object(
                activities, "materialize_decoded_tree", side_effect=AssertionError("workspace")
            ),
            mock.patch.object(activities.Ledger, "complete_operation", wraps=complete),
        ):
            adopted = await self.invoke("graft-retry")
        self.assertEqual(adopted, output)
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(
            (
                runtime().attempts_root
                / output.producer_operation_id
                / digest(b"graft-retry")
            ).exists()
        )

    async def test_mutation_scope_rejected_before_claim_or_workspace(self) -> None:
        for mutation_paths in (
            (),
            ("framework", "intermediate.apk", "patched-tree/build"),
            ("framework/1.apk", "intermediate.apk", "patched-tree"),
            ("intermediate.apk",),
            ("intermediate.apk", "patched-tree/build"),
            ("output",),
        ):
            with self.subTest(mutation_paths=mutation_paths):
                self.case = build_case(
                    digest(self.executable.read_bytes()),
                    allowed_mutation_paths=mutation_paths,
                )
                self.admitted = admit_v3(self.case)
                self.configure_fresh(f"mutation-scope-{len(mutation_paths)}-{mutation_paths[0] if mutation_paths else 'empty'}")
                with (
                    mock.patch.object(activities.Ledger, "begin_operation") as begin,
                    mock.patch.object(activities, "materialize_decoded_tree") as materialize,
                ):
                    with self.assertRaisesRegex(ValueError, "framework policy"):
                        await self.invoke("scope")
                begin.assert_not_called()
                materialize.assert_not_called()
                self.assertFalse(runtime().attempts_root.exists())

        self.case = build_case(
            digest(self.executable.read_bytes()),
            with_framework=True,
            allowed_mutation_paths=(
                "framework/1.apk",
                "intermediate.apk",
                "patched-tree/build",
            ),
        )
        self.admitted = admit_v3(self.case)
        self.configure_fresh("wrong-declared-framework-scope")
        with mock.patch.object(activities.Ledger, "begin_operation") as begin:
            with self.assertRaisesRegex(ValueError, "framework policy"):
                await self.invoke("wrong-framework-scope")
        begin.assert_not_called()

    async def test_reviewed_capability_hashes_and_operation_keys_replace_old_policy(self) -> None:
        old_case = build_case(
            digest(self.executable.read_bytes()),
            allowed_mutation_paths=("intermediate.apk",),
        )
        new_case = build_case(digest(self.executable.read_bytes()))
        old = admit_v3(old_case)
        new = admit_v3(new_case)
        self.assertNotEqual(
            old.capability("build").canonical_identity,
            new.capability("build").canonical_identity,
        )

        def operation_key_for(admitted: AdmittedReplayV3, name: str) -> str:
            self.case = old_case if admitted is old else new_case
            self.admitted = admitted
            self.configure_fresh(name)
            predecessors = activities._replay_build_predecessors(admitted)
            return activities._replay_build_operation_identity(
                admitted,
                predecessors[2],
                predecessors[3],
                predecessors[4],
                predecessors[0],
                predecessors[1],
            )[0]

        self.assertNotEqual(
            operation_key_for(old, "old-policy-key"),
            operation_key_for(new, "new-policy-key"),
        )

    async def test_private_directory_modes_reject_insecure_attempts_and_operation(self) -> None:
        for layer in ("attempts", "operation"):
            with self.subTest(layer=layer):
                self.configure_fresh(f"insecure-{layer}")
                predecessors = activities._replay_build_predecessors(self.admitted)
                key, _, _ = activities._replay_build_operation_identity(
                    self.admitted,
                    predecessors[2],
                    predecessors[3],
                    predecessors[4],
                    predecessors[0],
                    predecessors[1],
                )
                runtime().attempts_root.mkdir(mode=0o700)
                if layer == "attempts":
                    runtime().attempts_root.chmod(0o755)
                else:
                    operation = runtime().attempts_root / key
                    operation.mkdir(mode=0o755)
                    operation.chmod(0o755)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    await self.invoke(f"insecure-{layer}")
                with runtime().ledger._connection() as connection:
                    owner, status = connection.execute(
                        "SELECT owner_token, status FROM operation_claims WHERE operation_key = ?",
                        (key,),
                    ).fetchone()
                self.assertEqual((owner, status), ("", "pending"))
                self.assertFalse(
                    (runtime().attempts_root / key / digest(f"insecure-{layer}".encode())).exists()
                )

    async def test_no_tool_plan_does_not_read_or_materialize_tool(self) -> None:
        initial = build_case(digest(self.executable.read_bytes()))
        initial_admitted = admit_v3(initial)
        tool = next(
            item
            for item in initial_admitted.request.tools
            if item.tool_id == initial_admitted.profile.tool_for_role("build").tool_id
        )
        tool_payload = initial.resolve(tool.artifact)
        self.executable = self.root / "native-build-tool"
        self.executable.write_bytes(tool_payload)
        self.case = build_case(
            digest(tool_payload),
            uses_tool_path=False,
        )
        self.admitted = admit_v3(self.case)
        self.configure_fresh("no-tool")
        tool_ref = next(
            item.artifact
            for item in self.admitted.request.tools
            if item.tool_id == self.admitted.profile.tool_for_role("build").tool_id
        )
        real_read = runtime().store.read_bytes

        def reject_tool(reference: ArtifactRef) -> bytes:
            if reference == tool_ref:
                raise AssertionError("build tool artifact was read")
            return real_read(reference)

        with mock.patch.object(runtime().store, "read_bytes", side_effect=reject_tool):
            output = await self.invoke("no-tool-owner")
        workspace = (
            runtime().attempts_root / output.producer_operation_id / digest(b"no-tool-owner")
        )
        self.assertFalse((workspace / "tool").exists())
        self.assertNotIn("tool", self.launches[-1]["argv"][1:])

    async def test_candidate_only_and_missing_predecessor_reject_before_claim_or_workspace(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(replay_build_patched_apk_checkpoint_activity).parameters),
            ("candidate",),
        )
        with runtime().ledger._connection() as connection:
            connection.execute(
                "UPDATE operation_claims SET status = 'effect' WHERE operation_key = ?",
                (self.apply_output.producer_operation_id,),
            )
        with (
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(activities, "materialize_decoded_tree") as materialize,
        ):
            with self.assertRaises(ValueError):
                await self.invoke()
        begin.assert_not_called()
        materialize.assert_not_called()
        self.assertFalse(runtime().attempts_root.exists())

    async def test_preexisting_patched_tree_build_rejects_before_launch(self) -> None:
        self.preexisting_build = True
        self.configure_fresh("preexisting-build")
        with self.assertRaisesRegex(ValueError, "must be absent before launch"):
            await self.invoke("preexisting-build-owner")
        self.assertEqual(self.launches, [])
        with runtime().ledger._connection() as connection:
            key, status = connection.execute(
                "SELECT operation_key, status FROM operation_claims "
                "WHERE kind = 'replay_build_patched_apk_v1'"
            ).fetchone()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_nested_build_symlink_cleanup_never_touches_target(self) -> None:
        target = self.root / "symlink-target"
        target.write_bytes(b"preserve me")
        original = self.process.communicate

        async def add_symlink() -> tuple[bytes, bytes]:
            result = await original()
            assert self.process.cwd is not None
            link = self.process.cwd / "patched-tree/build/generated/target-link"
            link.symlink_to(target)
            return result

        self.process.communicate = add_symlink  # type: ignore[method-assign]
        output = await self.invoke("symlink-cleanup")
        workspace = (
            runtime().attempts_root / output.producer_operation_id / digest(b"symlink-cleanup")
        )
        self.assertEqual(target.read_bytes(), b"preserve me")
        self.assertFalse((workspace / "patched-tree/build").exists())

    async def test_cleanup_nonregular_replacement_and_unlink_failure_quarantine(self) -> None:
        for variant in ("nonregular", "replacement", "unlink-failure"):
            with self.subTest(variant=variant):
                self.configure_fresh(f"cleanup-{variant}")
                boundary: Any = contextlib.nullcontext()
                if variant == "nonregular":
                    original = self.process.communicate

                    async def add_fifo() -> tuple[bytes, bytes]:
                        result = await original()
                        assert self.process.cwd is not None
                        os.mkfifo(self.process.cwd / "patched-tree/build/generated/fifo")
                        return result

                    self.process.communicate = add_fifo  # type: ignore[method-assign]
                elif variant == "replacement":
                    real_open = activities._open_existing_directory

                    def replace_before_open(parent_fd: int, name: str) -> int:
                        if name == "build":
                            os.rename(
                                "build",
                                "build-replaced",
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                            os.mkdir("build", mode=0o700, dir_fd=parent_fd)
                        return real_open(parent_fd, name)

                    boundary = mock.patch.object(
                        activities,
                        "_open_existing_directory",
                        side_effect=replace_before_open,
                    )
                else:
                    real_unlink = activities.os.unlink

                    def fail_generated_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
                        if path == "artifact.bin":
                            raise OSError("cleanup unlink failed")
                        real_unlink(path, *args, **kwargs)

                    boundary = mock.patch.object(
                        activities.os, "unlink", side_effect=fail_generated_unlink
                    )
                with boundary:
                    with self.assertRaises((OSError, PermissionError, ValueError)):
                        await self.invoke(f"cleanup-{variant}-owner")
                with runtime().ledger._connection() as connection:
                    key, status = connection.execute(
                        "SELECT operation_key, status FROM operation_claims "
                        "WHERE kind = 'replay_build_patched_apk_v1'"
                    ).fetchone()
                self.assertEqual(status, "quarantined")
                self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_cleanup_rejects_build_replaced_after_initial_observation(self) -> None:
        real_remove = activities._secure_remove_tree_entry

        def replace_before_remove(
            parent_fd: int,
            name: str,
            label: str,
            *,
            expected: os.stat_result | None = None,
        ) -> None:
            if name == "build":
                os.rename(
                    "build",
                    "build-replaced",
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.mkdir("build", mode=0o700, dir_fd=parent_fd)
            real_remove(parent_fd, name, label, expected=expected)

        with mock.patch.object(
            activities, "_secure_remove_tree_entry", side_effect=replace_before_remove
        ):
            with self.assertRaisesRegex(ValueError, "changed before cleanup"):
                await self.invoke("build-observation-replacement")
        with runtime().ledger._connection() as connection:
            key, status = connection.execute(
                "SELECT operation_key, status FROM operation_claims "
                "WHERE kind = 'replay_build_patched_apk_v1'"
            ).fetchone()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_cleanup_rejects_framework_replaced_before_open(self) -> None:
        real_open = activities._open_pinned_regular

        def replace_before_open(
            parent_fd: int, name: str, label: str
        ) -> tuple[int, os.stat_result]:
            if label == "Generated framework APK":
                os.rename(
                    "1.apk",
                    "1-replaced.apk",
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                replacement = os.open(
                    "1.apk",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                os.write(replacement, b"replacement")
                os.close(replacement)
            return real_open(parent_fd, name, label)

        with mock.patch.object(
            activities, "_open_pinned_regular", side_effect=replace_before_open
        ):
            with self.assertRaisesRegex(ValueError, "changed during cleanup"):
                await self.invoke("framework-observation-replacement")
        with runtime().ledger._connection() as connection:
            key, status = connection.execute(
                "SELECT operation_key, status FROM operation_claims "
                "WHERE kind = 'replay_build_patched_apk_v1'"
            ).fetchone()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_undeclared_framework_name_and_declared_framework_mutation_reject(self) -> None:
        original = self.process.communicate

        async def add_extra_framework() -> tuple[bytes, bytes]:
            result = await original()
            assert self.process.cwd is not None
            (self.process.cwd / "framework/extra.apk").write_bytes(b"undeclared")
            return result

        self.process.communicate = add_extra_framework  # type: ignore[method-assign]
        with self.assertRaises(PermissionError):
            await self.invoke("extra-framework")
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM operation_claims "
                    "WHERE kind = 'replay_build_patched_apk_v1'"
                ).fetchone()[0],
                "quarantined",
            )

        self.case = build_case(digest(self.executable.read_bytes()), with_framework=True)
        self.admitted = admit_v3(self.case)
        self.configure_fresh("declared-framework-mutation")
        original = self.process.communicate

        async def mutate_declared_framework() -> tuple[bytes, bytes]:
            result = await original()
            assert self.process.cwd is not None
            (self.process.cwd / "framework/1.apk").write_bytes(b"mutated")
            return result

        self.process.communicate = mutate_declared_framework  # type: ignore[method-assign]
        with self.assertRaises(PermissionError):
            await self.invoke("declared-framework-mutation")
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM operation_claims "
                    "WHERE kind = 'replay_build_patched_apk_v1'"
                ).fetchone()[0],
                "quarantined",
            )

    async def test_preclaim_patched_tree_child_tamper_rejects_without_build_work(self) -> None:
        manifest = load_decoded_tree(
            runtime().store, self.apply_receipt.patched_tree_manifest
        )
        child = next(entry for entry in manifest.entries if entry.kind == "file")
        self.blob(child.sha256).chmod(0o644)
        launches = len(self.launches)
        with (
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(activities, "materialize_decoded_tree") as materialize,
        ):
            with self.assertRaises(ValueError):
                await self.invoke("preclaim-tamper")
        begin.assert_not_called()
        materialize.assert_not_called()
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(runtime().attempts_root.exists())

    async def test_missing_and_hash_mismatched_executable_release_pending_claim(self) -> None:
        predecessors = activities._replay_build_predecessors(self.admitted)
        key, _, _ = activities._replay_build_operation_identity(
            self.admitted,
            predecessors[2],
            predecessors[3],
            predecessors[4],
            predecessors[0],
            predecessors[1],
        )
        launcher = runtime().launcher
        configure_runtime(
            self.root / "state",
            attempts_root=self.root / "attempts",
            executor_paths={},
            launcher=launcher,
        )
        with self.assertRaisesRegex(ValueError, "No runtime executable"):
            await self.invoke("missing-executable")
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT owner_token, status FROM operation_claims WHERE operation_key = ?",
                    (key,),
                ).fetchone(),
                ("", "pending"),
            )
        self.assertEqual(runtime().ledger.operation_event_count(key, "pending"), 1)
        self.assertFalse(runtime().attempts_root.exists())

        wrong = self.root / "wrong-executable"
        wrong.write_bytes(b"wrong")
        configure_runtime(
            self.root / "state",
            attempts_root=self.root / "attempts",
            executor_paths={
                self.admitted.capability("build").executable_sha256: wrong
            },
            launcher=launcher,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            await self.invoke("mismatched-executable")
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT owner_token, status FROM operation_claims WHERE operation_key = ?",
                    (key,),
                ).fetchone(),
                ("", "pending"),
            )
        self.assertEqual(runtime().ledger.operation_event_count(key, "pending"), 2)
        self.assertFalse(runtime().attempts_root.exists())

    async def test_active_owner_nonzero_compose_and_cas_failures_follow_lifecycle(self) -> None:
        predecessors = activities._replay_build_predecessors(self.admitted)
        key, input_sha256, _ = activities._replay_build_operation_identity(
            self.admitted, predecessors[2], predecessors[3], predecessors[4], predecessors[0], predecessors[1]
        )
        runtime().ledger.begin_operation(key, "replay_build_patched_apk_v1", input_sha256, "active", retry_safe=False)
        with self.assertRaisesRegex(ValueError, "already claimed"):
            await self.invoke("other")

        for name in ("nonzero", "compose", "cas"):
            with self.subTest(name=name):
                root = self.root / name
                configure_runtime(
                    root / "state", attempts_root=root / "attempts",
                    executor_paths={self.admitted.capability("build").executable_sha256: self.executable},
                    launcher=runtime().launcher,
                )
                for reference in self.admitted.request.direct_artifacts:
                    runtime().store.put_bytes(kind=reference.kind, data=self.case.resolve(reference), producer_operation_id="fixture", input_hashes=())
                runtime().ledger.record_decision(self.admitted.decision)
                runtime().ledger.record_admitted_replay_v3(self.admitted)
                self.fixture_suffix = f"failure-{name}"
                self.framework_output, self.framework_receipt = self.record_framework()
                self.decode_output, self.decode_receipt = self.record_decode()
                self.apply_output, self.apply_receipt = self.record_apply()
                self.process = FakeBuildProcess(9 if name == "nonzero" else 0)
                boundary = (
                    mock.patch.object(
                        activities, "compose_apk", side_effect=RuntimeError("compose")
                    )
                    if name == "compose"
                    else mock.patch.object(
                        runtime().store, "put_bytes", side_effect=RuntimeError("cas")
                    )
                    if name == "cas"
                    else contextlib.nullcontext()
                )
                with boundary:
                    with self.assertRaises((RuntimeError, ValueError)):
                        await self.invoke(f"owner-{name}")
                build_rows = []
                with runtime().ledger._connection() as connection:
                    build_rows = connection.execute(
                        "SELECT status FROM operation_claims WHERE kind = 'replay_build_patched_apk_v1'"
                    ).fetchall()
                self.assertEqual(build_rows, [("quarantined",)])

    async def test_record_effect_failure_after_receipt_publication_quarantines(self) -> None:
        published: list[ArtifactRef] = []
        real_put = runtime().store.put_bytes

        def observe_publication(**kwargs: Any) -> ArtifactRef:
            reference = real_put(**kwargs)
            if kwargs["kind"] == "replay-patched-apk-receipt-v1":
                published.append(reference)
            return reference

        with (
            mock.patch.object(runtime().store, "put_bytes", side_effect=observe_publication),
            mock.patch.object(
                activities.Ledger,
                "record_effect",
                side_effect=RuntimeError("effect recording failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "effect recording failed"):
                await self.invoke("effect-failure")
        self.assertEqual(len(published), 1)
        activities._strict_replay_patched_apk_receipt(
            runtime().store.read_bytes(published[0])
        )
        key = published[0].producer_operation_id
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)
        launches = len(self.launches)
        with self.assertRaisesRegex(ValueError, "quarantined"):
            await self.invoke("effect-retry")
        self.assertEqual(len(self.launches), launches)
        self.assertFalse(
            (runtime().attempts_root / key / digest(b"effect-retry")).exists()
        )

    async def test_empty_framework_mutation_hits_explicit_activity_validation(self) -> None:
        async def mutate_empty_framework(
            _capability: object,
            _request: object,
            metadata: object,
            **_kwargs: Any,
        ) -> ExecutionResult:
            cwd = metadata.cwd  # type: ignore[attr-defined]
            with zipfile.ZipFile(cwd / "intermediate.apk", "w") as archive:
                archive.writestr("classes.dex", b"rebuilt-dex")
            (cwd / "framework/injected.apk").write_bytes(b"unexpected")
            (cwd / "framework/1.apk").write_bytes(b"generated framework")
            build = cwd / "patched-tree/build/generated"
            build.mkdir(parents=True)
            (build / "artifact").write_bytes(b"generated")
            return ExecutionResult(0, "built", "")

        real_snapshot = activities._framework_cache_snapshot
        with (
            mock.patch.object(activities, "execute", side_effect=mutate_empty_framework),
            mock.patch.object(
                activities,
                "_framework_cache_snapshot",
                wraps=real_snapshot,
            ) as snapshot,
        ):
            with self.assertRaisesRegex(ValueError, "Empty framework directory was mutated"):
                await self.invoke("empty-framework-mutation")
        self.assertEqual(snapshot.call_count, 2)
        with runtime().ledger._connection() as connection:
            key, status = connection.execute(
                "SELECT operation_key, status FROM operation_claims "
                "WHERE kind = 'replay_build_patched_apk_v1'"
            ).fetchone()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)
        self.assertEqual(self.launches, [])

    async def test_owner_workspace_privacy_validation_failure_quarantines(self) -> None:
        real_validate = activities._validate_private_directory

        def invalidate_owner_workspace(descriptor: int, label: str) -> None:
            if label == "owner workspace":
                os.fchmod(descriptor, 0o755)
            real_validate(descriptor, label)

        with mock.patch.object(
            activities,
            "_validate_private_directory",
            side_effect=invalidate_owner_workspace,
        ):
            with self.assertRaisesRegex(ValueError, "Unsafe owner workspace"):
                await self.invoke("privacy-failure")
        with runtime().ledger._connection() as connection:
            key, status = connection.execute(
                "SELECT operation_key, status FROM operation_claims "
                "WHERE kind = 'replay_build_patched_apk_v1'"
            ).fetchone()
        self.assertEqual(status, "quarantined")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)
        self.assertEqual(self.launches, [])
        self.assertTrue(
            (runtime().attempts_root / key / digest(b"privacy-failure")).is_dir()
        )

    async def test_post_effect_adoption_revalidates_without_launch_workspace_or_source(self) -> None:
        complete = activities.Ledger.complete_operation
        with mock.patch.object(activities.Ledger, "complete_operation", side_effect=RuntimeError("complete")):
            with self.assertRaisesRegex(RuntimeError, "complete"):
                await self.invoke("first")
        with runtime().ledger._connection() as connection:
            effect_output = ArtifactRef.from_dict(
                json.loads(
                    connection.execute(
                        "SELECT output_json FROM operation_claims "
                        "WHERE kind = 'replay_build_patched_apk_v1'"
                    ).fetchone()[0]
                )
            )
        effect_receipt = self.receipt(effect_output)
        apk_reads = {
            effect_receipt.stock_apk: 0,
            effect_receipt.intermediate_apk: 0,
            effect_receipt.patched_apk: 0,
        }
        real_read = runtime().store.read_bytes

        def count_apk_reads(reference: ArtifactRef) -> bytes:
            if reference in apk_reads:
                apk_reads[reference] += 1
            return real_read(reference)

        before = len(self.launches)
        with (
            mock.patch.object(runtime().store, "read_bytes", side_effect=count_apk_reads),
            mock.patch.object(activities, "execute", side_effect=AssertionError("launch")),
            mock.patch.object(activities, "materialize_decoded_tree", side_effect=AssertionError("workspace")),
            mock.patch.object(activities.Ledger, "complete_operation", wraps=complete),
        ):
            output = await self.invoke("retry")
        self.assertEqual(len(self.launches), before)
        self.assertEqual(set(apk_reads.values()), {1})
        retry_workspace = runtime().attempts_root / output.producer_operation_id / digest(b"retry")
        self.assertFalse(retry_workspace.exists())

    async def test_adoption_ignores_poisoned_source_root_and_source_helpers(self) -> None:
        complete = activities.Ledger.complete_operation
        with mock.patch.object(
            activities.Ledger, "complete_operation", side_effect=RuntimeError("complete")
        ):
            with self.assertRaisesRegex(RuntimeError, "complete"):
                await self.invoke("source-first")
        runtime().source_root = mock.MagicMock(
            side_effect=AssertionError("source root accessed")
        )
        with (
            mock.patch.object(
                activities, "admit_source_bundle_v2", side_effect=AssertionError("source")
            ),
            mock.patch.object(
                activities, "verify_staged_source_v2", side_effect=AssertionError("source")
            ),
            mock.patch.object(activities, "apply_port", side_effect=AssertionError("apply")),
            mock.patch.object(activities, "execute", side_effect=AssertionError("launch")),
            mock.patch.object(
                activities, "materialize_decoded_tree", side_effect=AssertionError("workspace")
            ),
            mock.patch.object(activities.Ledger, "complete_operation", wraps=complete),
        ):
            await self.invoke("source-retry")

    def forged_receipt(
        self, receipt: ReplayPatchedApkReceiptV1, *, field: str
    ) -> ReplayPatchedApkReceiptV1:
        execution_inputs = list(receipt.execution_input_hashes)
        changes: dict[str, Any] = {}
        if field == "request":
            changes["execution_request_sha256"] = "f" * 64
            execution_inputs[10] = "f" * 64
        else:
            wrong_predecessor = replace(
                receipt.completed_patched_tree_receipt,
                sha256="e" * 64,
                uri=f"cas://sha256/{'e' * 64}",
            )
            changes["completed_patched_tree_receipt"] = wrong_predecessor
            execution_inputs[1] = canonical_sha256(wrong_predecessor)
        intermediate = replace(
            receipt.intermediate_apk, input_hashes=tuple(execution_inputs)
        )
        patched = replace(
            receipt.patched_apk,
            input_hashes=(
                *execution_inputs,
                canonical_sha256(intermediate),
                receipt.composition.sha256,
            ),
        )
        return replace(
            receipt,
            intermediate_apk=intermediate,
            patched_apk=patched,
            **changes,
        )

    async def test_canonical_effect_output_forgeries_reject_without_launch_or_workspace(self) -> None:
        for field in ("request", "predecessor"):
            with self.subTest(field=field):
                self.configure_fresh(f"forgery-{field}")
                output = await self.invoke("forgery-first")
                receipt = self.receipt(output)
                forged = self.forged_receipt(receipt, field=field)
                forged_output = runtime().store.put_bytes(
                    kind="replay-patched-apk-receipt-v1",
                    data=canonical_json(forged).encode(),
                    producer_operation_id=receipt.operation_key,
                    input_hashes=forged.receipt_input_hashes,
                )
                with runtime().ledger._connection() as connection:
                    connection.execute(
                        "UPDATE operation_claims SET status = 'effect', output_json = ? "
                        "WHERE operation_key = ?",
                        (canonical_json(forged_output), receipt.operation_key),
                    )
                launches = len(self.launches)
                with (
                    mock.patch.object(
                        activities, "execute", side_effect=AssertionError("launch")
                    ),
                    mock.patch.object(
                        activities,
                        "materialize_decoded_tree",
                        side_effect=AssertionError("workspace"),
                    ),
                ):
                    with self.assertRaises(ValueError):
                        await self.invoke("forgery-retry")
                self.assertEqual(len(self.launches), launches)
                self.assertFalse(
                    (
                        runtime().attempts_root
                        / receipt.operation_key
                        / digest(b"forgery-retry")
                    ).exists()
                )

    async def test_adoption_rejects_receipt_intermediate_final_and_predecessor_child_tamper(self) -> None:
        targets = ("receipt", "intermediate", "final", "predecessor-child")
        for target in targets:
            with self.subTest(target=target):
                self.configure_fresh(f"tamper-{target}")
                output = await self.invoke("first")
                receipt = self.receipt(output)
                patched_manifest = load_decoded_tree(
                    runtime().store, self.apply_receipt.patched_tree_manifest
                )
                paths = {
                    "receipt": self.blob(output),
                    "intermediate": self.blob(receipt.intermediate_apk),
                    "final": self.blob(receipt.patched_apk),
                    "predecessor-child": self.blob(
                        next(
                            entry.sha256
                            for entry in patched_manifest.entries
                            if entry.kind == "file"
                        )
                    ),
                }
                paths[target].chmod(0o644)
                before = len(self.launches)
                with mock.patch.object(
                    activities, "execute", side_effect=AssertionError("launched")
                ):
                    with self.assertRaises((OSError, ValueError)):
                        await self.invoke("retry")
                self.assertEqual(len(self.launches), before)

    async def test_operation_and_receipt_are_independent_of_root_and_owner(self) -> None:
        first = await self.invoke("owner-a")
        first_bytes = runtime().store.read_bytes(first)
        self.configure_fresh("deterministic-second")
        second = await self.invoke("owner-b")
        self.assertEqual(second.producer_operation_id, first.producer_operation_id)
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(runtime().store.read_bytes(second), first_bytes)

    async def test_cancellation_supervises_composition_and_quarantines(self) -> None:
        started = threading.Event()
        release = threading.Event()
        real_compose = activities.compose_apk

        def blocked(*args: Any) -> BackendReport:
            started.set()
            if not release.wait(5):
                raise RuntimeError("release timeout")
            return real_compose(*args)

        with mock.patch.object(activities, "compose_apk", side_effect=blocked):
            task = asyncio.create_task(self.invoke("cancel"))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        predecessors = activities._replay_build_predecessors(self.admitted)
        key, _, _ = activities._replay_build_operation_identity(
            self.admitted, predecessors[2], predecessors[3], predecessors[4], predecessors[0], predecessors[1]
        )
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")

    async def test_subprocess_cancellation_kills_reaps_and_quarantines(self) -> None:
        self.process = FakeBuildProcess(block=True)
        task = asyncio.create_task(self.invoke("subprocess-cancel"))
        await asyncio.wait_for(self.process.started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.process.killed)
        self.assertTrue(self.process.reaped)
        predecessors = activities._replay_build_predecessors(self.admitted)
        key, _, _ = activities._replay_build_operation_identity(
            self.admitted,
            predecessors[2],
            predecessors[3],
            predecessors[4],
            predecessors[0],
            predecessors[1],
        )
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")

    async def test_workspace_mutation_and_receipt_publication_failure_quarantine(self) -> None:
        original_communicate = self.process.communicate

        async def mutate() -> tuple[bytes, bytes]:
            result = await original_communicate()
            assert self.process.cwd is not None
            (self.process.cwd / "patched-tree/smali/Example.smali").write_text(
                "mutated", encoding="utf-8"
            )
            return result

        self.process.communicate = mutate  # type: ignore[method-assign]
        with self.assertRaises(PermissionError):
            await self.invoke("mutation")

        self.configure_fresh("receipt-publication")
        original_put = runtime().store.put_bytes

        def fail_receipt(**kwargs: Any) -> ArtifactRef:
            if kwargs["kind"] == "replay-patched-apk-receipt-v1":
                raise RuntimeError("receipt publication")
            return original_put(**kwargs)

        with mock.patch.object(runtime().store, "put_bytes", side_effect=fail_receipt):
            with self.assertRaisesRegex(RuntimeError, "receipt publication"):
                await self.invoke("receipt-failure")
        predecessors = activities._replay_build_predecessors(self.admitted)
        key, _, _ = activities._replay_build_operation_identity(
            self.admitted,
            predecessors[2],
            predecessors[3],
            predecessors[4],
            predecessors[0],
            predecessors[1],
        )
        self.assertEqual(runtime().ledger.operation_status(key), "quarantined")

    async def test_unsafe_intermediate_and_final_outputs_quarantine(self) -> None:
        for variant in (
            "intermediate-missing",
            "intermediate-directory",
            "intermediate-symlink",
            "intermediate-hardlink",
            "final-hardlink",
            "final-replacement",
        ):
            with self.subTest(variant=variant):
                self.configure_fresh(f"unsafe-{variant}")
                async def unsafe_intermediate() -> tuple[bytes, bytes]:
                    assert self.process.cwd is not None
                    if variant == "intermediate-missing":
                        self.process.reaped = True
                        return b"built", b""
                    if variant == "intermediate-directory":
                        (self.process.cwd / "intermediate.apk").mkdir()
                    elif variant == "intermediate-symlink":
                        (self.process.cwd / "intermediate.apk").symlink_to("stock.apk")
                    else:
                        temporary = self.process.cwd / "linked-intermediate"
                        temporary.write_bytes(zip_bytes((("classes.dex", b"rebuilt"),)))
                        (self.process.cwd / "intermediate.apk").hardlink_to(temporary)
                    self.process.reaped = True
                    return b"built", b""

                boundary: Any = contextlib.nullcontext()
                if variant.startswith("intermediate-"):
                    self.process.communicate = unsafe_intermediate  # type: ignore[method-assign]
                else:
                    real_compose = activities.compose_apk

                    def unsafe_final(*args: Any) -> BackendReport:
                        report = real_compose(*args)
                        output_path = Path(args[3])
                        if variant == "final-hardlink":
                            output_path.with_name("linked-final").hardlink_to(output_path)
                        else:
                            output_path.unlink()
                            output_path.write_bytes(b"replacement")
                        return report

                    boundary = mock.patch.object(
                        activities, "compose_apk", side_effect=unsafe_final
                    )
                with boundary:
                    with self.assertRaises((OSError, PermissionError, RuntimeError, ValueError)):
                        await self.invoke(f"owner-{variant}")
                predecessors = activities._replay_build_predecessors(self.admitted)
                key, _, _ = activities._replay_build_operation_identity(
                    self.admitted,
                    predecessors[2],
                    predecessors[3],
                    predecessors[4],
                    predecessors[0],
                    predecessors[1],
                )
                self.assertEqual(runtime().ledger.operation_status(key), "quarantined")

    async def test_build_uses_concrete_ledger_lifecycle_methods(self) -> None:
        shadows = {
            name: mock.Mock(side_effect=AssertionError(f"{name} instance shadow called"))
            for name in (
                "require_admitted_replay_v3",
                "require_completed_operation",
                "begin_operation",
                "record_effect",
                "complete_operation",
                "release_pending_operation",
                "quarantine_operation",
            )
        }
        for name, shadow in shadows.items():
            setattr(runtime().ledger, name, shadow)
        output = await self.invoke("shadow-owner")
        self.assertEqual(runtime().ledger.operation_status(output.producer_operation_id), "completed")
        for shadow in shadows.values():
            shadow.assert_not_called()

    def test_backend_report_conversion_requires_exact_compiled_backend(self) -> None:
        compiled = compile_port(self.admitted.intent, self.admitted.resolution)
        report = BackendReport(
            compiled.backend.kind, "1" * 64, "2" * 64, "2" * 64,
            compiled.backend.final_dex_entries, (), (), 0, (), True,
        )
        composition = activities._replay_backend_composition(report, compiled)
        self.assertIs(type(composition), ReplayBackendCompositionV1)
        with self.assertRaises(ValueError):
            activities._replay_backend_composition(replace(report, final_dex_entries=("classes2.dex",)), compiled)


class ReplayBuildRegistrationTests(unittest.TestCase):
    def test_temporal_metadata_present_but_worker_and_workflow_exclude_activity(self) -> None:
        self.assertIsNotNone(
            activity._Definition.from_callable(replay_build_patched_apk_checkpoint_activity)  # type: ignore[attr-defined]
        )
        from dfinsta_pipeline import worker

        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined]
            for fn in worker.REGISTERED_ACTIVITIES
        }
        self.assertIn("replay_build_patched_apk_stage_activity", registered)
        self.assertNotIn("replay_build_patched_apk_checkpoint_activity", registered)


if __name__ == "__main__":
    unittest.main()
