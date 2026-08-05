import asyncio
import hashlib
import inspect
import itertools
import json
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from temporalio import activity

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_apply_tree_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.contracts import ArtifactRef, canonical_json, canonical_sha256
from dfinsta_pipeline.decoded_artifact import capture_decoded_tree, load_decoded_tree
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.port_contracts import SmaliEdit
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    GatePreparedEnvelopeV2,
    ReplayApplyOperationResultV1,
    ReplayDecodedTreeReceiptV1,
    ReplayDecodedTreeReceiptV2,
    ReplayFrameworkCacheReceiptV1,
    ReplayPatchedTreeReceiptV1,
    ReplaySourceAdmissionEvidenceV1,
    SourceManifestV1,
)
from tests.test_phase_b_replay_contracts import admit_v3, artifact_ref, fixture_v3


# id() is only unique among LIVE objects. These fixtures replace the ledger
# per subTest, so CPython can hand a freed address back and two iterations
# collide on the same path, failing mkdir(parents=True). A counter is
# genuinely unique; exist_ok would instead let two iterations share a tree.
_TREE_SEQUENCE = itertools.count()

WORKER_SMALI = (
    b".class public Lsample/Worker;\n"
    b".super Ljava/lang/Object;\n\n"
    b".method public run()V\n"
    b"    .registers 2\n"
    b"    const/4 v0, 0x0\n"
    b"    return-void\n"
    b".end method\n"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def smali_edit(final_value: str = "0x1") -> SmaliEdit:
    return SmaliEdit(
        "edit-worker",
        "smali_edit",
        ("retain-hook",),
        "Lsample/Worker;",
        "run()V",
        "replace",
        "all",
        None,
        ("const/4 v0, 0x0",),
        1,
        (f"const/4 v0, {final_value}",),
        (f"const/4 v0, {final_value}",),
        1,
    )


def apply_authority(
    *,
    suffix: str = "main",
    final_value: str = "0x1",
    with_framework: bool = False,
) -> AdmittedReplayV3:
    base = admit_v3(
        fixture_v3(
            with_framework,
            framework_package_ids=(2, 10) if with_framework else None,
        )
    )
    manifest = SourceManifestV1(())
    resolution = replace(
        base.resolution,
        source_bundle_sha256=manifest.sha256,
        operations=(smali_edit(final_value),),
    )
    resolution_payload = canonical_json(resolution).encode("utf-8")
    source_payload = canonical_json(manifest.records).encode("utf-8")
    resolution_ref = artifact_ref("resolution-spec", resolution_payload)
    source_ref = artifact_ref("source-manifest-v1", source_payload)
    gate = replace(
        base.gate_prepared,
        resolution=resolution_ref,
        source_manifest=source_ref,
    )
    gate_payload = canonical_json(gate).encode("utf-8")
    gate_inputs = (
        gate.stock_apk.sha256,
        gate.intent.sha256,
        gate.resolution.sha256,
        gate.source_manifest.sha256,
        gate.toolchain_profile.sha256,
        *(item.artifact.sha256 for item in gate.frameworks),
        *(item.artifact.sha256 for item in gate.tools),
    )
    gate_ref = artifact_ref("replay-gate-prepared-v2", gate_payload, inputs=gate_inputs)
    run_spec = replace(
        base.run_spec,
        run_id=f"replay-run-{suffix}",
        resolution_sha256=resolution.sha256,
        source_manifest_sha256=manifest.sha256,
        gate_prepared_sha256=gate_ref.sha256,
        gate_prepared_ref_sha256=canonical_sha256(gate_ref),
    )
    request = replace(
        base.request,
        run_spec_sha256=run_spec.sha256,
        gate_prepared=gate_ref,
        resolution=resolution_ref,
        source_manifest=source_ref,
    )
    decision = replace(
        base.decision,
        decision_id=f"decision-{suffix}",
        idempotency_id=f"decision-attempt-{suffix}",
        run_id=run_spec.run_id,
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_ref.sha256,
    )
    return AdmittedReplayV3(
        3,
        run_spec,
        request,
        decision,
        base.intent,
        resolution,
        manifest,
        base.profile,
        GatePreparedEnvelopeV2.from_dict(json.loads(gate_payload)),
        base.executor_capabilities,
    )


def contract_receipt() -> ReplayPatchedTreeReceiptV1:
    decode_key = "1" * 64
    apply_key = "2" * 64
    completed_decode = ArtifactRef(
        1,
        "replay-decoded-tree-receipt-v1",
        "3" * 64,
        7,
        f"cas://sha256/{'3' * 64}",
        decode_key,
        ("4" * 64,),
    )
    input_manifest = ArtifactRef(
        1,
        "decoded-tree-manifest-v1",
        "5" * 64,
        8,
        f"cas://sha256/{'5' * 64}",
        decode_key,
        ("6" * 64,),
    )
    result = ReplayApplyOperationResultV1("edit-worker", "applied")
    source_evidence = ReplaySourceAdmissionEvidenceV1(
        2,
        "7" * 64,
        "b" * 64,
        "d" * 64,
        0,
        "admitted-source",
        True,
    )
    fixed = (
        "7" * 64,
        canonical_sha256(completed_decode),
        canonical_sha256(input_manifest),
        "8" * 64,
        "9" * 64,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        source_evidence.sha256,
        "e" * 64,
    )
    output_manifest = ArtifactRef(
        1,
        "decoded-tree-manifest-v1",
        "f" * 64,
        9,
        f"cas://sha256/{'f' * 64}",
        apply_key,
        fixed,
    )
    return ReplayPatchedTreeReceiptV1(
        1,
        "7" * 64,
        completed_decode,
        input_manifest,
        "8" * 64,
        "9" * 64,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        source_evidence,
        (result,),
        "e" * 64,
        output_manifest,
        "0" * 64,
        apply_key,
        True,
    )


class ReplayApplyContractTests(unittest.TestCase):
    def test_result_and_receipt_strict_round_trip_and_lineage(self) -> None:
        result = ReplayApplyOperationResultV1("edit-worker", "applied")
        self.assertEqual(ReplayApplyOperationResultV1.from_dict(asdict(result)), result)
        for value in (
            {"operation_id": "edit-worker"},
            {"operation_id": "edit-worker", "status": "skipped"},
            {"operation_id": "edit-worker", "status": True},
            {"operation_id": "edit-worker", "status": "applied", "extra": 1},
        ):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                ReplayApplyOperationResultV1.from_dict(value)

        receipt = contract_receipt()
        value = asdict(receipt)
        self.assertEqual(ReplayPatchedTreeReceiptV1.from_dict(value), receipt)
        mutations = (
            {key: item for key, item in value.items() if key != "success"},
            {**value, "extra": 1},
            {**value, "schema_version": True},
            {**value, "success": 1},
            {
                **value,
                "completed_decode_receipt": {
                    **value["completed_decode_receipt"],
                    "kind": "other",
                },
            },
            {
                **value,
                "patched_tree_manifest": {
                    **value["patched_tree_manifest"],
                    "producer_operation_id": "0" * 64,
                },
            },
            {
                **value,
                "patched_tree_manifest": {
                    **value["patched_tree_manifest"],
                    "input_hashes": value["patched_tree_manifest"]["input_hashes"][:-1],
                },
            },
            {**value, "operation_results": [asdict(receipt.operation_results[0])] * 2},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises((TypeError, ValueError)):
                ReplayPatchedTreeReceiptV1.from_dict(mutation)

    def test_strict_receipt_parser_rejects_duplicate_and_noncanonical_json(self) -> None:
        receipt = contract_receipt()
        canonical = canonical_json(receipt).encode("utf-8")
        self.assertEqual(activities._strict_replay_patched_tree_receipt(canonical), receipt)
        for payload in (
            b'{"schema_version":1,"schema_version":1}',
            b" " + canonical,
            canonical.replace(b'"schema_version":1', b'"schema_version":NaN', 1),
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                activities._strict_replay_patched_tree_receipt(payload)


class ReplayApplyCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_mutation_failure_is_attached_to_racing_cancellation(self) -> None:
        async def fail() -> object:
            raise RuntimeError("mutation failed")

        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        self.assertTrue(task.done())
        with mock.patch.object(
            activities.asyncio,
            "shield",
            side_effect=asyncio.CancelledError(),
        ):
            with self.assertRaises(asyncio.CancelledError) as caught:
                await activities._await_apply_mutation(task)  # type: ignore[arg-type]
        self.assertTrue(
            any(
                "RuntimeError: mutation failed" in note
                for note in caught.exception.__notes__
            )
        )


class RequireCompletedOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Ledger(Path(temporary.name) / "ledger.sqlite3")

    @staticmethod
    def output(key: str) -> ArtifactRef:
        return ArtifactRef(
            1,
            "result",
            "a" * 64,
            1,
            f"cas://sha256/{'a' * 64}",
            key,
            ("b" * 64,),
        )

    def complete(self, key: str = "operation") -> ArtifactRef:
        output = self.output(key)
        self.ledger.begin_operation(key, "kind", "input", "owner", retry_safe=False)
        self.ledger.record_effect(key, "owner", output)
        self.ledger.complete_operation(key, output)
        return output

    def test_exact_completed_output_succeeds_without_writes(self) -> None:
        output = self.complete()
        with self.ledger._connection() as connection:
            before = connection.execute("SELECT * FROM operation_claims").fetchall(), connection.execute(
                "SELECT * FROM operation_events"
            ).fetchall()
        self.assertEqual(
            self.ledger.require_completed_operation("operation", "kind", "input"), output
        )
        with self.ledger._connection() as connection:
            after = connection.execute("SELECT * FROM operation_claims").fetchall(), connection.execute(
                "SELECT * FROM operation_events"
            ).fetchall()
        self.assertEqual(after, before)

    def test_missing_pending_effect_quarantined_and_claim_mismatch_fail(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.require_completed_operation("missing", "kind", "input")
        self.ledger.begin_operation("pending", "kind", "input", "owner", retry_safe=False)
        self.ledger.begin_operation("effect", "kind", "input", "owner", retry_safe=False)
        self.ledger.record_effect("effect", "owner", self.output("effect"))
        self.ledger.begin_operation("quarantined", "kind", "input", "owner", retry_safe=False)
        self.ledger.quarantine_operation("quarantined", "owner")
        for key in ("pending", "effect", "quarantined"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.ledger.require_completed_operation(key, "kind", "input")
        self.complete("completed")
        for kind, input_sha256 in (("other", "input"), ("kind", "other")):
            with self.subTest(kind=kind, input=input_sha256), self.assertRaises(ValueError):
                self.ledger.require_completed_operation("completed", kind, input_sha256)

    def test_corrupt_noncanonical_and_wrong_producer_outputs_fail(self) -> None:
        cases = {
            "corrupt": "{}",
            "noncanonical": " " + canonical_json(self.output("noncanonical")),
            "producer": canonical_json(replace(self.output("producer"), producer_operation_id="other")),
        }
        for key, output_json in cases.items():
            with self.subTest(key=key):
                self.complete(key)
                with self.ledger._connection() as connection:
                    connection.execute(
                        "UPDATE operation_claims SET output_json = ? WHERE operation_key = ?",
                        (output_json, key),
                    )
                with self.assertRaises(ValueError):
                    self.ledger.require_completed_operation(key, "kind", "input")


class ReplayApplyActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="apply-activity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.admitted = apply_authority()
        self.configure(self.root / "run")
        self.record_authority(self.admitted)
        self.decode_output, self.decoded_receipt = self.record_decode(self.admitted)

    def configure(self, root: Path, *, source: bool = True) -> None:
        source_root = root / "source"
        if source:
            source_root.mkdir(parents=True, exist_ok=True)
        configure_runtime(
            root / "state",
            attempts_root=root / "attempts",
            source_root=source_root if source else None,
        )

    @staticmethod
    def record_authority(admitted: AdmittedReplayV3) -> None:
        runtime().ledger.record_decision(admitted.decision)
        runtime().ledger.record_admitted_replay_v3(admitted)

    def record_decode(
        self,
        admitted: AdmittedReplayV3,
        *,
        status: str = "completed",
        completed_framework: ArtifactRef | None = None,
        framework_receipt: ReplayFrameworkCacheReceiptV1 | None = None,
    ) -> tuple[ArtifactRef, ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2]:
        tree = self.root / f"decode-{admitted.run_spec.run_id}-{next(_TREE_SEQUENCE)}"
        (tree / "smali/sample").mkdir(parents=True)
        (tree / "smali/sample/Worker.smali").write_bytes(WORKER_SMALI)
        (tree / "empty").mkdir()
        key, input_sha256, request_sha256 = activities._replay_decode_operation_identity(
            admitted, completed_framework, framework_receipt
        )
        plan = admitted.plan("decode")
        capability = admitted.capability("decode")
        tool = next(
            item
            for item in admitted.request.tools
            if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
        )
        execution_inputs: tuple[str, ...] = (
            admitted.sha256,
            canonical_sha256(admitted.request.stock_apk),
            admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request_sha256,
        )
        if completed_framework is not None and framework_receipt is not None:
            execution_inputs = (
                *execution_inputs,
                canonical_sha256(completed_framework),
                canonical_sha256(framework_receipt.framework_cache_manifest),
                framework_receipt.framework_cache_semantic_sha256,
            )
        manifest_ref = capture_decoded_tree(runtime().store, tree, key, execution_inputs)
        manifest = load_decoded_tree(runtime().store, manifest_ref)
        if completed_framework is None or framework_receipt is None:
            receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2 = (
                ReplayDecodedTreeReceiptV1(
                    1,
                    "stock_input",
                    admitted.sha256,
                    admitted.request.stock_apk,
                    admitted.profile.profile_id,
                    admitted.profile.sha256,
                    "decode",
                    plan.sha256,
                    capability.canonical_identity,
                    tool.artifact.sha256,
                    request_sha256,
                    manifest_ref,
                    manifest.decoded_tree_sha256,
                    key,
                    True,
                )
            )
        else:
            receipt = ReplayDecodedTreeReceiptV2(
                2,
                "stock_input",
                admitted.sha256,
                admitted.request.stock_apk,
                admitted.profile.profile_id,
                admitted.profile.sha256,
                "decode",
                plan.sha256,
                capability.canonical_identity,
                tool.artifact.sha256,
                request_sha256,
                completed_framework,
                framework_receipt.framework_cache_manifest,
                framework_receipt.framework_cache_semantic_sha256,
                manifest_ref,
                manifest.decoded_tree_sha256,
                key,
                True,
            )
        output = runtime().store.put_bytes(
            kind=(
                "replay-decoded-tree-receipt-v2"
                if isinstance(receipt, ReplayDecodedTreeReceiptV2)
                else "replay-decoded-tree-receipt-v1"
            ),
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        if status != "unrecorded":
            runtime().ledger.begin_operation(
                key,
                (
                    "replay_decode_tree_v2"
                    if isinstance(receipt, ReplayDecodedTreeReceiptV2)
                    else "replay_decode_tree_v1"
                ),
                input_sha256,
                "decode-owner",
                retry_safe=False,
            )
        if status in {"effect", "completed"}:
            runtime().ledger.record_effect(key, "decode-owner", output)
        if status == "completed":
            runtime().ledger.complete_operation(key, output)
        return output, receipt

    def record_framework(
        self, admitted: AdmittedReplayV3
    ) -> tuple[ArtifactRef, ReplayFrameworkCacheReceiptV1]:
        key, input_sha256, installations, _ = (
            activities._replay_framework_operation_identity(admitted)
        )
        tree = self.root / f"framework-{admitted.run_spec.run_id}-{next(_TREE_SEQUENCE)}"
        tree.mkdir(parents=True)
        for installation in installations:
            (tree / f"{installation.package_id}.apk").write_bytes(
                next(
                    admitted_framework.artifact.sha256.encode()
                    for admitted_framework in admitted.request.frameworks
                    if admitted_framework.package_id == installation.package_id
                )
            )
        plan = admitted.plan("install_framework")
        capability = admitted.capability("install_framework")
        tool = next(
            item
            for item in admitted.request.tools
            if item.tool_id == admitted.profile.tool_for_role("install_framework").tool_id
        )
        execution_inputs = (
            admitted.sha256,
            admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            *(canonical_sha256(item) for item in installations),
        )
        manifest_ref = capture_decoded_tree(runtime().store, tree, key, execution_inputs)
        manifest = load_decoded_tree(runtime().store, manifest_ref)
        receipt = ReplayFrameworkCacheReceiptV1(
            1,
            admitted.sha256,
            admitted.profile.profile_id,
            admitted.profile.sha256,
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
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        runtime().ledger.begin_operation(
            key,
            "replay_install_frameworks_v1",
            input_sha256,
            "framework-owner",
            retry_safe=False,
        )
        runtime().ledger.record_effect(key, "framework-owner", output)
        runtime().ledger.complete_operation(key, output)
        return output, receipt

    async def invoke(
        self, candidate: AdmittedReplayV3 | None = None, *, owner: str = "apply-owner"
    ) -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_apply_tree_checkpoint_activity(candidate or self.admitted)

    @staticmethod
    def receipt(output: ArtifactRef) -> ReplayPatchedTreeReceiptV1:
        return ReplayPatchedTreeReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def blob(reference: ArtifactRef | str) -> Path:
        digest_value = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / digest_value[:2] / digest_value

    def apply_key(self, admitted: AdmittedReplayV3 | None = None) -> tuple[str, str]:
        admitted = admitted or self.admitted
        compiled = compile_port(admitted.intent, admitted.resolution)
        operation_input = {
            "schema_version": 1,
            "admitted_replay_sha256": admitted.sha256,
            "completed_decode_receipt": self.decode_output,
            "input_decoded_tree_manifest": self.decoded_receipt.decoded_tree_manifest,
            "input_decoded_tree_semantic_sha256": self.decoded_receipt.decoded_tree_semantic_sha256,
            "intent_sha256": admitted.intent.sha256,
            "resolution_sha256": admitted.resolution.sha256,
            "source_manifest_sha256": admitted.source_manifest.sha256,
            "target_port_spec_sha256": compiled.sha256,
        }
        return (
            activities.operation_key("replay_apply_tree_v1", operation_input),
            canonical_sha256(operation_input),
        )

    async def test_real_happy_path_stages_compiles_applies_captures_and_completes(self) -> None:
        original = activities.apply_port
        observed: list[tuple[bytes, bytes]] = []

        def observe(spec: object, work_tree: Path, source_root: Path):
            before = (work_tree / "smali/sample/Worker.smali").read_bytes()
            report = original(spec, work_tree, source_root)
            after = (work_tree / "smali/sample/Worker.smali").read_bytes()
            observed.append((before, after))
            return report

        with mock.patch.object(activities, "apply_port", side_effect=observe):
            output = await self.invoke()
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.patched_tree_manifest)
        worker = next(entry for entry in manifest.entries if entry.path.endswith("Worker.smali"))
        self.assertEqual(receipt.operation_results, (ReplayApplyOperationResultV1("edit-worker", "applied"),))
        self.assertNotEqual(observed[0][0], observed[0][1])
        self.assertEqual(worker.sha256, digest(observed[0][1]))
        self.assertEqual(runtime().ledger.operation_status(output.producer_operation_id), "completed")
        self.assertEqual(runtime().ledger.operation_event_count(output.producer_operation_id, "effect"), 1)
        self.assertEqual(receipt.completed_decode_receipt, self.decode_output)
        self.assertEqual(receipt.input_decoded_tree_manifest, self.decoded_receipt.decoded_tree_manifest)
        self.assertEqual(receipt.patched_tree_semantic_sha256, manifest.decoded_tree_sha256)
        workspace = runtime().attempts_root / output.producer_operation_id / digest(b"apply-owner")
        self.assertTrue((workspace / "admitted-source").is_dir())

    async def test_candidate_only_and_exact_completed_predecessor_is_mandatory(self) -> None:
        parameters = tuple(inspect.signature(replay_apply_tree_checkpoint_activity).parameters)
        self.assertEqual(parameters, ("candidate",))
        for status in ("unrecorded", "effect"):
            with self.subTest(status=status):
                root = self.root / status
                self.configure(root)
                self.record_authority(self.admitted)
                self.decode_output, self.decoded_receipt = self.record_decode(
                    self.admitted, status=status
                )
                with (
                    mock.patch.object(runtime().ledger, "begin_operation") as begin,
                    mock.patch.object(activities, "admit_source_bundle_v2") as source,
                    mock.patch.object(activities, "materialize_decoded_tree") as workspace,
                ):
                    with self.assertRaises(ValueError):
                        await self.invoke(owner=f"owner-{status}")
                begin.assert_not_called()
                source.assert_not_called()
                workspace.assert_not_called()

    async def test_framework_authority_accepts_completed_v2_decode_and_rejects_wrong_predecessor(self) -> None:
        framework_admitted = apply_authority(
            suffix="framework", with_framework=True
        )
        self.admitted = framework_admitted
        self.configure(self.root / "framework-apply")
        self.record_authority(framework_admitted)
        framework_output, framework_receipt = self.record_framework(
            framework_admitted
        )
        self.decode_output, self.decoded_receipt = self.record_decode(
            framework_admitted,
            completed_framework=framework_output,
            framework_receipt=framework_receipt,
        )
        self.assertIsInstance(self.decoded_receipt, ReplayDecodedTreeReceiptV2)
        output = await self.invoke()
        receipt = self.receipt(output)
        self.assertEqual(receipt.completed_decode_receipt, self.decode_output)
        self.assertEqual(
            receipt.input_decoded_tree_manifest,
            self.decoded_receipt.decoded_tree_manifest,
        )
        self.assertEqual(
            runtime().ledger.operation_status(output.producer_operation_id), "completed"
        )

        wrong = apply_authority(
            suffix="wrong-framework", with_framework=True
        )
        self.configure(self.root / "wrong-framework-apply")
        self.record_authority(framework_admitted)
        self.record_framework(wrong)
        with (
            mock.patch.object(runtime().ledger, "begin_operation") as begin,
            mock.patch.object(activities, "admit_source_bundle_v2") as source,
            mock.patch.object(activities, "materialize_decoded_tree") as workspace,
        ):
            with self.assertRaises(ValueError):
                await self.invoke(framework_admitted)
        begin.assert_not_called()
        source.assert_not_called()
        workspace.assert_not_called()

    async def test_authority_precedence_and_normalized_return_value(self) -> None:
        candidates = (
            apply_authority(suffix="unrecorded"),
            apply_authority(final_value="0x2"),
        )
        for candidate in candidates:
            with self.subTest(run_id=candidate.run_spec.run_id):
                with (
                    mock.patch.object(
                        runtime().ledger, "require_completed_operation"
                    ) as predecessor,
                    mock.patch.object(runtime().ledger, "begin_operation") as begin,
                    mock.patch.object(runtime().store, "read_bytes") as cas,
                    mock.patch.object(activities, "admit_source_bundle_v2") as source,
                    mock.patch.object(activities, "materialize_decoded_tree") as workspace,
                ):
                    with self.assertRaisesRegex(ValueError, "authority"):
                        await self.invoke(candidate)
            predecessor.assert_not_called()
            begin.assert_not_called()
            cas.assert_not_called()
            source.assert_not_called()
            workspace.assert_not_called()

        normalized = AdmittedReplayV3.from_dict(json.loads(canonical_json(self.admitted)))
        self.assertIsNot(normalized, self.admitted)
        authority_shadow = mock.Mock(return_value=normalized)
        predecessor_shadow = mock.Mock(return_value=self.decode_output)
        runtime().ledger.require_admitted_replay_v3 = authority_shadow  # type: ignore[method-assign]
        runtime().ledger.require_completed_operation = predecessor_shadow  # type: ignore[method-assign]
        shadow_output = await self.invoke(owner="shadow-owner")
        authority_shadow.assert_not_called()
        predecessor_shadow.assert_not_called()
        self.assertEqual(self.receipt(shadow_output).admitted_replay_sha256, self.admitted.sha256)

        self.configure(self.root / "normalized")
        self.record_authority(normalized)
        self.decode_output, self.decoded_receipt = self.record_decode(normalized)
        seen: list[AdmittedReplayV3] = []
        real_compile = activities.compile_port

        def compile_normalized(intent: object, resolution: object):
            self.assertIs(intent, normalized.intent)
            self.assertIs(resolution, normalized.resolution)
            seen.append(normalized)
            return real_compile(intent, resolution)

        with (
            mock.patch.object(
                activities.Ledger, "require_admitted_replay_v3", return_value=normalized
            ),
            mock.patch.object(activities, "compile_port", side_effect=compile_normalized),
        ):
            output = await self.invoke()
        self.assertEqual(seen, [normalized])
        self.assertEqual(self.receipt(output).admitted_replay_sha256, normalized.sha256)

    async def test_cancellation_waits_for_mutation_then_releases(self) -> None:
        started = threading.Event()
        release = threading.Event()
        real_apply = activities.apply_port

        def blocked_apply(*args: object):
            started.set()
            if not release.wait(5):
                raise RuntimeError("apply release timed out")
            return real_apply(*args)  # type: ignore[arg-type]

        with mock.patch.object(activities, "apply_port", side_effect=blocked_apply):
            task = asyncio.create_task(self.invoke(owner="cancel-owner"))
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
        key, _ = self.apply_key()
        # Released: the mutation thread was drained before the cancellation
        # propagated, so nothing is still writing.
        self.assertEqual(runtime().ledger.operation_status(key), "pending")
        self.assertEqual(runtime().ledger.operation_event_count(key, "effect"), 0)

    async def test_adoption_rejects_source_evidence_file_count_mismatch(self) -> None:
        output = await self.invoke()
        receipt = self.receipt(output)
        source_evidence = replace(receipt.source_admission, file_count=1)
        execution_inputs = (
            *receipt.execution_input_hashes[:8],
            source_evidence.sha256,
            receipt.apply_report_sha256,
        )
        manifest_ref = replace(
            receipt.patched_tree_manifest,
            input_hashes=execution_inputs,
        )
        forged = replace(
            receipt,
            source_admission=source_evidence,
            patched_tree_manifest=manifest_ref,
        )
        forged_output = runtime().store.put_bytes(
            kind="replay-patched-tree-receipt-v1",
            data=canonical_json(forged).encode("utf-8"),
            producer_operation_id=receipt.operation_key,
            input_hashes=forged.receipt_input_hashes,
        )
        compiled = compile_port(self.admitted.intent, self.admitted.resolution)
        with self.assertRaisesRegex(ValueError, "admitted execution"):
            activities._validate_replay_patched_tree_receipt(
                forged_output,
                receipt.operation_key,
                admitted=self.admitted,
                completed_decode_receipt=self.decode_output,
                decoded_receipt=self.decoded_receipt,
                compiled=compiled,
            )

    async def test_missing_source_root_releases_before_workspace_and_recovers(self) -> None:
        state = self.root / "repair/state"
        attempts = self.root / "repair/attempts"
        configure_runtime(state, attempts_root=attempts)
        self.record_authority(self.admitted)
        self.decode_output, self.decoded_receipt = self.record_decode(self.admitted)
        with mock.patch.object(activities, "materialize_decoded_tree") as workspace:
            with self.assertRaisesRegex(ValueError, "source root"):
                await self.invoke(owner="first-owner")
        workspace.assert_not_called()
        key, _ = self.apply_key()
        self.assertEqual(runtime().ledger.operation_status(key), "pending")
        self.assertFalse(attempts.exists())

        source = self.root / "repair/source"
        source.mkdir(parents=True)
        configure_runtime(state, attempts_root=attempts, source_root=source)
        output = await self.invoke(owner="second-owner")
        self.assertEqual(output.producer_operation_id, key)
        self.assertEqual(runtime().ledger.operation_status(key), "completed")

    async def test_active_owner_rejected_and_workspace_failures_quarantine(self) -> None:
        key, input_sha256 = self.apply_key()
        runtime().ledger.begin_operation(
            key, "replay_apply_tree_v1", input_sha256, "active-owner", retry_safe=False
        )
        with mock.patch.object(activities, "materialize_decoded_tree") as workspace:
            with self.assertRaisesRegex(ValueError, "already claimed"):
                await self.invoke(owner="other-owner")
        workspace.assert_not_called()
        self.assertEqual(runtime().ledger.operation_status(key), "pending")

        failures = (
            ("source", "admit_source_bundle_v2"),
            ("apply", "apply_port"),
            ("capture", "capture_decoded_tree_fd"),
        )
        for name, boundary in failures:
            with self.subTest(name=name):
                self.configure(self.root / f"failure-{name}")
                self.record_authority(self.admitted)
                self.decode_output, self.decoded_receipt = self.record_decode(self.admitted)
                with mock.patch.object(activities, boundary, side_effect=RuntimeError(name)):
                    with self.assertRaisesRegex(RuntimeError, name):
                        await self.invoke(owner=f"owner-{name}")
                failed_key, _ = self.apply_key()
                self.assertEqual(runtime().ledger.operation_status(failed_key), "quarantined")
                self.assertEqual(runtime().ledger.operation_event_count(failed_key, "effect"), 0)

    async def test_post_effect_retry_adopts_and_revalidates_both_closures(self) -> None:
        complete = activities.Ledger.complete_operation
        with mock.patch.object(
            activities.Ledger,
            "complete_operation",
            side_effect=RuntimeError("completion unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "completion unavailable"):
                await self.invoke(owner="first-owner")
        key, _ = self.apply_key()
        self.assertEqual(runtime().ledger.operation_status(key), "effect")
        decode_manifest = self.decoded_receipt.decoded_tree_manifest
        with (
            mock.patch.object(activities, "materialize_decoded_tree") as workspace,
            mock.patch.object(activities, "admit_source_bundle_v2") as source,
            mock.patch.object(activities, "apply_port") as apply,
            mock.patch.object(activities, "capture_decoded_tree_fd") as capture,
            mock.patch.object(activities, "load_decoded_tree", wraps=activities.load_decoded_tree) as load,
            mock.patch.object(activities.Ledger, "complete_operation", wraps=complete),
        ):
            output = await self.invoke(owner="retry-owner")
        workspace.assert_not_called()
        source.assert_not_called()
        apply.assert_not_called()
        capture.assert_not_called()
        loaded = [call.args[1] for call in load.call_args_list]
        self.assertIn(decode_manifest, loaded)
        self.assertIn(self.receipt(output).patched_tree_manifest, loaded)
        self.assertEqual(runtime().ledger.operation_status(key), "completed")

    async def test_apply_lifecycle_methods_cannot_be_shadowed_on_ledger_instance(self) -> None:
        shadows = {
            name: mock.Mock(side_effect=AssertionError(f"{name} instance shadow called"))
            for name in (
                "begin_operation",
                "record_effect",
                "complete_operation",
                "quarantine_operation",
            )
        }
        for name, shadow in shadows.items():
            setattr(runtime().ledger, name, shadow)
        output = await self.invoke(owner="shadow-owner")
        self.assertEqual(runtime().ledger.operation_status(output.producer_operation_id), "completed")
        for shadow in shadows.values():
            shadow.assert_not_called()

    async def test_representative_decode_and_output_layers_fail_closed(self) -> None:
        mutations = (
            ("decode-receipt", "corrupt"),
            ("decode-child", "missing"),
            ("apply-receipt", "writable"),
            ("output-child", "hardlink"),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                self.configure(self.root / f"tamper-{name}")
                self.record_authority(self.admitted)
                self.decode_output, self.decoded_receipt = self.record_decode(self.admitted)
                output = await self.invoke(owner="first-owner")
                apply_receipt = self.receipt(output)
                decode_manifest = load_decoded_tree(
                    runtime().store, self.decoded_receipt.decoded_tree_manifest
                )
                output_manifest = load_decoded_tree(
                    runtime().store, apply_receipt.patched_tree_manifest
                )
                targets = {
                    "decode-receipt": self.blob(self.decode_output),
                    "decode-child": self.blob(
                        next(entry.sha256 for entry in decode_manifest.entries if entry.kind == "file")
                    ),
                    "apply-receipt": self.blob(output),
                    "output-child": self.blob(
                        next(entry.sha256 for entry in output_manifest.entries if entry.kind == "file")
                    ),
                }
                target = targets[name]
                if mutation == "corrupt":
                    target.chmod(0o644)
                    target.write_bytes(b"corrupt")
                elif mutation == "missing":
                    target.unlink()
                elif mutation == "writable":
                    target.chmod(0o644)
                else:
                    (self.root / f"hardlink-{name}").hardlink_to(target)
                with (
                    mock.patch.object(activities, "materialize_decoded_tree") as workspace,
                    mock.patch.object(activities, "apply_port") as apply,
                    mock.patch.object(activities, "capture_decoded_tree_fd") as capture,
                ):
                    with self.assertRaises((OSError, ValueError)):
                        await self.invoke(owner="retry-owner")
                workspace.assert_not_called()
                apply.assert_not_called()
                capture.assert_not_called()

    async def test_physical_roots_and_owners_do_not_change_canonical_output(self) -> None:
        first = await self.invoke(owner="owner-a")
        first_bytes = runtime().store.read_bytes(first)
        first_receipt = self.receipt(first)

        self.configure(self.root / "identity-second")
        self.record_authority(self.admitted)
        self.decode_output, self.decoded_receipt = self.record_decode(self.admitted)
        second = await self.invoke(owner="owner-b")
        second_receipt = self.receipt(second)
        self.assertEqual(second.producer_operation_id, first.producer_operation_id)
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(runtime().store.read_bytes(second), first_bytes)
        self.assertEqual(second_receipt.patched_tree_manifest, first_receipt.patched_tree_manifest)


class ReplayApplyRegistrationTests(unittest.TestCase):
    def test_runtime_source_root_safety_and_decode_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(TypeError):
                configure_runtime(root / "state-type", source_root=str(root / "source"))  # type: ignore[arg-type]
            protected = (
                root / "state",
                root / "state/cas/source",
                root / "attempts/source",
                root / "executor/source",
            )
            executable = root / "executor"
            executable.write_bytes(b"executor")
            for index, source in enumerate(protected):
                with self.subTest(source=source), self.assertRaises(ValueError):
                    configure_runtime(
                        root / "state",
                        attempts_root=root / "attempts",
                        source_root=source,
                        executor_paths={"a" * 64: executable},
                    )
            configure_runtime(root / "decode-state")
            self.assertIsNone(runtime().source_root)
            self.assertTrue(callable(activities.replay_decode_checkpoint_activity))

    def test_temporal_metadata_present_but_worker_and_workflow_do_not_register_it(self) -> None:
        definition = activity._Definition.from_callable(  # type: ignore[attr-defined]
            replay_apply_tree_checkpoint_activity
        )
        self.assertIsNotNone(definition)
        from dfinsta_pipeline import worker

        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined]
            for fn in worker.REGISTERED_ACTIVITIES
        }
        self.assertIn("replay_apply_tree_stage_activity", registered)
        self.assertNotIn("replay_apply_tree_checkpoint_activity", registered)


if __name__ == "__main__":
    unittest.main()
