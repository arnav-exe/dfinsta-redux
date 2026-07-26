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
    CapabilityBinding,
    FrameworkArtifact,
    FrameworkRequirement,
    ReplayRequest,
    ReplayRunSpecV1,
    SourceManifestV1,
    ToolchainProfile,
    admit_replay,
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


def fixture(with_framework: bool = False) -> Fixture:
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
        framework_payload = b"synthetic framework APK"
        framework_ref = artifact_ref("framework-apk", framework_payload)
        profile = replace(
            profile,
            frameworks=(FrameworkRequirement(7, framework_ref.sha256),),
        )
        framework_artifacts = (FrameworkArtifact(7, framework_ref),)
        framework_payloads.append((framework_ref, framework_payload))

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


class ReplayContractTests(unittest.TestCase):
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
