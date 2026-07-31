import asyncio
import hashlib
import inspect
import json
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from temporalio import activity

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_verify_final_apk_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.backend import validate_composed_apk_bytes
from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.contracts import ArtifactRef, GateDecision, canonical_json, canonical_sha256
from dfinsta_pipeline.decoded_artifact import load_decoded_tree
from dfinsta_pipeline.executor import ExecutionRequest
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.port_contracts import SourceFile
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayVerificationGrantV1,
    ReplayFinalApkVerificationReceiptV1,
    ReplayPatchedApkReceiptV1,
    ReplayVerificationGrantRequestV1,
    SourceManifestV1,
    admit_replay_verification_grant_v1,
)
from dfinsta_pipeline.verifier import VerificationReport
from tests import test_phase_b_build_activity as build_helpers
from tests.test_phase_b_replay_contracts import FixtureV3, admit_v3, artifact_ref
from tests.test_phase_b_verification_grant import verification_capability
from tests.test_phase_b_verifier import minimal_dex


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_with_real_source(base: FixtureV3, source_data: bytes) -> FixtureV3:
    admitted = admit_v3(base)
    source_manifest = SourceManifestV1(
        (SourceFile("code/source.bin", digest(source_data)),)
    )
    source_payload = canonical_json(source_manifest.records).encode("utf-8")
    source_ref = artifact_ref("source-manifest-v1", source_payload)
    resolution = replace(
        admitted.resolution, source_bundle_sha256=source_manifest.sha256
    )
    resolution_payload = canonical_json(resolution).encode("utf-8")
    resolution_ref = artifact_ref("resolution-spec", resolution_payload)
    gate = replace(
        admitted.gate_prepared,
        source_manifest=source_ref,
        resolution=resolution_ref,
    )
    gate_payload = canonical_json(gate).encode("utf-8")
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
        resolution_sha256=resolution.sha256,
        source_manifest_sha256=source_manifest.sha256,
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
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_ref.sha256,
    )
    payloads = dict(base.payloads)
    for reference in (
        base.request.gate_prepared,
        base.request.resolution,
        base.request.source_manifest,
    ):
        payloads.pop(canonical_sha256(reference))
    payloads[canonical_sha256(gate_ref)] = gate_payload
    payloads[canonical_sha256(resolution_ref)] = resolution_payload
    payloads[canonical_sha256(source_ref)] = source_payload
    return FixtureV3(run_spec, request, decision, payloads, base.capabilities)


class FakeDecodeProcess:
    def __init__(
        self,
        decoded_source: Path,
        *,
        block: bool = False,
        mutate: Callable[[Path], None] | None = None,
    ) -> None:
        self.decoded_source = decoded_source
        self.mutate = mutate
        self.returncode: int | None = None if block else 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cwd: Path | None = None
        self.killed = False
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if self.returncode is None and not self.killed:
            await self.release.wait()
            if self.returncode is None:
                self.returncode = 0
        if self.returncode == 0:
            assert self.cwd is not None
            if self.mutate is None:
                shutil.copytree(self.decoded_source, self.cwd / "output")
            else:
                self.mutate(self.cwd)
        self.reaped = True
        return b"decoded", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1
        self.release.set()


class ReplayFinalApkVerificationActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="verification-activity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.attempts = self.root / "attempts"
        self.source_root = self.root / "repository"
        self.source_data = b"exact admitted verification source"
        (self.source_root / "code").mkdir(parents=True)
        (self.source_root / "code/source.bin").write_bytes(self.source_data)
        self.decoded_source = self.root / "decoder-output"
        self.decoded_source.mkdir()
        (self.decoded_source / "AndroidManifest.xml").write_text(
            '<manifest package="sample.package"><application /></manifest>',
            encoding="utf-8",
        )
        self.executable = self.root / "executor.bin"
        self.executable.write_bytes(b"trusted final decode executor")
        self.stock_bytes = build_helpers.zip_bytes(
            (("classes.dex", b"stock classes"), ("assets/stock.bin", b"stock"))
        )
        self.final_bytes = build_helpers.zip_bytes(
            (("classes.dex", minimal_dex(("verified",))),)
        )
        self.with_framework = False
        self.launches: list[dict[str, Any]] = []
        self.process = FakeDecodeProcess(self.decoded_source)
        self.configure_case(False)

    def configure_case(self, with_framework: bool) -> None:
        self.with_framework = with_framework
        self.state = self.root / ("state-framework" if with_framework else "state")
        self.attempts = self.root / (
            "attempts-framework" if with_framework else "attempts"
        )
        base = build_helpers.build_case(
            digest(self.executable.read_bytes()),
            with_framework,
            stock_payload=self.stock_bytes,
        )
        self.case = fixture_with_real_source(base, self.source_data)
        self.admitted = admit_v3(self.case)
        self.launches = []
        self.process = FakeDecodeProcess(self.decoded_source)

        async def launcher(*argv: str, **kwargs: Any) -> FakeDecodeProcess:
            self.launches.append({"argv": argv, **kwargs})
            self.process.cwd = Path(kwargs["cwd"])
            return self.process

        self.launcher = launcher
        capability = replace(
            verification_capability(),
            executable_sha256=digest(self.executable.read_bytes()),
        )
        configure_runtime(
            self.state,
            attempts_root=self.attempts,
            source_root=self.source_root,
            executor_paths={capability.executable_sha256: self.executable},
            launcher=launcher,
        )
        for reference in self.admitted.request.direct_artifacts:
            runtime().store.put_bytes(
                kind=reference.kind,
                data=self.case.resolve(reference),
                producer_operation_id="fixture",
                input_hashes=(),
            )
        Ledger.record_decision(runtime().ledger, self.admitted.decision)
        Ledger.record_admitted_replay_v3(runtime().ledger, self.admitted)
        self.record_predecessors()
        self.completed_build, self.build_receipt = self.record_build()
        request = ReplayVerificationGrantRequestV1(
            1,
            f"verification-grant-{'framework' if with_framework else 'plain'}",
            self.admitted.run_spec.run_id,
            "final-verification-gate",
            self.admitted.run_spec.allowed_actor,
            self.admitted.run_spec.policy_revision,
            self.admitted.sha256,
            self.completed_build,
            self.build_receipt.patched_apk,
            self.admitted.profile.profile_id,
            self.admitted.profile.tool_for_role("decode").artifact_sha256,
            17,
            capability,
        )
        decision = GateDecision(
            1,
            f"verification-decision-{'framework' if with_framework else 'plain'}",
            f"verification-attempt-{'framework' if with_framework else 'plain'}",
            request.allowed_actor,
            request.run_id,
            request.gate_id,
            request.sha256,
            request.sha256,
            request.sha256,
            request.policy_revision,
            "approve",
            "Approved synthetic final verification",
            "2026-07-31T00:00:00Z",
        )
        Ledger.record_decision(runtime().ledger, decision)
        self.grant = admit_replay_verification_grant_v1(
            request,
            decision,
            self.admitted,
            self.build_receipt,
            lambda candidate: candidate == decision,
            runtime().store.read_bytes,
        )
        Ledger.record_admitted_replay_verification_grant_v1(
            runtime().ledger, self.grant
        )
        predecessors = activities._replay_verification_predecessors(self.grant)
        self.key, self.input_sha256, self.execution_request, self.execution_inputs = (
            activities._replay_verification_operation_identity(
                self.grant,
                predecessors[0],
                predecessors[3],
                predecessors[4],
                predecessors[5],
                predecessors[1],
                predecessors[2],
            )
        )

    def record_predecessors(self) -> None:
        helper = build_helpers.ReplayBuildActivityTests(methodName="runTest")
        helper.root = self.root
        helper.case = self.case
        helper.admitted = self.admitted
        helper.fixture_suffix = (
            "verification-framework" if self.with_framework else "verification"
        )
        helper.preexisting_build = False
        helper.framework_output, helper.framework_receipt = helper.record_framework()
        helper.decode_output, helper.decode_receipt = helper.record_decode()
        helper.apply_output, helper.apply_receipt = helper.record_apply()
        self.framework_output = helper.framework_output
        self.framework_receipt = helper.framework_receipt
        self.apply_output = helper.apply_output
        self.apply_receipt = helper.apply_receipt

    def record_build(self) -> tuple[ArtifactRef, ReplayPatchedApkReceiptV1]:
        compiled = compile_port(self.admitted.intent, self.admitted.resolution)
        key, input_sha256, request = activities._replay_build_operation_identity(
            self.admitted,
            self.apply_output,
            self.apply_receipt,
            compiled,
            self.framework_output,
            self.framework_receipt,
        )
        plan = self.admitted.plan("build")
        capability = self.admitted.capability("build")
        tool = next(
            item
            for item in self.admitted.request.tools
            if item.tool_id == self.admitted.profile.tool_for_role("build").tool_id
        )
        execution_inputs: tuple[str, ...] = (
            self.admitted.sha256,
            canonical_sha256(self.apply_output),
            canonical_sha256(self.apply_receipt.patched_tree_manifest),
            self.apply_receipt.patched_tree_semantic_sha256,
            compiled.sha256,
            canonical_sha256(self.admitted.request.stock_apk),
            self.admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request.canonical_identity,
        )
        if self.framework_output is not None and self.framework_receipt is not None:
            execution_inputs = (
                *execution_inputs,
                canonical_sha256(self.framework_output),
                canonical_sha256(self.framework_receipt.framework_cache_manifest),
                self.framework_receipt.framework_cache_semantic_sha256,
            )
        intermediate = runtime().store.put_bytes(
            kind="intermediate-apk",
            data=self.final_bytes,
            producer_operation_id=key,
            input_hashes=execution_inputs,
        )
        report = validate_composed_apk_bytes(
            compiled.backend, self.stock_bytes, self.final_bytes, self.final_bytes
        )
        composition = activities._replay_backend_composition(report, compiled)
        final = runtime().store.put_bytes(
            kind="final-apk",
            data=self.final_bytes,
            producer_operation_id=key,
            input_hashes=(
                *execution_inputs,
                canonical_sha256(intermediate),
                composition.sha256,
            ),
        )
        receipt = ReplayPatchedApkReceiptV1(
            1,
            self.admitted.sha256,
            self.apply_output,
            self.apply_receipt.patched_tree_manifest,
            self.apply_receipt.patched_tree_semantic_sha256,
            compiled.sha256,
            self.admitted.request.stock_apk,
            self.admitted.profile.profile_id,
            self.admitted.profile.sha256,
            "build",
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request.canonical_identity,
            self.framework_output,
            None
            if self.framework_receipt is None
            else self.framework_receipt.framework_cache_manifest,
            None
            if self.framework_receipt is None
            else self.framework_receipt.framework_cache_semantic_sha256,
            intermediate,
            composition,
            final,
            key,
            True,
        )
        output = runtime().store.put_bytes(
            kind="replay-patched-apk-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        Ledger.begin_operation(
            runtime().ledger,
            key,
            "replay_build_patched_apk_v1",
            input_sha256,
            "build-fixture",
            retry_safe=False,
        )
        Ledger.record_effect(runtime().ledger, key, "build-fixture", output)
        Ledger.complete_operation(runtime().ledger, key, output)
        return output, receipt

    async def invoke(self, owner: str = "verification-owner") -> ArtifactRef:
        with mock.patch.object(activities, "_activity_owner", return_value=owner):
            return await replay_verify_final_apk_checkpoint_activity(self.grant)

    @staticmethod
    def receipt(output: ArtifactRef) -> ReplayFinalApkVerificationReceiptV1:
        return ReplayFinalApkVerificationReceiptV1.from_dict(
            json.loads(runtime().store.read_bytes(output))
        )

    @staticmethod
    def blob(reference: ArtifactRef | str) -> Path:
        value = reference if isinstance(reference, str) else reference.sha256
        return runtime().store.root / "sha256" / value[:2] / value

    def assert_quarantined(self) -> None:
        self.assertEqual(Ledger.operation_status(runtime().ledger, self.key), "quarantined")
        self.assertEqual(Ledger.operation_event_count(runtime().ledger, self.key, "effect"), 0)

    async def test_no_framework_primary_and_repository_independent_adoption(self) -> None:
        def decode_with_generated_framework(cwd: Path) -> None:
            shutil.copytree(self.decoded_source, cwd / "output")
            (cwd / "framework/1.apk").write_bytes(b"apktool generated framework")

        self.process.mutate = decode_with_generated_framework
        execute = activities.execute
        with mock.patch.object(activities, "execute", wraps=execute) as execute_call:
            output = await self.invoke("primary")
        self.assertEqual(execute_call.call_count, 1)
        receipt = self.receipt(output)
        self.assertEqual(receipt.operation_key, self.key)
        self.assertEqual(receipt.expected_operation_key, self.key)
        self.assertEqual(output.input_hashes, receipt.receipt_input_hashes)
        self.assertEqual(receipt.assertion_results[-1].assertion_id, "backend.signature-policy")
        self.assertTrue(all(result.passed for result in receipt.assertion_results))
        self.assertEqual(receipt.operation_proof_count, 0)
        self.assertIsNone(receipt.completed_framework_cache_receipt)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(
            self.launches[0]["argv"][1:],
            ("-jar", "tool", "d", "-f", "input.apk", "-o", "output", "-p", "framework"),
        )
        self.assertEqual(self.execution_request.input_artifact.kind, "final-apk")
        self.assertEqual(self.execution_request, execute_call.call_args.args[1])
        self.assertEqual(execute_call.call_args.kwargs["timeout_seconds"], 17)
        workspace = self.attempts / self.key / digest(b"primary")
        self.assertEqual(
            (workspace / "framework/1.apk").read_bytes(),
            b"apktool generated framework",
        )

        shutil.rmtree(self.source_root)
        launches = len(self.launches)
        with mock.patch.object(
            activities, "execute", side_effect=AssertionError("adoption executed")
        ) as adopted_execute:
            adopted = await self.invoke("adoption")
        self.assertEqual(adopted, output)
        adopted_execute.assert_not_called()
        self.assertEqual(len(self.launches), launches)
        validation = self.attempts / self.key / f"validate-{digest(b'adoption')}"
        self.assertFalse(validation.exists())

    async def test_framework_primary_and_adoption_preserve_exact_cache(self) -> None:
        self.configure_case(True)
        output = await self.invoke("framework-primary")
        receipt = self.receipt(output)
        self.assertEqual(receipt.completed_framework_cache_receipt, self.framework_output)
        assert self.framework_receipt is not None
        self.assertEqual(
            receipt.framework_cache_manifest,
            self.framework_receipt.framework_cache_manifest,
        )
        workspace = self.attempts / self.key / digest(b"framework-primary")
        self.assertEqual(
            sorted(path.name for path in (workspace / "framework").iterdir()),
            [f"{item.package_id}.apk" for item in self.admitted.request.frameworks],
        )
        before = len(self.launches)
        with mock.patch.object(
            activities, "execute", side_effect=AssertionError("adoption executed")
        ):
            self.assertEqual(await self.invoke("framework-adoption"), output)
        self.assertEqual(len(self.launches), before)

    async def test_unrecorded_grant_rejects_before_predecessor_claim_or_access(self) -> None:
        other = self.root / "unrecorded"
        configure_runtime(other / "state", attempts_root=other / "attempts")
        with (
            mock.patch.object(activities, "_replay_verification_predecessors") as predecessor,
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(runtime().store, "read_bytes") as read,
            mock.patch.object(activities, "execute") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "authority"):
                await self.invoke("unrecorded")
        predecessor.assert_not_called()
        begin.assert_not_called()
        read.assert_not_called()
        execute.assert_not_called()
        self.assertFalse((other / "attempts").exists())

    async def test_exact_completed_build_and_predecessors_reject_before_claim(self) -> None:
        self.blob(self.completed_build).chmod(0o644)
        self.blob(self.completed_build).write_bytes(b"tampered build receipt")
        with (
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(activities, "execute") as execute,
        ):
            with self.assertRaises((OSError, ValueError)):
                await self.invoke("bad-build")
        begin.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(self.attempts.exists())

    async def test_incomplete_decode_predecessor_rejects_before_verification_claim(self) -> None:
        decode_key = self.apply_receipt.completed_decode_receipt.producer_operation_id
        with runtime().ledger._connection() as connection:
            connection.execute(
                "UPDATE operation_claims SET status = 'effect' WHERE operation_key = ?",
                (decode_key,),
            )
        with (
            mock.patch.object(activities.Ledger, "begin_operation") as begin,
            mock.patch.object(activities, "execute") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "not completed"):
                await self.invoke("bad-decode")
        begin.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(self.attempts.exists())

    def test_capability_request_and_operation_identity_are_final_apk_only(self) -> None:
        capability = self.grant.request.executor_capability
        self.assertEqual(capability.input_kinds, ("final-apk",))
        self.assertEqual(capability.output_kind, "decoded-tree")
        self.assertEqual(capability.allowed_mutation_paths, ("framework", "output"))
        self.assertIs(type(self.execution_request), ExecutionRequest)
        self.assertEqual(self.execution_request.input_artifact, self.build_receipt.patched_apk)
        self.assertEqual(
            self.execution_request.arguments,
            (
                ("decoded_tree", "output"),
                ("framework_dir", "framework"),
                ("input_apk", "input.apk"),
                ("tool", "tool"),
            ),
        )
        self.assertEqual(
            self.input_sha256,
            canonical_sha256(
                {
                    "schema_version": 1,
                    "admitted_verification_grant_sha256": self.grant.sha256,
                    "admitted_replay_sha256": self.admitted.sha256,
                    "completed_patched_apk_receipt": self.completed_build,
                    "patched_apk": self.build_receipt.patched_apk,
                    "target_port_spec_sha256": compile_port(
                        self.admitted.intent, self.admitted.resolution
                    ).sha256,
                    "stock_apk": self.admitted.request.stock_apk,
                    "decoder_profile_id": self.grant.request.decoder_profile_id,
                    "role": "final_decode",
                    "executor_capability_sha256": capability.canonical_identity,
                    "tool_artifact_sha256": self.grant.request.tool_artifact_sha256,
                    "execution_request_sha256": self.execution_request.canonical_identity,
                }
            ),
        )

    async def test_final_apk_descriptor_replacement_fails_closed(self) -> None:
        def replace_input(cwd: Path) -> None:
            original = cwd / "input.apk"
            data = original.read_bytes()
            original.unlink()
            original.write_bytes(data)
            shutil.copytree(self.decoded_source, cwd / "output")

        self.process = FakeDecodeProcess(self.decoded_source, mutate=replace_input)
        with self.assertRaisesRegex(ValueError, "Final APK changed"):
            await self.invoke("replace-final")
        self.assert_quarantined()

    async def test_output_symlink_fails_closed(self) -> None:
        def symlink_output(cwd: Path) -> None:
            (cwd / "output").symlink_to(self.decoded_source, target_is_directory=True)

        self.process = FakeDecodeProcess(self.decoded_source, mutate=symlink_output)
        with self.assertRaises((OSError, ValueError)):
            await self.invoke("symlink-output")
        self.assert_quarantined()

    async def test_output_special_node_fails_closed(self) -> None:
        def special_output(cwd: Path) -> None:
            (cwd / "output").mkdir()
            os.mkfifo(cwd / "output/unsafe")

        self.process = FakeDecodeProcess(self.decoded_source, mutate=special_output)
        with self.assertRaises(ValueError):
            await self.invoke("special-output")
        self.assert_quarantined()

    async def test_source_mutation_after_primary_capture_fails_closed(self) -> None:
        capture = activities.capture_decoded_tree_fd
        calls = 0

        def mutate_after_capture(*args: Any, **kwargs: Any) -> ArtifactRef:
            nonlocal calls
            result = capture(*args, **kwargs)
            calls += 1
            if calls == 1:
                source = next(self.attempts.rglob("admitted-source/code/source.bin"))
                source.chmod(0o600)
                source.write_bytes(b"mutated source")
            return result

        with mock.patch.object(
            activities, "capture_decoded_tree_fd", side_effect=mutate_after_capture
        ):
            with self.assertRaises(ValueError):
                await self.invoke("source-mutation")
        self.assert_quarantined()

    async def test_assertion_failure_quarantines_without_effect(self) -> None:
        real_verify = activities.verify_apk

        def fail_assertion(*args: Any, **kwargs: Any) -> VerificationReport:
            report = real_verify(*args, **kwargs)
            failed = replace(report.assertion_results[0], passed=False, detail="failed")
            return replace(
                report,
                assertion_results=(failed, *report.assertion_results[1:]),
                passed=False,
            )

        with mock.patch.object(activities, "verify_apk", side_effect=fail_assertion):
            with self.assertRaises(ValueError):
                await self.invoke("assertion-failure")
        self.assert_quarantined()

    async def test_post_effect_completion_failure_adopts_without_execute(self) -> None:
        complete = activities.Ledger.complete_operation
        with mock.patch.object(
            activities.Ledger, "complete_operation", side_effect=RuntimeError("complete")
        ):
            with self.assertRaisesRegex(RuntimeError, "complete"):
                await self.invoke("effect-primary")
        self.assertEqual(Ledger.operation_status(runtime().ledger, self.key), "effect")
        shutil.rmtree(self.source_root)
        before = len(self.launches)
        with (
            mock.patch.object(
                activities, "execute", side_effect=AssertionError("execute")
            ) as execute,
            mock.patch.object(activities.Ledger, "complete_operation", wraps=complete),
        ):
            output = await self.invoke("effect-adoption")
        execute.assert_not_called()
        self.assertEqual(len(self.launches), before)
        self.assertEqual(Ledger.operation_status(runtime().ledger, self.key), "completed")
        self.receipt(output)

    async def test_receipt_tamper_rejects_adoption(self) -> None:
        output = await self.invoke("receipt-primary")
        self.blob(output).chmod(0o644)
        self.blob(output).write_bytes(b"tampered receipt")
        with mock.patch.object(activities, "execute", side_effect=AssertionError("execute")):
            with self.assertRaises((OSError, ValueError)):
                await self.invoke("receipt-adoption")

    async def test_decoded_manifest_child_tamper_rejects_adoption(self) -> None:
        output = await self.invoke("decoded-primary")
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.final_decoded_manifest)
        child = next(entry for entry in manifest.entries if entry.kind == "file")
        self.blob(child.sha256).chmod(0o644)
        self.blob(child.sha256).write_bytes(b"tampered decoded child")
        with mock.patch.object(activities, "execute", side_effect=AssertionError("execute")):
            with self.assertRaises((OSError, ValueError)):
                await self.invoke("decoded-adoption")

    async def test_source_manifest_child_tamper_rejects_adoption(self) -> None:
        output = await self.invoke("source-primary")
        receipt = self.receipt(output)
        manifest = load_decoded_tree(runtime().store, receipt.source_manifest)
        child = next(entry for entry in manifest.entries if entry.kind == "file")
        self.blob(child.sha256).chmod(0o644)
        self.blob(child.sha256).write_bytes(b"tampered source child")
        with mock.patch.object(activities, "execute", side_effect=AssertionError("execute")):
            with self.assertRaises((OSError, ValueError)):
                await self.invoke("source-adoption")

    async def test_report_result_tamper_rejects_adoption(self) -> None:
        output = await self.invoke("report-primary")
        receipt = self.receipt(output)
        changed_result = replace(receipt.assertion_results[0], detail="tampered detail")
        forged = replace(
            receipt,
            assertion_results=(changed_result, *receipt.assertion_results[1:]),
        )
        forged_output = runtime().store.put_bytes(
            kind=output.kind,
            data=canonical_json(forged).encode("utf-8"),
            producer_operation_id=self.key,
            input_hashes=forged.receipt_input_hashes,
        )
        with runtime().ledger._connection() as connection:
            connection.execute(
                "UPDATE operation_claims SET output_json = ? WHERE operation_key = ?",
                (canonical_json(forged_output), self.key),
            )
        with mock.patch.object(activities, "execute", side_effect=AssertionError("execute")):
            with self.assertRaisesRegex(ValueError, "report"):
                await self.invoke("report-adoption")

    async def test_repeated_decoder_cancellation_waits_for_reap_then_quarantines(self) -> None:
        self.process = FakeDecodeProcess(self.decoded_source, block=True)
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def slow_reap() -> tuple[bytes, bytes]:
            self.process.started.set()
            await self.process.release.wait()
            cleanup_started.set()
            await cleanup_release.wait()
            self.process.reaped = True
            return b"", b""

        self.process.communicate = slow_reap  # type: ignore[method-assign]
        task = asyncio.create_task(self.invoke("decoder-cancel"))
        await asyncio.wait_for(self.process.started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.assertTrue(self.process.killed)
        self.assertFalse(self.process.reaped)
        cleanup_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.process.reaped)
        self.assert_quarantined()

    async def test_verification_cancellation_waits_for_thread_then_quarantines(self) -> None:
        started = threading.Event()
        release = threading.Event()
        real_verify = activities.verify_apk
        calls = 0

        def blocked_verify(*args: Any, **kwargs: Any) -> VerificationReport:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                if not release.wait(5):
                    raise RuntimeError("verification release timeout")
            return real_verify(*args, **kwargs)

        with mock.patch.object(activities, "verify_apk", side_effect=blocked_verify):
            task = asyncio.create_task(self.invoke("verification-cancel"))
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
        self.assert_quarantined()

    async def test_missing_executable_releases_pending_claim_before_workspace(self) -> None:
        configure_runtime(
            self.state,
            attempts_root=self.attempts,
            source_root=self.source_root,
            executor_paths={},
            launcher=self.launcher,
        )
        with self.assertRaisesRegex(ValueError, "No runtime executable"):
            await self.invoke("missing-executable")
        with runtime().ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT owner_token, status FROM operation_claims WHERE operation_key = ?",
                    (self.key,),
                ).fetchone(),
                ("", "pending"),
            )
        self.assertFalse(self.attempts.exists())

    async def test_after_workspace_failure_quarantines(self) -> None:
        self.process = FakeDecodeProcess(
            self.decoded_source,
            mutate=lambda cwd: (cwd / "output").symlink_to(
                self.decoded_source, target_is_directory=True
            ),
        )
        with self.assertRaises((OSError, ValueError)):
            await self.invoke("after-workspace")
        self.assert_quarantined()
        self.assertTrue((self.attempts / self.key / digest(b"after-workspace")).exists())

    async def test_concrete_ledger_calls_resist_instance_shadowing(self) -> None:
        shadows = {
            name: mock.Mock(side_effect=AssertionError(f"{name} instance shadow called"))
            for name in (
                "require_admitted_replay_verification_grant_v1",
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
        self.assertEqual(Ledger.operation_status(runtime().ledger, self.key), "completed")
        self.receipt(output)
        for shadow in shadows.values():
            shadow.assert_not_called()

    def test_temporal_metadata_exists_but_worker_and_workflow_exclude_activity(self) -> None:
        definition = activity._Definition.from_callable(  # type: ignore[attr-defined]
            replay_verify_final_apk_checkpoint_activity
        )
        self.assertEqual(definition.name, "replay_verify_final_apk_checkpoint_activity")
        root = Path(__file__).resolve().parents[1]
        for relative in ("src/dfinsta_pipeline/worker.py", "src/dfinsta_pipeline/workflow.py"):
            self.assertNotIn(
                "replay_verify_final_apk_checkpoint_activity",
                (root / relative).read_text(encoding="utf-8"),
            )
        self.assertEqual(
            tuple(inspect.signature(replay_verify_final_apk_checkpoint_activity).parameters),
            ("candidate",),
        )


if __name__ == "__main__":
    unittest.main()
