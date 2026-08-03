from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .apply import ApplyReport, OperationResult, apply_port
from .backend import BackendReport, compose_apk, validate_composed_apk_bytes
from .compiler import TargetPortSpecV2, compile_port
from .contracts import ArtifactRef, GateDecision, RunSpec, StageInput, canonical_json, canonical_sha256
from .decoded_artifact import (
    capture_decoded_tree_fd,
    load_decoded_tree,
    materialize_decoded_tree,
    verify_materialized_decoded_tree,
)
from .executor import ExecutionMetadata, ExecutionRequest, Launcher, execute
from . import assessment_record, replay_gate
from .feature_gate import (
    FeatureAssessmentGateV1,
    FeatureDispositionsAdmissionV1,
    FeatureDispositionsV1,
    derive_assessment_gate,
    derive_feature_gate_request,
    validate_submission,
)
from .ledger import Ledger
from .replay_contracts import (
    REPLAY_STAGES_WITHOUT_FRAMEWORK,
    REPLAY_STAGE_ORDER,
    AdmittedReplayHandleV1,
    AdmittedReplayVerificationGrantV1,
    AdmittedReplayV3,
    ReplayApplyOperationResultV1,
    ReplayExecutionPlanV1,
    ReplayVerificationAdmissionV1,
    ReplayVerificationGateV1,
    ReplayVerificationGrantHandleV1,
    admit_replay_verification_grant_v1,
    ReplayBackendCompositionV1,
    ReplayDecodedTreeReceiptV2,
    ReplayDecodedTreeReceiptV1,
    ReplayFrameworkCacheReceiptV1,
    ReplayFrameworkInstallationV1,
    ReplayPatchedTreeReceiptV1,
    ReplayPatchedApkReceiptV1,
    ReplayFinalApkVerificationReceiptV1,
    ReplaySourceAdmissionEvidenceV1,
    ReplayVerificationAssertionResultV1,
)
from .source_admission import admit_source_bundle_v2, verify_staged_source_v2
from .store import ContentStore
from .verifier import (
    AssertionResult,
    DecodedArtifactReceipt,
    VerificationReport,
    decoded_tree_sha256,
    verify_apk,
)


@dataclass
class ActivityRuntime:
    store: ContentStore
    ledger: Ledger
    attempts_root: Path
    source_root: Path | None
    executor_paths: Mapping[str, Path]
    launcher: Launcher | None


_runtime: ActivityRuntime | None = None


def configure_runtime(
    state_root: Path,
    *,
    attempts_root: Path | None = None,
    source_root: Path | None = None,
    executor_paths: Mapping[str, Path] | None = None,
    launcher: Launcher | None = None,
    read_only: bool = False,
) -> None:
    """Bind the module-global runtime to one state root.

    `read_only=True` opens the ledger through `Ledger(read_only=True)`. It is
    for the trusted submission client, which re-derives a gate subject by
    calling the same helpers the preparing Activity calls -- `replay_gate`
    reaches the ledger through `runtime()`, so the only way to reuse that exact
    derivation rather than reimplement it is to give it a runtime that cannot
    write. A second implementation of the derivation would defeat the point:
    the client's whole claim is that it computes what the Activity computed.
    """

    global _runtime
    if type(read_only) is not bool:
        raise TypeError("Runtime read_only must be a boolean")
    state_root = state_root.resolve()
    cas_root = state_root / "cas"
    ledger_path = state_root / "ledger.sqlite3"
    configured_attempts_root = Path(
        os.path.abspath(attempts_root or state_root / "attempts")
    )
    resolved_attempts_root = configured_attempts_root.resolve()
    if source_root is not None and not isinstance(source_root, Path):
        raise TypeError("Source root must be a Path")
    resolved_source_root = source_root.resolve() if source_root is not None else None
    copied_paths: dict[str, Path] = {}
    for digest, path in (executor_paths or {}).items():
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Executor path key must be a SHA-256")
        if not isinstance(path, Path):
            raise TypeError("Executor paths must be Path objects")
        copied_paths[digest] = path.resolve()
    if _paths_overlap(resolved_attempts_root, cas_root):
        raise ValueError("Attempts root must not overlap the content store")
    if (
        _paths_overlap(resolved_attempts_root, ledger_path)
        or resolved_attempts_root == ledger_path.parent
        or ledger_path.is_relative_to(resolved_attempts_root)
    ):
        raise ValueError("Attempts root is unsafe for the ledger")
    if any(
        _paths_overlap(resolved_attempts_root, executable)
        for executable in copied_paths.values()
    ):
        raise ValueError("Attempts root must not overlap an executor path")
    if resolved_source_root is not None:
        protected_paths = (
            state_root,
            cas_root,
            ledger_path,
            resolved_attempts_root,
            *copied_paths.values(),
        )
        if any(_paths_overlap(resolved_source_root, path) for path in protected_paths):
            raise ValueError("Source root must not overlap runtime state or executable paths")
    _runtime = ActivityRuntime(
        store=ContentStore(cas_root),
        ledger=Ledger(ledger_path, read_only=read_only),
        attempts_root=resolved_attempts_root,
        source_root=resolved_source_root,
        executor_paths=MappingProxyType(copied_paths),
        launcher=launcher,
    )


def runtime() -> ActivityRuntime:
    if _runtime is None:
        raise RuntimeError("Activity runtime is not configured")
    return _runtime


def operation_key(kind: str, value: object) -> str:
    return canonical_sha256({"kind": kind, "input": value})


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _activity_owner() -> str:
    info = activity.info()
    return f"{info.workflow_run_id}:{info.activity_id}:{info.attempt}"


def _secure_directory_flags() -> int:
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.mkdir not in os.supports_dir_fd
        or os.open not in os.supports_dir_fd
    ):
        raise RuntimeError("Secure descriptor-relative workspace creation is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_or_create_directory(path: Path) -> int:
    flags = _secure_directory_flags()
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            parent = descriptor
            descriptor = child
            _close_descriptors(parent)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _close_descriptors(*descriptors: int | None) -> None:
    first_error: OSError | None = None
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _open_existing_directory(parent_fd: int, name: str) -> int:
    return os.open(name, _secure_directory_flags(), dir_fd=parent_fd)


def _exclusive_directory(parent_fd: int, name: str) -> int:
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    return _open_existing_directory(parent_fd, name)


def _exclusive_file(parent_fd: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("Unable to materialize replay input")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)


def _verify_workspace_path(workspace: Path, workspace_fd: int) -> None:
    descriptor_stat = os.fstat(workspace_fd)
    path_stat = workspace.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino)
        != (path_stat.st_dev, path_stat.st_ino)
        or workspace.resolve(strict=True) != workspace
    ):
        raise ValueError("Replay workspace path changed after secure creation")


def _validate_private_directory(descriptor: int, label: str) -> None:
    # Parent-path and hostile same-UID mutation remain outside this checkpoint's scope.
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"Unsafe {label} directory")


