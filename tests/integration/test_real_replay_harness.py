from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock


if __name__ == "integration.test_real_replay_harness":
    sys.modules.setdefault("tests.integration.test_real_replay_harness", sys.modules[__name__])
elif __name__ == "tests.integration.test_real_replay_harness":
    sys.modules.setdefault("integration.test_real_replay_harness", sys.modules[__name__])

from dfinsta_pipeline import activities
from dfinsta_pipeline.activities import (
    configure_runtime,
    replay_apply_tree_checkpoint_activity,
    replay_build_patched_apk_checkpoint_activity,
    replay_decode_checkpoint_activity,
    replay_install_frameworks_checkpoint_activity,
    runtime,
)
from dfinsta_pipeline.contracts import (
    ArtifactRef,
    GateDecision,
    canonical_json,
    canonical_sha256,
)
from dfinsta_pipeline.decoded_artifact import load_decoded_tree
from dfinsta_pipeline.executor import ExecutorCapability
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.port_contracts import IntentSpecV2, ResolutionSpecV3
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayV3,
    CapabilityBinding,
    FrameworkArtifact,
    FrameworkRequirement,
    GatePreparedEnvelopeV2,
    ReplayDecodedTreeReceiptV1,
    ReplayDecodedTreeReceiptV2,
    ReplayFrameworkCacheReceiptV1,
    ReplayPatchedTreeReceiptV1,
    ReplayPatchedApkReceiptV1,
    ReplayRequestV2,
    ReplayRunSpecV2,
    RoleExecutionPlan,
    SourceManifestV1,
    ToolArtifact,
    ToolRequirement,
    ToolchainProfileV3,
    admit_replay_v3,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APKTOOL_SHA256 = "7956eb04194300ce0d0a84ad18771eebc94b89fb8d1ddcce8ea4c056818646f4"
JAVA_SHA256 = "1a86d087fa5a5be1ed3e8a531ae891da85fc80aad15ab6fa98060763f2eb7000"
API36_FRAMEWORK_SHA256 = "1f95cd4676f3e16e0432a0f19c01026593101fd26d8190233c70803de8453473"


@dataclass(frozen=True)
class TargetConfig:
    target: int
    stock_apk: str
    stock_sha256: str
    resolution: str
    source_manifest: str
    source_root: str
    profile_id: str
    source_file_count: int
    operation_count: int
    framework_apk: str | None = None
    framework_sha256: str | None = None


TARGETS = {
    340: TargetConfig(
        340,
        "apks/com.instagram.android_340.0.0.22.109-374010893_minAPI28(arm64-v8a)(nodpi).apk",
        "68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113",
        "pipeline_specs/resolutions/instagram_340.json",
        "pipeline_specs/source_manifests/instagram_340.json",
        "dfinsta_source_1.4.1",
        "apktool-2.9.3-aapt1",
        112,
        59,
    ),
    430: TargetConfig(
        430,
        "apks/com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk",
        "38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754",
        "pipeline_specs/resolutions/instagram_430.json",
        "pipeline_specs/source_manifests/instagram_430.json",
        "dfinsta_source_430",
        "apktool-2.9.3-aapt1-api36",
        5,
        7,
        "work/430-port/framework-res-api36.apk",
        API36_FRAMEWORK_SHA256,
    ),
}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_bytes(data: bytes) -> object:
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def select_targets(value: str | None) -> tuple[int, ...]:
    raw = "340,430" if value is None else value
    try:
        selected = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as error:
        raise ValueError("DFINSTA_REAL_REPLAY_TARGETS must contain only 340 and/or 430") from error
    if not selected or len(selected) != len(set(selected)) or any(item not in TARGETS for item in selected):
        raise ValueError("DFINSTA_REAL_REPLAY_TARGETS must contain unique 340 and/or 430 targets")
    return tuple(sorted(selected))


def validate_run_root(value: str | None, repository_root: Path = REPOSITORY_ROOT) -> Path:
    if not value:
        raise ValueError("DFINSTA_REAL_REPLAY_ROOT is required")
    supplied = Path(value)
    if not supplied.is_absolute():
        raise ValueError("DFINSTA_REAL_REPLAY_ROOT must be absolute")
    root = supplied.resolve(strict=False)
    repository = repository_root.resolve(strict=True)
    if root == repository or root.is_relative_to(repository) or repository.is_relative_to(root):
        raise ValueError("DFINSTA_REAL_REPLAY_ROOT must not overlap the repository/source root")
    if os.path.lexists(root):
        raise FileExistsError("DFINSTA_REAL_REPLAY_ROOT must be initially absent; overwrite is refused")
    return root


