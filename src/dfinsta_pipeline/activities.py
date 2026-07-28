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
from .compiler import TargetPortSpecV2, compile_port
from .contracts import ArtifactRef, GateDecision, RunSpec, StageInput, canonical_json, canonical_sha256
from .decoded_artifact import (
    capture_decoded_tree_fd,
    load_decoded_tree,
    materialize_decoded_tree,
    verify_materialized_decoded_tree,
)
from .executor import ExecutionMetadata, ExecutionRequest, Launcher, execute
from .ledger import Ledger
from .replay_contracts import (
    AdmittedReplayV3,
    ReplayApplyOperationResultV1,
    ReplayDecodedTreeReceiptV2,
    ReplayDecodedTreeReceiptV1,
    ReplayFrameworkCacheReceiptV1,
    ReplayFrameworkInstallationV1,
    ReplayPatchedTreeReceiptV1,
    ReplaySourceAdmissionEvidenceV1,
)
from .source_admission import admit_source_bundle_v2, verify_staged_source_v2
from .store import ContentStore


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
) -> None:
    global _runtime
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
        ledger=Ledger(ledger_path),
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
