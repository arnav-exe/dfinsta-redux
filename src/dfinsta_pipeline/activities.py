from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import ArtifactRef, GateDecision, RunSpec, StageInput, canonical_json, canonical_sha256
from .decoded_artifact import capture_decoded_tree_fd, load_decoded_tree
from .executor import ExecutionMetadata, ExecutionRequest, Launcher, execute
from .ledger import Ledger
from .replay_contracts import AdmittedReplayV3, ReplayDecodedTreeReceiptV1
from .store import ContentStore


@dataclass
class ActivityRuntime:
    store: ContentStore
    ledger: Ledger
    attempts_root: Path
    executor_paths: Mapping[str, Path]
    launcher: Launcher | None


_runtime: ActivityRuntime | None = None


def configure_runtime(
    state_root: Path,
    *,
    attempts_root: Path | None = None,
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
    _runtime = ActivityRuntime(
        store=ContentStore(cas_root),
        ledger=Ledger(ledger_path),
        attempts_root=resolved_attempts_root,
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


def _strict_replay_decoded_tree_receipt(data: bytes) -> ReplayDecodedTreeReceiptV1:
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
        result = ReplayDecodedTreeReceiptV1.from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("Invalid replay decoded-tree receipt") from error
    if canonical_json(result).encode("utf-8") != data:
        raise ValueError("Replay decoded-tree receipt is not canonical")
    return result


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
async def replay_decode_checkpoint_activity(candidate: AdmittedReplayV3) -> ArtifactRef:
    configured = runtime()
    admitted = configured.ledger.require_admitted_replay_v3(candidate)

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
    receipt_relationships = {
        "decoded_apk_role": "stock_input",
        "admitted_replay_sha256": admitted.sha256,
        "input_apk": admitted.request.stock_apk,
        "toolchain_profile_id": admitted.profile.profile_id,
        "toolchain_profile_sha256": admitted.profile.sha256,
        "role": role,
        "execution_plan_sha256": plan.sha256,
        "executor_capability_sha256": capability.canonical_identity,
        "tool_artifact_sha256": tool_artifact_sha256,
        "execution_request_sha256": request.canonical_identity,
    }
    operation_input = {"schema_version": 1, **receipt_relationships}
    key = operation_key("replay_decode_tree_v1", operation_input)
    execution_input_hashes = (
        admitted.sha256,
        canonical_sha256(admitted.request.stock_apk),
        admitted.profile.sha256,
        plan.sha256,
        capability.canonical_identity,
        tool_artifact_sha256,
        request.canonical_identity,
    )
    owner = _activity_owner()
    operation_claimed = False
    existing = configured.ledger.begin_operation(
        key,
        "replay_decode_tree_v1",
        canonical_sha256(operation_input),
        owner,
        retry_safe=False,
    )
    if existing is not None:
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
        return configured.ledger.complete_operation(key, existing)
    operation_claimed = True

    workspace_created = False
    effect_recorded = False
    try:
        try:
            executable = configured.executor_paths[capability.executable_sha256]
        except KeyError as error:
            raise ValueError("No runtime executable for admitted capability") from error
        if not executable.is_file():
            raise ValueError("Runtime executable is not a regular file")

        stock_bytes = configured.store.read_bytes(admitted.request.stock_apk)
        tool_bytes = configured.store.read_bytes(tool.artifact)
        uses_tool_path = any(slot == "tool" for _, slot in plan.arguments)

        attempts_fd = _open_or_create_directory(configured.attempts_root)
        operation_fd: int | None = None
        workspace_fd: int | None = None
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
                os.mkdir("framework", mode=0o700, dir_fd=workspace_fd)
            try:
                os.stat("output", dir_fd=workspace_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Replay decoded-tree output must be absent before launch")
            _verify_workspace_path(workspace, workspace_fd)

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
            output_fd = _open_existing_directory(workspace_fd, "output")
            manifest_ref = capture_decoded_tree_fd(
                configured.store,
                output_fd,
                key,
                execution_input_hashes,
            )
        finally:
            _close_descriptors(output_fd, workspace_fd, operation_fd, attempts_fd)
        manifest = load_decoded_tree(configured.store, manifest_ref)
        receipt = ReplayDecodedTreeReceiptV1(
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
        output = configured.store.put_bytes(
            kind="replay-decoded-tree-receipt-v1",
            data=canonical_json(receipt).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=receipt.receipt_input_hashes,
        )
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
        configured.ledger.record_effect(key, owner, output)
        effect_recorded = True
        return configured.ledger.complete_operation(key, output)
    except asyncio.CancelledError:
        if not effect_recorded:
            configured.ledger.quarantine_operation(key, owner)
        raise
    except BaseException as error:
        if workspace_created and not effect_recorded:
            configured.ledger.quarantine_operation(key, owner)
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
