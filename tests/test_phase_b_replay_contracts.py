import dataclasses
import hashlib
import unittest
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from dfinsta_pipeline.contracts import (
    ArtifactRef,
    GateDecision,
    RunSpec,
    canonical_json,
    canonical_sha256,
)
from dfinsta_pipeline.executor import ExecutorCapability
from dfinsta_pipeline.port_contracts import (
    ApktoolFullRebuildBackend,
    HookIntent,
    IntentResolution,
    IntentSpecV2,
    ResolutionSpecV3,
    SourceFile,
    TargetIdentity,
)
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplay,
    AdmittedReplayV2,
    AdmittedReplayV3,
    CapabilityBinding,
    FrameworkArtifact,
    FrameworkRequirement,
    GatePreparedEnvelopeV2,
    ReplayRequest,
    ReplayRequestV2,
    ReplayDecodedTreeReceiptV1,
    ReplayDecodedTreeReceiptV2,
    ReplayFrameworkCacheReceiptV1,
    ReplayFrameworkInstallationV1,
    ReplayRunSpecV1,
    ReplayRunSpecV2,
    RoleExecutionPlan,
    SourceManifestV1,
    ToolArtifact,
    ToolchainProfile,
    ToolchainProfileV2,
    ToolchainProfileV3,
    ToolRequirement,
    admit_replay,
    admit_replay_v2,
    admit_replay_v3,
)


def artifact_ref(kind: str, payload: bytes, *, inputs: tuple[str, ...] = ()) -> ArtifactRef:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactRef(
        1,
        kind,
        digest,
        len(payload),
        f"cas://sha256/{digest}",
        "fixture-producer",
        inputs,
    )


def json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def synthetic_intent() -> IntentSpecV2:
    return IntentSpecV2(
        2,
        "policy-replay-1",
        (
            HookIntent(
                "retain-hook",
                "feature",
                "retain",
                "Synthetic replay intent",
                ("smali_edit",),
                (),
                (),
            ),
        ),
    )


def synthetic_profile(with_framework: bool = False) -> ToolchainProfile:
    bindings = [CapabilityBinding("build", "a" * 64), CapabilityBinding("decode", "b" * 64)]
    frameworks: tuple[FrameworkRequirement, ...] = ()
    if with_framework:
        bindings.append(CapabilityBinding("install_framework", "c" * 64))
        frameworks = (FrameworkRequirement(7, "1" * 64),)
    return ToolchainProfile(
        1,
        "synthetic-full",
        "apktool_full_rebuild",
        tuple(bindings),
        frameworks,
    )


@dataclass(frozen=True)
class Fixture:
    run_spec: ReplayRunSpecV1
    request: ReplayRequest
    decision: GateDecision
    payloads: dict[str, bytes]

    def resolve(self, artifact: ArtifactRef) -> bytes:
        return self.payloads[canonical_sha256(artifact)]

    def decision_is_recorded(self, decision: GateDecision) -> bool:
        return canonical_sha256(decision) == canonical_sha256(self.decision)


def fixture(
    with_framework: bool = False,
    *,
    framework_package_ids: tuple[int, ...] | None = None,
    framework_payload_suffix: bytes = b"",
) -> Fixture:
    if framework_package_ids is not None:
        with_framework = bool(framework_package_ids)
    package_ids = framework_package_ids or ((7,) if with_framework else ())
    intent = synthetic_intent()
    profile = synthetic_profile(with_framework)
    source_manifest = SourceManifestV1(
        (
            SourceFile("code/Preference.smali", "d" * 64),
            SourceFile("code/hooks.smali", "e" * 64),
        )
    )
    stock_payload = b"synthetic stock APK"
    stock_ref = artifact_ref("stock-apk", stock_payload)

    framework_artifacts: tuple[FrameworkArtifact, ...] = ()
    framework_payloads: list[tuple[ArtifactRef, bytes]] = []
    if with_framework:
        framework_payloads = [
            (
                artifact_ref(
                    "framework-apk",
                    f"synthetic framework APK {package_id}".encode()
                    + framework_payload_suffix,
                ),
                f"synthetic framework APK {package_id}".encode()
                + framework_payload_suffix,
            )
            for package_id in package_ids
        ]
        profile = replace(
            profile,
            frameworks=tuple(
                FrameworkRequirement(package_id, framework_ref.sha256)
                for package_id, (framework_ref, _) in zip(
                    package_ids, framework_payloads, strict=True
                )
            ),
        )
        framework_artifacts = tuple(
            FrameworkArtifact(package_id, framework_ref)
            for package_id, (framework_ref, _) in zip(
                package_ids, framework_payloads, strict=True
            )
        )

    resolution = ResolutionSpecV3(
        3,
        intent.sha256,
        TargetIdentity("example.app", "synthetic", 7, stock_ref.sha256, "monolithic"),
        source_manifest.sha256,
        ApktoolFullRebuildBackend(
            "apktool_full_rebuild", profile.profile_id, ("classes.dex",)
        ),
        (IntentResolution("retain-hook", "implemented", None),),
        (),
        (),
    )
    intent_payload = json_bytes(intent)
    resolution_payload = json_bytes(resolution)
    source_payload = json_bytes(source_manifest.records)
    profile_payload = json_bytes(profile)
    intent_ref = artifact_ref("intent-spec", intent_payload, inputs=(stock_ref.sha256,))
    resolution_ref = artifact_ref(
        "resolution-spec", resolution_payload, inputs=(intent_ref.sha256,)
    )
    source_ref = artifact_ref("source-manifest-v1", source_payload)
    profile_ref = artifact_ref("toolchain-profile", profile_payload)

    capabilities = tuple(
        sorted(binding.executor_capability_sha256 for binding in profile.capability_bindings)
    )
    run_spec = ReplayRunSpecV1(
        1,
        "replay-run",
        stock_ref.sha256,
        intent.sha256,
        resolution.sha256,
        source_manifest.sha256,
        profile.sha256,
        capabilities,
        "replay-gate",
        "8" * 64,
        "9" * 64,
        "operator-1",
        intent.policy_revision,
        "monolithic",
    )
    request = ReplayRequest(
        1,
        run_spec.sha256,
        stock_ref,
        intent_ref,
        resolution_ref,
        source_ref,
        profile_ref,
        framework_artifacts,
    )
    decision = GateDecision(
        1,
        "decision-1",
        "decision-attempt-1",
        run_spec.allowed_actor,
        run_spec.run_id,
        run_spec.gate_id,
        run_spec.sha256,
        run_spec.gate_admission_sha256,
        run_spec.gate_prepared_sha256,
        run_spec.policy_revision,
        "approve",
        "Approved synthetic replay",
        "2026-01-01T00:00:00Z",
    )
    payloads = {
        canonical_sha256(stock_ref): stock_payload,
        canonical_sha256(intent_ref): intent_payload,
        canonical_sha256(resolution_ref): resolution_payload,
        canonical_sha256(source_ref): source_payload,
        canonical_sha256(profile_ref): profile_payload,
    }
    payloads.update(
        (canonical_sha256(framework_ref), payload)
        for framework_ref, payload in framework_payloads
    )
    return Fixture(run_spec, request, decision, payloads)


@dataclass(frozen=True)
class FixtureV2:
    run_spec: ReplayRunSpecV2
    request: ReplayRequestV2
    decision: GateDecision
    payloads: dict[str, bytes]

    def resolve(self, artifact: ArtifactRef) -> bytes:
        return self.payloads[canonical_sha256(artifact)]

    def decision_is_recorded(self, decision: GateDecision) -> bool:
        return canonical_sha256(decision) == canonical_sha256(self.decision)