def _capability(
    role: str,
    java_sha256: str,
    target: int,
) -> ExecutorCapability:
    if role == "decode":
        return ExecutorCapability(
            1,
            f"real-replay-{target}-apktool-decode-java",
            java_sha256,
            (
                "-jar",
                "{tool}",
                "d",
                "-f",
                "{input_apk}",
                "-o",
                "{decoded_tree}",
                "-p",
                "{framework_dir}",
            ),
            ("decoded_tree", "framework_dir", "input_apk", "tool"),
            ("stock-apk",),
            "decoded-tree",
            (),
            (),
            ("framework", "output"),
        )
    if role == "install_framework":
        return ExecutorCapability(
            1,
            f"real-replay-{target}-apktool-install-framework-java",
            java_sha256,
            ("-jar", "{tool}", "if", "{framework_apk}", "-p", "{framework_dir}"),
            ("framework_apk", "framework_dir", "tool"),
            ("framework-apk",),
            "framework-cache",
            (),
            (),
            ("framework",),
        )
    if role == "build":
        return ExecutorCapability(
            1,
            f"real-replay-{target}-apktool-build-java-provisional",
            java_sha256,
            (
                "-jar",
                "{tool}",
                "b",
                "{decoded_tree}",
                "-o",
                "{intermediate_apk}",
                "-p",
                "{framework_dir}",
                "--use-aapt1",
            ),
            ("decoded_tree", "framework_dir", "intermediate_apk", "tool"),
            ("decoded-tree-manifest-v1",),
            "intermediate-apk",
            (),
            (),
            ("intermediate.apk",),
        )
    raise ValueError(f"Unsupported role: {role}")


def build_profile(
    config: TargetConfig,
    *,
    java_sha256: str = JAVA_SHA256,
    apktool_sha256: str = APKTOOL_SHA256,
) -> tuple[ToolchainProfileV3, tuple[ExecutorCapability, ...]]:
    roles = ("build", "decode") + (("install_framework",) if config.framework_sha256 else ())
    capabilities_by_role = {
        role: _capability(role, java_sha256, config.target) for role in roles
    }
    capabilities = tuple(capabilities_by_role[role] for role in sorted(roles))
    bindings = tuple(
        CapabilityBinding(role, capabilities_by_role[role].canonical_identity)
        for role in sorted(roles)
    )
    frameworks = (
        (FrameworkRequirement(1, config.framework_sha256),)
        if config.framework_sha256 is not None
        else ()
    )
    tool = ToolRequirement("apktool", "java-archive", apktool_sha256, tuple(sorted(roles)))
    plans = {
        "build": RoleExecutionPlan(
            "build",
            "apktool",
            (
                ("decoded_tree", "decoded_tree"),
                ("framework_dir", "framework_dir"),
                ("intermediate_apk", "intermediate_apk"),
                ("tool", "tool"),
            ),
            600,
        ),
        "decode": RoleExecutionPlan(
            "decode",
            "apktool",
            (
                ("decoded_tree", "decoded_tree"),
                ("framework_dir", "framework_dir"),
                ("input_apk", "input_apk"),
                ("tool", "tool"),
            ),
            600,
        ),
        "install_framework": RoleExecutionPlan(
            "install_framework",
            "apktool",
            (
                ("framework_apk", "framework_apk"),
                ("framework_dir", "framework_dir"),
                ("tool", "tool"),
            ),
            300,
        ),
    }
    profile = ToolchainProfileV3(
        3,
        config.profile_id,
        "apktool_full_rebuild" if config.target == 340 else "stock_dex_graft",
        bindings,
        frameworks,
        (tool,),
        tuple(plans[role] for role in sorted(roles)),
    )
    for binding, capability in zip(profile.capability_bindings, capabilities, strict=True):
        profile.validate_capability(binding.role, capability)
    return profile, capabilities


def admit_and_record(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    decision: GateDecision,
    ledger: Ledger,
    artifact_resolver: Callable[[ArtifactRef], bytes],
    capability_resolver: Callable[[str], ExecutorCapability],
) -> AdmittedReplayV3:
    ledger.record_decision(decision)
    admitted = admit_replay_v3(
        run_spec,
        request,
        decision,
        ledger.has_decision,
        artifact_resolver,
        capability_resolver,
    )
    ledger.record_admitted_replay_v3(admitted)
    return ledger.require_admitted_replay_v3(admitted)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_verified(path: Path, expected_sha256: str) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    actual = _sha256(data)
    if actual != expected_sha256:
        raise ValueError(f"Identity mismatch for {path}: expected {expected_sha256}, got {actual}")
    return data


