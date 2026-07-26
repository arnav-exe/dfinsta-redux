from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from .contracts import canonical_sha256


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX_PATTERN = re.compile(r"^(?:[0-9a-f]{2})+$")
DESCRIPTOR_PATTERN = re.compile(r"^L[A-Za-z0-9_$-]+(?:/[A-Za-z0-9_$-]+)*;$")
METHOD_PATTERN = re.compile(
    r"^(?:<init>|<clinit>|[A-Za-z0-9_$-]+)"
    r"\((?:\[*(?:[ZBSCIJFD]|L[A-Za-z0-9_$-]+(?:/[A-Za-z0-9_$-]+)*;))*\)"
    r"(?:V|\[*(?:[ZBSCIJFD]|L[A-Za-z0-9_$-]+(?:/[A-Za-z0-9_$-]+)*;))$"
)
STRATEGIES = {
    "smali_edit",
    "overlay_tree",
    "append_resource_entries",
    "replace_resource_entry",
    "append_manifest_components",
    "delete_path",
}


def _keys(data: object, cls: type[object], label: str) -> dict[str, Any]:
    if type(data) is not dict or any(type(key) is not str for key in data):
        raise TypeError(f"{label} must be an object with string keys")
    expected = {field.name for field in dataclasses.fields(cls)}
    unknown = set(data) - expected
    missing = expected - set(data)
    if unknown:
        raise ValueError(f"Unknown {label} field: {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"Missing {label} field: {sorted(missing)[0]}")
    return data


def _array(value: object, label: str) -> tuple[object, ...]:
    if type(value) not in {list, tuple}:
        raise TypeError(f"{label} must be an array")
    return tuple(value)


def _strings(value: object, label: str) -> tuple[str, ...]:
    values = _array(value, label)
    if any(type(item) is not str for item in values):
        raise TypeError(f"{label} must be an array of strings")
    return tuple(values)


def _string(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _id(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _sha(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _count(value: object, label: str, *, positive: bool = False) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        raise ValueError(f"Invalid {label}")


def _sorted_unique(values: tuple[Any, ...], label: str, *, key=lambda value: value) -> None:
    keys = tuple(key(value) for value in values)
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{label} must be sorted")
    if len(keys) != len(set(keys)):
        raise ValueError(f"Duplicate {label}")


def _archive_path(value: object, label: str) -> None:
    _string(value, label)
    assert isinstance(value, str)
    if (
        "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or ":" in value.split("/", 1)[0]
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).as_posix() != value
    ):
        raise ValueError(f"Unsafe or noncanonical {label}")


def _descriptor(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not DESCRIPTOR_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _method(value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str:
        raise TypeError("Method signature must be a string")
    if not METHOD_PATTERN.fullmatch(value):
        raise ValueError("Invalid method signature")


def _lines(value: tuple[str, ...], label: str, *, empty: bool = False) -> None:
    if not empty and not value:
        raise ValueError(f"{label} must not be empty")
    if any(type(line) is not str for line in value):
        raise TypeError(f"{label} must be a tuple of strings")
    if any(not line or "\n" in line or "\r" in line for line in value):
        raise ValueError(f"{label} contains an empty or multiline line")


def _intent_ids(value: tuple[str, ...]) -> None:
    if not isinstance(value, tuple) or any(type(item) is not str for item in value):
        raise TypeError("Intent IDs must be a tuple of strings")
    if not value:
        raise ValueError("Intent IDs must not be empty")
    _sorted_unique(value, "intent IDs")
    for intent_id in value:
        _id(intent_id, "intent id")


def _dex_key(entry: str) -> int:
    return 1 if entry == "classes.dex" else int(entry[7:-4])


def _dex_entries(entries: tuple[str, ...], label: str, *, nonempty: bool = False) -> None:
    if not isinstance(entries, tuple) or any(type(item) is not str for item in entries):
        raise TypeError(f"{label} must be a tuple of strings")
    if nonempty and not entries:
        raise ValueError(f"{label} must not be empty")
    for entry in entries:
        if not re.fullmatch(r"classes(?:[2-9]|[1-9][0-9]+)?\.dex", entry):
            raise ValueError("Invalid DEX entry")
    _sorted_unique(entries, "DEX entries", key=_dex_key)


@dataclass(frozen=True, slots=True)
class HookIntent:
    hook_id: str
    feature_id: str
    disposition: Literal["retain", "retire"]
    description: str
    allowed_strategies: tuple[str, ...]
    semantic_dependencies: tuple[str, ...]
    forbidden_fallbacks: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.hook_id, "hook id")
        _id(self.feature_id, "feature id")
        if self.disposition not in {"retain", "retire"}:
            raise ValueError("Invalid hook disposition")
        _string(self.description, "hook description")
        for values, label in (
            (self.allowed_strategies, "allowed strategies"),
            (self.semantic_dependencies, "semantic dependencies"),
            (self.forbidden_fallbacks, "forbidden fallbacks"),
        ):
            if not isinstance(values, tuple) or any(type(item) is not str for item in values):
                raise TypeError(f"{label} must be a tuple of strings")
            _sorted_unique(values, label)
        if self.disposition == "retain" and not self.allowed_strategies:
            raise ValueError("Retained hooks require an allowed strategy")
        if any(strategy not in STRATEGIES for strategy in self.allowed_strategies):
            raise ValueError("Invalid hook strategy")
        for value in (*self.semantic_dependencies, *self.forbidden_fallbacks):
            _string(value, "hook constraint")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HookIntent:
        data = _keys(data, cls, "hook intent")
        return cls(
            data["hook_id"],
            data["feature_id"],
            data["disposition"],
            data["description"],
            _strings(data["allowed_strategies"], "allowed strategies"),
            _strings(data["semantic_dependencies"], "semantic dependencies"),
            _strings(data["forbidden_fallbacks"], "forbidden fallbacks"),
        )


@dataclass(frozen=True, slots=True)
class IntentSpecV2:
    schema_version: int
    policy_revision: str
    hooks: tuple[HookIntent, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported intent schema")
        _id(self.policy_revision, "policy revision")
        if not isinstance(self.hooks, tuple) or any(not isinstance(item, HookIntent) for item in self.hooks):
            raise TypeError("Hooks must be a tuple of HookIntent objects")
        if not self.hooks:
            raise ValueError("Intent specification is empty")
        _sorted_unique(self.hooks, "hooks", key=lambda hook: hook.hook_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentSpecV2:
        data = _keys(data, cls, "intent v2")
        return cls(
            data["schema_version"],
            data["policy_revision"],
            tuple(HookIntent.from_dict(item) for item in _array(data["hooks"], "hooks")),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    package_name: str
    version_name: str
    version_code: int
    apk_sha256: str
    composition: Literal["monolithic"]

    def __post_init__(self) -> None:
        _string(self.package_name, "package name")
        _string(self.version_name, "version name")
        _count(self.version_code, "version code", positive=True)
        _sha(self.apk_sha256, "target APK SHA-256")
        if self.composition != "monolithic":
            raise ValueError("Unsupported target composition")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetIdentity:
        return cls(**_keys(data, cls, "target identity"))


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        _archive_path(self.relative_path, "source file path")
        _sha(self.sha256, "source file SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceFile:
        return cls(**_keys(data, cls, "source file"))


@dataclass(frozen=True, slots=True)
class ResourceEntry:
    resource_type: str
    name: str
    canonical_xml: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.resource_type, self.name

    def __post_init__(self) -> None:
        _id(self.resource_type, "resource type")
        _id(self.name, "resource name")
        _string(self.canonical_xml, "canonical resource XML")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceEntry:
        return cls(**_keys(data, cls, "resource entry"))


@dataclass(frozen=True, slots=True)
class ManifestComponent:
    tag: str
    android_name: str
    canonical_xml: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.tag, self.android_name

    def __post_init__(self) -> None:
        _id(self.tag, "manifest component tag")
        _string(self.android_name, "manifest android name")
        _string(self.canonical_xml, "canonical manifest XML")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestComponent:
        return cls(**_keys(data, cls, "manifest component"))


@dataclass(frozen=True, slots=True)
class SmaliEdit:
    operation_id: str
    kind: Literal["smali_edit"]
    intent_ids: tuple[str, ...]
    descriptor: str
    method_signature: str
    mode: Literal["insert_before", "insert_after", "replace"]
    match_policy: Literal["all", "occurrence"]
    occurrence: int | None
    precondition_sequence: tuple[str, ...]
    expected_precondition_count: int
    payload: tuple[str, ...]
    final_sequence: tuple[str, ...]
    expected_final_count: int

    def __post_init__(self) -> None:
        _id(self.operation_id, "operation id")
        if self.kind != "smali_edit":
            raise ValueError("Invalid operation kind")
        _intent_ids(self.intent_ids)
        _descriptor(self.descriptor, "smali descriptor")
        _method(self.method_signature)
        if self.mode not in {"insert_before", "insert_after", "replace"}:
            raise ValueError("Invalid smali edit mode")
        if self.match_policy not in {"all", "occurrence"}:
            raise ValueError("Invalid smali match policy")
        if self.match_policy == "all" and self.occurrence is not None:
            raise ValueError("All-match edits cannot select an occurrence")
        if self.match_policy == "occurrence":
            _count(self.occurrence, "smali occurrence")
        _lines(self.precondition_sequence, "precondition sequence")
        _lines(self.payload, "smali payload", empty=True)
        _lines(self.final_sequence, "final proof sequence")
        _count(self.expected_precondition_count, "precondition count", positive=True)
        _count(self.expected_final_count, "final count")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmaliEdit:
        data = _keys(data, cls, "smali edit")
        return cls(
            **{
                **data,
                "intent_ids": _strings(data["intent_ids"], "intent IDs"),
                "precondition_sequence": _strings(data["precondition_sequence"], "precondition sequence"),
                "payload": _strings(data["payload"], "smali payload"),
                "final_sequence": _strings(data["final_sequence"], "final proof sequence"),
            }
        )


@dataclass(frozen=True, slots=True)
class OverlayTree:
    operation_id: str
    kind: Literal["overlay_tree"]
    intent_ids: tuple[str, ...]
    source_prefix: str
    target_prefix: str
    source_files: tuple[SourceFile, ...]
    source_manifest_sha256: str
    collision_policy: Literal["forbid", "require_exact"]

    def __post_init__(self) -> None:
        _id(self.operation_id, "operation id")
        if self.kind != "overlay_tree":
            raise ValueError("Invalid operation kind")
        _intent_ids(self.intent_ids)
        _archive_path(self.source_prefix, "overlay source prefix")
        _archive_path(self.target_prefix, "overlay target prefix")
        if not isinstance(self.source_files, tuple) or any(
            not isinstance(item, SourceFile) for item in self.source_files
        ):
            raise TypeError("Source files must be a tuple of SourceFile objects")
        if not self.source_files:
            raise ValueError("Source files must not be empty")
        _sorted_unique(self.source_files, "source files", key=lambda item: item.relative_path)
        _sha(self.source_manifest_sha256, "source manifest SHA-256")
        if self.source_manifest_sha256 != canonical_sha256(self.source_files):
            raise ValueError("Source manifest SHA-256 mismatch")
        if self.collision_policy not in {"forbid", "require_exact"}:
            raise ValueError("Invalid overlay collision policy")

    @property
    def exact_target_files(self) -> tuple[SourceFile, ...]:
        return self.source_files

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverlayTree:
        data = _keys(data, cls, "overlay tree")
        return cls(
            **{
                **data,
                "intent_ids": _strings(data["intent_ids"], "intent IDs"),
                "source_files": tuple(
                    SourceFile.from_dict(item) for item in _array(data["source_files"], "source files")
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class AppendResourceEntries:
    operation_id: str
    kind: Literal["append_resource_entries"]
    intent_ids: tuple[str, ...]
    archive_path: str
    entries: tuple[ResourceEntry, ...]

    def __post_init__(self) -> None:
        _operation_header(self.operation_id, self.kind, "append_resource_entries", self.intent_ids)
        _archive_path(self.archive_path, "resource archive path")
        _entry_set(self.entries, ResourceEntry, "resource entries")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppendResourceEntries:
        data = _keys(data, cls, "append resource entries")
        return cls(
            data["operation_id"],
            data["kind"],
            _strings(data["intent_ids"], "intent IDs"),
            data["archive_path"],
            tuple(ResourceEntry.from_dict(item) for item in _array(data["entries"], "resource entries")),
        )


@dataclass(frozen=True, slots=True)
class ReplaceResourceEntry:
    operation_id: str
    kind: Literal["replace_resource_entry"]
    intent_ids: tuple[str, ...]
    archive_path: str
    before: ResourceEntry
    after: ResourceEntry

    def __post_init__(self) -> None:
        _operation_header(self.operation_id, self.kind, "replace_resource_entry", self.intent_ids)
        _archive_path(self.archive_path, "resource archive path")
        if not isinstance(self.before, ResourceEntry) or not isinstance(self.after, ResourceEntry):
            raise TypeError("Resource replacement requires ResourceEntry objects")
        if self.before.identity != self.after.identity:
            raise ValueError("Resource replacement identity must not change")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplaceResourceEntry:
        data = _keys(data, cls, "replace resource entry")
        return cls(
            data["operation_id"],
            data["kind"],
            _strings(data["intent_ids"], "intent IDs"),
            data["archive_path"],
            ResourceEntry.from_dict(data["before"]),
            ResourceEntry.from_dict(data["after"]),
        )


@dataclass(frozen=True, slots=True)
class AppendManifestComponents:
    operation_id: str
    kind: Literal["append_manifest_components"]
    intent_ids: tuple[str, ...]
    archive_path: str
    components: tuple[ManifestComponent, ...]

    def __post_init__(self) -> None:
        _operation_header(self.operation_id, self.kind, "append_manifest_components", self.intent_ids)
        _archive_path(self.archive_path, "manifest archive path")
        _entry_set(self.components, ManifestComponent, "manifest components")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppendManifestComponents:
        data = _keys(data, cls, "append manifest components")
        return cls(
            data["operation_id"],
            data["kind"],
            _strings(data["intent_ids"], "intent IDs"),
            data["archive_path"],
            tuple(
                ManifestComponent.from_dict(item)
                for item in _array(data["components"], "manifest components")
            ),
        )


@dataclass(frozen=True, slots=True)
class DeletePath:
    operation_id: str
    kind: Literal["delete_path"]
    intent_ids: tuple[str, ...]
    archive_path: str
    expected_present: bool

    def __post_init__(self) -> None:
        _operation_header(self.operation_id, self.kind, "delete_path", self.intent_ids)
        _archive_path(self.archive_path, "deleted archive path")
        if type(self.expected_present) is not bool:
            raise TypeError("Expected-present flag must be a boolean")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeletePath:
        data = _keys(data, cls, "delete path")
        return cls(
            data["operation_id"],
            data["kind"],
            _strings(data["intent_ids"], "intent IDs"),
            data["archive_path"],
            data["expected_present"],
        )


def _operation_header(operation_id: object, kind: object, expected: str, intents: tuple[str, ...]) -> None:
    _id(operation_id, "operation id")
    if kind != expected:
        raise ValueError("Invalid operation kind")
    _intent_ids(intents)


def _entry_set(values: tuple[Any, ...], cls: type[object], label: str) -> None:
    if not isinstance(values, tuple) or any(not isinstance(item, cls) for item in values):
        raise TypeError(f"{label} must contain {cls.__name__} objects")
    if not values:
        raise ValueError(f"{label} must not be empty")
    _sorted_unique(values, label, key=lambda item: item.identity)


Operation = (
    SmaliEdit
    | OverlayTree
    | AppendResourceEntries
    | ReplaceResourceEntry
    | AppendManifestComponents
    | DeletePath
)
OPERATION_TYPES = {
    "smali_edit": SmaliEdit,
    "overlay_tree": OverlayTree,
    "append_resource_entries": AppendResourceEntries,
    "replace_resource_entry": ReplaceResourceEntry,
    "append_manifest_components": AppendManifestComponents,
    "delete_path": DeletePath,
}


def _operation(data: object) -> Operation:
    if type(data) is not dict:
        raise TypeError("Operation must be an object")
    kind = data.get("kind")
    if type(kind) is not str or kind not in OPERATION_TYPES:
        raise ValueError("Invalid operation kind")
    return OPERATION_TYPES[kind].from_dict(data)


@dataclass(frozen=True, slots=True)
class ExactSmaliSequenceCount:
    assertion_id: str
    kind: Literal["exact_smali_sequence_count"]
    descriptor: str
    method_signature: str | None
    sequence: tuple[str, ...]
    expected_count: int

    def __post_init__(self) -> None:
        _id(self.assertion_id, "assertion id")
        if self.kind != "exact_smali_sequence_count":
            raise ValueError("Invalid assertion kind")
        _descriptor(self.descriptor, "smali assertion descriptor")
        _method(self.method_signature, optional=True)
        _lines(self.sequence, "smali sequence")
        _count(self.expected_count, "smali sequence count")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExactSmaliSequenceCount:
        data = _keys(data, cls, "exact smali sequence assertion")
        return cls(**{**data, "sequence": _strings(data["sequence"], "smali sequence")})


@dataclass(frozen=True, slots=True)
class DexEntrySetEquality:
    assertion_id: str
    kind: Literal["dex_entry_set_equality"]
    entries: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.assertion_id, "assertion id")
        if self.kind != "dex_entry_set_equality":
            raise ValueError("Invalid assertion kind")
        _dex_entries(self.entries, "asserted DEX entries", nonempty=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DexEntrySetEquality:
        data = _keys(data, cls, "DEX entry set assertion")
        return cls(**{**data, "entries": _strings(data["entries"], "DEX entries")})


@dataclass(frozen=True, slots=True)
class DescriptorSetEquality:
    assertion_id: str
    kind: Literal["descriptor_set_equality"]
    dex_entry: str
    descriptors: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.assertion_id, "assertion id")
        if self.kind != "descriptor_set_equality":
            raise ValueError("Invalid assertion kind")
        _dex_entries((self.dex_entry,), "descriptor DEX entry", nonempty=True)
        if not isinstance(self.descriptors, tuple) or any(type(item) is not str for item in self.descriptors):
            raise TypeError("Descriptors must be a tuple of strings")
        _sorted_unique(self.descriptors, "descriptors")
        for descriptor in self.descriptors:
            _descriptor(descriptor, "descriptor")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DescriptorSetEquality:
        data = _keys(data, cls, "descriptor set assertion")
        return cls(**{**data, "descriptors": _strings(data["descriptors"], "descriptors")})


@dataclass(frozen=True, slots=True)
class BytesPresent:
    assertion_id: str
    kind: Literal["bytes_present"]
    archive_path: str
    bytes_hex: str

    def __post_init__(self) -> None:
        _bytes_assertion(self.assertion_id, self.kind, "bytes_present", self.archive_path, self.bytes_hex)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BytesPresent:
        return cls(**_keys(data, cls, "bytes-present assertion"))


@dataclass(frozen=True, slots=True)
class BytesAbsent:
    assertion_id: str
    kind: Literal["bytes_absent"]
    archive_path: str
    bytes_hex: str

    def __post_init__(self) -> None:
        _bytes_assertion(self.assertion_id, self.kind, "bytes_absent", self.archive_path, self.bytes_hex)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BytesAbsent:
        return cls(**_keys(data, cls, "bytes-absent assertion"))


def _bytes_assertion(
    assertion_id: object, kind: object, expected: str, archive_path: object, bytes_hex: object
) -> None:
    _id(assertion_id, "assertion id")
    if kind != expected:
        raise ValueError("Invalid assertion kind")
    _archive_path(archive_path, "byte assertion archive path")
    if type(bytes_hex) is not str:
        raise TypeError("Asserted bytes must be hexadecimal text")
    if not HEX_PATTERN.fullmatch(bytes_hex):
        raise ValueError("Invalid asserted bytes")


@dataclass(frozen=True, slots=True)
class ArchiveEntryNamesAndBytesPreservedExcept:
    assertion_id: str
    kind: Literal["archive_preservation_except"]
    exclusions: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.assertion_id, "assertion id")
        if self.kind != "archive_preservation_except":
            raise ValueError("Invalid assertion kind")
        if not isinstance(self.exclusions, tuple) or any(type(item) is not str for item in self.exclusions):
            raise TypeError("Archive exclusions must be a tuple of strings")
        _sorted_unique(self.exclusions, "archive exclusions")
        for exclusion in self.exclusions:
            _archive_path(exclusion, "archive exclusion")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveEntryNamesAndBytesPreservedExcept:
        data = _keys(data, cls, "archive preservation assertion")
        return cls(**{**data, "exclusions": _strings(data["exclusions"], "archive exclusions")})


StaticAssertion = (
    ExactSmaliSequenceCount
    | DexEntrySetEquality
    | DescriptorSetEquality
    | BytesPresent
    | BytesAbsent
    | ArchiveEntryNamesAndBytesPreservedExcept
)
ASSERTION_TYPES = {
    "exact_smali_sequence_count": ExactSmaliSequenceCount,
    "dex_entry_set_equality": DexEntrySetEquality,
    "descriptor_set_equality": DescriptorSetEquality,
    "bytes_present": BytesPresent,
    "bytes_absent": BytesAbsent,
    "archive_preservation_except": ArchiveEntryNamesAndBytesPreservedExcept,
}


def _assertion(data: object) -> StaticAssertion:
    if type(data) is not dict:
        raise TypeError("Static assertion must be an object")
    kind = data.get("kind")
    if type(kind) is not str or kind not in ASSERTION_TYPES:
        raise ValueError("Invalid assertion kind")
    return ASSERTION_TYPES[kind].from_dict(data)


@dataclass(frozen=True, slots=True)
class ApktoolFullRebuildBackend:
    kind: Literal["apktool_full_rebuild"]
    profile_id: str
    dex_entries: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind != "apktool_full_rebuild":
            raise ValueError("Invalid backend kind")
        _id(self.profile_id, "backend profile id")
        _dex_entries(self.dex_entries, "backend DEX entries", nonempty=True)

    @property
    def final_dex_entries(self) -> tuple[str, ...]:
        return self.dex_entries

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApktoolFullRebuildBackend:
        data = _keys(data, cls, "apktool backend")
        return cls(data["kind"], data["profile_id"], _strings(data["dex_entries"], "DEX entries"))


@dataclass(frozen=True, slots=True)
class StockDexGraftBackend:
    kind: Literal["stock_dex_graft"]
    profile_id: str
    stock_dex_entries: tuple[str, ...]
    replace_dex_entries: tuple[str, ...]
    add_dex_entries: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind != "stock_dex_graft":
            raise ValueError("Invalid backend kind")
        _id(self.profile_id, "backend profile id")
        _dex_entries(self.stock_dex_entries, "stock DEX entries", nonempty=True)
        _dex_entries(self.replace_dex_entries, "replacement DEX entries")
        _dex_entries(self.add_dex_entries, "added DEX entries")
        stock = set(self.stock_dex_entries)
        replacements = set(self.replace_dex_entries)
        additions = set(self.add_dex_entries)
        if not replacements <= stock:
            raise ValueError("Replacement DEX entries must exist in stock")
        if additions & stock:
            raise ValueError("Added DEX entries collide with stock")

    @property
    def final_dex_entries(self) -> tuple[str, ...]:
        return tuple(sorted((*self.stock_dex_entries, *self.add_dex_entries), key=_dex_key))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StockDexGraftBackend:
        data = _keys(data, cls, "DEX graft backend")
        return cls(
            data["kind"],
            data["profile_id"],
            _strings(data["stock_dex_entries"], "stock DEX entries"),
            _strings(data["replace_dex_entries"], "replacement DEX entries"),
            _strings(data["add_dex_entries"], "added DEX entries"),
        )


Backend = ApktoolFullRebuildBackend | StockDexGraftBackend
BACKEND_TYPES = {
    "apktool_full_rebuild": ApktoolFullRebuildBackend,
    "stock_dex_graft": StockDexGraftBackend,
}


def _backend(data: object) -> Backend:
    if type(data) is not dict:
        raise TypeError("Backend must be an object")
    kind = data.get("kind")
    if type(kind) is not str or kind not in BACKEND_TYPES:
        raise ValueError("Invalid backend kind")
    return BACKEND_TYPES[kind].from_dict(data)


@dataclass(frozen=True, slots=True)
class ResolutionSpecV2:
    schema_version: int
    intent_sha256: str
    target: TargetIdentity
    source_bundle_sha256: str
    backend: Backend
    operations: tuple[Operation, ...]
    additional_assertions: tuple[StaticAssertion, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported resolution schema")
        _sha(self.intent_sha256, "intent SHA-256")
        _sha(self.source_bundle_sha256, "source bundle SHA-256")
        if not isinstance(self.target, TargetIdentity):
            raise TypeError("Target must be a TargetIdentity")
        if not isinstance(self.backend, (ApktoolFullRebuildBackend, StockDexGraftBackend)):
            raise TypeError("Invalid backend")
        if not isinstance(self.operations, tuple) or any(
            not isinstance(item, tuple(OPERATION_TYPES.values())) for item in self.operations
        ):
            raise TypeError("Operations must be a tuple of closed operation variants")
        if not self.operations:
            raise ValueError("Resolution operations must not be empty")
        if not isinstance(self.additional_assertions, tuple) or any(
            not isinstance(item, tuple(ASSERTION_TYPES.values())) for item in self.additional_assertions
        ):
            raise TypeError("Assertions must be a tuple of closed assertion variants")
        _sorted_unique(
            self.additional_assertions,
            "additional assertions",
            key=lambda assertion: assertion.assertion_id,
        )
        identifiers = tuple(item.operation_id for item in self.operations) + tuple(
            item.assertion_id for item in self.additional_assertions
        )
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate operation or assertion id")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResolutionSpecV2:
        data = _keys(data, cls, "resolution v2")
        return cls(
            data["schema_version"],
            data["intent_sha256"],
            TargetIdentity.from_dict(data["target"]),
            data["source_bundle_sha256"],
            _backend(data["backend"]),
            tuple(_operation(item) for item in _array(data["operations"], "operations")),
            tuple(
                _assertion(item)
                for item in _array(data["additional_assertions"], "additional assertions")
            ),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)