def fixture_v2(
    with_framework: bool = False,
    *,
    tool_payload_suffix: bytes = b"",
    framework_package_ids: tuple[int, ...] | None = None,
    framework_payload_suffix: bytes = b"",
) -> FixtureV2:
    base = fixture(
        with_framework,
        framework_package_ids=framework_package_ids,
        framework_payload_suffix=framework_payload_suffix,
    )
    admitted = admit(base)
    tool_payloads = (
        (
            "bundle-engine",
            "native-binary",
            b"synthetic bundle engine" + tool_payload_suffix,
        ),
        (
            "resource-compiler",
            "java-archive",
            b"synthetic resource compiler" + tool_payload_suffix,
        ),
    )
    tool_artifacts = tuple(
        ToolArtifact(tool_id, artifact_ref(kind, payload))
        for tool_id, kind, payload in tool_payloads
    )
    tool_roles = {
        "bundle-engine": ("decode",),
        "resource-compiler": (
            ("build", "install_framework") if with_framework else ("build",)
        ),
    }
    profile = ToolchainProfileV2(
        2,
        admitted.profile.profile_id,
        admitted.profile.backend_kind,
        admitted.profile.capability_bindings,
        admitted.profile.frameworks,
        tuple(
            ToolRequirement(
                tool.tool_id,
                tool.artifact.kind,
                tool.artifact.sha256,
                tool_roles[tool.tool_id],
            )
            for tool in tool_artifacts
        ),
    )
    profile_payload = json_bytes(profile)
    profile_ref = artifact_ref("toolchain-profile", profile_payload)
    gate_prepared = GatePreparedEnvelopeV2(
        2,
        base.request.stock_apk,
        base.request.intent,
        base.request.resolution,
        base.request.source_manifest,
        profile_ref,
        base.request.frameworks,
        tool_artifacts,
    )
    gate_prepared_payload = json_bytes(gate_prepared)
    gate_prepared_inputs = (
        gate_prepared.stock_apk.sha256,
        gate_prepared.intent.sha256,
        gate_prepared.resolution.sha256,
        gate_prepared.source_manifest.sha256,
        gate_prepared.toolchain_profile.sha256,
        *(framework.artifact.sha256 for framework in gate_prepared.frameworks),
        *(tool.artifact.sha256 for tool in gate_prepared.tools),
    )
    gate_prepared_ref = artifact_ref(
        "replay-gate-prepared-v2",
        gate_prepared_payload,
        inputs=gate_prepared_inputs,
    )
    run_spec = ReplayRunSpecV2(
        2,
        base.run_spec.run_id,
        base.run_spec.subject_sha256,
        base.run_spec.intent_sha256,
        base.run_spec.resolution_sha256,
        base.run_spec.source_manifest_sha256,
        profile.sha256,
        base.run_spec.executor_capability_sha256s,
        base.run_spec.gate_id,
        base.run_spec.gate_admission_sha256,
        gate_prepared_ref.sha256,
        canonical_sha256(gate_prepared_ref),
        base.run_spec.allowed_actor,
        base.run_spec.policy_revision,
        base.run_spec.apk_composition,
    )
    request = ReplayRequestV2(
        2,
        run_spec.sha256,
        gate_prepared_ref,
        base.request.stock_apk,
        base.request.intent,
        base.request.resolution,
        base.request.source_manifest,
        profile_ref,
        base.request.frameworks,
        tool_artifacts,
    )
    decision = replace(
        base.decision,
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_prepared_ref.sha256,
    )
    payloads = {
        key: value
        for key, value in base.payloads.items()
        if key != canonical_sha256(base.request.toolchain_profile)
    }
    payloads[canonical_sha256(profile_ref)] = profile_payload
    payloads[canonical_sha256(gate_prepared_ref)] = gate_prepared_payload
    payloads.update(
        (canonical_sha256(tool.artifact), payload)
        for tool, (_, _, payload) in zip(tool_artifacts, tool_payloads, strict=True)
    )
    return FixtureV2(run_spec, request, decision, payloads)


def profile_v3(
    with_framework: bool = False,
    *,
    framework_package_ids: tuple[int, ...] | None = None,
    framework_payload_suffix: bytes = b"",
) -> ToolchainProfileV3:
    profile = admit_v2(
        fixture_v2(
            with_framework,
            framework_package_ids=framework_package_ids,
            framework_payload_suffix=framework_payload_suffix,
        )
    ).profile
    plans = [
        RoleExecutionPlan(
            "build",
            "resource-compiler",
            (
                ("decoded_tree", "decoded_tree"),
                ("framework_dir", "framework_dir"),
                ("intermediate_apk", "intermediate_apk"),
                ("tool", "tool"),
            ),
            300,
        ),
        RoleExecutionPlan(
            "decode",
            "bundle-engine",
            (
                ("decoded_tree", "decoded_tree"),
                ("framework_dir", "framework_dir"),
                ("input_apk", "input_apk"),
                ("tool", "tool"),
            ),
            300,
        ),
    ]
    if with_framework:
        plans.append(
            RoleExecutionPlan(
                "install_framework",
                "resource-compiler",
                (
                    ("framework_apk", "framework_apk"),
                    ("framework_dir", "framework_dir"),
                    ("tool", "tool"),
                ),
                300,
            )
        )
    return ToolchainProfileV3(
        3,
        profile.profile_id,
        profile.backend_kind,
        profile.capability_bindings,
        profile.frameworks,
        profile.tools,
        tuple(sorted(plans, key=lambda plan: plan.role)),
    )


def capability_for_plan(
    profile: ToolchainProfileV3,
    role: str,
    *,
    executable_sha256: str = "f" * 64,
) -> ExecutorCapability:
    plan = profile.plan(role)
    names = tuple(name for name, _ in plan.arguments)
    return ExecutorCapability(
        1,
        f"{role}-capability",
        executable_sha256,
        tuple(f"{{{name}}}" for name in names),
        names,
        ("framework-apk",) if role == "install_framework" else ("stock-apk",),
        "framework-cache" if role == "install_framework" else "synthetic-output",
        (),
        (),
        ("framework",) if role == "install_framework" else ("output",),
    )


@dataclass(frozen=True)
class FixtureV3:
    run_spec: ReplayRunSpecV2
    request: ReplayRequestV2
    decision: GateDecision
    payloads: dict[str, bytes]
    capabilities: tuple[ExecutorCapability, ...]

    def resolve(self, artifact: ArtifactRef) -> bytes:
        return self.payloads[canonical_sha256(artifact)]

    def resolve_capability(self, capability_sha256: str) -> ExecutorCapability:
        return {
            capability.canonical_identity: capability
            for capability in self.capabilities
        }[capability_sha256]

    def decision_is_recorded(self, decision: GateDecision) -> bool:
        return canonical_sha256(decision) == canonical_sha256(self.decision)


def bind_v3_fixture(
    base: FixtureV2,
    profile: ToolchainProfileV3,
    capabilities: tuple[ExecutorCapability, ...],
) -> FixtureV3:
    profile_payload = json_bytes(profile)
    profile_ref = artifact_ref("toolchain-profile", profile_payload)
    gate_prepared = replace(
        GatePreparedEnvelopeV2.from_dict(
            _json_dict(base.resolve(base.request.gate_prepared))
        ),
        toolchain_profile=profile_ref,
    )
    gate_payload = json_bytes(gate_prepared)
    gate_inputs = (
        gate_prepared.stock_apk.sha256,
        gate_prepared.intent.sha256,
        gate_prepared.resolution.sha256,
        gate_prepared.source_manifest.sha256,
        gate_prepared.toolchain_profile.sha256,
        *(framework.artifact.sha256 for framework in gate_prepared.frameworks),
        *(tool.artifact.sha256 for tool in gate_prepared.tools),
    )
    gate_ref = artifact_ref(
        "replay-gate-prepared-v2", gate_payload, inputs=gate_inputs
    )
    run_spec = replace(
        base.run_spec,
        toolchain_profile_sha256=profile.sha256,
        executor_capability_sha256s=tuple(
            sorted(capability.canonical_identity for capability in capabilities)
        ),
        gate_prepared_sha256=gate_ref.sha256,
        gate_prepared_ref_sha256=canonical_sha256(gate_ref),
    )
    request = replace(
        base.request,
        run_spec_sha256=run_spec.sha256,
        gate_prepared=gate_ref,
        toolchain_profile=profile_ref,
    )
    decision = replace(
        base.decision,
        subject_sha256=run_spec.sha256,
        prepared_sha256=gate_ref.sha256,
    )
    payloads = dict(base.payloads)
    payloads.pop(canonical_sha256(base.request.toolchain_profile))
    payloads.pop(canonical_sha256(base.request.gate_prepared))
    payloads[canonical_sha256(profile_ref)] = profile_payload
    payloads[canonical_sha256(gate_ref)] = gate_payload
    return FixtureV3(run_spec, request, decision, payloads, capabilities)


def _json_dict(payload: bytes) -> dict[str, Any]:
    import json

    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture payload must be a JSON object")
    return value