def _path(relative: str) -> Path:
    path = (REPOSITORY_ROOT / relative).resolve(strict=True)
    if not path.is_relative_to(REPOSITORY_ROOT):
        raise ValueError(f"Repository artifact escapes root: {relative}")
    return path


def _put(
    kind: str,
    data: bytes,
    producer: str,
    inputs: tuple[str, ...] = (),
) -> ArtifactRef:
    operation_input = {
        "schema_version": 1,
        "import_label": producer,
        "kind": kind,
        "content_sha256": _sha256(data),
        "input_hashes": inputs,
    }
    key = activities.operation_key("real_replay_import_v1", operation_input)
    owner = f"real-replay-import-{key}"
    existing = Ledger.begin_operation(
        runtime().ledger,
        key,
        "real_replay_import_v1",
        canonical_sha256(operation_input),
        owner,
        retry_safe=False,
    )
    if existing is not None:
        if (
            existing.kind != kind
            or existing.input_hashes != inputs
            or runtime().store.read_bytes(existing) != data
        ):
            raise ValueError("Existing real replay import does not match exact bytes")
        return Ledger.complete_operation(runtime().ledger, key, existing)
    reference = runtime().store.put_bytes(
        kind=kind,
        data=data,
        producer_operation_id=key,
        input_hashes=inputs,
    )
    Ledger.record_effect(runtime().ledger, key, owner, reference)
    return Ledger.complete_operation(runtime().ledger, key, reference)


def _canonical_artifact(kind: str, value: object, producer: str, inputs: tuple[str, ...]) -> ArtifactRef:
    return _put(kind, canonical_json(value).encode("utf-8"), producer, inputs)


def _run_checked(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8")


def preflight_tools() -> tuple[Path, dict[str, str]]:
    java_name = shutil.which("java")
    if java_name is None:
        raise FileNotFoundError("java was not found on PATH")
    java = Path(java_name).resolve(strict=True)
    _read_verified(java, JAVA_SHA256)
    apktool = _path("apktool_2.9.3.jar")
    _read_verified(apktool, APKTOOL_SHA256)
    java_version = _run_checked((str(java), "-version"))
    apktool_version = _run_checked((str(java), "-jar", str(apktool), "--version"))
    version = apktool_version.stdout.strip()
    if version != "2.9.3":
        raise ValueError(f"Expected apktool 2.9.3, got {version!r}")
    return java, {
        "java": (java_version.stderr or java_version.stdout).strip(),
        "apktool": version,
    }


class RecordingProcess:
    def __init__(self, process: asyncio.subprocess.Process, record: dict[str, Any]) -> None:
        self._process = process
        self._record = record
        self._finished = False

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout, stderr = await self._process.communicate()
        self._finish(stdout, stderr)
        return stdout, stderr

    async def wait(self) -> int:
        result = await self._process.wait()
        self._finish(b"", b"")
        return result

    def kill(self) -> None:
        self._record["kill_requested"] = True
        self._process.kill()

    def _finish(self, stdout: bytes, stderr: bytes) -> None:
        if self._finished:
            return
        self._finished = True
        ended_ns = time.time_ns()
        self._record.update(
            {
                "ended_at": datetime.fromtimestamp(ended_ns / 1_000_000_000, timezone.utc).isoformat(),
                "duration_ns": time.monotonic_ns() - self._record.pop("_started_monotonic_ns"),
                "returncode": self._process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stdout_sha256": _sha256(stdout),
                "stderr_sha256": _sha256(stderr),
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            }
        )


class RecordingLauncher:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> RecordingProcess:
        started_ns = time.time_ns()
        record: dict[str, Any] = {
            "sequence": len(self.records) + 1,
            "argv": list(argv),
            "cwd": str(kwargs.get("cwd")),
            "started_at": datetime.fromtimestamp(started_ns / 1_000_000_000, timezone.utc).isoformat(),
            "_started_monotonic_ns": time.monotonic_ns(),
            "kill_requested": False,
        }
        self.records.append(record)
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        record["pid"] = process.pid
        return RecordingProcess(process, record)


def _manifest_evidence(reference: ArtifactRef) -> dict[str, Any]:
    manifest = load_decoded_tree(runtime().store, reference)
    files = tuple(entry for entry in manifest.entries if entry.kind == "file")
    return {
        "artifact": asdict(reference),
        "semantic_sha256": manifest.decoded_tree_sha256,
        "entry_count": len(manifest.entries),
        "file_count": len(files),
        "total_file_bytes": sum(entry.size or 0 for entry in files),
    }


def _ledger_evidence(ledger: Ledger) -> dict[str, Any]:
    with ledger._connection() as connection:
        claims = connection.execute(
            "SELECT operation_key, kind, input_sha256, owner_token, owner_attempt, status, output_json "
            "FROM operation_claims ORDER BY operation_key"
        ).fetchall()
        events = connection.execute(
            "SELECT event_id, operation_key, kind, input_sha256, status, output_json "
            "FROM operation_events ORDER BY event_id"
        ).fetchall()
    return {
        "claims": [
            dict(zip(("operation_key", "kind", "input_sha256", "owner", "owner_attempt", "status", "output_json"), row))
            for row in claims
        ],
        "events": [
            dict(zip(("event_id", "operation_key", "kind", "input_sha256", "status", "output_json"), row))
            for row in events
        ],
    }


def _artifact_refs(value: object) -> tuple[ArtifactRef, ...]:
    found: list[ArtifactRef] = []

    def visit(item: object) -> None:
        if type(item) is ArtifactRef:
            found.append(item)
        elif is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(dict.fromkeys(found))


def _require_completed_producers(ledger: Ledger, values: object) -> list[dict[str, str]]:
    references = _artifact_refs(values)
    with ledger._connection() as connection:
        claims = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT operation_key, status FROM operation_claims"
            ).fetchall()
        }
    evidence = []
    for reference in references:
        status = claims.get(reference.producer_operation_id)
        if status != "completed":
            raise AssertionError(
                f"Artifact producer {reference.producer_operation_id} is not completed"
            )
        runtime().store.read_bytes(reference)
        evidence.append(
            {
                "producer_operation_id": reference.producer_operation_id,
                "artifact_sha256": reference.sha256,
                "status": status,
            }
        )
    return sorted(
        evidence,
        key=lambda item: (item["producer_operation_id"], item["artifact_sha256"]),
    )


