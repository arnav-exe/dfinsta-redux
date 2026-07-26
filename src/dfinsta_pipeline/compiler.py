from __future__ import annotations

from dataclasses import dataclass

from .contracts import canonical_sha256
from .port_contracts import (
    AppendManifestComponents,
    AppendResourceEntries,
    ArchiveEntriesAbsent,
    ArchiveEntryNamesAndBytesPreservedExcept,
    Backend,
    BytesAbsent,
    BytesPresent,
    DeletePath,
    DescriptorSetEquality,
    DexEntrySetEquality,
    DexStringsAbsent,
    DexStringsPresent,
    IntentResolution,
    IntentSpecV2,
    Operation,
    OperationPostcondition,
    OverlayTree,
    ReplaceResourceEntry,
    ResolutionSpecV2,
    ResolutionSpecV3,
    SmaliEdit,
    StaticAssertion,
    StockDexGraftBackend,
    TargetIdentity,
)


@dataclass(frozen=True, slots=True)
class TargetPortSpec:
    schema_version: int
    intent_sha256: str
    resolution_sha256: str
    target: TargetIdentity
    backend: Backend
    operations: tuple[Operation, ...]
    assertions: tuple[StaticAssertion, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported target port specification schema")
    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class TargetPortSpecV2:
    schema_version: int
    intent_sha256: str
    resolution_sha256: str
    target: TargetIdentity
    backend: Backend
    intent_statuses: tuple[IntentResolution, ...]
    operations: tuple[Operation, ...]
    assertions: tuple[StaticAssertion, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported target port specification schema")
        if not isinstance(self.intent_statuses, tuple) or any(
            not isinstance(item, IntentResolution) for item in self.intent_statuses
        ):
            raise TypeError("Intent statuses must be a tuple of IntentResolution objects")
        intent_ids = tuple(status.intent_id for status in self.intent_statuses)
        if intent_ids != tuple(sorted(intent_ids)):
            raise ValueError("Intent statuses must be sorted")
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("Duplicate intent statuses")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def compile_port(
    intent: IntentSpecV2, resolution: ResolutionSpecV2 | ResolutionSpecV3
) -> TargetPortSpec | TargetPortSpecV2:
    if resolution.intent_sha256 != intent.sha256:
        raise ValueError("Resolution is not bound to the supplied intent")

    hooks = {hook.hook_id: hook for hook in intent.hooks}
    if isinstance(resolution, ResolutionSpecV3):
        intent_statuses = resolution.intent_statuses
        status_ids = {status.intent_id for status in intent_statuses}
        unknown = sorted(status_ids - set(hooks))
        if unknown:
            raise ValueError(f"Intent status references unknown intent: {unknown[0]}")
        missing = sorted(set(hooks) - status_ids)
        if missing:
            raise ValueError(f"Intent status is missing: {missing[0]}")
        status_by_id = {status.intent_id: status for status in intent_statuses}
        retired_implemented = sorted(
            hook.hook_id
            for hook in intent.hooks
            if hook.disposition == "retire" and status_by_id[hook.hook_id].status == "implemented"
        )
        if retired_implemented:
            raise ValueError(f"Globally retired intent cannot be implemented: {retired_implemented[0]}")
    else:
        intent_statuses = tuple(
            IntentResolution(
                intent_id=hook.hook_id,
                status="implemented" if hook.disposition == "retain" else "omitted",
                rationale=None if hook.disposition == "retain" else "Globally retired intent",
            )
            for hook in intent.hooks
        )
        status_by_id = {status.intent_id: status for status in intent_statuses}

    covered: set[str] = set()
    generated: list[StaticAssertion] = []
    for operation in resolution.operations:
        for intent_id in operation.intent_ids:
            hook = hooks.get(intent_id)
            if hook is None:
                raise ValueError(f"Operation references unknown intent: {intent_id}")
            if isinstance(resolution, ResolutionSpecV2) and hook.disposition != "retain":
                raise ValueError(f"Operation references retired intent: {intent_id}")
            if status_by_id[intent_id].status != "implemented":
                raise ValueError(f"Operation references omitted intent: {intent_id}")
            if operation.kind not in hook.allowed_strategies:
                raise ValueError(
                    f"Operation strategy {operation.kind} is not allowed for intent: {intent_id}"
                )
            covered.add(intent_id)
        generated.append(
            OperationPostcondition(
                assertion_id=f"{operation.operation_id}.final",
                kind="operation_postcondition",
                operation_id=operation.operation_id,
                operation_sha256=canonical_sha256(operation),
            )
        )

    implemented = {
        status.intent_id for status in intent_statuses if status.status == "implemented"
    }
    missing = sorted(implemented - covered)
    if missing:
        label = "Retained" if isinstance(resolution, ResolutionSpecV2) else "Implemented"
        raise ValueError(f"{label} intent has no operation: {missing[0]}")

    generated.append(
        DexEntrySetEquality(
            assertion_id="backend.final-dex-entries",
            kind="dex_entry_set_equality",
            entries=resolution.backend.final_dex_entries,
        )
    )
    _validate_operation_order(resolution.operations)
    _validate_backend_compatibility(resolution)
    _validate_destination_collisions(resolution.operations)
    for assertion in resolution.additional_assertions:
        if isinstance(assertion, OperationPostcondition):
            raise ValueError("Operation postconditions are compiler-owned")
        if (
            isinstance(assertion, DexEntrySetEquality)
            and assertion.entries != resolution.backend.final_dex_entries
        ):
            raise ValueError("DEX entry assertion disagrees with backend topology")
        if isinstance(assertion, DescriptorSetEquality) and (
            assertion.dex_entry not in resolution.backend.final_dex_entries
        ):
            raise ValueError("Descriptor assertion references a DEX outside backend topology")
        if isinstance(assertion, (BytesPresent, BytesAbsent)) and _is_dex_entry(
            assertion.archive_path
        ) and assertion.archive_path not in resolution.backend.final_dex_entries:
            raise ValueError("Byte assertion references a DEX outside backend topology")
        if isinstance(assertion, (DexStringsPresent, DexStringsAbsent)) and (
            assertion.dex_entry not in resolution.backend.final_dex_entries
        ):
            raise ValueError("DEX string assertion references a DEX outside backend topology")
    if isinstance(resolution.backend, StockDexGraftBackend):
        preservation = tuple(
            assertion
            for assertion in resolution.additional_assertions
            if isinstance(assertion, ArchiveEntryNamesAndBytesPreservedExcept)
        )
        if len(preservation) != 1:
            raise ValueError("DEX graft requires one archive preservation assertion")
        required_exclusions = set(resolution.backend.replace_dex_entries) | set(
            resolution.backend.add_dex_entries
        )
        dex_exclusions = {entry for entry in preservation[0].exclusions if _is_dex_entry(entry)}
        if dex_exclusions != required_exclusions:
            raise ValueError("Archive preservation DEX exclusions do not match grafted entries")
        signature_exclusions = {
            entry for entry in preservation[0].exclusions if _is_signature_artifact(entry)
        }
        absent_signatures = {
            entry
            for assertion in resolution.additional_assertions
            if isinstance(assertion, ArchiveEntriesAbsent)
            for entry in assertion.entries
            if _is_signature_artifact(entry)
        }
        if signature_exclusions != absent_signatures:
            raise ValueError("Signature exclusions require exact archive-absence coverage")

    assertions = tuple(
        sorted((*generated, *resolution.additional_assertions), key=lambda item: item.assertion_id)
    )
    identifiers = [operation.operation_id for operation in resolution.operations] + [
        assertion.assertion_id for assertion in assertions
    ]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Generated assertion identity collision")
    common = {
        "intent_sha256": intent.sha256,
        "resolution_sha256": resolution.sha256,
        "target": resolution.target,
        "backend": resolution.backend,
        "operations": resolution.operations,
        "assertions": assertions,
    }
    if isinstance(resolution, ResolutionSpecV3):
        return TargetPortSpecV2(
            schema_version=2,
            intent_statuses=intent_statuses,
            **common,
        )
    return TargetPortSpec(schema_version=1, **common)


def _is_dex_entry(value: str) -> bool:
    return value == "classes.dex" or (
        value.startswith("classes") and value.endswith(".dex") and value[7:-4].isdigit()
    )


def _is_signature_artifact(value: str) -> bool:
    parts = value.upper().split("/")
    return len(parts) == 2 and parts[0] == "META-INF" and (
        parts[1] == "MANIFEST.MF" or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))
    )