def fixture_v3(
    with_framework: bool = False,
    *,
    framework_package_ids: tuple[int, ...] | None = None,
    framework_payload_suffix: bytes = b"",
) -> FixtureV3:
    base = fixture_v2(
        with_framework,
        framework_package_ids=framework_package_ids,
        framework_payload_suffix=framework_payload_suffix,
    )
    profile = profile_v3(
        with_framework,
        framework_package_ids=framework_package_ids,
        framework_payload_suffix=framework_payload_suffix,
    )
    capabilities = tuple(
        capability_for_plan(profile, binding.role)
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


def resolve_from(payloads: dict[str, bytes]) -> Callable[[ArtifactRef], bytes]:
    return lambda artifact: payloads[canonical_sha256(artifact)]


def admit(
    case: Fixture,
    *,
    run_spec: ReplayRunSpecV1 | None = None,
    request: ReplayRequest | None = None,
    decision: GateDecision | None = None,
    decision_is_recorded: Callable[[GateDecision], bool] | None = None,
    artifact_resolver: Callable[[ArtifactRef], bytes] | None = None,
) -> AdmittedReplay:
    return admit_replay(
        run_spec or case.run_spec,
        request or case.request,
        decision or case.decision,
        decision_is_recorded or case.decision_is_recorded,
        artifact_resolver or case.resolve,
    )


def admit_v2(
    case: FixtureV2,
    *,
    run_spec: ReplayRunSpecV2 | None = None,
    request: ReplayRequestV2 | None = None,
    decision: GateDecision | None = None,
    decision_is_recorded: Callable[[GateDecision], bool] | None = None,
    artifact_resolver: Callable[[ArtifactRef], bytes] | None = None,
) -> AdmittedReplayV2:
    return admit_replay_v2(
        run_spec or case.run_spec,
        request or case.request,
        decision or case.decision,
        decision_is_recorded or case.decision_is_recorded,
        artifact_resolver or case.resolve,
    )


def admit_v3(
    case: FixtureV3,
    *,
    run_spec: ReplayRunSpecV2 | None = None,
    request: ReplayRequestV2 | None = None,
    decision: GateDecision | None = None,
    decision_is_recorded: Callable[[GateDecision], bool] | None = None,
    artifact_resolver: Callable[[ArtifactRef], bytes] | None = None,
    capability_resolver: Callable[[str], ExecutorCapability] | None = None,
) -> AdmittedReplayV3:
    return admit_replay_v3(
        run_spec or case.run_spec,
        request or case.request,
        decision or case.decision,
        decision_is_recorded or case.decision_is_recorded,
        artifact_resolver or case.resolve,
        capability_resolver or case.resolve_capability,
    )


def framework_contract_receipt() -> ReplayFrameworkCacheReceiptV1:
    operation_key = "a" * 64
    installations = tuple(
        ReplayFrameworkInstallationV1(
            package_id,
            artifact_ref(
                "framework-apk", f"framework contract {package_id}".encode()
            ),
            str(package_id % 10) * 64,
        )
        for package_id in (2, 10)
    )
    execution_inputs = (
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        *(canonical_sha256(item) for item in installations),
    )
    manifest = ArtifactRef(
        1,
        "decoded-tree-manifest-v1",
        "1" * 64,
        11,
        f"cas://sha256/{'1' * 64}",
        operation_key,
        execution_inputs,
    )
    return ReplayFrameworkCacheReceiptV1(
        1,
        "b" * 64,
        "synthetic-full",
        "c" * 64,
        "install_framework",
        "d" * 64,
        "e" * 64,
        "f" * 64,
        installations,
        manifest,
        "2" * 64,
        operation_key,
        True,
    )


def decoded_v2_contract_receipt() -> ReplayDecodedTreeReceiptV2:
    framework = framework_contract_receipt()
    completed = ArtifactRef(
        1,
        "replay-framework-cache-receipt-v1",
        framework.sha256,
        len(json_bytes(framework)),
        f"cas://sha256/{framework.sha256}",
        framework.operation_key,
        framework.receipt_input_hashes,
    )
    input_apk = artifact_ref("stock-apk", b"contract stock")
    operation_key = "3" * 64
    fixed = (
        "4" * 64,
        canonical_sha256(input_apk),
        "5" * 64,
        "6" * 64,
        "7" * 64,
        "8" * 64,
        "9" * 64,
        canonical_sha256(completed),
        canonical_sha256(framework.framework_cache_manifest),
        framework.framework_cache_semantic_sha256,
    )
    manifest = ArtifactRef(
        1,
        "decoded-tree-manifest-v1",
        "0" * 64,
        9,
        f"cas://sha256/{'0' * 64}",
        operation_key,
        fixed,
    )
    return ReplayDecodedTreeReceiptV2(
        2,
        "stock_input",
        "4" * 64,
        input_apk,
        "synthetic-full",
        "5" * 64,
        "decode",
        "6" * 64,
        "7" * 64,
        "8" * 64,
        "9" * 64,
        completed,
        framework.framework_cache_manifest,
        framework.framework_cache_semantic_sha256,
        manifest,
        "a" * 64,
        operation_key,
        True,
    )


class ReplayContractTests(unittest.TestCase):
    def test_v1_identity_is_pinned(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ToolchainProfile)),
            (
                "schema_version",
                "profile_id",
                "backend_kind",
                "capability_bindings",
                "frameworks",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ReplayRequest)),
            (
                "schema_version",
                "run_spec_sha256",
                "stock_apk",
                "intent",
                "resolution",
                "source_manifest",
                "toolchain_profile",
                "frameworks",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(AdmittedReplay)),
            (
                "schema_version",
                "run_spec",
                "request",
                "decision",
                "intent",
                "resolution",
                "source_manifest",
                "profile",
            ),
        )
        self.assertEqual(
            admit(fixture()).sha256,
            "bc8e7fa6c90a6a9998b74a035ef025dc4447ed2d75eddee52600e33726890a20",
        )
        self.assertEqual(
            admit_v2(fixture_v2()).sha256,
            "2811658b7a502ac2665f8048fed4c450b07d967ed7abd5369d07da22332384b3",
        )

    def test_framework_contracts_are_strict_ordered_and_lineage_bound(self) -> None:
        receipt = framework_contract_receipt()
        value = asdict(receipt)
        self.assertEqual(
            ReplayFrameworkInstallationV1.from_dict(value["installations"][0]),
            receipt.installations[0],
        )
        self.assertEqual(ReplayFrameworkCacheReceiptV1.from_dict(value), receipt)
        self.assertEqual(
            tuple(item.package_id for item in receipt.installations), (2, 10)
        )
        self.assertEqual(
            receipt.framework_cache_manifest.input_hashes,
            receipt.execution_input_hashes,
        )
        self.assertEqual(
            receipt.receipt_input_hashes,
            (
                *receipt.execution_input_hashes,
                canonical_sha256(receipt.framework_cache_manifest),
                receipt.framework_cache_semantic_sha256,
            ),
        )

        installation = value["installations"][0]
        installation_mutations = (
            {key: item for key, item in installation.items() if key != "package_id"},
            {**installation, "unknown": 1},
            {**installation, "package_id": True},
            {
                **installation,
                "framework_apk": {**installation["framework_apk"], "kind": "stock-apk"},
            },
            {**installation, "execution_request_sha256": "A" * 64},
        )
        for mutation in installation_mutations:
            with self.subTest(installation=mutation), self.assertRaises(
                (TypeError, ValueError)
            ):
                ReplayFrameworkInstallationV1.from_dict(mutation)

        receipt_mutations = (
            {key: item for key, item in value.items() if key != "success"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
            {**value, "role": "decode"},
            {**value, "installations": list(reversed(value["installations"]))},
            {**value, "installations": [value["installations"][0]] * 2},
            {
                **value,
                "framework_cache_manifest": {
                    **value["framework_cache_manifest"],
                    "kind": "other-manifest",
                },
            },
            {
                **value,
                "framework_cache_manifest": {
                    **value["framework_cache_manifest"],
                    "producer_operation_id": "0" * 64,
                },
            },
            {
                **value,
                "framework_cache_manifest": {
                    **value["framework_cache_manifest"],
                    "input_hashes": value["framework_cache_manifest"]["input_hashes"][:-1],
                },
            },
        )
        for mutation in receipt_mutations:
            with self.subTest(receipt=mutation), self.assertRaises(
                (TypeError, ValueError)
            ):
                ReplayFrameworkCacheReceiptV1.from_dict(mutation)

    def test_decoded_v2_contract_is_strict_and_binds_framework_lineage(self) -> None:
        receipt = decoded_v2_contract_receipt()
        value = asdict(receipt)
        self.assertEqual(ReplayDecodedTreeReceiptV2.from_dict(value), receipt)
        self.assertEqual(
            receipt.execution_input_hashes[-3:],
            (
                canonical_sha256(receipt.completed_framework_cache_receipt),
                canonical_sha256(receipt.framework_cache_manifest),
                receipt.framework_cache_semantic_sha256,
            ),
        )
        mutations = (
            {key: item for key, item in value.items() if key != "success"},
            {**value, "unknown": 1},
            {**value, "schema_version": True},
            {**value, "success": 1},
            {**value, "role": "build"},
            {
                **value,
                "completed_framework_cache_receipt": {
                    **value["completed_framework_cache_receipt"],
                    "kind": "other",
                },
            },
            {
                **value,
                "framework_cache_manifest": {
                    **value["framework_cache_manifest"],
                    "producer_operation_id": "f" * 64,
                },
            },
            {
                **value,
                "decoded_tree_manifest": {
                    **value["decoded_tree_manifest"],
                    "kind": "other",
                },
            },
            {
                **value,
                "decoded_tree_manifest": {
                    **value["decoded_tree_manifest"],
                    "input_hashes": value["decoded_tree_manifest"]["input_hashes"][:-1],
                },
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(
                (TypeError, ValueError)
            ):
                ReplayDecodedTreeReceiptV2.from_dict(mutation)

        with self.assertRaises((TypeError, ValueError)):
            ReplayDecodedTreeReceiptV1.from_dict(value)
        with self.assertRaises((TypeError, ValueError)):
            ReplayDecodedTreeReceiptV2.from_dict(
                asdict(
                    ReplayDecodedTreeReceiptV1(
                        1,
                        receipt.decoded_apk_role,
                        receipt.admitted_replay_sha256,
                        receipt.input_apk,
                        receipt.toolchain_profile_id,
                        receipt.toolchain_profile_sha256,
                        receipt.role,
                        receipt.execution_plan_sha256,
                        receipt.executor_capability_sha256,
                        receipt.tool_artifact_sha256,
                        receipt.execution_request_sha256,
                        replace(
                            receipt.decoded_tree_manifest,
                            input_hashes=receipt.execution_input_hashes[:7],
                        ),
                        receipt.decoded_tree_semantic_sha256,
                        receipt.operation_key,
                        True,
                    )
                )
            )

    def test_framework_fixture_numeric_install_and_lexical_manifest_orders_are_distinct(self) -> None:
        admitted = admit_v3(
            fixture_v3(True, framework_package_ids=(2, 10))
        )
        self.assertEqual(
            tuple(item.package_id for item in admitted.request.frameworks), (2, 10)
        )
        self.assertEqual(
            admitted.capability("install_framework").input_kinds,
            ("framework-apk",),
        )
        self.assertEqual(
            admitted.capability("install_framework").output_kind,
            "framework-cache",
        )
        self.assertEqual(
            admitted.capability("install_framework").allowed_mutation_paths,
            ("framework",),
        )

    def test_replay_run_gate_fields_are_strict_and_phase_a_is_unchanged(self) -> None:
        run_spec = fixture().run_spec
        self.assertEqual(ReplayRunSpecV1.from_dict(asdict(run_spec)), run_spec)
        data = asdict(run_spec)
        invalid = (
            {**data, "schema_version": True},
            {**data, "gate_id": "bad gate"},
            {**data, "gate_admission_sha256": "A" * 64},
            {**data, "gate_prepared_sha256": "short"},
            {**data, "allowed_actor": "bad actor"},
            {**data, "executor_capability_sha256s": []},
            {**data, "executor_capability_sha256s": ["b" * 64, "a" * 64]},
        )
        for mutation in invalid:
            with self.subTest(mutation=mutation):
                with self.assertRaises((TypeError, ValueError)):
                    ReplayRunSpecV1.from_dict(mutation)

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(RunSpec)),
            (
                "schema_version",
                "run_id",
                "subject_sha256",
                "intent_sha256",
                "resolution_sha256",
                "executor_capability_sha256",
                "policy_revision",
                "allowed_actor",
                "gate_timeout_seconds",
                "apk_composition",
                "crash_after_effect",
                "apply_delay_seconds",
            ),
        )

    def test_profile_manifest_framework_and_request_contracts(self) -> None:
        profile = synthetic_profile()
        self.assertEqual(
            profile.sha256,
            "7a7302ab9c41cec2230d791af5848d0eaf470b64c4e425a847cd4d9f262ef04b",
        )
        self.assertEqual(ToolchainProfile.from_dict(asdict(profile)), profile)

        records = [
            {"relative_path": "a/file.smali", "sha256": "1" * 64},
            {"relative_path": "b/file.smali", "sha256": "2" * 64},
        ]
        manifest = SourceManifestV1.from_json_value(records)
        self.assertEqual(manifest.sha256, canonical_sha256(manifest.records))
        self.assertNotEqual(manifest.sha256, canonical_sha256(manifest))

        case = fixture(True)
        self.assertEqual(case.request.source_manifest.kind, "source-manifest-v1")
        self.assertEqual(ReplayRequest.from_dict(asdict(case.request)), case.request)
        self.assertEqual(
            FrameworkArtifact.from_dict(asdict(case.request.frameworks[0])),
            case.request.frameworks[0],
        )
        framework_ref = case.request.frameworks[0].artifact
        for package_id in (True, 0, 256):
            with self.subTest(package_id=package_id):
                with self.assertRaises((TypeError, ValueError)):
                    FrameworkRequirement(package_id, framework_ref.sha256)
                with self.assertRaises((TypeError, ValueError)):
                    FrameworkArtifact(package_id, framework_ref)

    def test_admits_no_framework_and_framework_replays(self) -> None:
        for with_framework in (False, True):
            case = fixture(with_framework)
            admitted = admit(case)
            with self.subTest(with_framework=with_framework):
                self.assertEqual(admitted.run_spec, case.run_spec)
                self.assertEqual(admitted.request, case.request)
                self.assertEqual(admitted.decision, case.decision)
                self.assertEqual(admitted.run_spec_sha256, case.run_spec.sha256)
                self.assertEqual(admitted.replay_request_sha256, case.request.sha256)
                self.assertEqual(admitted.decision_sha256, canonical_sha256(case.decision))
                self.assertEqual(admitted.capability_bindings, admitted.profile.capability_bindings)
                self.assertEqual(
                    admitted.install_framework_executor_capability_sha256,
                    "c" * 64 if with_framework else None,
                )
                self.assertEqual(AdmittedReplay.from_dict(asdict(admitted)), admitted)

    def test_gate_decision_tuple_and_approval_are_exact(self) -> None:
        case = fixture()
        mutations: tuple[dict[str, Any], ...] = (
            {"decision": "reject"},
            {"decision": "defer"},
            {"actor": "operator-2"},
            {"run_id": "other-run"},
            {"gate_id": "other-gate"},
            {"subject_sha256": "0" * 64},
            {"admission_sha256": "0" * 64},
            {"prepared_sha256": "0" * 64},
            {"policy_revision": "other-policy"},
        )
        for changes in mutations:
            decision = replace(case.decision, **changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    admit(case, decision=decision, decision_is_recorded=lambda _: True)

        changed_hash = replace(case.decision, rationale="Substituted approval")
        with self.assertRaises(ValueError):
            admit(case, decision=changed_hash)

    def test_decision_recording_predicate_is_trusted_and_strict(self) -> None:
        case = fixture()
        with self.assertRaises(ValueError):
            admit(case, decision_is_recorded=lambda _: False)
        with self.assertRaises(TypeError):
            admit(case, decision_is_recorded=lambda _: 1)

        def raises(_: GateDecision) -> bool:
            raise RuntimeError("ledger unavailable")

        with self.assertRaises(ValueError):
            admit(case, decision_is_recorded=raises)

    def test_resolver_binds_exact_artifact_reference_lineage(self) -> None:
        case = fixture()
        substitutions = (
            replace(case.request.intent, producer_operation_id="other-producer"),
            replace(case.request.intent, input_hashes=("0" * 64,)),
        )
        for substituted in substitutions:
            with self.subTest(substituted=substituted):
                self.assertEqual(substituted.sha256, case.request.intent.sha256)
                with self.assertRaises(ValueError):
                    admit(case, request=replace(case.request, intent=substituted))

    def test_resolver_still_checks_bytes_hash_size_and_json(self) -> None:
        case = fixture()
        payloads = dict(case.payloads)
        payloads[canonical_sha256(case.request.stock_apk)] = b"tampered"
        with self.assertRaises(ValueError):
            admit(case, artifact_resolver=resolve_from(payloads))

        wrong_size = replace(case.request.intent, size=case.request.intent.size + 1)
        payloads = dict(case.payloads)
        payloads[canonical_sha256(wrong_size)] = case.resolve(case.request.intent)
        with self.assertRaises(ValueError):
            admit(
                case,
                request=replace(case.request, intent=wrong_size),
                artifact_resolver=resolve_from(payloads),
            )

        for payload in (b"\xff", b'{"schema_version":2,"schema_version":2}', b'{"x":NaN}'):
            with self.subTest(payload=payload):
                changed_ref = artifact_ref("intent-spec", payload)
                payloads = dict(case.payloads)
                payloads[canonical_sha256(changed_ref)] = payload
                with self.assertRaises((TypeError, ValueError)):
                    admit(
                        case,
                        request=replace(case.request, intent=changed_ref),
                        artifact_resolver=resolve_from(payloads),
                    )

    def test_admitted_relational_mutations_are_rejected(self) -> None:
        case = fixture()
        admitted = admit(case)
        changed_intent = replace(
            admitted.intent,
            hooks=(replace(admitted.intent.hooks[0], description="Changed"),),
        )
        changed_resolution = replace(
            admitted.resolution,
            target=replace(admitted.resolution.target, apk_sha256="0" * 64),
        )
        changed_manifest = SourceManifestV1(
            (*admitted.source_manifest.records, SourceFile("code/new.smali", "0" * 64))
        )
        changed_profile = replace(admitted.profile, profile_id="synthetic-other")
        changed_request = replace(admitted.request, run_spec_sha256="0" * 64)
        changed_decision = replace(admitted.decision, gate_id="other-gate")
        changed_run = replace(admitted.run_spec, gate_id="other-gate")
        mutations = (
            {"run_spec": changed_run},
            {"request": changed_request},
            {"decision": changed_decision},
            {"intent": changed_intent},
            {"resolution": changed_resolution},
            {"source_manifest": changed_manifest},
            {"profile": changed_profile},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaises((TypeError, ValueError)):
                    replace(admitted, **changes)

    def test_framework_pairs_capabilities_backend_and_installer_are_relational(self) -> None:
        case = fixture(True)
        admitted = admit(case)
        framework = admitted.request.frameworks[0]
        with self.assertRaises(ValueError):
            replace(
                admitted,
                request=replace(
                    admitted.request,
                    frameworks=(replace(framework, package_id=8),),
                ),
            )

        extra_capability = replace(
            admitted.run_spec,
            executor_capability_sha256s=(
                "0" * 64,
                *admitted.run_spec.executor_capability_sha256s,
            ),
        )
        with self.assertRaises(ValueError):
            replace(
                admitted,
                run_spec=extra_capability,
                request=replace(admitted.request, run_spec_sha256=extra_capability.sha256),
                decision=replace(admitted.decision, subject_sha256=extra_capability.sha256),
            )
        with self.assertRaises(ValueError):
            replace(
                admitted,
                profile=replace(admitted.profile, backend_kind="stock_dex_graft"),
            )

    def test_v2_roundtrips_and_hashes_include_tools(self) -> None:
        case = fixture_v2()
        admitted = admit_v2(case)
        tool = case.request.tools[0]
        requirement = admitted.profile.tools[0]
        self.assertEqual(ToolArtifact.from_dict(asdict(tool)), tool)
        self.assertEqual(ToolRequirement.from_dict(asdict(requirement)), requirement)
        self.assertEqual(requirement.requirement_sha256, canonical_sha256(requirement))
        self.assertEqual(ToolchainProfileV2.from_dict(asdict(admitted.profile)), admitted.profile)
        self.assertEqual(ReplayRequestV2.from_dict(asdict(case.request)), case.request)
        self.assertEqual(
            GatePreparedEnvelopeV2.from_dict(asdict(admitted.gate_prepared)),
            admitted.gate_prepared,
        )
        self.assertEqual(AdmittedReplayV2.from_dict(asdict(admitted)), admitted)
        self.assertEqual(admitted.direct_artifacts, case.request.direct_artifacts)
        self.assertEqual(admitted.toolchain_profile_sha256, admitted.profile.sha256)
        self.assertEqual(
            case.run_spec.gate_prepared_sha256,
            case.request.gate_prepared.sha256,
        )
        self.assertEqual(
            case.run_spec.gate_prepared_ref_sha256,
            canonical_sha256(case.request.gate_prepared),
        )
        self.assertEqual(case.decision.prepared_sha256, case.request.gate_prepared.sha256)
        self.assertNotEqual(
            admitted.profile.sha256,
            replace(
                admitted.profile,
                tools=(
                    replace(requirement, artifact_kind="portable-binary"),
                    *admitted.profile.tools[1:],
                ),
            ).sha256,
        )
        changed_tool = replace(tool, artifact=artifact_ref(tool.artifact.kind, b"changed tool"))
        self.assertNotEqual(
            case.request.sha256,
            replace(case.request, tools=(changed_tool, *case.request.tools[1:])).sha256,
        )

    def test_v2_admits_role_bound_tools_with_and_without_frameworks(self) -> None:
        for with_framework in (False, True):
            case = fixture_v2(with_framework)
            admitted = admit_v2(case)
            with self.subTest(with_framework=with_framework):
                self.assertEqual(
                    tuple(tool.tool_id for tool in admitted.profile.tools),
                    ("bundle-engine", "resource-compiler"),
                )
                self.assertEqual(
                    admitted.install_framework_executor_capability_sha256,
                    "c" * 64 if with_framework else None,
                )
                self.assertEqual(
                    tuple(tool.artifact for tool in admitted.request.tools),
                    admitted.direct_artifacts[-2:],
                )
                self.assertEqual(
                    admitted.profile.tool_for_role("decode").tool_id,
                    "bundle-engine",
                )
                self.assertEqual(
                    admitted.profile.tool_for_role("build").tool_id,
                    "resource-compiler",
                )

    def test_v2_tool_contracts_reject_bad_ids_duplicates_hashes_and_order(self) -> None:
        case = fixture_v2()
        admitted = admit_v2(case)
        first, second = admitted.profile.tools
        for constructor in (
            lambda: ToolRequirement("Uppercase", "native-binary", "1" * 64, ("decode",)),
            lambda: ToolRequirement("valid", "", "1" * 64, ("decode",)),
            lambda: ToolRequirement("valid", "native-binary", "1" * 64, ()),
            lambda: ToolArtifact("Uppercase", case.request.tools[0].artifact),
        ):
            with self.subTest(constructor=constructor):
                with self.assertRaises((TypeError, ValueError)):
                    constructor()
        with self.assertRaises(ValueError):
            replace(admitted.profile, tools=())
        with self.assertRaises(ValueError):
            replace(admitted.profile, tools=(second, first))
        with self.assertRaises(ValueError):
            replace(admitted.profile, tools=(first, first))
        with self.assertRaises(ValueError):
            replace(admitted.profile, tools=(first, replace(second, roles=("decode",))))
        with self.assertRaises(ValueError):
            replace(admitted.profile, tools=(first, replace(second, roles=("install_framework",))))
        with self.assertRaises(ValueError):
            replace(
                admitted.profile,
                tools=(
                    first,
                    replace(second, artifact_sha256=first.artifact_sha256),
                ),
            )
        with self.assertRaises(ValueError):
            replace(case.request, tools=tuple(reversed(case.request.tools)))
        with self.assertRaises(ValueError):
            replace(case.request, tools=(case.request.tools[0], case.request.tools[0]))

    def test_v3_execution_plans_are_exact_role_authority(self) -> None:
        profile = profile_v3(True)
        build, decode, install = profile.execution_plans
        self.assertEqual(RoleExecutionPlan.from_dict(asdict(build)), build)
        self.assertEqual(ToolchainProfileV3.from_dict(asdict(profile)), profile)
        self.assertEqual(profile.plan("build"), build)
        self.assertEqual(profile.plan("decode"), decode)
        self.assertEqual(profile.plan("install_framework"), install)
        with self.assertRaises(ValueError):
            replace(profile, execution_plans=(build, decode))
        with self.assertRaises(ValueError):
            replace(profile, execution_plans=(decode, build, install))
        with self.assertRaises(ValueError):
            replace(
                profile,
                execution_plans=(replace(build, tool_id="bundle-engine"), decode, install),
            )
        with self.assertRaises(ValueError):
            replace(
                profile,
                execution_plans=(
                    build,
                    decode,
                    replace(install, tool_id="bundle-engine"),
                ),
            )
        with self.assertRaises(ValueError):
            replace(
                build,
                arguments=tuple(
                    pair for pair in build.arguments if pair[1] != "intermediate_apk"
                ),
            )
        with self.assertRaises(ValueError):
            replace(build, arguments=(*build.arguments, ("extra", "tool")))
        with self.assertRaises(ValueError):
            replace(build, timeout_seconds=0)

    def test_v3_execution_plan_matches_resolved_capability_paths(self) -> None:
        profile = profile_v3()
        capabilities = {}
        for role in ("build", "decode"):
            plan = profile.plan(role)
            names = tuple(name for name, _ in plan.arguments)
            capabilities[role] = ExecutorCapability(
                1,
                f"{role}-capability",
                "f" * 64,
                tuple(f"{{{name}}}" for name in names),
                names,
                ("stock-apk",),
                "synthetic-output",
                (),
                (),
                ("output",),
            )
        profile = replace(
            profile,
            capability_bindings=tuple(
                CapabilityBinding(role, capabilities[role].canonical_identity)
                for role in ("build", "decode")
            ),
        )
        self.assertEqual(profile.validate_capability("build", capabilities["build"]), profile.plan("build"))
        self.assertEqual(
            profile.validate_capability("decode", capabilities["decode"]),
            profile.plan("decode"),
        )
        with self.assertRaises(ValueError):
            profile.validate_capability("build", capabilities["decode"])

        renamed = replace(
            profile.plan("build"),
            arguments=tuple(
                (f"argument_{index}", slot)
                for index, (_, slot) in enumerate(profile.plan("build").arguments)
            ),
        )
        renamed_profile = replace(
            profile,
            execution_plans=(renamed, profile.plan("decode")),
        )
        with self.assertRaisesRegex(ValueError, "path arguments"):
            renamed_profile.validate_capability("build", capabilities["build"])

        decode = capabilities["decode"]
        non_path = replace(decode, argv_template=(*decode.argv_template, "{mode}"))
        non_path_profile = replace(
            profile,
            capability_bindings=(
                profile.capability_bindings[0],
                CapabilityBinding("decode", non_path.canonical_identity),
            ),
        )
        with self.assertRaisesRegex(ValueError, "path arguments"):
            non_path_profile.validate_capability("decode", non_path)

    def test_a_profile_with_frameworks_must_pin_the_framework_directory(self) -> None:
        """The tool's fallback is a directory shared with every other attempt.

        `RoleExecutionPlan` lets `decode` and `build` omit the slot, which is
        right for a native tool that has no framework concept —
        `test_v3_native_tool_plan_needs_only_role_io_paths` is that case and must
        keep passing. But a profile that *installs* frameworks is using a tool
        that has one, and apktool without `-p` writes under $HOME. That is the
        only mutable state a stage can reach outside its own workspace, and
        `execute`'s mutation guard snapshots the workspace root, so it is
        invisible.
        """
        base = profile_v3(with_framework=True)
        self.assertTrue(base.frameworks)

        for role in ("decode", "build"):
            with self.subTest(role=role):
                plans = tuple(
                    replace(
                        plan,
                        arguments=tuple(
                            pair for pair in plan.arguments if pair[1] != "framework_dir"
                        ),
                    )
                    if plan.role == role
                    else plan
                    for plan in base.execution_plans
                )
                with self.assertRaises(ValueError) as caught:
                    replace(base, execution_plans=plans)
                self.assertIn("framework_dir", str(caught.exception))
                self.assertIn(role, str(caught.exception))
                self.assertIn("shared with every other attempt", str(caught.exception))

        # The positive control: the unmodified profile, which pins it everywhere,
        # is accepted. Without this the test would pass against a rule that
        # rejected every framework profile.
        self.assertEqual(
            replace(base, execution_plans=base.execution_plans).frameworks, base.frameworks
        )

    def test_a_profile_without_frameworks_may_omit_it(self) -> None:
        """The rule is about frameworks, not about roles."""
        base = profile_v3(with_framework=False)
        self.assertFalse(base.frameworks)
        plans = tuple(
            replace(
                plan,
                arguments=tuple(
                    pair for pair in plan.arguments if pair[1] != "framework_dir"
                ),
            )
            if plan.role == "decode"
            else plan
            for plan in base.execution_plans
        )
        relaxed = replace(base, execution_plans=plans)
        self.assertNotIn(
            "framework_dir",
            {slot for plan in relaxed.execution_plans for _, slot in plan.arguments
             if plan.role == "decode"},
        )

    def test_v3_native_tool_plan_needs_only_role_io_paths(self) -> None:
        base = profile_v3()
        tool = replace(
            base.tool_for_role("decode"),
            artifact_sha256="f" * 64,
        )
        tools = (tool, base.tool_for_role("build"))
        plan = RoleExecutionPlan(
            "decode",
            tool.tool_id,
            (("input", "input_apk"), ("output", "decoded_tree")),
            45,
        )
        capability = ExecutorCapability(
            1,
            "native-decode",
            tool.artifact_sha256,
            ("decode", "{input}", "--output", "{output}"),
            ("input", "output"),
            ("stock-apk",),
            "decoded-tree",
            (),
            (),
            ("output",),
        )
        profile = ToolchainProfileV3(
            3,
            base.profile_id,
            base.backend_kind,
            (
                base.capability_bindings[0],
                CapabilityBinding("decode", capability.canonical_identity),
            ),
            base.frameworks,
            tools,
            (base.plan("build"), plan),
        )
        self.assertEqual(profile.validate_capability("decode", capability), plan)
        self.assertEqual(profile.validate_capability("decode", capability).timeout_seconds, 45)
        mismatched = replace(capability, executable_sha256="e" * 64)
        mismatched_profile = replace(
            profile,
            capability_bindings=(
                profile.capability_bindings[0],
                CapabilityBinding("decode", mismatched.canonical_identity),
            ),
        )
        with self.assertRaisesRegex(ValueError, "Native tool"):
            mismatched_profile.validate_capability("decode", mismatched)

    def test_v3_admits_framework_and_no_framework_replays(self) -> None:
        for with_framework in (False, True):
            case = fixture_v3(with_framework)
            admitted = admit_v3(case)
            with self.subTest(with_framework=with_framework):
                self.assertEqual(admitted.schema_version, 3)
                self.assertEqual(admitted.run_spec, case.run_spec)
                self.assertEqual(admitted.request, case.request)
                self.assertEqual(admitted.executor_capabilities, case.capabilities)
                self.assertEqual(admitted.direct_artifacts, case.request.direct_artifacts)
                self.assertEqual(admitted.run_spec_sha256, case.run_spec.sha256)
                self.assertEqual(admitted.replay_request_sha256, case.request.sha256)
                self.assertEqual(
                    admitted.decision_sha256, canonical_sha256(case.decision)
                )
                self.assertEqual(admitted.intent_sha256, admitted.intent.sha256)
                self.assertEqual(admitted.resolution_sha256, admitted.resolution.sha256)
                self.assertEqual(
                    admitted.source_manifest_sha256, admitted.source_manifest.sha256
                )
                self.assertEqual(
                    admitted.toolchain_profile_sha256, admitted.profile.sha256
                )
                self.assertEqual(
                    admitted.capability_bindings, admitted.profile.capability_bindings
                )
                self.assertEqual(
                    admitted.capability("decode"),
                    case.resolve_capability(
                        admitted.decode_executor_capability_sha256
                    ),
                )
                self.assertEqual(admitted.plan("build"), admitted.profile.plan("build"))
                self.assertEqual(
                    admitted.install_framework_executor_capability_sha256,
                    admitted.profile.binding("install_framework")
                    if with_framework
                    else None,
                )

    def test_v3_roundtrip_hash_and_schema_separation(self) -> None:
        admitted = admit_v3(fixture_v3())
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(AdmittedReplayV3)),
            (
                "schema_version",
                "run_spec",
                "request",
                "decision",
                "intent",
                "resolution",
                "source_manifest",
                "profile",
                "gate_prepared",
                "executor_capabilities",
            ),
        )
        self.assertEqual(AdmittedReplayV3.from_dict(asdict(admitted)), admitted)
        self.assertEqual(admitted.sha256, canonical_sha256(admitted))
        with self.assertRaises((TypeError, ValueError)):
            AdmittedReplayV2.from_dict(asdict(admitted))
        with self.assertRaises((TypeError, ValueError)):
            AdmittedReplayV3.from_dict(asdict(admit_v2(fixture_v2())))

        unknown = asdict(admitted)
        unknown["unexpected"] = True
        with self.assertRaises(ValueError):
            AdmittedReplayV3.from_dict(unknown)

    def test_v3_resolvers_run_in_authority_order_and_resolve_every_artifact_once(self) -> None:
        case = fixture_v3(True)
        events: list[tuple[str, object]] = []

        def recorded(decision: GateDecision) -> bool:
            events.append(("decision", decision))
            return case.decision_is_recorded(decision)

        def artifact_resolver(artifact: ArtifactRef) -> bytes:
            events.append(("artifact", artifact))
            return case.resolve(artifact)

        def capability_resolver(capability_sha256: str) -> ExecutorCapability:
            events.append(("capability", capability_sha256))
            return case.resolve_capability(capability_sha256)

        admit_v3(
            case,
            decision_is_recorded=recorded,
            artifact_resolver=artifact_resolver,
            capability_resolver=capability_resolver,
        )
        self.assertEqual(events[0], ("decision", case.decision))
        self.assertEqual(
            tuple(value for kind, value in events if kind == "artifact"),
            case.request.direct_artifacts,
        )
        self.assertEqual(
            tuple(value for kind, value in events if kind == "capability"),
            tuple(
                binding.executor_capability_sha256
                for binding in admit_v3(case).profile.capability_bindings
            ),
        )
        first_capability = next(
            index for index, (kind, _) in enumerate(events) if kind == "capability"
        )
        self.assertTrue(all(kind == "artifact" for kind, _ in events[1:first_capability]))

        for decision, predicate in (
            (replace(case.decision, actor="other-operator"), lambda _: True),
            (replace(case.decision, rationale="unrecorded"), lambda _: False),
        ):
            blocked_events: list[str] = []
            with self.subTest(decision=decision):
                with self.assertRaises(ValueError):
                    admit_v3(
                        case,
                        decision=decision,
                        decision_is_recorded=predicate,
                        artifact_resolver=lambda artifact: (
                            blocked_events.append("artifact") or case.resolve(artifact)
                        ),
                        capability_resolver=lambda digest: (
                            blocked_events.append("capability")
                            or case.resolve_capability(digest)
                        ),
                    )
                self.assertEqual(blocked_events, [])

    def test_v3_validates_non_capability_relationships_before_capability_lookup(self) -> None:
        case = fixture_v3()
        first = case.request.tools[0]
        substituted = replace(
            first,
            artifact=replace(first.artifact, producer_operation_id="other-producer"),
        )
        capabilities: list[str] = []
        with self.assertRaises(ValueError):
            admit_v3(
                case,
                request=replace(
                    case.request,
                    tools=(substituted, *case.request.tools[1:]),
                ),
                capability_resolver=lambda digest: (
                    capabilities.append(digest) or case.resolve_capability(digest)
                ),
            )
        self.assertEqual(capabilities, [])

    def test_v3_capability_resolver_rejects_wrong_type_hash_and_role(self) -> None:
        case = fixture_v3()
        build, decode = case.capabilities

        class SpoofedCapability(ExecutorCapability):
            @property
            def canonical_identity(self) -> str:
                return build.canonical_identity

        spoofed = SpoofedCapability(
            *(getattr(build, field.name) for field in dataclasses.fields(ExecutorCapability))
        )
        substitutions = (
            lambda _: object(),
            lambda _: replace(build, capability_id="substituted-build"),
            lambda _: decode,
            lambda _: spoofed,
        )
        for resolver in substitutions:
            with self.subTest(resolver=resolver):
                with self.assertRaises((TypeError, ValueError)):
                    admit_v3(case, capability_resolver=resolver)  # type: ignore[arg-type]

        def raises(_: str) -> ExecutorCapability:
            raise RuntimeError("capability store unavailable")

        with self.assertRaisesRegex(ValueError, "Unable to resolve") as raised:
            admit_v3(case, capability_resolver=raises)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_v3_rejects_placeholder_and_native_executable_mismatches(self) -> None:
        admitted = admit_v3(fixture_v3())
        base = fixture_v2()
        build = admitted.capability("build")
        placeholder_capability = replace(
            build,
            argv_template=(*build.argv_template, "{mode}"),
        )
        placeholder_capabilities = (
            placeholder_capability,
            admitted.capability("decode"),
        )
        placeholder_profile = replace(
            admitted.profile,
            capability_bindings=(
                CapabilityBinding("build", placeholder_capability.canonical_identity),
                admitted.profile.capability_bindings[1],
            ),
        )
        placeholder_case = bind_v3_fixture(
            base, placeholder_profile, placeholder_capabilities
        )
        with self.assertRaisesRegex(ValueError, "path arguments"):
            admit_v3(placeholder_case)

        decode_tool = admitted.profile.tool_for_role("decode")
        native_plan = RoleExecutionPlan(
            "decode",
            decode_tool.tool_id,
            (("input", "input_apk"), ("output", "decoded_tree")),
            45,
        )
        native_capability = capability_for_plan(
            replace(
                admitted.profile,
                execution_plans=(admitted.plan("build"), native_plan),
            ),
            "decode",
            executable_sha256="0" * 64,
        )
        native_profile = replace(
            admitted.profile,
            capability_bindings=(
                admitted.profile.capability_bindings[0],
                CapabilityBinding("decode", native_capability.canonical_identity),
            ),
            execution_plans=(admitted.plan("build"), native_plan),
        )
        native_case = bind_v3_fixture(
            base,
            native_profile,
            (admitted.capability("build"), native_capability),
        )
        with self.assertRaisesRegex(ValueError, "Native tool"):
            admit_v3(native_case)

    def test_v3_capability_tuple_count_order_and_hash_are_exact(self) -> None:
        admitted = admit_v3(fixture_v3())
        build, decode = admitted.executor_capabilities
        mutations = (
            (build,),
            (build, decode, build),
            (decode, build),
            (replace(build, capability_id="other-build"), decode),
        )
        for capabilities in mutations:
            with self.subTest(capabilities=capabilities):
                with self.assertRaises(ValueError):
                    replace(admitted, executor_capabilities=capabilities)

    def test_v3_timeout_and_plan_substitutions_require_new_recorded_authority(self) -> None:
        approved = fixture_v3()
        admitted = admit_v3(approved)
        build = admitted.plan("build")
        swapped_arguments = tuple(
            (name, {"decoded_tree": "framework_dir", "framework_dir": "decoded_tree"}.get(slot, slot))
            for name, slot in build.arguments
        )
        changed_plans = (
            replace(build, timeout_seconds=build.timeout_seconds + 1),
            replace(build, arguments=swapped_arguments),
        )
        for changed_build in changed_plans:
            profile = replace(
                admitted.profile,
                execution_plans=(changed_build, admitted.plan("decode")),
            )
            substituted = bind_v3_fixture(
                fixture_v2(), profile, admitted.executor_capabilities
            )
            reads: list[str] = []
            with self.subTest(changed_build=changed_build):
                with self.assertRaisesRegex(ValueError, "subject does not bind"):
                    admit_v3(
                        substituted,
                        decision=approved.decision,
                        decision_is_recorded=approved.decision_is_recorded,
                        artifact_resolver=lambda artifact: (
                            reads.append("artifact") or substituted.resolve(artifact)
                        ),
                        capability_resolver=lambda digest: (
                            reads.append("capability")
                            or substituted.resolve_capability(digest)
                        ),
                    )
                self.assertEqual(reads, [])

    def test_v3_self_consistent_unrecorded_substitution_reads_nothing(self) -> None:
        approved = fixture_v3()
        admitted = admit_v3(approved)
        changed_build = replace(
            admitted.plan("build"), timeout_seconds=admitted.plan("build").timeout_seconds + 1
        )
        substituted = bind_v3_fixture(
            fixture_v2(),
            replace(
                admitted.profile,
                execution_plans=(changed_build, admitted.plan("decode")),
            ),
            admitted.executor_capabilities,
        )
        reads: list[str] = []
        with self.assertRaisesRegex(ValueError, "not recorded"):
            admit_v3(
                substituted,
                decision_is_recorded=approved.decision_is_recorded,
                artifact_resolver=lambda artifact: (
                    reads.append("artifact") or substituted.resolve(artifact)
                ),
                capability_resolver=lambda digest: (
                    reads.append("capability")
                    or substituted.resolve_capability(digest)
                ),
            )
        self.assertEqual(reads, [])

    def test_v2_admission_still_rejects_v3_profile_bytes(self) -> None:
        case = fixture_v3()
        with self.assertRaises((TypeError, ValueError)):
            admit_replay_v2(
                case.run_spec,
                case.request,
                case.decision,
                case.decision_is_recorded,
                case.resolve,
            )

    def test_v1_and_v2_decoders_are_schema_separated(self) -> None:
        v1 = admit(fixture())
        v2 = admit_v2(fixture_v2())
        v3_profile = profile_v3()
        decoder_pairs = (
            (ReplayRunSpecV1.from_dict, asdict(v2.run_spec)),
            (ReplayRunSpecV2.from_dict, asdict(v1.run_spec)),
            (ToolchainProfile.from_dict, asdict(v2.profile)),
            (ToolchainProfileV2.from_dict, asdict(v1.profile)),
            (ReplayRequest.from_dict, asdict(v2.request)),
            (ReplayRequestV2.from_dict, asdict(v1.request)),
            (AdmittedReplay.from_dict, asdict(v2)),
            (AdmittedReplayV2.from_dict, asdict(v1)),
            (ToolchainProfileV2.from_dict, asdict(v3_profile)),
            (ToolchainProfileV3.from_dict, asdict(v2.profile)),
        )
        for decoder, payload in decoder_pairs:
            with self.subTest(decoder=decoder):
                with self.assertRaises((TypeError, ValueError)):
                    decoder(payload)

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(ToolchainProfileV2)),
            (
                "schema_version",
                "profile_id",
                "backend_kind",
                "capability_bindings",
                "frameworks",
                "tools",
            ),
        )

    def test_v2_requires_exact_tool_id_kind_and_hash_pairs(self) -> None:
        admitted = admit_v2(fixture_v2())
        first, second = admitted.request.tools
        additional = ToolArtifact("z-tool", artifact_ref("tool-binary", b"additional"))
        substitutions = (
            (first,),
            (replace(first, tool_id="alternate-tool"), second),
            (replace(first, artifact=replace(first.artifact, kind="portable-binary")), second),
            (replace(first, artifact=artifact_ref(first.artifact.kind, b"substitute")), second),
            (first, second, additional),
        )
        for tools in substitutions:
            with self.subTest(tools=tools):
                with self.assertRaises(ValueError):
                    replace(admitted, request=replace(admitted.request, tools=tools))

    def test_v2_tool_bytes_and_exact_producer_lineage_are_resolved(self) -> None:
        case = fixture_v2()
        admitted = admit_v2(case)
        tool = case.request.tools[0]
        substituted = replace(
            tool,
            artifact=replace(tool.artifact, producer_operation_id="other-producer"),
        )
        substituted_request = replace(
            admitted.request,
            tools=(substituted, *admitted.request.tools[1:]),
        )
        with self.assertRaises(ValueError):
            replace(admitted, request=substituted_request)
        admitted_data = asdict(admitted)
        admitted_data["request"]["tools"][0]["artifact"][
            "producer_operation_id"
        ] = "other-producer"
        with self.assertRaises(ValueError):
            AdmittedReplayV2.from_dict(admitted_data)

        reads: list[ArtifactRef] = []

        def resolver(artifact: ArtifactRef) -> bytes:
            reads.append(artifact)
            return case.resolve(artifact)

        with self.assertRaises(ValueError):
            admit_v2(case, request=substituted_request, artifact_resolver=resolver)
        self.assertEqual(reads, [case.request.gate_prepared])

        payloads = dict(case.payloads)
        payloads[canonical_sha256(tool.artifact)] = b"tampered tool bytes"
        with self.assertRaises(ValueError):
            admit_v2(case, artifact_resolver=resolve_from(payloads))

    def test_v2_from_dict_rejects_direct_and_whitespace_raw_ref_substitutions(self) -> None:
        admitted = admit_v2(fixture_v2())
        direct_substitution = asdict(admitted)
        direct_substitution["request"]["intent"]["producer_operation_id"] = (
            "other-producer"
        )
        with self.assertRaises(ValueError):
            AdmittedReplayV2.from_dict(direct_substitution)

        envelope_lineage_substitution = asdict(admitted)
        envelope_lineage_substitution["request"]["gate_prepared"][
            "producer_operation_id"
        ] = "other-producer"
        with self.assertRaises(ValueError):
            AdmittedReplayV2.from_dict(envelope_lineage_substitution)

        whitespace_payload = b" \n" + json_bytes(admitted.gate_prepared)
        whitespace_ref = artifact_ref(
            "replay-gate-prepared-v2", whitespace_payload
        )
        whitespace_substitution = asdict(admitted)
        whitespace_substitution["request"]["gate_prepared"] = asdict(whitespace_ref)
        whitespace_substitution["run_spec"]["gate_prepared_sha256"] = (
            whitespace_ref.sha256
        )
        whitespace_substitution["run_spec"]["gate_prepared_ref_sha256"] = (
            canonical_sha256(whitespace_ref)
        )
        changed_run = ReplayRunSpecV2.from_dict(whitespace_substitution["run_spec"])
        whitespace_substitution["request"]["run_spec_sha256"] = changed_run.sha256
        whitespace_substitution["decision"]["subject_sha256"] = changed_run.sha256
        whitespace_substitution["decision"]["prepared_sha256"] = whitespace_ref.sha256
        with self.assertRaises(ValueError):
            AdmittedReplayV2.from_dict(whitespace_substitution)

    def test_v2_rejects_incomplete_gate_prepared_input_lineage(self) -> None:
        admitted = admit_v2(fixture_v2())
        substituted = asdict(admitted)
        gate_ref = replace(
            admitted.request.gate_prepared,
            input_hashes=admitted.request.gate_prepared.input_hashes[:-1],
        )
        substituted["request"]["gate_prepared"] = asdict(gate_ref)
        substituted["run_spec"]["gate_prepared_ref_sha256"] = canonical_sha256(gate_ref)
        changed_run = ReplayRunSpecV2.from_dict(substituted["run_spec"])
        substituted["request"]["run_spec_sha256"] = changed_run.sha256
        substituted["decision"]["subject_sha256"] = changed_run.sha256
        with self.assertRaisesRegex(ValueError, "input lineage is incomplete"):
            AdmittedReplayV2.from_dict(substituted)

    def test_v2_self_consistent_substitution_fails_unchanged_recorded_decision(self) -> None:
        approved = fixture_v2()
        substituted = fixture_v2(tool_payload_suffix=b" substituted")
        reads: list[ArtifactRef] = []

        def resolver(artifact: ArtifactRef) -> bytes:
            reads.append(artifact)
            return substituted.resolve(artifact)

        with self.assertRaisesRegex(ValueError, "subject does not bind"):
            admit_v2(
                substituted,
                decision=approved.decision,
                decision_is_recorded=approved.decision_is_recorded,
                artifact_resolver=resolver,
            )
        self.assertEqual(reads, [])

    def test_v2_decision_is_checked_and_recorded_before_artifact_reads(self) -> None:
        case = fixture_v2()
        reads: list[ArtifactRef] = []

        def resolver(artifact: ArtifactRef) -> bytes:
            reads.append(artifact)
            return case.resolve(artifact)

        with self.assertRaises(ValueError):
            admit_v2(
                case,
                decision=replace(case.decision, actor="other-operator"),
                decision_is_recorded=lambda _: True,
                artifact_resolver=resolver,
            )
        self.assertEqual(reads, [])
        with self.assertRaises(ValueError):
            admit_v2(case, decision_is_recorded=lambda _: False, artifact_resolver=resolver)
        self.assertEqual(reads, [])

    def test_v2_capabilities_backend_frameworks_and_decision_are_relational(self) -> None:
        admitted = admit_v2(fixture_v2(True))
        changed_run = replace(
            admitted.run_spec,
            executor_capability_sha256s=(
                "0" * 64,
                *admitted.run_spec.executor_capability_sha256s,
            ),
        )
        mutations = (
            {"decision": replace(admitted.decision, gate_id="other-gate")},
            {
                "run_spec": changed_run,
                "request": replace(admitted.request, run_spec_sha256=changed_run.sha256),
                "decision": replace(admitted.decision, subject_sha256=changed_run.sha256),
            },
            {"profile": replace(admitted.profile, backend_kind="stock_dex_graft")},
            {
                "request": replace(
                    admitted.request,
                    frameworks=(replace(admitted.request.frameworks[0], package_id=8),),
                )
            },
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(admitted, **changes)

    def test_replay_contract_source_is_target_neutral(self) -> None:
        source = (
            Path(__file__).parents[1] / "src" / "dfinsta_pipeline" / "replay_contracts.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "com.instagram.android",
            "300.0.0.29.110",
            "340.0.0.22.109",
            "430.0.0.0.0",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