def _authority_timestamp(target: int) -> str:
    return {340: "2026-01-01T03:40:00Z", 430: "2026-01-01T04:30:00Z"}[target]


def _load_target_inputs(config: TargetConfig) -> dict[str, Any]:
    intent_bytes = _path("pipeline_specs/intent_v2.json").read_bytes()
    resolution_bytes = _path(config.resolution).read_bytes()
    source_manifest_bytes = _path(config.source_manifest).read_bytes()
    intent = IntentSpecV2.from_dict(strict_json_bytes(intent_bytes))
    resolution = ResolutionSpecV3.from_dict(strict_json_bytes(resolution_bytes))
    source_manifest = SourceManifestV1.from_json_value(strict_json_bytes(source_manifest_bytes))
    if resolution.target.apk_sha256 != config.stock_sha256:
        raise ValueError("Resolution target APK does not match the target table")
    if resolution.target.package_name != "com.instagram.android":
        raise ValueError("Resolution target package does not match Instagram")
    if resolution.backend.profile_id != config.profile_id:
        raise ValueError("Resolution backend profile does not match the target table")
    if intent.sha256 != resolution.intent_sha256:
        raise ValueError("Intent semantic hash does not bind the resolution")
    if source_manifest.sha256 != resolution.source_bundle_sha256:
        raise ValueError("Source manifest semantic hash does not bind the resolution")
    if len(source_manifest.records) != config.source_file_count:
        raise ValueError("Unexpected source manifest file count")
    if len(resolution.operations) != config.operation_count:
        raise ValueError("Unexpected resolution operation count")
    _path(config.source_root)
    source_prefix = f"{config.source_root}/"
    source_bytes = []
    for source in source_manifest.records:
        if not source.relative_path.startswith(source_prefix):
            raise ValueError("Source manifest record is outside the target source root")
        source_bytes.append(_read_verified(_path(source.relative_path), source.sha256))
    stock_bytes = _read_verified(_path(config.stock_apk), config.stock_sha256)
    framework_bytes = None
    if config.framework_apk is not None and config.framework_sha256 is not None:
        framework_bytes = _read_verified(_path(config.framework_apk), config.framework_sha256)
    return {
        "intent_bytes": intent_bytes,
        "resolution_bytes": resolution_bytes,
        "source_manifest_bytes": source_manifest_bytes,
        "intent": intent,
        "resolution": resolution,
        "source_manifest": source_manifest,
        "source_bytes": tuple(source_bytes),
        "stock_bytes": stock_bytes,
        "framework_bytes": framework_bytes,
    }