def _validate_runtime_executable(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise ValueError("Runtime executable is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Runtime executable does not match admitted capability")


def _strict_receipt_json(data: bytes, contract: type[object], label: str) -> object:
    def object_pairs(value: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, item in value:
            if name in result:
                raise ValueError(f"Duplicate JSON key: {name}")
            result[name] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValueError(f"Invalid JSON constant: {constant}")

    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
        result = contract.from_dict(value)  # type: ignore[attr-defined]
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid {label}") from error
    if canonical_json(result).encode("utf-8") != data:
        raise ValueError(f"{label.capitalize()} is not canonical")
    return result


def _strict_replay_decoded_tree_receipt(data: bytes) -> ReplayDecodedTreeReceiptV1:
    return _strict_receipt_json(
        data, ReplayDecodedTreeReceiptV1, "replay decoded-tree receipt"
    )  # type: ignore[return-value]


def _strict_replay_decoded_tree_receipt_v2(data: bytes) -> ReplayDecodedTreeReceiptV2:
    return _strict_receipt_json(
        data, ReplayDecodedTreeReceiptV2, "replay decoded-tree V2 receipt"
    )  # type: ignore[return-value]


def _strict_replay_framework_cache_receipt(
    data: bytes,
) -> ReplayFrameworkCacheReceiptV1:
    return _strict_receipt_json(
        data, ReplayFrameworkCacheReceiptV1, "replay framework cache receipt"
    )  # type: ignore[return-value]


def _validate_replay_decoded_tree_receipt(
    output: ArtifactRef,
    key: str,
    *,
    admitted_replay_sha256: str,
    input_apk: ArtifactRef,
    toolchain_profile_id: str,
    toolchain_profile_sha256: str,
    execution_plan_sha256: str,
    executor_capability_sha256: str,
    tool_artifact_sha256: str,
    execution_request_sha256: str,
) -> ReplayDecodedTreeReceiptV1:
    if (
        output.kind != "replay-decoded-tree-receipt-v1"
        or output.producer_operation_id != key
    ):
        raise ValueError("Adopted replay decoded-tree receipt does not match operation lineage")
    receipt = _strict_replay_decoded_tree_receipt(runtime().store.read_bytes(output))
    if (
        output.sha256 != receipt.sha256
        or output.input_hashes != receipt.receipt_input_hashes
    ):
        raise ValueError("Adopted replay decoded-tree receipt does not match operation lineage")
    if (
        receipt.decoded_apk_role != "stock_input"
        or receipt.admitted_replay_sha256 != admitted_replay_sha256
        or receipt.input_apk != input_apk
        or receipt.toolchain_profile_id != toolchain_profile_id
        or receipt.toolchain_profile_sha256 != toolchain_profile_sha256
        or receipt.role != "decode"
        or receipt.execution_plan_sha256 != execution_plan_sha256
        or receipt.executor_capability_sha256 != executor_capability_sha256
        or receipt.tool_artifact_sha256 != tool_artifact_sha256
        or receipt.execution_request_sha256 != execution_request_sha256
        or receipt.operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted replay decoded-tree receipt does not match admitted execution")
    manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
    if manifest.decoded_tree_sha256 != receipt.decoded_tree_semantic_sha256:
        raise ValueError("Decoded-tree receipt semantic SHA-256 does not match manifest")
    return receipt


def _validate_framework_cache_topology(
    receipt: ReplayFrameworkCacheReceiptV1,
) -> None:
    manifest = load_decoded_tree(runtime().store, receipt.framework_cache_manifest)
    if manifest.decoded_tree_sha256 != receipt.framework_cache_semantic_sha256:
        raise ValueError("Framework cache receipt semantic SHA-256 does not match manifest")
    expected_paths = tuple(
        sorted(
            (f"{item.package_id}.apk" for item in receipt.installations),
            key=lambda value: value.encode("utf-8"),
        )
    )
    if (
        tuple(entry.path for entry in manifest.entries) != expected_paths
        or any(entry.kind != "file" for entry in manifest.entries)
    ):
        raise ValueError("Framework cache topology does not exactly match installations")


def _framework_cache_snapshot(framework_fd: int) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    with os.scandir(framework_fd) as entries:
        names = sorted((entry.name for entry in entries), key=lambda value: value.encode("utf-8"))
    for name in names:
        metadata = os.stat(name, dir_fd=framework_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Framework cache contains an unsafe entry")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=framework_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("Framework cache entry changed while being opened")
            digest = hashlib.sha256()
            for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=framework_fd, follow_symlinks=False)
            stable = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if stable(metadata) != stable(after) or stable(metadata) != stable(current):
                raise ValueError("Framework cache entry changed while being read")
            snapshot[name] = digest.hexdigest()
        finally:
            os.close(descriptor)
    return snapshot


def _framework_installation_requests(
    admitted: AdmittedReplayV3,
) -> tuple[
    object,
    object,
    object,
    tuple[ReplayFrameworkInstallationV1, ...],
    tuple[ExecutionRequest, ...],
]:
    if not admitted.request.frameworks:
        raise ValueError("Replay framework installation requires nonempty frameworks")
    role = "install_framework"
    plan = admitted.plan(role)
    capability = admitted.capability(role)
    requirement = admitted.profile.tool_for_role(role)
    tool = next(
        item for item in admitted.request.tools if item.tool_id == requirement.tool_id
    )
    logical_paths = {
        "tool": "tool",
        "framework_dir": "framework",
    }
    requests = tuple(
        ExecutionRequest(
            1,
            capability.capability_id,
            capability.canonical_identity,
            framework.artifact,
            capability.output_kind,
            tuple(
                (
                    name,
                    f"framework-apks/{framework.package_id}.apk"
                    if slot == "framework_apk"
                    else logical_paths[slot],
                )
                for name, slot in plan.arguments
            ),
            (),
            admitted.run_spec.apk_composition,
        )
        for framework in admitted.request.frameworks
    )
    installations = tuple(
        ReplayFrameworkInstallationV1(
            framework.package_id,
            framework.artifact,
            request.canonical_identity,
        )
        for framework, request in zip(admitted.request.frameworks, requests, strict=True)
    )
    return plan, capability, tool, installations, requests


def _replay_framework_operation_identity(
    admitted: AdmittedReplayV3,
) -> tuple[
    str,
    str,
    tuple[ReplayFrameworkInstallationV1, ...],
    tuple[ExecutionRequest, ...],
]:
    plan, capability, tool, installations, requests = _framework_installation_requests(
        admitted
    )
    operation_input = {
        "schema_version": 1,
        "admitted_replay_sha256": admitted.sha256,
        "toolchain_profile_id": admitted.profile.profile_id,
        "toolchain_profile_sha256": admitted.profile.sha256,
        "role": "install_framework",
        "execution_plan_sha256": plan.sha256,
        "executor_capability_sha256": capability.canonical_identity,
        "tool_artifact_sha256": tool.artifact.sha256,
        "installations": installations,
    }
    return (
        operation_key("replay_install_frameworks_v1", operation_input),
        canonical_sha256(operation_input),
        installations,
        requests,
    )


def _validate_replay_framework_cache_receipt(
    output: ArtifactRef,
    key: str,
    *,
    admitted: AdmittedReplayV3,
    installations: tuple[ReplayFrameworkInstallationV1, ...],
) -> ReplayFrameworkCacheReceiptV1:
    if (
        output.kind != "replay-framework-cache-receipt-v1"
        or output.producer_operation_id != key
    ):
        raise ValueError("Adopted framework cache receipt does not match operation lineage")
    receipt = _strict_replay_framework_cache_receipt(runtime().store.read_bytes(output))
    plan = admitted.plan("install_framework")
    capability = admitted.capability("install_framework")
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("install_framework").tool_id
    )
    if (
        output.sha256 != receipt.sha256
        or output.input_hashes != receipt.receipt_input_hashes
        or receipt.admitted_replay_sha256 != admitted.sha256
        or receipt.toolchain_profile_id != admitted.profile.profile_id
        or receipt.toolchain_profile_sha256 != admitted.profile.sha256
        or receipt.role != "install_framework"
        or receipt.execution_plan_sha256 != plan.sha256
        or receipt.executor_capability_sha256 != capability.canonical_identity
        or receipt.tool_artifact_sha256 != tool.artifact.sha256
        or receipt.installations != installations
        or receipt.operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted framework cache receipt does not match admitted execution")
    _validate_framework_cache_topology(receipt)
    return receipt


def _replay_decode_operation_identity(
    admitted: AdmittedReplayV3,
    completed_framework_cache_receipt: ArtifactRef | None = None,
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None = None,
) -> tuple[str, str, str]:
    role = "decode"
    plan = admitted.plan(role)
    capability = admitted.capability(role)
    requirement = admitted.profile.tool_for_role(role)
    tool = next(
        tool for tool in admitted.request.tools if tool.tool_id == requirement.tool_id
    )
    logical_paths = {
        "tool": "tool",
        "framework_dir": "framework",
        "input_apk": "input.apk",
        "decoded_tree": "output",
    }
    request = ExecutionRequest(
        1,
        capability.capability_id,
        capability.canonical_identity,
        admitted.request.stock_apk,
        capability.output_kind,
        tuple((name, logical_paths[slot]) for name, slot in plan.arguments),
        (),
        admitted.run_spec.apk_composition,
    )
    operation_input = {
        "schema_version": 1,
        "decoded_apk_role": "stock_input",
        "admitted_replay_sha256": admitted.sha256,
        "input_apk": admitted.request.stock_apk,
        "toolchain_profile_id": admitted.profile.profile_id,
        "toolchain_profile_sha256": admitted.profile.sha256,
        "role": role,
        "execution_plan_sha256": plan.sha256,
        "executor_capability_sha256": capability.canonical_identity,
        "tool_artifact_sha256": tool.artifact.sha256,
        "execution_request_sha256": request.canonical_identity,
    }
    if admitted.request.frameworks:
        if completed_framework_cache_receipt is None or framework_receipt is None:
            raise ValueError("Framework-aware decode requires its completed framework cache")
        if "framework_dir" not in {slot for _, slot in plan.arguments}:
            raise ValueError("Framework-aware decode plan must consume the framework cache")
        operation_input.update(
            {
                "completed_framework_cache_receipt": completed_framework_cache_receipt,
                "framework_cache_manifest": framework_receipt.framework_cache_manifest,
                "framework_cache_semantic_sha256": framework_receipt.framework_cache_semantic_sha256,
            }
        )
    return (
        operation_key(
            "replay_decode_tree_v2" if admitted.request.frameworks else "replay_decode_tree_v1",
            operation_input,
        ),
        canonical_sha256(operation_input),
        request.canonical_identity,
    )


def _validate_replay_decoded_tree_receipt_v2(
    output: ArtifactRef,
    key: str,
    *,
    admitted: AdmittedReplayV3,
    execution_request_sha256: str,
    completed_framework_cache_receipt: ArtifactRef,
    framework_receipt: ReplayFrameworkCacheReceiptV1,
) -> ReplayDecodedTreeReceiptV2:
    if (
        output.kind != "replay-decoded-tree-receipt-v2"
        or output.producer_operation_id != key
    ):
        raise ValueError("Adopted replay decoded-tree V2 receipt does not match operation lineage")
    framework_key, _, installations, _ = _replay_framework_operation_identity(admitted)
    validated_framework_receipt = _validate_replay_framework_cache_receipt(
        completed_framework_cache_receipt,
        framework_key,
        admitted=admitted,
        installations=installations,
    )
    if validated_framework_receipt != framework_receipt:
        raise ValueError("Framework cache predecessor does not match validated receipt")
    receipt = _strict_replay_decoded_tree_receipt_v2(runtime().store.read_bytes(output))
    plan = admitted.plan("decode")
    capability = admitted.capability("decode")
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
    )
    if (
        output.sha256 != receipt.sha256
        or output.input_hashes != receipt.receipt_input_hashes
        or receipt.decoded_apk_role != "stock_input"
        or receipt.admitted_replay_sha256 != admitted.sha256
        or receipt.input_apk != admitted.request.stock_apk
        or receipt.toolchain_profile_id != admitted.profile.profile_id
        or receipt.toolchain_profile_sha256 != admitted.profile.sha256
        or receipt.role != "decode"
        or receipt.execution_plan_sha256 != plan.sha256
        or receipt.executor_capability_sha256 != capability.canonical_identity
        or receipt.tool_artifact_sha256 != tool.artifact.sha256
        or receipt.execution_request_sha256 != execution_request_sha256
        or receipt.completed_framework_cache_receipt
        != completed_framework_cache_receipt
        or receipt.framework_cache_manifest != framework_receipt.framework_cache_manifest
        or receipt.framework_cache_semantic_sha256
        != framework_receipt.framework_cache_semantic_sha256
        or receipt.operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted replay decoded-tree V2 receipt does not match admitted execution")
    manifest = load_decoded_tree(runtime().store, receipt.decoded_tree_manifest)
    if manifest.decoded_tree_sha256 != receipt.decoded_tree_semantic_sha256:
        raise ValueError("Decoded-tree receipt semantic SHA-256 does not match manifest")
    return receipt


def _strict_replay_patched_tree_receipt(data: bytes) -> ReplayPatchedTreeReceiptV1:
    def object_pairs(value: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, item in value:
            if name in result:
                raise ValueError(f"Duplicate JSON key: {name}")
            result[name] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValueError(f"Invalid JSON constant: {constant}")

    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
        result = ReplayPatchedTreeReceiptV1.from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("Invalid replay patched-tree receipt") from error
    if canonical_json(result).encode("utf-8") != data:
        raise ValueError("Replay patched-tree receipt is not canonical")
    return result


def _strict_replay_patched_apk_receipt(data: bytes) -> ReplayPatchedApkReceiptV1:
    return _strict_receipt_json(
        data, ReplayPatchedApkReceiptV1, "replay patched APK receipt"
    )  # type: ignore[return-value]


def _strict_replay_final_apk_verification_receipt(
    data: bytes,
) -> ReplayFinalApkVerificationReceiptV1:
    return _strict_receipt_json(
        data,
        ReplayFinalApkVerificationReceiptV1,
        "replay final APK verification receipt",
    )  # type: ignore[return-value]


async def _await_apply_mutation(task: asyncio.Task[ApplyReport]) -> ApplyReport:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done():
                cancellation = cancellation or error
                try:
                    task.result()
                except asyncio.CancelledError:
                    cancellation.add_note(
                        "Replay apply mutation task was cancelled unexpectedly"
                    )
                except BaseException as apply_error:
                    cancellation.add_note(
                        "Replay apply failed while cancellation raced with mutation: "
                        f"{type(apply_error).__name__}: {apply_error}"
                    )
                raise cancellation
            if cancellation is None:
                cancellation = error
            else:
                cancellation.add_note(
                    "Repeated cancellation waited for replay apply mutation"
                )
            continue
        except BaseException as error:
            if cancellation is None:
                raise
            cancellation.add_note(
                "Replay apply failed while cancellation waited for mutation: "
                f"{type(error).__name__}: {error}"
            )
            raise cancellation
        if cancellation is not None:
            raise cancellation
        return result


async def _await_backend_composition(task: asyncio.Task[BackendReport]) -> BackendReport:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done():
                cancellation = cancellation or error
                try:
                    task.result()
                except asyncio.CancelledError:
                    cancellation.add_note(
                        "Replay backend composition task was cancelled unexpectedly"
                    )
                except BaseException as composition_error:
                    cancellation.add_note(
                        "Replay backend composition failed while cancellation raced with "
                        f"composition: {type(composition_error).__name__}: {composition_error}"
                    )
                raise cancellation
            if cancellation is None:
                cancellation = error
            else:
                cancellation.add_note(
                    "Repeated cancellation waited for replay backend composition"
                )
            continue
        except BaseException as error:
            if cancellation is None:
                raise
            cancellation.add_note(
                "Replay backend composition failed while cancellation waited for "
                f"composition: {type(error).__name__}: {error}"
            )
            raise cancellation
        if cancellation is not None:
            raise cancellation
        return result


async def _await_verification_work(task: asyncio.Task[object]) -> object:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done():
                cancellation = cancellation or error
                try:
                    task.result()
                except asyncio.CancelledError:
                    cancellation.add_note("Replay verification work was cancelled unexpectedly")
                except BaseException as work_error:
                    cancellation.add_note(
                        "Replay verification failed while cancellation raced with work: "
                        f"{type(work_error).__name__}: {work_error}"
                    )
                raise cancellation
            if cancellation is None:
                cancellation = error
            else:
                cancellation.add_note("Repeated cancellation waited for replay verification work")
            continue
        except BaseException as error:
            if cancellation is None:
                raise
            cancellation.add_note(
                "Replay verification failed while cancellation waited for work: "
                f"{type(error).__name__}: {error}"
            )
            raise cancellation
        if cancellation is not None:
            raise cancellation
        return result


async def _await_verification_execution(task: asyncio.Task[object]) -> object:
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
                task.cancel()
            else:
                cancellation.add_note(
                    "Repeated cancellation waited for replay verification decoder cleanup"
                )
            if not task.done():
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as execution_error:
                cancellation.add_note(
                    "Replay verification decoder failed while cancellation waited for cleanup: "
                    f"{type(execution_error).__name__}: {execution_error}"
                )
            raise cancellation
        except BaseException as error:
            if cancellation is None:
                raise
            cancellation.add_note(
                "Replay verification decoder failed while cancellation waited for cleanup: "
                f"{type(error).__name__}: {error}"
            )
            raise cancellation
        if cancellation is not None:
            raise cancellation
        return result


def _open_pinned_regular(parent_fd: int, name: str, label: str) -> tuple[int, os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} must be a singly linked regular file")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _read_pinned_regular(
    parent_fd: int,
    name: str,
    descriptor: int,
    initial: os.stat_result,
    label: str,
) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    final = os.fstat(descriptor)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if (
        not stat.S_ISREG(current.st_mode)
        or initial.st_nlink != 1
        or final.st_nlink != 1
        or current.st_nlink != 1
        or identity(initial) != identity(final)
        or identity(initial) != identity(current)
    ):
        raise ValueError(f"{label} changed while being captured")
    data = b"".join(chunks)
    if len(data) != initial.st_size:
        raise ValueError(f"{label} changed while being captured")
    return data


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _node_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _secure_remove_tree_entry(
    parent_fd: int,
    name: str,
    label: str,
    *,
    expected: os.stat_result | None = None,
) -> None:
    initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if expected is not None and _stable_file_identity(initial) != _stable_file_identity(
        expected
    ):
        raise ValueError(f"{label} changed before cleanup")
    if stat.S_ISDIR(initial.st_mode):
        child_fd = _open_existing_directory(parent_fd, name)
        try:
            if _stable_file_identity(os.fstat(child_fd)) != _stable_file_identity(initial):
                raise ValueError(f"{label} changed while being opened")
            with os.scandir(child_fd) as entries:
                names = sorted(
                    (entry.name for entry in entries), key=lambda value: value.encode("utf-8")
                )
            for child_name in names:
                _secure_remove_tree_entry(child_fd, child_name, label)
            with os.scandir(child_fd) as entries:
                if next(entries, None) is not None:
                    raise ValueError(f"{label} changed during cleanup")
            final = os.fstat(child_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _node_identity(final) != _node_identity(initial)
                or _node_identity(current) != _node_identity(initial)
            ):
                raise ValueError(f"{label} changed during cleanup")
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
        return
    if stat.S_ISLNK(initial.st_mode):
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stable_file_identity(current) != _stable_file_identity(initial):
            raise ValueError(f"{label} changed during cleanup")
        os.unlink(name, dir_fd=parent_fd)
        return
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ValueError(f"{label} contains an unsafe entry")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        if _stable_file_identity(os.fstat(descriptor)) != _stable_file_identity(initial):
            raise ValueError(f"{label} changed while being opened")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stable_file_identity(current) != _stable_file_identity(initial):
            raise ValueError(f"{label} changed during cleanup")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def _secure_remove_optional_build_tree(patched_tree_fd: int) -> None:
    try:
        initial = os.stat("build", dir_fd=patched_tree_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _secure_remove_tree_entry(
        patched_tree_fd,
        "build",
        "Patched-tree build output",
        expected=initial,
    )


def _secure_unlink_framework_one(framework_fd: int) -> None:
    expected = os.stat("1.apk", dir_fd=framework_fd, follow_symlinks=False)
    descriptor, initial = _open_pinned_regular(
        framework_fd, "1.apk", "Generated framework APK"
    )
    try:
        current = os.stat("1.apk", dir_fd=framework_fd, follow_symlinks=False)
        if (
            _stable_file_identity(initial) != _stable_file_identity(expected)
            or _stable_file_identity(os.fstat(descriptor)) != _stable_file_identity(initial)
            or _stable_file_identity(current) != _stable_file_identity(initial)
        ):
            raise ValueError("Generated framework APK changed during cleanup")
        os.unlink("1.apk", dir_fd=framework_fd)
    finally:
        os.close(descriptor)


def _validate_replay_patched_tree_receipt(
    output: ArtifactRef,
    key: str,
    *,
    admitted: AdmittedReplayV3,
    completed_decode_receipt: ArtifactRef,
    decoded_receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2,
    compiled: TargetPortSpecV2,
) -> ReplayPatchedTreeReceiptV1:
    if (
        output.kind != "replay-patched-tree-receipt-v1"
        or output.producer_operation_id != key
    ):
        raise ValueError("Adopted replay patched-tree receipt does not match operation lineage")
    receipt = _strict_replay_patched_tree_receipt(runtime().store.read_bytes(output))
    if output.sha256 != receipt.sha256 or output.input_hashes != receipt.receipt_input_hashes:
        raise ValueError("Adopted replay patched-tree receipt does not match operation lineage")
    expected_ids = tuple(operation.operation_id for operation in compiled.operations)
    result_ids = tuple(result.operation_id for result in receipt.operation_results)
    apply_report = ApplyReport(
        tuple(OperationResult(result.operation_id, result.status) for result in receipt.operation_results)
    )
    if (
        receipt.admitted_replay_sha256 != admitted.sha256
        or receipt.completed_decode_receipt != completed_decode_receipt
        or receipt.input_decoded_tree_manifest != decoded_receipt.decoded_tree_manifest
        or receipt.input_decoded_tree_semantic_sha256
        != decoded_receipt.decoded_tree_semantic_sha256
        or receipt.intent_sha256 != admitted.intent.sha256
        or receipt.resolution_sha256 != admitted.resolution.sha256
        or receipt.source_manifest_sha256 != admitted.source_manifest.sha256
        or receipt.target_port_spec_sha256 != compiled.sha256
        or receipt.source_admission.admitted_replay_sha256 != admitted.sha256
        or receipt.source_admission.source_manifest_sha256
        != admitted.source_manifest.sha256
        or receipt.source_admission.file_count
        != len(admitted.source_manifest.records)
        or result_ids != expected_ids
        or receipt.apply_report_sha256 != apply_report.sha256
        or receipt.operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted replay patched-tree receipt does not match admitted execution")
    manifest = load_decoded_tree(runtime().store, receipt.patched_tree_manifest)
    if manifest.decoded_tree_sha256 != receipt.patched_tree_semantic_sha256:
        raise ValueError("Patched-tree receipt semantic SHA-256 does not match manifest")
    return receipt


def _replay_build_predecessors(
    admitted: AdmittedReplayV3,
) -> tuple[
    ArtifactRef | None,
    ReplayFrameworkCacheReceiptV1 | None,
    ArtifactRef,
    ReplayPatchedTreeReceiptV1,
    TargetPortSpecV2,
]:
    configured = runtime()
    completed_framework: ArtifactRef | None = None
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None = None
    if admitted.request.frameworks:
        framework_key, framework_input, installations, _ = (
            _replay_framework_operation_identity(admitted)
        )
        completed_framework = Ledger.require_completed_operation(
            configured.ledger,
            framework_key,
            "replay_install_frameworks_v1",
            framework_input,
        )
        framework_receipt = _validate_replay_framework_cache_receipt(
            completed_framework,
            framework_key,
            admitted=admitted,
            installations=installations,
        )

    decode_key, decode_input, decode_request = _replay_decode_operation_identity(
        admitted, completed_framework, framework_receipt
    )
    completed_decode = Ledger.require_completed_operation(
        configured.ledger,
        decode_key,
        "replay_decode_tree_v2" if framework_receipt is not None else "replay_decode_tree_v1",
        decode_input,
    )
    if framework_receipt is None or completed_framework is None:
        decoded_receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2 = (
            _validate_replay_decoded_tree_receipt(
                completed_decode,
                decode_key,
                admitted_replay_sha256=admitted.sha256,
                input_apk=admitted.request.stock_apk,
                toolchain_profile_id=admitted.profile.profile_id,
                toolchain_profile_sha256=admitted.profile.sha256,
                execution_plan_sha256=admitted.plan("decode").sha256,
                executor_capability_sha256=admitted.capability("decode").canonical_identity,
                tool_artifact_sha256=next(
                    item.artifact.sha256
                    for item in admitted.request.tools
                    if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
                ),
                execution_request_sha256=decode_request,
            )
        )
    else:
        decoded_receipt = _validate_replay_decoded_tree_receipt_v2(
            completed_decode,
            decode_key,
            admitted=admitted,
            execution_request_sha256=decode_request,
            completed_framework_cache_receipt=completed_framework,
            framework_receipt=framework_receipt,
        )

    compiled = compile_port(admitted.intent, admitted.resolution)
    if type(compiled) is not TargetPortSpecV2:
        raise TypeError("Replay build requires an exact TargetPortSpecV2")
    apply_input = {
        "schema_version": 1,
        "admitted_replay_sha256": admitted.sha256,
        "completed_decode_receipt": completed_decode,
        "input_decoded_tree_manifest": decoded_receipt.decoded_tree_manifest,
        "input_decoded_tree_semantic_sha256": decoded_receipt.decoded_tree_semantic_sha256,
        "intent_sha256": admitted.intent.sha256,
        "resolution_sha256": admitted.resolution.sha256,
        "source_manifest_sha256": admitted.source_manifest.sha256,
        "target_port_spec_sha256": compiled.sha256,
    }
    apply_key = operation_key("replay_apply_tree_v1", apply_input)
    completed_apply = Ledger.require_completed_operation(
        configured.ledger,
        apply_key,
        "replay_apply_tree_v1",
        canonical_sha256(apply_input),
    )
    patched_receipt = _validate_replay_patched_tree_receipt(
        completed_apply,
        apply_key,
        admitted=admitted,
        completed_decode_receipt=completed_decode,
        decoded_receipt=decoded_receipt,
        compiled=compiled,
    )
    return completed_framework, framework_receipt, completed_apply, patched_receipt, compiled


def _replay_build_operation_identity(
    admitted: AdmittedReplayV3,
    completed_patched_tree_receipt: ArtifactRef,
    patched_receipt: ReplayPatchedTreeReceiptV1,
    compiled: TargetPortSpecV2,
    completed_framework_cache_receipt: ArtifactRef | None,
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None,
) -> tuple[str, str, ExecutionRequest]:
    plan = admitted.plan("build")
    capability = admitted.capability("build")
    if capability.output_kind != "intermediate-apk":
        raise ValueError("Replay build capability must produce an intermediate APK")
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("build").tool_id
    )
    logical_paths = {
        "tool": "tool",
        "framework_dir": "framework",
        "decoded_tree": "patched-tree",
        "intermediate_apk": "intermediate.apk",
    }
    request = ExecutionRequest(
        1,
        capability.capability_id,
        capability.canonical_identity,
        patched_receipt.patched_tree_manifest,
        "intermediate-apk",
        tuple((name, logical_paths[slot]) for name, slot in plan.arguments),
        (),
        admitted.run_spec.apk_composition,
    )
    operation_input: dict[str, object] = {
        "schema_version": 1,
        "admitted_replay_sha256": admitted.sha256,
        "completed_patched_tree_receipt": completed_patched_tree_receipt,
        "patched_tree_manifest": patched_receipt.patched_tree_manifest,
        "patched_tree_semantic_sha256": patched_receipt.patched_tree_semantic_sha256,
        "target_port_spec_sha256": compiled.sha256,
        "backend_kind": compiled.backend.kind,
        "backend_profile_id": compiled.backend.profile_id,
        "backend_sha256": canonical_sha256(compiled.backend),
        "stock_apk": admitted.request.stock_apk,
        "toolchain_profile_id": admitted.profile.profile_id,
        "toolchain_profile_sha256": admitted.profile.sha256,
        "role": "build",
        "execution_plan_sha256": plan.sha256,
        "executor_capability_sha256": capability.canonical_identity,
        "tool_artifact_sha256": tool.artifact.sha256,
        "execution_request_sha256": request.canonical_identity,
    }
    if completed_framework_cache_receipt is not None and framework_receipt is not None:
        operation_input.update(
            {
                "completed_framework_cache_receipt": completed_framework_cache_receipt,
                "framework_cache_manifest": framework_receipt.framework_cache_manifest,
                "framework_cache_semantic_sha256": framework_receipt.framework_cache_semantic_sha256,
            }
        )
    return (
        operation_key("replay_build_patched_apk_v1", operation_input),
        canonical_sha256(operation_input),
        request,
    )


def _expected_replay_build_mutation_paths(
    admitted: AdmittedReplayV3,
) -> tuple[str, ...]:
    return (
        ("intermediate.apk", "patched-tree/build")
        if admitted.request.frameworks
        else ("framework/1.apk", "intermediate.apk", "patched-tree/build")
    )


def _replay_backend_composition(
    report: BackendReport, compiled: TargetPortSpecV2
) -> ReplayBackendCompositionV1:
    if type(report) is not BackendReport:
        raise TypeError("Replay backend validation must return an exact BackendReport")
    backend = compiled.backend
    expected_replaced = getattr(backend, "replace_dex_entries", ())
    expected_added = getattr(backend, "add_dex_entries", ())
    if (
        report.kind != backend.kind
        or report.final_dex_entries != backend.final_dex_entries
        or report.replaced_entries != expected_replaced
        or report.added_entries != expected_added
        or report.passed is not True
    ):
        raise ValueError("Backend report does not exactly match the compiled backend")
    return ReplayBackendCompositionV1(
        1,
        backend.kind,
        backend.profile_id,
        canonical_sha256(backend),
        report.stock_sha256,
        report.intermediate_sha256,
        report.output_sha256,
        report.final_dex_entries,
        report.replaced_entries,
        report.added_entries,
        report.retained_entry_count,
        report.stripped_signature_entries,
        True,
    )


def _validate_replay_patched_apk_receipt(
    output: ArtifactRef,
    key: str,
    *,
    admitted: AdmittedReplayV3,
    completed_patched_tree_receipt: ArtifactRef,
    patched_receipt: ReplayPatchedTreeReceiptV1,
    compiled: TargetPortSpecV2,
    execution_request: ExecutionRequest,
    completed_framework_cache_receipt: ArtifactRef | None,
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None,
) -> ReplayPatchedApkReceiptV1:
    configured = runtime()
    if output.kind != "replay-patched-apk-receipt-v1" or output.producer_operation_id != key:
        raise ValueError("Adopted replay patched APK receipt does not match operation lineage")
    receipt = _strict_replay_patched_apk_receipt(configured.store.read_bytes(output))
    plan = admitted.plan("build")
    capability = admitted.capability("build")
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("build").tool_id
    )
    expected_framework = (
        completed_framework_cache_receipt,
        None if framework_receipt is None else framework_receipt.framework_cache_manifest,
        None if framework_receipt is None else framework_receipt.framework_cache_semantic_sha256,
    )
    actual_framework = (
        receipt.completed_framework_cache_receipt,
        receipt.framework_cache_manifest,
        receipt.framework_cache_semantic_sha256,
    )
    if (
        output.sha256 != receipt.sha256
        or output.input_hashes != receipt.receipt_input_hashes
        or receipt.admitted_replay_sha256 != admitted.sha256
        or receipt.completed_patched_tree_receipt != completed_patched_tree_receipt
        or receipt.patched_tree_manifest != patched_receipt.patched_tree_manifest
        or receipt.patched_tree_semantic_sha256 != patched_receipt.patched_tree_semantic_sha256
        or receipt.target_port_spec_sha256 != compiled.sha256
        or receipt.stock_apk != admitted.request.stock_apk
        or receipt.toolchain_profile_id != admitted.profile.profile_id
        or receipt.toolchain_profile_sha256 != admitted.profile.sha256
        or receipt.role != "build"
        or receipt.execution_plan_sha256 != plan.sha256
        or receipt.executor_capability_sha256 != capability.canonical_identity
        or receipt.tool_artifact_sha256 != tool.artifact.sha256
        or receipt.execution_request_sha256 != execution_request.canonical_identity
        or actual_framework != expected_framework
        or receipt.operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted replay patched APK receipt does not match admitted execution")
    if (
        receipt.composition.backend_kind != compiled.backend.kind
        or receipt.composition.backend_profile_id != compiled.backend.profile_id
        or receipt.composition.backend_sha256 != canonical_sha256(compiled.backend)
        or receipt.composition.final_dex_entries != compiled.backend.final_dex_entries
        or receipt.composition.replaced_entries
        != getattr(compiled.backend, "replace_dex_entries", ())
        or receipt.composition.added_entries
        != getattr(compiled.backend, "add_dex_entries", ())
    ):
        raise ValueError("Adopted backend composition does not match compiled backend")

    stock_bytes = configured.store.read_bytes(receipt.stock_apk)
    intermediate_bytes = configured.store.read_bytes(receipt.intermediate_apk)
    final_bytes = configured.store.read_bytes(receipt.patched_apk)
    report = validate_composed_apk_bytes(
        compiled.backend, stock_bytes, intermediate_bytes, final_bytes
    )
    composition = _replay_backend_composition(report, compiled)
    if composition != receipt.composition:
        raise ValueError("Adopted backend composition is not independently reproducible")
    return receipt


def _adopt_existing(
    key: str,
    existing: ArtifactRef | None,
    *,
    expected_kind: str,
    expected_input_hashes: tuple[str, ...],
) -> ArtifactRef | None:
    if existing is None:
        return None
    if (
        existing.kind != expected_kind
        or existing.producer_operation_id != key
        or existing.input_hashes != expected_input_hashes
    ):
        raise ValueError("Adopted artifact does not match operation lineage")
    runtime().store.read_bytes(existing)
    return runtime().ledger.complete_operation(key, existing)


@activity.defn
async def replay_install_frameworks_checkpoint_activity(
    candidate: AdmittedReplayV3,
) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.require_admitted_replay_v3(configured.ledger, candidate)
    key, operation_input_sha256, installations, requests = (
        _replay_framework_operation_identity(admitted)
    )
    plan = admitted.plan("install_framework")
    capability = admitted.capability("install_framework")
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("install_framework").tool_id
    )
    admitted_spec = RunSpec(
        1,
        admitted.run_spec.run_id,
        admitted.run_spec.subject_sha256,
        admitted.run_spec.intent_sha256,
        admitted.run_spec.resolution_sha256,
        capability.canonical_identity,
        admitted.run_spec.policy_revision,
        admitted.run_spec.allowed_actor,
        1,
        admitted.run_spec.apk_composition,
        False,
        0,
    )
    owner = _activity_owner()
    operation_claimed = False
    existing = Ledger.begin_operation(
        configured.ledger,
        key,
        "replay_install_frameworks_v1",
        operation_input_sha256,
        owner,
        retry_safe=False,
    )
    if existing is not None:
        _validate_replay_framework_cache_receipt(
            existing,
            key,
            admitted=admitted,
            installations=installations,
        )
        return Ledger.complete_operation(configured.ledger, key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        try:
            executable = configured.executor_paths[capability.executable_sha256]
        except KeyError as error:
            raise ValueError("No runtime executable for admitted capability") from error
        _validate_runtime_executable(executable, capability.executable_sha256)

        tool_bytes = configured.store.read_bytes(tool.artifact)
        framework_bytes = tuple(
            configured.store.read_bytes(item.framework_apk) for item in installations
        )
        execution_input_hashes = (
            admitted.sha256,
            admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            *(canonical_sha256(item) for item in installations),
        )

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
        framework_apks_fd: int | None = None
        framework_fd: int | None = None
        try:
            try:
                os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
            except FileExistsError:
                pass
            operation_fd = _open_existing_directory(attempts_fd, key)
            owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            workspace_fd = _exclusive_directory(operation_fd, owner_hash)
            workspace = configured.attempts_root / key / owner_hash
            workspace_created = True
            framework_apks_fd = _exclusive_directory(workspace_fd, "framework-apks")
            framework_fd = _exclusive_directory(workspace_fd, "framework")
            _exclusive_file(workspace_fd, "tool", tool_bytes)
            for installation, data in zip(installations, framework_bytes, strict=True):
                _exclusive_file(
                    framework_apks_fd,
                    f"{installation.package_id}.apk",
                    data,
                )
            _verify_workspace_path(workspace, workspace_fd)

            cache_snapshot: dict[str, str] = {}
            for installation, request in zip(installations, requests, strict=True):
                execution = await execute(
                    capability,
                    request,
                    ExecutionMetadata(executable, workspace, workspace),
                    admitted_spec=admitted_spec,
                    timeout_seconds=plan.timeout_seconds,
                    launcher=configured.launcher,
                )
                if execution.returncode != 0:
                    raise RuntimeError(
                        "Replay framework installation "
                        f"{installation.package_id} failed with exit code {execution.returncode}"
                    )
                _verify_workspace_path(workspace, workspace_fd)
                _verify_workspace_path(workspace / "framework", framework_fd)
                current_snapshot = _framework_cache_snapshot(framework_fd)
                expected_name = f"{installation.package_id}.apk"
                if (
                    expected_name in cache_snapshot
                    or set(current_snapshot) != {*cache_snapshot, expected_name}
                    or any(
                        current_snapshot[name] != digest
                        for name, digest in cache_snapshot.items()
                    )
                ):
                    raise ValueError(
                        "Framework installation did not add exactly its declared package id"
                    )
                cache_snapshot = current_snapshot

            manifest_ref = capture_decoded_tree_fd(
                configured.store,
                framework_fd,
                key,
                execution_input_hashes,
            )
        finally:
            _close_descriptors(
                framework_fd,
                framework_apks_fd,
                workspace_fd,
                operation_fd,
                attempts_fd,
            )

        manifest = load_decoded_tree(configured.store, manifest_ref)
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
        _validate_framework_cache_topology(receipt)
        output = configured.store.put_bytes(
            kind="replay-framework-cache-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        _validate_replay_framework_cache_receipt(
            output,
            key,
            admitted=admitted,
            installations=installations,
        )
        Ledger.record_effect(configured.ledger, key, owner, output)
        effect_recorded = True
        return Ledger.complete_operation(configured.ledger, key, output)
    except asyncio.CancelledError:
        if not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            try:
                Ledger.release_pending_operation(configured.ledger, key, owner)
            except BaseException as release_error:
                error.add_note(
                    "Pending operation release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )
        raise


@activity.defn
async def replay_decode_checkpoint_activity(candidate: AdmittedReplayV3) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.require_admitted_replay_v3(configured.ledger, candidate)

    role = "decode"
    plan = admitted.plan(role)
    capability = admitted.capability(role)
    requirement = admitted.profile.tool_for_role(role)
    tool = next(
        tool for tool in admitted.request.tools if tool.tool_id == requirement.tool_id
    )
    logical_paths = {
        "tool": "tool",
        "framework_dir": "framework",
        "input_apk": "input.apk",
        "decoded_tree": "output",
    }
    request = ExecutionRequest(
        1,
        capability.capability_id,
        capability.canonical_identity,
        admitted.request.stock_apk,
        capability.output_kind,
        tuple((name, logical_paths[slot]) for name, slot in plan.arguments),
        (),
        admitted.run_spec.apk_composition,
    )
    admitted_spec = RunSpec(
        1,
        admitted.run_spec.run_id,
        admitted.run_spec.subject_sha256,
        admitted.run_spec.intent_sha256,
        admitted.run_spec.resolution_sha256,
        capability.canonical_identity,
        admitted.run_spec.policy_revision,
        admitted.run_spec.allowed_actor,
        1,
        admitted.run_spec.apk_composition,
        False,
        0,
    )
    tool_artifact_sha256 = tool.artifact.sha256
    completed_framework_cache_receipt: ArtifactRef | None = None
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None = None
    if admitted.request.frameworks:
        (
            framework_key,
            framework_input_sha256,
            installations,
            _,
        ) = _replay_framework_operation_identity(admitted)
        completed_framework_cache_receipt = Ledger.require_completed_operation(
            configured.ledger,
            framework_key,
            "replay_install_frameworks_v1",
            framework_input_sha256,
        )
        framework_receipt = _validate_replay_framework_cache_receipt(
            completed_framework_cache_receipt,
            framework_key,
            admitted=admitted,
            installations=installations,
        )
    key, operation_input_sha256, _ = _replay_decode_operation_identity(
        admitted,
        completed_framework_cache_receipt,
        framework_receipt,
    )
    execution_input_hashes = (
        admitted.sha256,
        canonical_sha256(admitted.request.stock_apk),
        admitted.profile.sha256,
        plan.sha256,
        capability.canonical_identity,
        tool_artifact_sha256,
        request.canonical_identity,
    )
    if framework_receipt is not None and completed_framework_cache_receipt is not None:
        execution_input_hashes = (
            *execution_input_hashes,
            canonical_sha256(completed_framework_cache_receipt),
            canonical_sha256(framework_receipt.framework_cache_manifest),
            framework_receipt.framework_cache_semantic_sha256,
        )
    owner = _activity_owner()
    operation_claimed = False
    decode_kind = (
        "replay_decode_tree_v2" if framework_receipt is not None else "replay_decode_tree_v1"
    )
    existing = Ledger.begin_operation(
        configured.ledger,
        key,
        decode_kind,
        operation_input_sha256,
        owner,
        retry_safe=False,
    )
    if existing is not None:
        if framework_receipt is None or completed_framework_cache_receipt is None:
            _validate_replay_decoded_tree_receipt(
                existing,
                key,
                admitted_replay_sha256=admitted.sha256,
                input_apk=admitted.request.stock_apk,
                toolchain_profile_id=admitted.profile.profile_id,
                toolchain_profile_sha256=admitted.profile.sha256,
                execution_plan_sha256=plan.sha256,
                executor_capability_sha256=capability.canonical_identity,
                tool_artifact_sha256=tool_artifact_sha256,
                execution_request_sha256=request.canonical_identity,
            )
        else:
            _validate_replay_decoded_tree_receipt_v2(
                existing,
                key,
                admitted=admitted,
                execution_request_sha256=request.canonical_identity,
                completed_framework_cache_receipt=completed_framework_cache_receipt,
                framework_receipt=framework_receipt,
            )
        return Ledger.complete_operation(configured.ledger, key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        try:
            executable = configured.executor_paths[capability.executable_sha256]
        except KeyError as error:
            raise ValueError("No runtime executable for admitted capability") from error
        _validate_runtime_executable(executable, capability.executable_sha256)

        stock_bytes = configured.store.read_bytes(admitted.request.stock_apk)
        tool_bytes = configured.store.read_bytes(tool.artifact)
        uses_tool_path = any(slot == "tool" for _, slot in plan.arguments)

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
        framework_fd: int | None = None
        output_fd: int | None = None
        try:
            try:
                os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
            except FileExistsError:
                pass
            operation_fd = _open_existing_directory(attempts_fd, key)
            owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            workspace_fd = _exclusive_directory(operation_fd, owner_hash)
            workspace = configured.attempts_root / key / owner_hash
            workspace_created = True
            _exclusive_file(workspace_fd, "input.apk", stock_bytes)
            if uses_tool_path:
                _exclusive_file(workspace_fd, "tool", tool_bytes)
            if any(slot == "framework_dir" for _, slot in plan.arguments):
                if framework_receipt is None:
                    os.mkdir("framework", mode=0o700, dir_fd=workspace_fd)
                else:
                    materialize_decoded_tree(
                        configured.store,
                        framework_receipt.framework_cache_manifest,
                        workspace,
                        "framework",
                    )
                framework_fd = _open_existing_directory(workspace_fd, "framework")
            try:
                os.stat("output", dir_fd=workspace_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Replay decoded-tree output must be absent before launch")
            _verify_workspace_path(workspace, workspace_fd)
            if framework_receipt is not None:
                assert framework_fd is not None
                _verify_workspace_path(workspace / "framework", framework_fd)

            execution = await execute(
                capability,
                request,
                ExecutionMetadata(executable, workspace, workspace),
                admitted_spec=admitted_spec,
                timeout_seconds=plan.timeout_seconds,
                launcher=configured.launcher,
            )
            if execution.returncode != 0:
                raise RuntimeError(f"Replay decode failed with exit code {execution.returncode}")
            if framework_receipt is not None:
                assert framework_fd is not None
                _verify_workspace_path(workspace / "framework", framework_fd)
                framework_manifest = load_decoded_tree(
                    configured.store,
                    framework_receipt.framework_cache_manifest,
                )
                verify_materialized_decoded_tree(
                    framework_manifest,
                    workspace / "framework",
                )
            output_fd = _open_existing_directory(workspace_fd, "output")
            manifest_ref = capture_decoded_tree_fd(
                configured.store,
                output_fd,
                key,
                execution_input_hashes,
            )
        finally:
            _close_descriptors(
                output_fd,
                framework_fd,
                workspace_fd,
                operation_fd,
                attempts_fd,
            )
        manifest = load_decoded_tree(configured.store, manifest_ref)
        if framework_receipt is None or completed_framework_cache_receipt is None:
            receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2 = (
                ReplayDecodedTreeReceiptV1(
                    1,
                    "stock_input",
                    admitted.sha256,
                    admitted.request.stock_apk,
                    admitted.profile.profile_id,
                    admitted.profile.sha256,
                    role,
                    plan.sha256,
                    capability.canonical_identity,
                    tool_artifact_sha256,
                    request.canonical_identity,
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
                role,
                plan.sha256,
                capability.canonical_identity,
                tool_artifact_sha256,
                request.canonical_identity,
                completed_framework_cache_receipt,
                framework_receipt.framework_cache_manifest,
                framework_receipt.framework_cache_semantic_sha256,
                manifest_ref,
                manifest.decoded_tree_sha256,
                key,
                True,
            )
        output = configured.store.put_bytes(
            kind=(
                "replay-decoded-tree-receipt-v2"
                if framework_receipt is not None
                else "replay-decoded-tree-receipt-v1"
            ),
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        if framework_receipt is None or completed_framework_cache_receipt is None:
            _validate_replay_decoded_tree_receipt(
                output,
                key,
                admitted_replay_sha256=admitted.sha256,
                input_apk=admitted.request.stock_apk,
                toolchain_profile_id=admitted.profile.profile_id,
                toolchain_profile_sha256=admitted.profile.sha256,
                execution_plan_sha256=plan.sha256,
                executor_capability_sha256=capability.canonical_identity,
                tool_artifact_sha256=tool_artifact_sha256,
                execution_request_sha256=request.canonical_identity,
            )
        else:
            _validate_replay_decoded_tree_receipt_v2(
                output,
                key,
                admitted=admitted,
                execution_request_sha256=request.canonical_identity,
                completed_framework_cache_receipt=completed_framework_cache_receipt,
                framework_receipt=framework_receipt,
            )
        Ledger.record_effect(configured.ledger, key, owner, output)
        effect_recorded = True
        return Ledger.complete_operation(configured.ledger, key, output)
    except asyncio.CancelledError:
        if not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            try:
                Ledger.release_pending_operation(configured.ledger, key, owner)
            except BaseException as release_error:
                error.add_note(
                    "Pending operation release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )
        raise


@activity.defn
async def replay_apply_tree_checkpoint_activity(candidate: AdmittedReplayV3) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.require_admitted_replay_v3(configured.ledger, candidate)

    completed_framework_cache_receipt: ArtifactRef | None = None
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None = None
    if admitted.request.frameworks:
        (
            framework_key,
            framework_input_sha256,
            installations,
            _,
        ) = _replay_framework_operation_identity(admitted)
        completed_framework_cache_receipt = Ledger.require_completed_operation(
            configured.ledger,
            framework_key,
            "replay_install_frameworks_v1",
            framework_input_sha256,
        )
        framework_receipt = _validate_replay_framework_cache_receipt(
            completed_framework_cache_receipt,
            framework_key,
            admitted=admitted,
            installations=installations,
        )
    (
        decode_key,
        decode_input_sha256,
        decode_execution_request_sha256,
    ) = _replay_decode_operation_identity(
        admitted,
        completed_framework_cache_receipt,
        framework_receipt,
    )
    completed_decode_receipt = Ledger.require_completed_operation(
        configured.ledger,
        decode_key,
        "replay_decode_tree_v2" if framework_receipt is not None else "replay_decode_tree_v1",
        decode_input_sha256,
    )
    if framework_receipt is None or completed_framework_cache_receipt is None:
        decoded_receipt: ReplayDecodedTreeReceiptV1 | ReplayDecodedTreeReceiptV2 = (
            _validate_replay_decoded_tree_receipt(
                completed_decode_receipt,
                decode_key,
                admitted_replay_sha256=admitted.sha256,
                input_apk=admitted.request.stock_apk,
                toolchain_profile_id=admitted.profile.profile_id,
                toolchain_profile_sha256=admitted.profile.sha256,
                execution_plan_sha256=admitted.plan("decode").sha256,
                executor_capability_sha256=admitted.capability("decode").canonical_identity,
                tool_artifact_sha256=next(
                    tool.artifact.sha256
                    for tool in admitted.request.tools
                    if tool.tool_id == admitted.profile.tool_for_role("decode").tool_id
                ),
                execution_request_sha256=decode_execution_request_sha256,
            )
        )
    else:
        decoded_receipt = _validate_replay_decoded_tree_receipt_v2(
            completed_decode_receipt,
            decode_key,
            admitted=admitted,
            execution_request_sha256=decode_execution_request_sha256,
            completed_framework_cache_receipt=completed_framework_cache_receipt,
            framework_receipt=framework_receipt,
        )
    compiled = compile_port(admitted.intent, admitted.resolution)
    if type(compiled) is not TargetPortSpecV2:
        raise TypeError("Replay apply requires an exact TargetPortSpecV2")

    operation_input = {
        "schema_version": 1,
        "admitted_replay_sha256": admitted.sha256,
        "completed_decode_receipt": completed_decode_receipt,
        "input_decoded_tree_manifest": decoded_receipt.decoded_tree_manifest,
        "input_decoded_tree_semantic_sha256": decoded_receipt.decoded_tree_semantic_sha256,
        "intent_sha256": admitted.intent.sha256,
        "resolution_sha256": admitted.resolution.sha256,
        "source_manifest_sha256": admitted.source_manifest.sha256,
        "target_port_spec_sha256": compiled.sha256,
    }
    key = operation_key("replay_apply_tree_v1", operation_input)
    owner = _activity_owner()
    operation_claimed = False
    existing = Ledger.begin_operation(
        configured.ledger,
        key,
        "replay_apply_tree_v1",
        canonical_sha256(operation_input),
        owner,
        retry_safe=False,
    )
    if existing is not None:
        _validate_replay_patched_tree_receipt(
            existing,
            key,
            admitted=admitted,
            completed_decode_receipt=completed_decode_receipt,
            decoded_receipt=decoded_receipt,
            compiled=compiled,
        )
        return Ledger.complete_operation(configured.ledger, key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        if configured.source_root is None:
            raise ValueError("Replay apply requires a configured source root")

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
        work_tree_fd: int | None = None
        try:
            try:
                os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
            except FileExistsError:
                pass
            operation_fd = _open_existing_directory(attempts_fd, key)
            owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            workspace_fd = _exclusive_directory(operation_fd, owner_hash)
            workspace = configured.attempts_root / key / owner_hash
            workspace_created = True
            _verify_workspace_path(workspace, workspace_fd)

            work_tree = materialize_decoded_tree(
                configured.store,
                decoded_receipt.decoded_tree_manifest,
                workspace,
                "work-tree",
            )
            work_tree_fd = _open_existing_directory(workspace_fd, "work-tree")
            source_report = admit_source_bundle_v2(
                admitted,
                configured.source_root,
                workspace,
                configured.ledger,
            )
            staged_source = workspace / source_report.relative_destination
            verify_staged_source_v2(
                source_report,
                admitted,
                staged_source,
                configured.ledger,
            )
            source_evidence = ReplaySourceAdmissionEvidenceV1.from_dict(
                asdict(source_report)
            )
            _verify_workspace_path(workspace, workspace_fd)
            _verify_workspace_path(work_tree, work_tree_fd)

            apply_task = asyncio.create_task(
                asyncio.to_thread(apply_port, compiled, work_tree, staged_source)
            )
            apply_report = await _await_apply_mutation(apply_task)
            if type(apply_report) is not ApplyReport:
                raise TypeError("Replay apply must return an exact ApplyReport")
            expected_ids = tuple(operation.operation_id for operation in compiled.operations)
            result_ids = tuple(result.operation_id for result in apply_report.results)
            if result_ids != expected_ids:
                raise ValueError("Replay apply results do not match compiled operation order")
            _verify_workspace_path(workspace, workspace_fd)
            _verify_workspace_path(work_tree, work_tree_fd)

            operation_results = tuple(
                ReplayApplyOperationResultV1(result.operation_id, result.status)
                for result in apply_report.results
            )
            execution_input_hashes = (
                admitted.sha256,
                canonical_sha256(completed_decode_receipt),
                canonical_sha256(decoded_receipt.decoded_tree_manifest),
                decoded_receipt.decoded_tree_semantic_sha256,
                admitted.intent.sha256,
                admitted.resolution.sha256,
                admitted.source_manifest.sha256,
                compiled.sha256,
                source_report.sha256,
                apply_report.sha256,
            )
            await asyncio.sleep(0)
            manifest_ref = capture_decoded_tree_fd(
                configured.store,
                work_tree_fd,
                key,
                execution_input_hashes,
            )
        finally:
            _close_descriptors(work_tree_fd, workspace_fd, operation_fd, attempts_fd)

        manifest = load_decoded_tree(configured.store, manifest_ref)
        receipt = ReplayPatchedTreeReceiptV1(
            1,
            admitted.sha256,
            completed_decode_receipt,
            decoded_receipt.decoded_tree_manifest,
            decoded_receipt.decoded_tree_semantic_sha256,
            admitted.intent.sha256,
            admitted.resolution.sha256,
            admitted.source_manifest.sha256,
            compiled.sha256,
            source_evidence,
            operation_results,
            apply_report.sha256,
            manifest_ref,
            manifest.decoded_tree_sha256,
            key,
            True,
        )
        output = configured.store.put_bytes(
            kind="replay-patched-tree-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        _validate_replay_patched_tree_receipt(
            output,
            key,
            admitted=admitted,
            completed_decode_receipt=completed_decode_receipt,
            decoded_receipt=decoded_receipt,
            compiled=compiled,
        )
        await asyncio.sleep(0)
        Ledger.record_effect(configured.ledger, key, owner, output)
        effect_recorded = True
        return Ledger.complete_operation(configured.ledger, key, output)
    except asyncio.CancelledError:
        if not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            try:
                Ledger.release_pending_operation(configured.ledger, key, owner)
            except BaseException as release_error:
                error.add_note(
                    "Pending operation release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )
        raise


@activity.defn
async def replay_build_patched_apk_checkpoint_activity(
    candidate: AdmittedReplayV3,
) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.require_admitted_replay_v3(configured.ledger, candidate)
    (
        completed_framework,
        framework_receipt,
        completed_apply,
        patched_receipt,
        compiled,
    ) = _replay_build_predecessors(admitted)
    capability = admitted.capability("build")
    expected_mutations = _expected_replay_build_mutation_paths(admitted)
    if capability.allowed_mutation_paths != expected_mutations:
        raise ValueError(
            "Replay build capability mutation paths do not match framework policy"
        )
    key, operation_input_sha256, request = _replay_build_operation_identity(
        admitted,
        completed_apply,
        patched_receipt,
        compiled,
        completed_framework,
        framework_receipt,
    )
    plan = admitted.plan("build")
    uses_tool_path = any(slot == "tool" for _, slot in plan.arguments)
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("build").tool_id
    )
    admitted_spec = RunSpec(
        1,
        admitted.run_spec.run_id,
        admitted.run_spec.subject_sha256,
        admitted.run_spec.intent_sha256,
        admitted.run_spec.resolution_sha256,
        capability.canonical_identity,
        admitted.run_spec.policy_revision,
        admitted.run_spec.allowed_actor,
        1,
        admitted.run_spec.apk_composition,
        False,
        0,
    )
    owner = _activity_owner()
    operation_claimed = False
    existing = Ledger.begin_operation(
        configured.ledger,
        key,
        "replay_build_patched_apk_v1",
        operation_input_sha256,
        owner,
        retry_safe=False,
    )
    if existing is not None:
        _validate_replay_patched_apk_receipt(
            existing,
            key,
            admitted=admitted,
            completed_patched_tree_receipt=completed_apply,
            patched_receipt=patched_receipt,
            compiled=compiled,
            execution_request=request,
            completed_framework_cache_receipt=completed_framework,
            framework_receipt=framework_receipt,
        )
        return Ledger.complete_operation(configured.ledger, key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        try:
            executable = configured.executor_paths[capability.executable_sha256]
        except KeyError as error:
            raise ValueError("No runtime executable for admitted capability") from error
        _validate_runtime_executable(executable, capability.executable_sha256)
        stock_bytes = configured.store.read_bytes(admitted.request.stock_apk)
        tool_bytes = configured.store.read_bytes(tool.artifact) if uses_tool_path else None
        load_decoded_tree(configured.store, patched_receipt.patched_tree_manifest)
        if framework_receipt is not None:
            load_decoded_tree(configured.store, framework_receipt.framework_cache_manifest)

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
        patched_tree_fd: int | None = None
        framework_fd: int | None = None
        intermediate_fd: int | None = None
        final_fd: int | None = None
        try:
            _validate_private_directory(attempts_fd, "attempts root")
            try:
                os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
            except FileExistsError:
                pass
            operation_fd = _open_existing_directory(attempts_fd, key)
            _validate_private_directory(operation_fd, "operation")
            owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            workspace_fd = _exclusive_directory(operation_fd, owner_hash)
            workspace_created = True
            _validate_private_directory(workspace_fd, "owner workspace")
            workspace = configured.attempts_root / key / owner_hash
            _exclusive_file(workspace_fd, "stock.apk", stock_bytes)
            if tool_bytes is not None:
                _exclusive_file(workspace_fd, "tool", tool_bytes)
            materialize_decoded_tree(
                configured.store,
                patched_receipt.patched_tree_manifest,
                workspace,
                "patched-tree",
            )
            patched_tree_fd = _open_existing_directory(workspace_fd, "patched-tree")
            if any(slot == "framework_dir" for _, slot in plan.arguments):
                if framework_receipt is None:
                    os.mkdir("framework", mode=0o700, dir_fd=workspace_fd)
                else:
                    materialize_decoded_tree(
                        configured.store,
                        framework_receipt.framework_cache_manifest,
                        workspace,
                        "framework",
                    )
                framework_fd = _open_existing_directory(workspace_fd, "framework")
            try:
                os.stat("build", dir_fd=patched_tree_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Patched-tree build output must be absent before launch")
            if framework_receipt is None:
                if framework_fd is None:
                    raise ValueError("Framework-aware build plan requires an empty framework directory")
                if _framework_cache_snapshot(framework_fd):
                    raise ValueError("No-framework build requires an empty framework directory")
            for output_name in ("intermediate.apk", "patched.apk"):
                try:
                    os.stat(output_name, dir_fd=workspace_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("Replay build output must be absent before launch")
            _verify_workspace_path(workspace, workspace_fd)
            _verify_workspace_path(workspace / "patched-tree", patched_tree_fd)
            if framework_fd is not None:
                _verify_workspace_path(workspace / "framework", framework_fd)

            execution = await execute(
                capability,
                request,
                ExecutionMetadata(executable, workspace, workspace),
                admitted_spec=admitted_spec,
                timeout_seconds=plan.timeout_seconds,
                launcher=configured.launcher,
            )
            if execution.returncode != 0:
                raise RuntimeError(
                    f"Replay patched APK build failed with exit code {execution.returncode}"
                )
            _verify_workspace_path(workspace, workspace_fd)
            _verify_workspace_path(workspace / "patched-tree", patched_tree_fd)
            _secure_remove_optional_build_tree(patched_tree_fd)
            if framework_fd is not None:
                _verify_workspace_path(workspace / "framework", framework_fd)
                if framework_receipt is None:
                    _secure_unlink_framework_one(framework_fd)
                    if _framework_cache_snapshot(framework_fd):
                        raise ValueError("Empty framework directory was mutated by replay build")
                else:
                    framework_manifest = load_decoded_tree(
                        configured.store, framework_receipt.framework_cache_manifest
                    )
                    verify_materialized_decoded_tree(
                        framework_manifest, workspace / "framework"
                    )
            patched_manifest = load_decoded_tree(
                configured.store, patched_receipt.patched_tree_manifest
            )
            verify_materialized_decoded_tree(patched_manifest, workspace / "patched-tree")

            intermediate_fd, intermediate_stat = _open_pinned_regular(
                workspace_fd, "intermediate.apk", "Intermediate APK"
            )
            composition_task = asyncio.create_task(
                asyncio.to_thread(
                    compose_apk,
                    compiled.backend,
                    workspace / "stock.apk",
                    workspace / "intermediate.apk",
                    workspace / "patched.apk",
                )
            )
            composed_report = await _await_backend_composition(composition_task)
            final_fd, final_stat = _open_pinned_regular(
                workspace_fd, "patched.apk", "Patched APK"
            )
            intermediate_bytes = _read_pinned_regular(
                workspace_fd,
                "intermediate.apk",
                intermediate_fd,
                intermediate_stat,
                "Intermediate APK",
            )
            final_bytes = _read_pinned_regular(
                workspace_fd,
                "patched.apk",
                final_fd,
                final_stat,
                "Patched APK",
            )
            validated_report = validate_composed_apk_bytes(
                compiled.backend, stock_bytes, intermediate_bytes, final_bytes
            )
            if composed_report != validated_report:
                raise ValueError("Composed APK report does not match independent validation")
            composition = _replay_backend_composition(validated_report, compiled)
        finally:
            _close_descriptors(
                final_fd,
                intermediate_fd,
                framework_fd,
                patched_tree_fd,
                workspace_fd,
                operation_fd,
                attempts_fd,
            )

        execution_input_hashes: tuple[str, ...] = (
            admitted.sha256,
            canonical_sha256(completed_apply),
            canonical_sha256(patched_receipt.patched_tree_manifest),
            patched_receipt.patched_tree_semantic_sha256,
            compiled.sha256,
            canonical_sha256(admitted.request.stock_apk),
            admitted.profile.sha256,
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request.canonical_identity,
        )
        if completed_framework is not None and framework_receipt is not None:
            execution_input_hashes = (
                *execution_input_hashes,
                canonical_sha256(completed_framework),
                canonical_sha256(framework_receipt.framework_cache_manifest),
                framework_receipt.framework_cache_semantic_sha256,
            )
        intermediate_ref = configured.store.put_bytes(
            kind="intermediate-apk",
            data=intermediate_bytes,
            producer_operation_id=key,
            input_hashes=execution_input_hashes,
        )
        if (
            intermediate_ref.sha256 != composition.intermediate_sha256
            or hashlib.sha256(final_bytes).hexdigest() != composition.output_sha256
        ):
            raise ValueError("Captured APK bytes do not match backend composition")
        patched_ref = configured.store.put_bytes(
            kind="final-apk",
            data=final_bytes,
            producer_operation_id=key,
            input_hashes=(
                *execution_input_hashes,
                canonical_sha256(intermediate_ref),
                composition.sha256,
            ),
        )
        receipt = ReplayPatchedApkReceiptV1(
            1,
            admitted.sha256,
            completed_apply,
            patched_receipt.patched_tree_manifest,
            patched_receipt.patched_tree_semantic_sha256,
            compiled.sha256,
            admitted.request.stock_apk,
            admitted.profile.profile_id,
            admitted.profile.sha256,
            "build",
            plan.sha256,
            capability.canonical_identity,
            tool.artifact.sha256,
            request.canonical_identity,
            completed_framework,
            None if framework_receipt is None else framework_receipt.framework_cache_manifest,
            None if framework_receipt is None else framework_receipt.framework_cache_semantic_sha256,
            intermediate_ref,
            composition,
            patched_ref,
            key,
            True,
        )
        output = configured.store.put_bytes(
            kind="replay-patched-apk-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        _validate_replay_patched_apk_receipt(
            output,
            key,
            admitted=admitted,
            completed_patched_tree_receipt=completed_apply,
            patched_receipt=patched_receipt,
            compiled=compiled,
            execution_request=request,
            completed_framework_cache_receipt=completed_framework,
            framework_receipt=framework_receipt,
        )
        Ledger.record_effect(configured.ledger, key, owner, output)
        effect_recorded = True
        return Ledger.complete_operation(configured.ledger, key, output)
    except asyncio.CancelledError:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            Ledger.release_pending_operation(configured.ledger, key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            try:
                Ledger.release_pending_operation(configured.ledger, key, owner)
            except BaseException as release_error:
                error.add_note(
                    "Pending operation release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )
        raise


def _replay_verification_predecessors(
    grant: AdmittedReplayVerificationGrantV1,
) -> tuple[
    AdmittedReplayV3,
    ArtifactRef | None,
    ReplayFrameworkCacheReceiptV1 | None,
    ArtifactRef,
    ReplayPatchedApkReceiptV1,
    TargetPortSpecV2,
]:
    configured = runtime()
    admitted = Ledger.require_admitted_replay_v3(
        configured.ledger, grant.admitted_replay
    )
    if admitted != grant.admitted_replay:
        raise ValueError("Verification grant admitted replay is not exact")
    (
        completed_framework,
        framework_receipt,
        completed_apply,
        patched_tree_receipt,
        compiled,
    ) = _replay_build_predecessors(admitted)
    build_key, build_input, build_request = _replay_build_operation_identity(
        admitted,
        completed_apply,
        patched_tree_receipt,
        compiled,
        completed_framework,
        framework_receipt,
    )
    completed_build = Ledger.require_completed_operation(
        configured.ledger,
        build_key,
        "replay_build_patched_apk_v1",
        build_input,
    )
    build_receipt = _validate_replay_patched_apk_receipt(
        completed_build,
        build_key,
        admitted=admitted,
        completed_patched_tree_receipt=completed_apply,
        patched_receipt=patched_tree_receipt,
        compiled=compiled,
        execution_request=build_request,
        completed_framework_cache_receipt=completed_framework,
        framework_receipt=framework_receipt,
    )
    if (
        completed_build != grant.request.completed_patched_apk_receipt
        or build_receipt != grant.patched_apk_receipt
        or build_receipt.patched_apk != grant.request.patched_apk
    ):
        raise ValueError("Verification grant does not match the exact completed build")
    return (
        admitted,
        completed_framework,
        framework_receipt,
        completed_build,
        build_receipt,
        compiled,
    )


def _replay_verification_operation_identity(
    grant: AdmittedReplayVerificationGrantV1,
    admitted: AdmittedReplayV3,
    completed_build: ArtifactRef,
    build_receipt: ReplayPatchedApkReceiptV1,
    compiled: TargetPortSpecV2,
    completed_framework: ArtifactRef | None,
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None,
) -> tuple[str, str, ExecutionRequest, tuple[str, ...]]:
    capability = grant.request.executor_capability
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
    )
    request = ExecutionRequest(
        1,
        capability.capability_id,
        capability.canonical_identity,
        build_receipt.patched_apk,
        "decoded-tree",
        (
            ("decoded_tree", "output"),
            ("framework_dir", "framework"),
            ("input_apk", "input.apk"),
            ("tool", "tool"),
        ),
        (),
        admitted.run_spec.apk_composition,
    )
    operation_input: dict[str, object] = {
        "schema_version": 1,
        "admitted_verification_grant_sha256": grant.sha256,
        "admitted_replay_sha256": admitted.sha256,
        "completed_patched_apk_receipt": completed_build,
        "patched_apk": build_receipt.patched_apk,
        "target_port_spec_sha256": compiled.sha256,
        "stock_apk": admitted.request.stock_apk,
        "decoder_profile_id": grant.request.decoder_profile_id,
        "role": "final_decode",
        "executor_capability_sha256": capability.canonical_identity,
        "tool_artifact_sha256": tool.artifact.sha256,
        "execution_request_sha256": request.canonical_identity,
    }
    framework_hashes: tuple[str, ...] = ()
    if completed_framework is not None and framework_receipt is not None:
        operation_input.update(
            {
                "completed_framework_cache_receipt": completed_framework,
                "framework_cache_manifest": framework_receipt.framework_cache_manifest,
                "framework_cache_semantic_sha256": framework_receipt.framework_cache_semantic_sha256,
            }
        )
        framework_hashes = (
            canonical_sha256(completed_framework),
            canonical_sha256(framework_receipt.framework_cache_manifest),
            framework_receipt.framework_cache_semantic_sha256,
        )
    execution_input_hashes = (
        grant.sha256,
        admitted.sha256,
        canonical_sha256(completed_build),
        canonical_sha256(build_receipt.patched_apk),
        compiled.sha256,
        canonical_sha256(admitted.request.stock_apk),
        canonical_sha256(grant.request.decoder_profile_id),
        capability.canonical_identity,
        tool.artifact.sha256,
        request.canonical_identity,
        *framework_hashes,
    )
    return (
        operation_key("replay_verify_final_apk_v1", operation_input),
        canonical_sha256(operation_input),
        request,
        execution_input_hashes,
    )


def _verification_report_results(
    report: VerificationReport,
) -> tuple[ReplayVerificationAssertionResultV1, ...]:
    if type(report) is not VerificationReport or any(
        type(result) is not AssertionResult for result in report.assertion_results
    ):
        raise TypeError("Verifier must return an exact VerificationReport")
    return tuple(
        ReplayVerificationAssertionResultV1(
            result.assertion_id,
            result.kind,
            result.passed,
            result.detail,
        )
        for result in report.assertion_results
    )


def _remove_private_workspace(operation_fd: int, name: str) -> None:
    _secure_remove_tree_entry(operation_fd, name, "Verification validation workspace")


def _validate_replay_final_apk_verification_receipt(
    output: ArtifactRef,
    key: str,
    *,
    grant: AdmittedReplayVerificationGrantV1,
    admitted: AdmittedReplayV3,
    completed_build: ArtifactRef,
    build_receipt: ReplayPatchedApkReceiptV1,
    compiled: TargetPortSpecV2,
    execution_request: ExecutionRequest,
    completed_framework: ArtifactRef | None,
    framework_receipt: ReplayFrameworkCacheReceiptV1 | None,
    owner: str,
) -> ReplayFinalApkVerificationReceiptV1:
    configured = runtime()
    if (
        output.kind != "replay-final-apk-verification-receipt-v1"
        or output.producer_operation_id != key
    ):
        raise ValueError("Adopted final APK verification receipt has invalid lineage")
    receipt = _strict_replay_final_apk_verification_receipt(
        configured.store.read_bytes(output)
    )
    expected_framework = (
        completed_framework,
        None if framework_receipt is None else framework_receipt.framework_cache_manifest,
        None
        if framework_receipt is None
        else framework_receipt.framework_cache_semantic_sha256,
    )
    actual_framework = (
        receipt.completed_framework_cache_receipt,
        receipt.framework_cache_manifest,
        receipt.framework_cache_semantic_sha256,
    )
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
    )
    if (
        output.sha256 != receipt.sha256
        or output.input_hashes != receipt.receipt_input_hashes
        or receipt.admitted_verification_grant_sha256 != grant.sha256
        or receipt.admitted_replay_sha256 != admitted.sha256
        or receipt.completed_patched_apk_receipt != completed_build
        or receipt.patched_apk != build_receipt.patched_apk
        or receipt.target_port_spec_sha256 != compiled.sha256
        or receipt.stock_apk != admitted.request.stock_apk
        or receipt.decoder_profile_id != grant.request.decoder_profile_id
        or receipt.executor_capability_sha256
        != grant.request.executor_capability.canonical_identity
        or receipt.tool_artifact_sha256 != tool.artifact.sha256
        or receipt.execution_request_sha256 != execution_request.canonical_identity
        or actual_framework != expected_framework
        or receipt.operation_key != key
        or receipt.expected_operation_key != key
        or receipt.success is not True
    ):
        raise ValueError("Adopted final APK verification receipt does not match grant")
    final_manifest = load_decoded_tree(configured.store, receipt.final_decoded_manifest)
    source_manifest = load_decoded_tree(configured.store, receipt.source_manifest)
    if (
        final_manifest.decoded_tree_sha256 != receipt.final_decoded_semantic_sha256
        or source_manifest.decoded_tree_sha256 != receipt.source_semantic_sha256
    ):
        raise ValueError("Verification closure semantic hash mismatch")

    stock_bytes = configured.store.read_bytes(receipt.stock_apk)
    intermediate_bytes = configured.store.read_bytes(build_receipt.intermediate_apk)
    final_bytes = configured.store.read_bytes(receipt.patched_apk)
    composition = _replay_backend_composition(
        validate_composed_apk_bytes(
            compiled.backend, stock_bytes, intermediate_bytes, final_bytes
        ),
        compiled,
    )
    if composition != build_receipt.composition:
        raise ValueError("Completed build composition is not independently reproducible")

    attempts_fd = _open_or_create_directory(configured.attempts_root)
    operation_fd: int | None = None
    workspace_fd: int | None = None
    workspace_created = False
    workspace_name = "validate-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()
    try:
        _validate_private_directory(attempts_fd, "attempts root")
        try:
            os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
        except FileExistsError:
            pass
        operation_fd = _open_existing_directory(attempts_fd, key)
        _validate_private_directory(operation_fd, "operation")
        workspace_fd = _exclusive_directory(operation_fd, workspace_name)
        workspace_created = True
        _validate_private_directory(workspace_fd, "verification validation workspace")
        workspace = configured.attempts_root / key / workspace_name
        _exclusive_file(workspace_fd, "stock.apk", stock_bytes)
        _exclusive_file(workspace_fd, "final.apk", final_bytes)
        decoded = materialize_decoded_tree(
            configured.store, receipt.final_decoded_manifest, workspace, "decoded"
        )
        source = materialize_decoded_tree(
            configured.store, receipt.source_manifest, workspace, "source"
        )
        verifier_tree_hash = decoded_tree_sha256(decoded)
        if verifier_tree_hash != receipt.verifier_decoded_tree_sha256:
            raise ValueError("Verifier decoded-tree hash is not independently reproducible")
        decoded_receipt = DecodedArtifactReceipt(
            receipt.patched_apk.sha256,
            verifier_tree_hash,
            receipt.decoder_profile_id,
            receipt.executor_capability_sha256,
        )
        report = verify_apk(
            compiled,
            workspace / "stock.apk",
            workspace / "final.apk",
            decoded,
            source,
            decoded_receipt,
        )
        if (
            report.sha256 != receipt.verifier_report_sha256
            or _verification_report_results(report) != receipt.assertion_results
            or report.operation_proof_count != receipt.operation_proof_count
            or report.passed is not True
        ):
            raise ValueError("Verifier report is not exactly reproducible")
    finally:
        _close_descriptors(workspace_fd)
        try:
            if operation_fd is not None and workspace_created:
                _remove_private_workspace(operation_fd, workspace_name)
        finally:
            _close_descriptors(operation_fd, attempts_fd)
    return receipt


@activity.defn
async def replay_verify_final_apk_checkpoint_activity(
    candidate: AdmittedReplayVerificationGrantV1,
) -> ArtifactRef:
    configured = runtime()
    grant = Ledger.require_admitted_replay_verification_grant_v1(
        configured.ledger, candidate
    )
    (
        admitted,
        completed_framework,
        framework_receipt,
        completed_build,
        build_receipt,
        compiled,
    ) = _replay_verification_predecessors(grant)
    key, operation_input_sha256, request, execution_input_hashes = (
        _replay_verification_operation_identity(
            grant,
            admitted,
            completed_build,
            build_receipt,
            compiled,
            completed_framework,
            framework_receipt,
        )
    )
    capability = grant.request.executor_capability
    tool = next(
        item
        for item in admitted.request.tools
        if item.tool_id == admitted.profile.tool_for_role("decode").tool_id
    )
    admitted_spec = RunSpec(
        1,
        admitted.run_spec.run_id,
        admitted.run_spec.subject_sha256,
        admitted.run_spec.intent_sha256,
        admitted.run_spec.resolution_sha256,
        capability.canonical_identity,
        admitted.run_spec.policy_revision,
        admitted.run_spec.allowed_actor,
        1,
        admitted.run_spec.apk_composition,
        False,
        0,
    )
    owner = _activity_owner()
    operation_claimed = False
    existing = Ledger.begin_operation(
        configured.ledger,
        key,
        "replay_verify_final_apk_v1",
        operation_input_sha256,
        owner,
        retry_safe=False,
    )
    if existing is not None:
        validation_task = asyncio.create_task(
            asyncio.to_thread(
                _validate_replay_final_apk_verification_receipt,
                existing,
                key,
                grant=grant,
                admitted=admitted,
                completed_build=completed_build,
                build_receipt=build_receipt,
                compiled=compiled,
                execution_request=request,
                completed_framework=completed_framework,
                framework_receipt=framework_receipt,
                owner=owner,
            )
        )
        await _await_verification_work(validation_task)
        return Ledger.complete_operation(configured.ledger, key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        if configured.source_root is None:
            raise ValueError("Replay final APK verification requires a configured source root")
        try:
            executable = configured.executor_paths[capability.executable_sha256]
        except KeyError as error:
            raise ValueError("No runtime executable for admitted capability") from error
        _validate_runtime_executable(executable, capability.executable_sha256)
        stock_bytes = configured.store.read_bytes(admitted.request.stock_apk)
        final_bytes = configured.store.read_bytes(build_receipt.patched_apk)
        tool_bytes = configured.store.read_bytes(tool.artifact)
        if hashlib.sha256(final_bytes).hexdigest() != build_receipt.patched_apk.sha256:
            raise ValueError("Final APK bytes do not match completed build")

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
        framework_fd: int | None = None
        source_fd: int | None = None
        final_fd: int | None = None
        try:
            _validate_private_directory(attempts_fd, "attempts root")
            try:
                os.mkdir(key, mode=0o700, dir_fd=attempts_fd)
            except FileExistsError:
                pass
            operation_fd = _open_existing_directory(attempts_fd, key)
            _validate_private_directory(operation_fd, "operation")
            owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
            workspace_fd = _exclusive_directory(operation_fd, owner_hash)
            workspace_created = True
            _validate_private_directory(workspace_fd, "owner workspace")
            workspace = configured.attempts_root / key / owner_hash
            _exclusive_file(workspace_fd, "stock.apk", stock_bytes)
            _exclusive_file(workspace_fd, "input.apk", final_bytes)
            _exclusive_file(workspace_fd, "tool", tool_bytes)
            final_fd, final_stat = _open_pinned_regular(
                workspace_fd, "input.apk", "Final APK"
            )
            if framework_receipt is None:
                os.mkdir("framework", mode=0o700, dir_fd=workspace_fd)
            else:
                materialize_decoded_tree(
                    configured.store,
                    framework_receipt.framework_cache_manifest,
                    workspace,
                    "framework",
                )
            framework_fd = _open_existing_directory(workspace_fd, "framework")
            if framework_receipt is None and _framework_cache_snapshot(framework_fd):
                raise ValueError("No-framework verification requires an empty framework directory")
            source_report = admit_source_bundle_v2(
                admitted,
                configured.source_root,
                workspace,
                configured.ledger,
            )
            staged_source = workspace / source_report.relative_destination
            verify_staged_source_v2(
                source_report,
                admitted,
                staged_source,
                configured.ledger,
            )
            source_fd = _open_existing_directory(workspace_fd, source_report.relative_destination)
            source_capture_task = asyncio.create_task(
                asyncio.to_thread(
                    capture_decoded_tree_fd,
                    configured.store,
                    source_fd,
                    key,
                    execution_input_hashes,
                )
            )
            source_manifest_ref = await _await_verification_work(source_capture_task)
            if type(source_manifest_ref) is not ArtifactRef:
                raise TypeError("Source capture must return an exact ArtifactRef")
            try:
                os.stat("output", dir_fd=workspace_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Final decoded output must be absent before launch")
            _verify_workspace_path(workspace, workspace_fd)
            _verify_workspace_path(workspace / "framework", framework_fd)
            _verify_workspace_path(staged_source, source_fd)

            execution_task = asyncio.create_task(
                execute(
                    capability,
                    request,
                    ExecutionMetadata(executable, workspace, workspace),
                    admitted_spec=admitted_spec,
                    timeout_seconds=grant.request.timeout_seconds,
                    launcher=configured.launcher,
                )
            )
            execution = await _await_verification_execution(execution_task)
            if execution.returncode != 0:
                raise RuntimeError(
                    f"Replay final APK decode failed with exit code {execution.returncode}"
                )

            def capture_and_verify() -> tuple[ArtifactRef, str, str, object]:
                _verify_workspace_path(workspace, workspace_fd)
                _verify_workspace_path(workspace / "framework", framework_fd)
                _verify_workspace_path(staged_source, source_fd)
                captured_final = _read_pinned_regular(
                    workspace_fd,
                    "input.apk",
                    final_fd,
                    final_stat,
                    "Final APK",
                )
                if captured_final != final_bytes:
                    raise ValueError("Final APK changed during decode")
                if framework_receipt is None:
                    _framework_cache_snapshot(framework_fd)
                else:
                    framework_manifest = load_decoded_tree(
                        configured.store, framework_receipt.framework_cache_manifest
                    )
                    verify_materialized_decoded_tree(
                        framework_manifest, workspace / "framework"
                    )
                source_manifest = load_decoded_tree(
                    configured.store, source_manifest_ref
                )
                verify_materialized_decoded_tree(source_manifest, staged_source)
                output_descriptor = _open_existing_directory(workspace_fd, "output")
                try:
                    _verify_workspace_path(workspace / "output", output_descriptor)
                    final_manifest_ref = capture_decoded_tree_fd(
                        configured.store,
                        output_descriptor,
                        key,
                        execution_input_hashes,
                    )
                    _verify_workspace_path(workspace / "output", output_descriptor)
                    final_manifest = load_decoded_tree(
                        configured.store, final_manifest_ref
                    )
                    os.mkdir("clean", mode=0o700, dir_fd=workspace_fd)
                    clean = workspace / "clean"
                    clean_decoded = materialize_decoded_tree(
                        configured.store, final_manifest_ref, clean, "decoded"
                    )
                    clean_source = materialize_decoded_tree(
                        configured.store, source_manifest_ref, clean, "source"
                    )
                    verifier_tree_hash = decoded_tree_sha256(clean_decoded)
                    decoded_receipt = DecodedArtifactReceipt(
                        build_receipt.patched_apk.sha256,
                        verifier_tree_hash,
                        grant.request.decoder_profile_id,
                        capability.canonical_identity,
                    )
                    report = verify_apk(
                        compiled,
                        workspace / "stock.apk",
                        workspace / "input.apk",
                        clean_decoded,
                        clean_source,
                        decoded_receipt,
                    )
                    return (
                        final_manifest_ref,
                        final_manifest.decoded_tree_sha256,
                        verifier_tree_hash,
                        report,
                    )
                finally:
                    os.close(output_descriptor)

            verification_task = asyncio.create_task(asyncio.to_thread(capture_and_verify))
            verification_result = await _await_verification_work(verification_task)
            if type(verification_result) is not tuple or len(verification_result) != 4:
                raise TypeError("Verification work returned an invalid result")
            (
                final_manifest_ref,
                final_semantic_sha256,
                verifier_tree_sha256,
                report,
            ) = verification_result
            if getattr(report, "passed", None) is not True:
                failed_ids = ", ".join(
                    result.assertion_id
                    for result in getattr(report, "assertion_results", ())
                    if getattr(result, "passed", None) is False
                )
                raise ValueError(
                    f"Final APK verification assertions did not all pass: {failed_ids}"
                )
            assertion_results = _verification_report_results(report)
        finally:
            _close_descriptors(
                final_fd,
                source_fd,
                framework_fd,
                workspace_fd,
                operation_fd,
                attempts_fd,
            )

        source_manifest = load_decoded_tree(configured.store, source_manifest_ref)
        receipt = ReplayFinalApkVerificationReceiptV1(
            1,
            grant.sha256,
            admitted.sha256,
            completed_build,
            build_receipt.patched_apk,
            compiled.sha256,
            admitted.request.stock_apk,
            grant.request.decoder_profile_id,
            "final_decode",
            capability.canonical_identity,
            tool.artifact.sha256,
            request.canonical_identity,
            completed_framework,
            None if framework_receipt is None else framework_receipt.framework_cache_manifest,
            None
            if framework_receipt is None
            else framework_receipt.framework_cache_semantic_sha256,
            final_manifest_ref,
            final_semantic_sha256,
            verifier_tree_sha256,
            source_manifest_ref,
            source_manifest.decoded_tree_sha256,
            assertion_results,
            report.operation_proof_count,
            report.sha256,
            key,
            True,
        )
        output = configured.store.put_bytes(
            kind="replay-final-apk-verification-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
        validation_task = asyncio.create_task(
            asyncio.to_thread(
                _validate_replay_final_apk_verification_receipt,
                output,
                key,
                grant=grant,
                admitted=admitted,
                completed_build=completed_build,
                build_receipt=build_receipt,
                compiled=compiled,
                execution_request=request,
                completed_framework=completed_framework,
                framework_receipt=framework_receipt,
                owner=owner,
            )
        )
        await _await_verification_work(validation_task)
        Ledger.record_effect(configured.ledger, key, owner, output)
        effect_recorded = True
        return Ledger.complete_operation(configured.ledger, key, output)
    except asyncio.CancelledError:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            Ledger.release_pending_operation(configured.ledger, key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            Ledger.quarantine_operation(configured.ledger, key, owner)
        elif operation_claimed and not effect_recorded:
            try:
                Ledger.release_pending_operation(configured.ledger, key, owner)
            except BaseException as release_error:
                error.add_note(
                    "Pending operation release failed: "
                    f"{type(release_error).__name__}: {release_error}"
                )
        raise


@activity.defn
async def admit_activity(spec: RunSpec) -> ArtifactRef:
    key = operation_key("phase_a_admit", spec)
    owner = _activity_owner()
    input_hashes = (
        spec.subject_sha256,
        spec.intent_sha256,
        spec.resolution_sha256,
        spec.executor_capability_sha256,
    )
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(
            key,
            "phase_a_admit",
            canonical_sha256(spec),
            owner,
            retry_safe=True,
        ),
        expected_kind="phase-a-admission",
        expected_input_hashes=input_hashes,
    )
    if existing:
        return existing
    output = runtime().store.put_bytes(
        kind="phase-a-admission",
        data=canonical_json(spec).encode("utf-8"),
        producer_operation_id=key,
        input_hashes=input_hashes,
    )
    runtime().ledger.record_effect(key, owner, output)
    return runtime().ledger.complete_operation(key, output)


@activity.defn
async def prepare_activity(stage: StageInput) -> ArtifactRef:
    if len(stage.upstream) != 1 or stage.upstream[0].kind != "phase-a-admission":
        raise ValueError("Prepare requires an admission artifact")
    key = operation_key("phase_a_prepare", stage)
    owner = _activity_owner()
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(
            key,
            "phase_a_prepare",
            canonical_sha256(stage),
            owner,
            retry_safe=True,
        ),
        expected_kind="phase-a-prepared",
        expected_input_hashes=stage.input_hashes,
    )
    if existing:
        return existing
    output = runtime().store.put_bytes(
        kind="phase-a-prepared",
        data=f"prepared:{stage.spec.run_id}:{stage.upstream[0].sha256}".encode("utf-8"),
        producer_operation_id=key,
        input_hashes=stage.input_hashes,
    )
    runtime().ledger.record_effect(key, owner, output)
    return runtime().ledger.complete_operation(key, output)


@activity.defn
async def record_decision_activity(decision: GateDecision) -> None:
    runtime().ledger.record_decision(decision)


@activity.defn
async def apply_activity(stage: StageInput) -> ArtifactRef:
    if (
        tuple(reference.kind for reference in stage.upstream)
        != ("phase-a-admission", "phase-a-prepared")
        or stage.decision is None
    ):
        raise ValueError("Apply requires approved admission and prepared artifacts")
    if not runtime().ledger.has_decision(stage.decision):
        raise ValueError("Apply decision is not recorded")
    key = operation_key("phase_a_apply", stage)
    owner = _activity_owner()
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(
            key,
            "phase_a_apply",
            canonical_sha256(stage),
            owner,
            retry_safe=True,
        ),
        expected_kind="phase-a-output",
        expected_input_hashes=stage.input_hashes,
    )
    if existing:
        return existing
    try:
        output = runtime().store.put_bytes(
            kind="phase-a-output",
            data=(
                f"applied:{stage.spec.run_id}:{stage.upstream[-1].sha256}:"
                f"{canonical_sha256(stage.decision)}"
            ).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=stage.input_hashes,
        )
        runtime().ledger.record_effect(key, owner, output)
        for elapsed in range(stage.spec.apply_delay_seconds):
            activity.heartbeat({"elapsed_seconds": elapsed})
            await asyncio.sleep(1)
        if stage.spec.crash_after_effect and activity.info().attempt == 1:
            raise ApplicationError("Injected post-effect failure", type="InjectedPostEffect")
        return runtime().ledger.complete_operation(key, output)
    except asyncio.CancelledError:
        runtime().ledger.quarantine_operation(key, owner)
        raise


# Stage budgets are derived, never hard-coded per target. Each replay stage does
# far more than its subprocess: apply runs every compiled operation, build
# composes and re-validates an APK of ~90-135 MB, and verify re-decodes it and
# runs every static assertion. Observed real durations are 932-2,448 s against
# subprocess timeouts of 300-600 s, so budgets are deliberate multiples.
#
# These are ceilings, not targets, and they are generous on purpose: an expired
# start_to_close delivers CancelledError, which quarantines the operation, which
# is terminal. A tight timeout does not retry a stage, it destroys the run.
_STAGE_BUDGET_ROLE = {
    "install_framework": "install_framework",
    "decode": "decode",
    "apply": "decode",
    "build": "build",
    "verify": "decode",
}
_STAGE_BUDGET_MULTIPLIER = {
    "install_framework": 6,
    "decode": 6,
    "apply": 6,
    "build": 9,
    "verify": 18,
}


def _replay_stage_budget(admitted: AdmittedReplayV3, stage: str) -> int:
    plan = admitted.plan(_STAGE_BUDGET_ROLE[stage])
    return plan.timeout_seconds * _STAGE_BUDGET_MULTIPLIER[stage]



# ---------------------------------------------------------------- public seams
#
# `replay_gate.resolve_admitted_build` needs these three, and reaching for the
# private names made a module-boundary coupling that only a signature-drift test
# was holding together. These aliases remove the *private* half of that coupling
# at zero risk: every function body and the executed call graph are byte-identical,
# because an alias is the same object under a second name.
#
# What they do NOT do is deduplicate `replay_gate.resolve_admitted_build` against
# `_replay_verification_predecessors`, which is the real extraction. That one edits
# a body inside the verify Activity's proven execution path, so it belongs in the
# slice that re-establishes that evidence with a real run.
replay_build_predecessors = _replay_build_predecessors
replay_build_operation_identity = _replay_build_operation_identity
validate_replay_patched_apk_receipt = _validate_replay_patched_apk_receipt

@activity.defn
async def prepare_replay_plan_activity(handle: AdmittedReplayHandleV1) -> ReplayExecutionPlanV1:
    """Derive the stage sequence from recorded authority.

    Stage membership is target-dependent but not target-conditional: a profile
    that declares frameworks needs the install stage. Deriving this here rather
    than accepting it as Workflow input means a caller cannot omit a stage that
    the admitted profile requires.
    """
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    stages = REPLAY_STAGE_ORDER if admitted.request.frameworks else REPLAY_STAGES_WITHOUT_FRAMEWORK
    return ReplayExecutionPlanV1(
        1,
        admitted.run_spec.run_id,
        admitted.sha256,
        stages,
        tuple(_replay_stage_budget(admitted, stage) for stage in stages),
    )


@activity.defn
async def replay_install_frameworks_stage_activity(handle: AdmittedReplayHandleV1) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    return await replay_install_frameworks_checkpoint_activity(admitted)


@activity.defn
async def replay_decode_stage_activity(handle: AdmittedReplayHandleV1) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    return await replay_decode_checkpoint_activity(admitted)


@activity.defn
async def replay_apply_tree_stage_activity(handle: AdmittedReplayHandleV1) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    return await replay_apply_tree_checkpoint_activity(admitted)


@activity.defn
async def replay_build_patched_apk_stage_activity(handle: AdmittedReplayHandleV1) -> ArtifactRef:
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    return await replay_build_patched_apk_checkpoint_activity(admitted)


@activity.defn
async def replay_verify_final_apk_stage_activity(
    handle: ReplayVerificationGrantHandleV1,
) -> ArtifactRef:
    configured = runtime()
    grant = Ledger.load_admitted_replay_verification_grant_v1(configured.ledger, handle)
    return await replay_verify_final_apk_checkpoint_activity(grant)


@activity.defn
async def prepare_replay_verification_gate_activity(
    handle: AdmittedReplayHandleV1,
) -> ReplayVerificationGateV1:
    """Publish only the hash of the final-verification gate subject.

    The subject binds a build receipt that does not exist when the run is
    admitted, so it is derived here from recorded state after the build stage.
    The Workflow never sees the request body; it binds the human decision to
    this hash, and the admitting Activity re-derives the request independently.
    """
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, handle)
    completed_build, build_receipt = replay_gate.resolve_admitted_build(admitted)
    request = replay_gate.derive_verification_request(admitted, completed_build, build_receipt)
    return ReplayVerificationGateV1(
        1,
        admitted.run_spec.run_id,
        request.gate_id,
        request.sha256,
        request.allowed_actor,
        request.policy_revision,
    )


@activity.defn
async def admit_replay_verification_grant_activity(
    admission: ReplayVerificationAdmissionV1,
) -> ReplayVerificationGrantHandleV1:
    """Re-derive the gate subject and admit the grant the decision authorized.

    Derivation is repeated rather than carried through the Workflow: a subject
    that arrived as Workflow state would be a caller assertion, and the whole
    authority model rests on not accepting those. Because derivation is pure,
    the re-derived request must hash to exactly what the decision bound.
    """
    configured = runtime()
    admitted = Ledger.load_admitted_replay_v3(configured.ledger, admission.handle)
    completed_build, build_receipt = replay_gate.resolve_admitted_build(admitted)
    request = replay_gate.derive_verification_request(admitted, completed_build, build_receipt)
    decision = admission.decision
    if (
        decision.subject_sha256 != request.sha256
        or decision.admission_sha256 != request.sha256
        or decision.prepared_sha256 != request.sha256
    ):
        raise ValueError("Verification decision does not bind the derived request")
    Ledger.record_decision(configured.ledger, decision)
    grant = admit_replay_verification_grant_v1(
        request,
        decision,
        admitted,
        build_receipt,
        lambda candidate: Ledger.has_decision(configured.ledger, candidate),
        configured.store.read_bytes,
    )
    Ledger.record_admitted_replay_verification_grant_v1(configured.ledger, grant)
    recorded = Ledger.require_admitted_replay_verification_grant_v1(configured.ledger, grant)
    return ReplayVerificationGrantHandleV1(1, recorded.request.grant_id, recorded.sha256)


@activity.defn
async def prepare_feature_gate_activity(run_id: str) -> FeatureAssessmentGateV1:
    """Publish only the hash of the feature-assessment gate subject.

    Same shape as the replay verification gate and for the same reason: the
    Workflow binds a human decision to a hash it cannot itself compute, and the
    admitting Activity re-derives the request independently. Carrying the request
    body through History would make the Workflow the place that decides what was
    approved.

    Everything here comes from the recorded assessment, reached by run id through
    `recorded_assessments_v1`. That row is why this gate is answerable at all: a
    client holding the same run id reaches the same recorded state and derives the
    same hash, which is precisely what `PortRunWorkflow`'s `phase-a-approval`
    cannot do.
    """
    configured = runtime()
    recorded = assessment_record.resolve_with(configured.ledger, configured.store, run_id)
    request = derive_feature_gate_request(
        recorded.run_id,
        recorded.assessment,
        recorded.policy_revision,
        recorded.allowed_actor,
        recorded.candidate_ids,
    )
    return derive_assessment_gate(request)


@activity.defn
async def admit_feature_dispositions_activity(
    admission: FeatureDispositionsAdmissionV1,
) -> ArtifactRef:
    """Re-derive the gate subject, fetch the rulings, and admit them or refuse.

    **This is where the answer is actually checked.** The Workflow's validator
    runs in a sandbox with no I/O, so it can only check what the payload carries —
    the decision's fields and the shape of the dispositions reference. It cannot
    read the document those rulings live in. So the validator is a filter and this
    is the authority.

    Three things happen here that cannot happen anywhere else:

    * the request is derived **again**, from the ledger, rather than taken from
      the Workflow. A subject carried through History is a subject the Workflow
      could have got wrong.
    * the dispositions document is fetched **by the reference the human signed**,
      and `ContentStore.read_blob` re-verifies its digest and size — so the bytes
      admitted are the bytes whose hash the human confirmed.
    * `feature_gate.validate_submission` binds them together: same assessment,
      same policy, every candidate ruled on exactly once, no unknown candidate,
      and a rationale wherever a verdict is not `ignore`.

    Returns the dispositions reference on success. It is the same value that went
    in, and returning it is deliberate: the Workflow's result then names the
    artifact this run admitted, rather than one it merely passed along.
    """
    configured = runtime()
    recorded = assessment_record.resolve_with(
        configured.ledger, configured.store, admission.run_id
    )
    request = derive_feature_gate_request(
        recorded.run_id,
        recorded.assessment,
        recorded.policy_revision,
        recorded.allowed_actor,
        recorded.candidate_ids,
    )
    reference = admission.submission.dispositions
    body = configured.store.read_blob(reference.sha256, reference.size)
    try:
        document = FeatureDispositionsV1.from_dict(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ApplicationError(
            f"Admitted dispositions are not a readable document: {error}",
            type="FeatureDispositionsUnreadable",
            non_retryable=True,
        ) from error
    try:
        validate_submission(request, admission.submission, document)
    except (TypeError, ValueError) as error:
        # Non-retryable on purpose. A submission that does not bind its gate will
        # not start binding it on a second attempt, and retrying would turn a
        # clear refusal into a timeout nobody can read.
        raise ApplicationError(
            f"Feature dispositions do not authorise this gate: {error}",
            type="FeatureDispositionsRefused",
            non_retryable=True,
        ) from error
    configured.ledger.record_decision(admission.submission.decision)
    return reference