def _smali_target_dex(target_prefix: str) -> str | None:
    root = target_prefix.split("/", 1)[0]
    if root == "smali":
        return "classes.dex"
    prefix = "smali_classes"
    if root.startswith(prefix) and root[len(prefix) :].isdigit():
        return f"classes{root[len(prefix):]}.dex"
    return None


def _validate_backend_compatibility(resolution: ResolutionSpecV2 | ResolutionSpecV3) -> None:
    if not isinstance(resolution.backend, StockDexGraftBackend):
        return
    overlay_dex_entries: set[str] = set()
    for operation in resolution.operations:
        if not isinstance(operation, (SmaliEdit, OverlayTree)):
            raise ValueError(f"DEX graft backend cannot apply operation: {operation.kind}")
        if isinstance(operation, OverlayTree):
            target_dex = _smali_target_dex(operation.target_prefix)
            if target_dex not in resolution.backend.add_dex_entries:
                raise ValueError("DEX graft overlay does not target a declared added DEX")
            overlay_dex_entries.add(target_dex)
    if overlay_dex_entries != set(resolution.backend.add_dex_entries):
        raise ValueError("Declared added DEX entries do not match overlay producers")


def _validate_destination_collisions(operations: tuple[Operation, ...]) -> None:
    destinations: set[tuple[str, ...]] = set()
    written_paths: set[str] = set()
    deleted_paths: set[str] = set()
    for operation in operations:
        current: list[tuple[str, ...]] = []
        if isinstance(operation, OverlayTree):
            current.extend(
                ("path", f"{operation.target_prefix}/{source.relative_path}".casefold())
                for source in operation.source_files
            )
            written_paths.update(
                f"{operation.target_prefix}/{source.relative_path}".casefold()
                for source in operation.source_files
            )
        elif isinstance(operation, AppendResourceEntries):
            current.extend(
                (
                    "resource",
                    operation.archive_path.casefold(),
                    *entry.identity,
                )
                for entry in operation.entries
            )
            written_paths.add(operation.archive_path.casefold())
        elif isinstance(operation, ReplaceResourceEntry):
            current.append(
                (
                    "resource",
                    operation.archive_path.casefold(),
                    *operation.after.identity,
                )
            )
            written_paths.add(operation.archive_path.casefold())
        elif isinstance(operation, AppendManifestComponents):
            current.extend(
                (
                    "manifest",
                    operation.archive_path.casefold(),
                    *component.identity,
                )
                for component in operation.components
            )
            written_paths.add(operation.archive_path.casefold())
        elif isinstance(operation, DeletePath):
            current.append(("path", operation.archive_path.casefold()))
            deleted_paths.add(operation.archive_path.casefold())
        for destination in current:
            if destination in destinations:
                raise ValueError("Operations write the same destination identity")
            destinations.add(destination)
    for deleted in deleted_paths:
        if any(
            written == deleted
            or written.startswith(f"{deleted}/")
            or deleted.startswith(f"{written}/")
            for written in written_paths
        ):
            raise ValueError("Delete operation conflicts with a written path")


def _validate_operation_order(operations: tuple[Operation, ...]) -> None:
    overlay_seen = False
    for operation in operations:
        if isinstance(operation, OverlayTree):
            overlay_seen = True
        elif overlay_seen and isinstance(operation, SmaliEdit):
            raise ValueError("Smali edits must precede all overlay operations")