def _create_authority(config: TargetConfig, inputs: dict[str, Any]) -> tuple[AdmittedReplayV3, dict[str, ArtifactRef]]:
    target = config.target
    prefix = f"real-replay-{target}"
    stock_ref = _put("stock-apk", inputs["stock_bytes"], f"{prefix}-import-stock")
    intent_ref = _put("intent-spec", inputs["intent_bytes"], f"{prefix}-import-intent")
    source_file_refs = tuple(
        _put(
            "source-file",
            data,
            f"{prefix}-import-source",
        )
        for source, data in zip(
            inputs["source_manifest"].records,
            inputs["source_bytes"],
            strict=True,
        )
    )
    source_ref = _put(
        "source-manifest-v1",
        inputs["source_manifest_bytes"],
        f"{prefix}-import-source-manifest",
        tuple(reference.sha256 for reference in source_file_refs),
    )
    resolution_ref = _put(
        "resolution-spec",
        inputs["resolution_bytes"],
        f"{prefix}-import-resolution",
        (stock_ref.sha256, intent_ref.sha256, source_ref.sha256),
    )
    apktool_bytes = _read_verified(_path("apktool_2.9.3.jar"), APKTOOL_SHA256)
    tool_ref = _put("java-archive", apktool_bytes, f"{prefix}-import-apktool")
    framework_refs: tuple[FrameworkArtifact, ...] = ()
    if inputs["framework_bytes"] is not None:
        framework_ref = _put(
            "framework-apk",
            inputs["framework_bytes"],
            f"{prefix}-import-api36-framework",
        )
        framework_refs = (FrameworkArtifact(1, framework_ref),)
    profile, capabilities = build_profile(config)
    profile_ref = _canonical_artifact(
        "toolchain-profile",
        profile,
        f"{prefix}-profile",
        (
            tool_ref.sha256,
            *(item.artifact.sha256 for item in framework_refs),
            *(capability.canonical_identity for capability in capabilities),
        ),
    )
    tools = (ToolArtifact("apktool", tool_ref),)
    gate_prepared = GatePreparedEnvelopeV2(
        2,
        stock_ref,
        intent_ref,
        resolution_ref,
        source_ref,
        profile_ref,
        framework_refs,
        tools,
    )
    gate_inputs = (
        stock_ref.sha256,
        intent_ref.sha256,
        resolution_ref.sha256,
        source_ref.sha256,
        profile_ref.sha256,
        *(item.artifact.sha256 for item in framework_refs),
        tool_ref.sha256,
    )
    gate_prepared_ref = _canonical_artifact(
        "replay-gate-prepared-v2",
        gate_prepared,
        f"{prefix}-gate-prepared",
        gate_inputs,
    )
    gate_admission = {
        "schema_version": 1,
        "target": target,
        "gate_id": f"real-replay-{target}-gate",
        "allowed_actor": f"real-replay-{target}-operator",
        "gate_prepared_ref_sha256": canonical_sha256(gate_prepared_ref),
        "profile_sha256": profile.sha256,
        "capability_sha256s": sorted(capability.canonical_identity for capability in capabilities),
    }
    gate_admission_ref = _canonical_artifact(
        "replay-gate-admission-v1",
        gate_admission,
        f"{prefix}-gate-admission",
        (canonical_sha256(gate_prepared_ref), profile.sha256),
    )
    run_spec = ReplayRunSpecV2(
        2,
        f"real-replay-{target}-run",
        stock_ref.sha256,
        inputs["intent"].sha256,
        inputs["resolution"].sha256,
        inputs["source_manifest"].sha256,
        profile.sha256,
        tuple(sorted(capability.canonical_identity for capability in capabilities)),
        f"real-replay-{target}-gate",
        gate_admission_ref.sha256,
        gate_prepared_ref.sha256,
        canonical_sha256(gate_prepared_ref),
        f"real-replay-{target}-operator",
        inputs["intent"].policy_revision,
        "monolithic",
    )
    run_spec_ref = _canonical_artifact(
        "replay-run-spec-v2",
        run_spec,
        f"{prefix}-run-spec",
        (
            stock_ref.sha256,
            intent_ref.sha256,
            resolution_ref.sha256,
            source_ref.sha256,
            profile_ref.sha256,
            gate_admission_ref.sha256,
            gate_prepared_ref.sha256,
            *run_spec.executor_capability_sha256s,
        ),
    )
    request = ReplayRequestV2(
        2,
        run_spec.sha256,
        gate_prepared_ref,
        stock_ref,
        intent_ref,
        resolution_ref,
        source_ref,
        profile_ref,
        framework_refs,
        tools,
    )
    request_ref = _canonical_artifact(
        "replay-request-v2",
        request,
        f"{prefix}-request",
        (run_spec_ref.sha256, *(reference.sha256 for reference in request.direct_artifacts)),
    )
    decision = GateDecision(
        1,
        f"real-replay-{target}-decision",
        f"real-replay-{target}-decision-attempt",
        run_spec.allowed_actor,
        run_spec.run_id,
        run_spec.gate_id,
        run_spec.sha256,
        run_spec.gate_admission_sha256,
        run_spec.gate_prepared_sha256,
        run_spec.policy_revision,
        "approve",
        f"Self-issued test-only mechanical replay fixture for Instagram {target}",
        _authority_timestamp(target),
    )
    decision_ref = _canonical_artifact(
        "gate-decision",
        decision,
        f"{prefix}-decision",
        (run_spec_ref.sha256, gate_admission_ref.sha256, gate_prepared_ref.sha256),
    )
    capability_map = {capability.canonical_identity: capability for capability in capabilities}
    admitted = admit_and_record(
        run_spec,
        request,
        decision,
        runtime().ledger,
        runtime().store.read_bytes,
        capability_map.__getitem__,
    )
    admitted_ref = _canonical_artifact(
        "admitted-replay-v3",
        admitted,
        f"{prefix}-admitted",
        (run_spec_ref.sha256, request_ref.sha256, decision_ref.sha256),
    )
    refs = {
        "stock": stock_ref,
        "intent": intent_ref,
        "resolution": resolution_ref,
        "source_manifest": source_ref,
        "tool": tool_ref,
        "profile": profile_ref,
        "gate_prepared": gate_prepared_ref,
        "gate_admission": gate_admission_ref,
        "run_spec": run_spec_ref,
        "request": request_ref,
        "decision": decision_ref,
        "admitted": admitted_ref,
    }
    refs.update({f"source_file_{index:03d}": ref for index, ref in enumerate(source_file_refs)})
    if framework_refs:
        refs["framework"] = framework_refs[0].artifact
    return admitted, refs


async def _invoke(stage: str, admitted: AdmittedReplayV3, owner: str) -> ArtifactRef:
    function = {
        "framework": replay_install_frameworks_checkpoint_activity,
        "decode": replay_decode_checkpoint_activity,
        "apply": replay_apply_tree_checkpoint_activity,
        "build": replay_build_patched_apk_checkpoint_activity,
    }[stage]
    with mock.patch.object(activities, "_activity_owner", return_value=owner):
        return await function(admitted)


async def _run_target(
    config: TargetConfig,
    target_root: Path,
    java: Path,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    launcher = RecordingLauncher()
    if diagnostics is not None:
        diagnostics["process_records"] = launcher.records
        diagnostics["target_root"] = str(target_root)
        diagnostics["ledger_path"] = str(target_root / "state" / "ledger.sqlite3")
    configure_runtime(
        target_root / "state",
        attempts_root=target_root / "attempts",
        source_root=REPOSITORY_ROOT,
        executor_paths={JAVA_SHA256: java},
        launcher=launcher,
    )
    inputs = _load_target_inputs(config)
    admitted, authority_refs = _create_authority(config, inputs)
    stages = (
        ("framework", "decode", "apply", "build")
        if config.target == 430
        else ("decode", "apply", "build")
    )
    outcomes: list[dict[str, Any]] = []
    receipt_refs: dict[str, ArtifactRef] = {}
    adoption: list[dict[str, Any]] = []
    expected_launches = {"framework": 1, "decode": 1, "apply": 0, "build": 1}
    for stage in stages:
        first_owner = f"real-replay-{config.target}-{stage}-primary"
        adopted_owner = f"real-replay-{config.target}-{stage}-adopt"
        before_launches = len(launcher.records)
        first = await _invoke(stage, admitted, first_owner)
        primary_launches = len(launcher.records) - before_launches
        if primary_launches != expected_launches[stage]:
            raise AssertionError(
                f"{stage} launched {primary_launches} processes; "
                f"expected {expected_launches[stage]}"
            )
        operation_dir = runtime().attempts_root / first.producer_operation_id
        retry_workspace = operation_dir / hashlib.sha256(adopted_owner.encode("utf-8")).hexdigest()
        before_adoption_launches = len(launcher.records)
        second = await _invoke(stage, admitted, adopted_owner)
        adoption_launches = len(launcher.records) - before_adoption_launches
        if first != second or adoption_launches != 0 or retry_workspace.exists():
            raise AssertionError(f"{stage} did not adopt its completed artifact cleanly")
        receipt_refs[stage] = first
        outcomes.extend(
            (
                {"stage": stage, "owner": first_owner, "mode": "primary", "artifact": asdict(first), "process_launches": primary_launches},
                {"stage": stage, "owner": adopted_owner, "mode": "adopted", "artifact": asdict(second), "process_launches": adoption_launches},
            )
        )
        adoption.append(
            {
                "stage": stage,
                "same_artifact_ref": first == second,
                "new_process_launches": adoption_launches,
                "retry_workspace": str(retry_workspace),
                "retry_workspace_absent": not retry_workspace.exists(),
            }
        )

    expected_argv = {
        "framework": (
            str(java),
            "-jar",
            "tool",
            "if",
            "framework-apks/1.apk",
            "-p",
            "framework",
        ),
        "decode": (
            str(java),
            "-jar",
            "tool",
            "d",
            "-f",
            "input.apk",
            "-o",
            "output",
            "-p",
            "framework",
        ),
        "build": (
            str(java),
            "-jar",
            "tool",
            "b",
            "patched-tree",
            "-o",
            "intermediate.apk",
            "-p",
            "framework",
            "--use-aapt1",
        ),
    }
    expected_process_stages = (
        ("framework", "decode", "build")
        if config.target == 430
        else ("decode", "build")
    )
    if len(launcher.records) != len(expected_process_stages):
        raise AssertionError("Unexpected total replay process count")
    for stage, record in zip(expected_process_stages, launcher.records, strict=True):
        if tuple(record["argv"]) != expected_argv[stage]:
            raise AssertionError(f"Unexpected {stage} argv: {record['argv']}")
        if (
            record.get("returncode") != 0
            or "ended_at" not in record
            or type(record.get("pid")) is not int
        ):
            raise AssertionError(f"{stage} process record is incomplete or unsuccessful")
        owner = f"real-replay-{config.target}-{stage}-primary"
        expected_cwd = (
            runtime().attempts_root
            / receipt_refs[stage].producer_operation_id
            / hashlib.sha256(owner.encode("utf-8")).hexdigest()
        )
        cwd = Path(record["cwd"])
        if not cwd.is_dir() or cwd.resolve(strict=True) != expected_cwd.resolve(strict=True):
            raise AssertionError(f"Unexpected {stage} process cwd: {cwd}")

    framework_receipt = None
    framework_manifest = None
    if "framework" in receipt_refs:
        framework_receipt = ReplayFrameworkCacheReceiptV1.from_dict(
            strict_json_bytes(runtime().store.read_bytes(receipt_refs["framework"]))
        )
        framework_manifest = _manifest_evidence(framework_receipt.framework_cache_manifest)
    decode_payload = strict_json_bytes(runtime().store.read_bytes(receipt_refs["decode"]))
    decode_receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2
    if config.target == 430:
        decode_receipt = ReplayDecodedTreeReceiptV2.from_dict(decode_payload)
    else:
        decode_receipt = ReplayDecodedTreeReceiptV1.from_dict(decode_payload)
    apply_receipt = ReplayPatchedTreeReceiptV1.from_dict(
        strict_json_bytes(runtime().store.read_bytes(receipt_refs["apply"]))
    )
    build_receipt = ReplayPatchedApkReceiptV1.from_dict(
        strict_json_bytes(runtime().store.read_bytes(receipt_refs["build"]))
    )
    if len(apply_receipt.operation_results) != config.operation_count:
        raise AssertionError("Apply receipt operation count does not match the target table")
    if any(result.status != "applied" for result in apply_receipt.operation_results):
        raise AssertionError("Real replay did not apply every operation")
    if apply_receipt.source_admission.file_count != config.source_file_count:
        raise AssertionError("Apply source evidence file count does not match the target table")
    producer_claims = _require_completed_producers(
        runtime().ledger,
        (
            admitted,
            authority_refs,
            receipt_refs,
            framework_receipt,
            decode_receipt,
            apply_receipt,
            build_receipt,
        ),
    )
    ledger = _ledger_evidence(runtime().ledger)
    if any(claim["status"] != "completed" for claim in ledger["claims"]):
        raise AssertionError("Not every replay operation completed")
    return {
        "target": config.target,
        "artifact_identities": {name: asdict(reference) for name, reference in sorted(authority_refs.items())},
        "semantic_hashes": {
            "intent": admitted.intent.sha256,
            "resolution": admitted.resolution.sha256,
            "source_manifest": admitted.source_manifest.sha256,
            "toolchain_profile": admitted.profile.sha256,
        },
        "profile": asdict(admitted.profile),
        "capabilities": [asdict(capability) for capability in admitted.executor_capabilities],
        "build_capability_status": "mechanically-executed-test-only-not-production-authority",
        "admission_scope": "self-issued-test-only-mechanical-not-authenticated",
        "admitted_replay_sha256": admitted.sha256,
        "self_issued_test_authority_refs": {name: asdict(authority_refs[name]) for name in ("gate_admission", "gate_prepared", "run_spec", "request", "decision", "admitted")},
        "receipt_refs": {name: asdict(reference) for name, reference in receipt_refs.items()},
        "manifests": {
            "framework": framework_manifest,
            "decoded": _manifest_evidence(decode_receipt.decoded_tree_manifest),
            "patched": _manifest_evidence(apply_receipt.patched_tree_manifest),
        },
        "source_evidence": asdict(apply_receipt.source_admission),
        "operation_results": [asdict(result) for result in apply_receipt.operation_results],
        "patched_apk": {
            "intermediate": asdict(build_receipt.intermediate_apk),
            "final": asdict(build_receipt.patched_apk),
            "composition": asdict(build_receipt.composition),
        },
        "ordered_outcomes": outcomes,
        "adoption_proof": adoption,
        "ledger": ledger,
        "artifact_producer_claims": producer_claims,
        "process_records": launcher.records,
    }


def _write_exclusive(path: Path, value: object) -> None:
    data = canonical_json(value).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


async def run_real_replay() -> Path:
    selected = select_targets(os.environ.get("DFINSTA_REAL_REPLAY_TARGETS"))
    run_root = validate_run_root(os.environ.get("DFINSTA_REAL_REPLAY_ROOT"))
    java, versions = preflight_tools()
    run_root.mkdir(mode=0o700)
    diagnostics: dict[str, Any] = {"targets": [], "current_target": None}
    try:
        target_evidence = []
        for target in selected:
            diagnostics["current_target"] = target
            current_diagnostics: dict[str, Any] = {"target": target}
            diagnostics["current"] = current_diagnostics
            target_root = run_root / str(target)
            target_root.mkdir(mode=0o700)
            evidence = await _run_target(
                TARGETS[target], target_root, java, current_diagnostics
            )
            target_evidence.append(evidence)
            diagnostics["targets"].append(evidence)
            diagnostics["current"] = None
        git_head = _run_checked(("git", "rev-parse", "HEAD")).stdout.strip()
        git_status = _run_checked(("git", "status", "--short", "--untracked-files=all")).stdout
        harness = Path(__file__).resolve(strict=True)
        summary = {
            "schema_version": 1,
            "status": "mechanical-direct-activity-success-not-production-authority",
            "admission_scope": "self-issued-test-only-mechanical-not-authenticated",
            "targets": list(selected),
            "authority_timestamps": {str(target): _authority_timestamp(target) for target in selected},
            "tool_versions": versions,
            "tool_identities": {
                "java": {"path": str(java), "sha256": JAVA_SHA256},
                "apktool": {"path": str(_path("apktool_2.9.3.jar")), "sha256": APKTOOL_SHA256},
                "api36_framework": {"path": str(_path(TARGETS[430].framework_apk or "")), "sha256": API36_FRAMEWORK_SHA256} if 430 in selected else None,
            },
            "harness": {"path": str(harness.relative_to(REPOSITORY_ROOT)), "sha256": _sha256(harness.read_bytes())},
            "git": {"head": git_head, "status_short": git_status},
            "target_evidence": target_evidence,
        }
        summary_path = run_root / "success.json"
        _write_exclusive(summary_path, summary)
        return summary_path
    except BaseException as error:
        try:
            current = diagnostics.get("current")
            ledger_path = Path(current["ledger_path"]) if isinstance(current, dict) and "ledger_path" in current else None
            if ledger_path is not None and ledger_path.is_file():
                diagnostics["current_ledger"] = _ledger_evidence(Ledger(ledger_path))
        except BaseException as ledger_error:
            diagnostics["ledger_error"] = f"{type(ledger_error).__name__}: {ledger_error}"
        failure = {
            "schema_version": 1,
            "status": "failure-not-replay-evidence",
            "error_type": type(error).__name__,
            "error": str(error),
            "targets": list(selected),
            "diagnostics": diagnostics,
        }
        try:
            _write_exclusive(run_root / "failure.json", failure)
        except BaseException as write_error:
            error.add_note(f"Could not write exclusive failure marker: {write_error}")
        raise


@unittest.skipUnless(
    os.environ.get("DFINSTA_RUN_REAL_REPLAY") == "1",
    "set DFINSTA_RUN_REAL_REPLAY=1 to run real apktool replay",
)
class RealReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_replay_checkpoint_activities(self) -> None:
        summary = await run_real_replay()
        self.assertTrue(summary.is_file())


if __name__ == "__main__":
    unittest.main()
