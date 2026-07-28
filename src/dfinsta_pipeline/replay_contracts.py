from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import string
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .contracts import ArtifactRef, GateDecision, canonical_sha256
from .executor import ExecutorCapability
from .port_contracts import IntentSpecV2, ResolutionSpecV3, SourceFile


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOWER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
CapabilityRole = Literal["install_framework", "decode", "build"]
BackendKind = Literal["apktool_full_rebuild", "stock_dex_graft"]
LogicalPath = Literal[
    "tool",
    "framework_apk",
    "framework_dir",
    "input_apk",
    "decoded_tree",
    "intermediate_apk",
]


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


def _sha256(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _identifier(value: object, label: str, *, lowercase: bool = False) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    pattern = LOWER_ID_PATTERN if lowercase else ID_PATTERN
    if not pattern.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _artifact(value: object, kind: str, label: str) -> None:
    if not isinstance(value, ArtifactRef):
        raise TypeError(f"{label} must be an ArtifactRef")
    if value.kind != kind:
        raise ValueError(f"Invalid {label} kind")


def _sorted_unique(values: tuple[Any, ...], label: str, *, key=lambda value: value) -> None:
    keys = tuple(key(value) for value in values)
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{label} must be sorted")
    if len(keys) != len(set(keys)):
        raise ValueError(f"Duplicate {label}")


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _decode_json(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid {label} JSON") from error


def _resolve_artifact(
    artifact_resolver: Callable[[ArtifactRef], bytes], artifact: ArtifactRef
) -> bytes:
    try:
        data = artifact_resolver(artifact)
    except Exception as error:
        raise ValueError(f"Unable to resolve {artifact.kind} artifact") from error
    if type(data) is not bytes:
        raise TypeError("Artifact resolver must return bytes")
    if len(data) != artifact.size:
        raise ValueError(f"{artifact.kind} artifact size mismatch")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError(f"{artifact.kind} artifact SHA-256 mismatch")
    return data


@dataclass(frozen=True, slots=True)
class ReplayRunSpecV1:
    schema_version: int
    run_id: str
    subject_sha256: str
    intent_sha256: str
    resolution_sha256: str
    source_manifest_sha256: str
    toolchain_profile_sha256: str
    executor_capability_sha256s: tuple[str, ...]
    gate_id: str
    gate_admission_sha256: str
    gate_prepared_sha256: str
    allowed_actor: str
    policy_revision: str
    apk_composition: Literal["monolithic"]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay run schema")
        _identifier(self.run_id, "replay run id")
        for value, label in (
            (self.subject_sha256, "run subject SHA-256"),
            (self.intent_sha256, "intent SHA-256"),
            (self.resolution_sha256, "resolution SHA-256"),
            (self.source_manifest_sha256, "source manifest SHA-256"),
            (self.toolchain_profile_sha256, "toolchain profile SHA-256"),
            (self.gate_admission_sha256, "gate admission SHA-256"),
            (self.gate_prepared_sha256, "gate prepared SHA-256"),
        ):
            _sha256(value, label)
        if not isinstance(self.executor_capability_sha256s, tuple) or any(
            type(value) is not str for value in self.executor_capability_sha256s
        ):
            raise TypeError("Executor capability SHA-256s must be a tuple of strings")
        if not self.executor_capability_sha256s:
            raise ValueError("Executor capability SHA-256s must not be empty")
        for value in self.executor_capability_sha256s:
            _sha256(value, "executor capability SHA-256")
        _sorted_unique(self.executor_capability_sha256s, "executor capability SHA-256s")
        _identifier(self.gate_id, "gate id")
        _identifier(self.allowed_actor, "allowed actor")
        if type(self.policy_revision) is not str:
            raise TypeError("Policy revision must be a string")
        if not self.policy_revision.strip() or len(self.policy_revision) > 128:
            raise ValueError("Invalid policy revision")
        if self.apk_composition != "monolithic":
            raise ValueError("Split APK sets are not supported")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayRunSpecV1:
        data = _keys(data, cls, "replay run")
        return cls(
            data["schema_version"],
            data["run_id"],
            data["subject_sha256"],
            data["intent_sha256"],
            data["resolution_sha256"],
            data["source_manifest_sha256"],
            data["toolchain_profile_sha256"],
            tuple(_array(data["executor_capability_sha256s"], "executor capability SHA-256s")),
            data["gate_id"],
            data["gate_admission_sha256"],
            data["gate_prepared_sha256"],
            data["allowed_actor"],
            data["policy_revision"],
            data["apk_composition"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayRunSpecV2:
    schema_version: int
    run_id: str
    subject_sha256: str
    intent_sha256: str
    resolution_sha256: str
    source_manifest_sha256: str
    toolchain_profile_sha256: str
    executor_capability_sha256s: tuple[str, ...]
    gate_id: str
    gate_admission_sha256: str
    gate_prepared_sha256: str
    gate_prepared_ref_sha256: str
    allowed_actor: str
    policy_revision: str
    apk_composition: Literal["monolithic"]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported replay run schema")
        _identifier(self.run_id, "replay run id")
        for value, label in (
            (self.subject_sha256, "run subject SHA-256"),
            (self.intent_sha256, "intent SHA-256"),
            (self.resolution_sha256, "resolution SHA-256"),
            (self.source_manifest_sha256, "source manifest SHA-256"),
            (self.toolchain_profile_sha256, "toolchain profile SHA-256"),
            (self.gate_admission_sha256, "gate admission SHA-256"),
            (self.gate_prepared_sha256, "gate prepared SHA-256"),
            (self.gate_prepared_ref_sha256, "gate prepared reference SHA-256"),
        ):
            _sha256(value, label)
        if not isinstance(self.executor_capability_sha256s, tuple) or any(
            type(value) is not str for value in self.executor_capability_sha256s
        ):
            raise TypeError("Executor capability SHA-256s must be a tuple of strings")
        if not self.executor_capability_sha256s:
            raise ValueError("Executor capability SHA-256s must not be empty")
        for value in self.executor_capability_sha256s:
            _sha256(value, "executor capability SHA-256")
        _sorted_unique(self.executor_capability_sha256s, "executor capability SHA-256s")
        _identifier(self.gate_id, "gate id")
        _identifier(self.allowed_actor, "allowed actor")
        if type(self.policy_revision) is not str:
            raise TypeError("Policy revision must be a string")
        if not self.policy_revision.strip() or len(self.policy_revision) > 128:
            raise ValueError("Invalid policy revision")
        if self.apk_composition != "monolithic":
            raise ValueError("Split APK sets are not supported")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayRunSpecV2:
        data = _keys(data, cls, "replay run")
        return cls(
            data["schema_version"],
            data["run_id"],
            data["subject_sha256"],
            data["intent_sha256"],
            data["resolution_sha256"],
            data["source_manifest_sha256"],
            data["toolchain_profile_sha256"],
            tuple(_array(data["executor_capability_sha256s"], "executor capability SHA-256s")),
            data["gate_id"],
            data["gate_admission_sha256"],
            data["gate_prepared_sha256"],
            data["gate_prepared_ref_sha256"],
            data["allowed_actor"],
            data["policy_revision"],
            data["apk_composition"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class SourceManifestV1:
    records: tuple[SourceFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or any(
            not isinstance(record, SourceFile) for record in self.records
        ):
            raise TypeError("Source manifest records must be a tuple of SourceFile objects")
        _sorted_unique(self.records, "source manifest records", key=lambda item: item.relative_path)

    @classmethod
    def from_json_value(cls, value: object) -> SourceManifestV1:
        return cls(
            tuple(
                SourceFile.from_dict(item)
                for item in _array(value, "source manifest records")
            )
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.records)


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    role: CapabilityRole
    executor_capability_sha256: str

    def __post_init__(self) -> None:
        if self.role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        _sha256(self.executor_capability_sha256, "executor capability SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityBinding:
        return cls(**_keys(data, cls, "capability binding"))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FrameworkRequirement:
    package_id: int
    apk_sha256: str

    def __post_init__(self) -> None:
        if type(self.package_id) is not int:
            raise TypeError("Framework package id must be an integer")
        if not 1 <= self.package_id <= 255:
            raise ValueError("Framework package id must be between 1 and 255")
        _sha256(self.apk_sha256, "framework APK SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameworkRequirement:
        return cls(**_keys(data, cls, "framework requirement"))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    tool_id: str
    artifact_kind: str
    artifact_sha256: str
    roles: tuple[CapabilityRole, ...]

    def __post_init__(self) -> None:
        _identifier(self.tool_id, "tool id", lowercase=True)
        _identifier(self.artifact_kind, "tool artifact kind")
        _sha256(self.artifact_sha256, "tool artifact SHA-256")
        if not isinstance(self.roles, tuple) or any(
            role not in {"install_framework", "decode", "build"} for role in self.roles
        ):
            raise TypeError("Tool roles must be a tuple of capability roles")
        if not self.roles:
            raise ValueError("Tool roles must not be empty")
        _sorted_unique(self.roles, "tool roles")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRequirement:
        data = _keys(data, cls, "tool requirement")
        return cls(
            data["tool_id"],
            data["artifact_kind"],
            data["artifact_sha256"],
            tuple(_array(data["roles"], "tool roles")),
        )

    @property
    def requirement_sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class RoleExecutionPlan:
    role: CapabilityRole
    tool_id: str
    arguments: tuple[tuple[str, LogicalPath], ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        required_slots = {
            "install_framework": {"framework_apk", "framework_dir"},
            "decode": {"input_apk", "decoded_tree"},
            "build": {"decoded_tree", "intermediate_apk"},
        }
        allowed_slots = {
            "install_framework": required_slots["install_framework"] | {"tool"},
            "decode": required_slots["decode"] | {"tool", "framework_dir"},
            "build": required_slots["build"] | {"tool", "framework_dir"},
        }
        if self.role not in required_slots:
            raise ValueError("Invalid execution plan role")
        _identifier(self.tool_id, "execution plan tool id", lowercase=True)
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
            for pair in self.arguments
        ):
            raise TypeError("Execution plan arguments must be a tuple of string pairs")
        names = tuple(pair[0] for pair in self.arguments)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("Execution plan argument names must be sorted and unique")
        if any(not name.isidentifier() for name in names):
            raise ValueError("Invalid execution plan argument name")
        slots = tuple(pair[1] for pair in self.arguments)
        if (
            len(slots) != len(set(slots))
            or not required_slots[self.role].issubset(slots)
            or not set(slots).issubset(allowed_slots[self.role])
        ):
            raise ValueError("Execution plan logical paths do not match role")
        if type(self.timeout_seconds) is not int:
            raise TypeError("Execution plan timeout must be an integer")
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("Execution plan timeout must be between 1 and 3600 seconds")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoleExecutionPlan:
        data = _keys(data, cls, "role execution plan")
        arguments = _array(data["arguments"], "execution plan arguments")
        return cls(
            data["role"],
            data["tool_id"],
            tuple(
                tuple(_array(pair, "execution plan argument"))
                for pair in arguments
            ),
            data["timeout_seconds"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ToolchainProfile:
    schema_version: int
    profile_id: str
    backend_kind: BackendKind
    capability_bindings: tuple[CapabilityBinding, ...]
    frameworks: tuple[FrameworkRequirement, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported toolchain profile schema")
        _identifier(self.profile_id, "profile id", lowercase=True)
        if self.backend_kind not in {"apktool_full_rebuild", "stock_dex_graft"}:
            raise ValueError("Invalid toolchain backend kind")
        if not isinstance(self.capability_bindings, tuple) or any(
            not isinstance(binding, CapabilityBinding) for binding in self.capability_bindings
        ):
            raise TypeError("Capability bindings must be a tuple of CapabilityBinding objects")
        _sorted_unique(self.capability_bindings, "capability bindings", key=lambda item: item.role)
        expected_roles = {"build", "decode"} | ({"install_framework"} if self.frameworks else set())
        if {binding.role for binding in self.capability_bindings} != expected_roles:
            raise ValueError("Toolchain capability roles do not match framework requirements")
        digests = tuple(
            binding.executor_capability_sha256 for binding in self.capability_bindings
        )
        if len(digests) != len(set(digests)):
            raise ValueError("Executor capability SHA-256s must be unique across roles")
        if not isinstance(self.frameworks, tuple) or any(
            not isinstance(requirement, FrameworkRequirement) for requirement in self.frameworks
        ):
            raise TypeError("Frameworks must be a tuple of FrameworkRequirement objects")
        _sorted_unique(
            self.frameworks,
            "framework requirements",
            key=lambda item: (item.package_id, item.apk_sha256),
        )
        package_ids = tuple(requirement.package_id for requirement in self.frameworks)
        apk_hashes = tuple(requirement.apk_sha256 for requirement in self.frameworks)
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("Duplicate framework package id")
        if len(apk_hashes) != len(set(apk_hashes)):
            raise ValueError("Duplicate framework APK SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolchainProfile:
        data = _keys(data, cls, "toolchain profile")
        return cls(
            data["schema_version"],
            data["profile_id"],
            data["backend_kind"],
            tuple(
                CapabilityBinding.from_dict(item)
                for item in _array(data["capability_bindings"], "capability bindings")
            ),
            tuple(
                FrameworkRequirement.from_dict(item)
                for item in _array(data["frameworks"], "framework requirements")
            ),
        )

    def binding(self, role: CapabilityRole) -> str:
        if role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        for binding in self.capability_bindings:
            if binding.role == role:
                return binding.executor_capability_sha256
        raise KeyError(role)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ToolchainProfileV2:
    schema_version: int
    profile_id: str
    backend_kind: BackendKind
    capability_bindings: tuple[CapabilityBinding, ...]
    frameworks: tuple[FrameworkRequirement, ...]
    tools: tuple[ToolRequirement, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported toolchain profile schema")
        _identifier(self.profile_id, "profile id", lowercase=True)
        if self.backend_kind not in {"apktool_full_rebuild", "stock_dex_graft"}:
            raise ValueError("Invalid toolchain backend kind")
        if not isinstance(self.capability_bindings, tuple) or any(
            not isinstance(binding, CapabilityBinding) for binding in self.capability_bindings
        ):
            raise TypeError("Capability bindings must be a tuple of CapabilityBinding objects")
        _sorted_unique(self.capability_bindings, "capability bindings", key=lambda item: item.role)
        if not isinstance(self.frameworks, tuple) or any(
            not isinstance(requirement, FrameworkRequirement) for requirement in self.frameworks
        ):
            raise TypeError("Frameworks must be a tuple of FrameworkRequirement objects")
        _sorted_unique(
            self.frameworks,
            "framework requirements",
            key=lambda item: (item.package_id, item.apk_sha256),
        )
        expected_roles = {"build", "decode"} | ({"install_framework"} if self.frameworks else set())
        if {binding.role for binding in self.capability_bindings} != expected_roles:
            raise ValueError("Toolchain capability roles do not match framework requirements")
        capability_hashes = tuple(
            binding.executor_capability_sha256 for binding in self.capability_bindings
        )
        if len(capability_hashes) != len(set(capability_hashes)):
            raise ValueError("Executor capability SHA-256s must be unique across roles")
        package_ids = tuple(requirement.package_id for requirement in self.frameworks)
        framework_hashes = tuple(requirement.apk_sha256 for requirement in self.frameworks)
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("Duplicate framework package id")
        if len(framework_hashes) != len(set(framework_hashes)):
            raise ValueError("Duplicate framework APK SHA-256")
        if not isinstance(self.tools, tuple) or any(
            not isinstance(requirement, ToolRequirement) for requirement in self.tools
        ):
            raise TypeError("Tools must be a tuple of ToolRequirement objects")
        if not self.tools:
            raise ValueError("Tools must not be empty")
        _sorted_unique(self.tools, "tool requirements", key=lambda item: item.tool_id)
        tool_hashes = tuple(requirement.artifact_sha256 for requirement in self.tools)
        if len(tool_hashes) != len(set(tool_hashes)):
            raise ValueError("Tool artifact SHA-256s must be unique")
        tool_roles = tuple(role for requirement in self.tools for role in requirement.roles)
        if len(tool_roles) != len(set(tool_roles)):
            raise ValueError("Tool roles must be assigned exactly once")
        if set(tool_roles) != expected_roles:
            raise ValueError("Tool roles do not match capability roles")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolchainProfileV2:
        data = _keys(data, cls, "toolchain profile")
        return cls(
            data["schema_version"],
            data["profile_id"],
            data["backend_kind"],
            tuple(
                CapabilityBinding.from_dict(item)
                for item in _array(data["capability_bindings"], "capability bindings")
            ),
            tuple(
                FrameworkRequirement.from_dict(item)
                for item in _array(data["frameworks"], "framework requirements")
            ),
            tuple(
                ToolRequirement.from_dict(item)
                for item in _array(data["tools"], "tool requirements")
            ),
        )

    def binding(self, role: CapabilityRole) -> str:
        if role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        for binding in self.capability_bindings:
            if binding.role == role:
                return binding.executor_capability_sha256
        raise KeyError(role)

    def tool_for_role(self, role: CapabilityRole) -> ToolRequirement:
        if role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        for requirement in self.tools:
            if role in requirement.roles:
                return requirement
        raise KeyError(role)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ToolchainProfileV3:
    schema_version: int
    profile_id: str
    backend_kind: BackendKind
    capability_bindings: tuple[CapabilityBinding, ...]
    frameworks: tuple[FrameworkRequirement, ...]
    tools: tuple[ToolRequirement, ...]
    execution_plans: tuple[RoleExecutionPlan, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 3:
            raise ValueError("Unsupported toolchain profile schema")
        base = ToolchainProfileV2(
            2,
            self.profile_id,
            self.backend_kind,
            self.capability_bindings,
            self.frameworks,
            self.tools,
        )
        expected_roles = {binding.role for binding in base.capability_bindings}
        if not isinstance(self.execution_plans, tuple) or any(
            not isinstance(plan, RoleExecutionPlan) for plan in self.execution_plans
        ):
            raise TypeError("Execution plans must be a tuple of RoleExecutionPlan objects")
        _sorted_unique(self.execution_plans, "execution plans", key=lambda item: item.role)
        if {plan.role for plan in self.execution_plans} != expected_roles:
            raise ValueError("Execution plans do not match capability roles")
        tools_by_id = {tool.tool_id: tool for tool in self.tools}
        if any(
            plan.tool_id not in tools_by_id or plan.role not in tools_by_id[plan.tool_id].roles
            for plan in self.execution_plans
        ):
            raise ValueError("Execution plans do not match tool role assignments")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolchainProfileV3:
        data = _keys(data, cls, "toolchain profile")
        return cls(
            data["schema_version"],
            data["profile_id"],
            data["backend_kind"],
            tuple(
                CapabilityBinding.from_dict(item)
                for item in _array(data["capability_bindings"], "capability bindings")
            ),
            tuple(
                FrameworkRequirement.from_dict(item)
                for item in _array(data["frameworks"], "framework requirements")
            ),
            tuple(
                ToolRequirement.from_dict(item)
                for item in _array(data["tools"], "tool requirements")
            ),
            tuple(
                RoleExecutionPlan.from_dict(item)
                for item in _array(data["execution_plans"], "execution plans")
            ),
        )

    def binding(self, role: CapabilityRole) -> str:
        return ToolchainProfileV2(
            2,
            self.profile_id,
            self.backend_kind,
            self.capability_bindings,
            self.frameworks,
            self.tools,
        ).binding(role)

    def tool_for_role(self, role: CapabilityRole) -> ToolRequirement:
        return ToolchainProfileV2(
            2,
            self.profile_id,
            self.backend_kind,
            self.capability_bindings,
            self.frameworks,
            self.tools,
        ).tool_for_role(role)

    def plan(self, role: CapabilityRole) -> RoleExecutionPlan:
        if role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        for plan in self.execution_plans:
            if plan.role == role:
                return plan
        raise KeyError(role)

    def validate_capability(
        self, role: CapabilityRole, capability: ExecutorCapability
    ) -> RoleExecutionPlan:
        if type(capability) is not ExecutorCapability:
            raise TypeError("Capability must be an ExecutorCapability")
        if capability.canonical_identity != self.binding(role):
            raise ValueError("Executor capability does not match execution plan role")
        plan = self.plan(role)
        placeholders = {
            field_name
            for template in capability.argv_template
            for _, field_name, _, _ in string.Formatter().parse(template)
            if field_name is not None
        }
        argument_names = tuple(name for name, _ in plan.arguments)
        if placeholders != set(capability.path_arguments) or argument_names != capability.path_arguments:
            raise ValueError("Execution plan arguments do not match capability path arguments")
        tool = self.tool_for_role(role)
        if "tool" not in {slot for _, slot in plan.arguments} and (
            capability.executable_sha256 != tool.artifact_sha256
        ):
            raise ValueError("Native tool does not match capability executable")
        return plan

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FrameworkArtifact:
    package_id: int
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        if type(self.package_id) is not int:
            raise TypeError("Framework package id must be an integer")
        if not 1 <= self.package_id <= 255:
            raise ValueError("Framework package id must be between 1 and 255")
        _artifact(self.artifact, "framework-apk", "framework APK")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameworkArtifact:
        data = _keys(data, cls, "framework artifact")
        return cls(data["package_id"], ArtifactRef.from_dict(data["artifact"]))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ToolArtifact:
    tool_id: str
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        _identifier(self.tool_id, "tool id", lowercase=True)
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("Tool artifact must be an ArtifactRef")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolArtifact:
        data = _keys(data, cls, "tool artifact")
        return cls(data["tool_id"], ArtifactRef.from_dict(data["artifact"]))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class GatePreparedEnvelopeV2:
    schema_version: int
    stock_apk: ArtifactRef
    intent: ArtifactRef
    resolution: ArtifactRef
    source_manifest: ArtifactRef
    toolchain_profile: ArtifactRef
    frameworks: tuple[FrameworkArtifact, ...]
    tools: tuple[ToolArtifact, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported gate prepared envelope schema")
        for value, kind, label in (
            (self.stock_apk, "stock-apk", "stock APK"),
            (self.intent, "intent-spec", "intent"),
            (self.resolution, "resolution-spec", "resolution"),
            (self.source_manifest, "source-manifest-v1", "source manifest"),
            (self.toolchain_profile, "toolchain-profile", "toolchain profile"),
        ):
            _artifact(value, kind, label)
        if not isinstance(self.frameworks, tuple) or any(
            not isinstance(framework, FrameworkArtifact) for framework in self.frameworks
        ):
            raise TypeError("Frameworks must be a tuple of FrameworkArtifact objects")
        _sorted_unique(self.frameworks, "framework artifacts", key=lambda item: item.package_id)
        if not isinstance(self.tools, tuple) or any(
            not isinstance(tool, ToolArtifact) for tool in self.tools
        ):
            raise TypeError("Tools must be a tuple of ToolArtifact objects")
        if not self.tools:
            raise ValueError("Tools must not be empty")
        _sorted_unique(self.tools, "tool artifacts", key=lambda item: item.tool_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatePreparedEnvelopeV2:
        data = _keys(data, cls, "gate prepared envelope")
        return cls(
            data["schema_version"],
            ArtifactRef.from_dict(data["stock_apk"]),
            ArtifactRef.from_dict(data["intent"]),
            ArtifactRef.from_dict(data["resolution"]),
            ArtifactRef.from_dict(data["source_manifest"]),
            ArtifactRef.from_dict(data["toolchain_profile"]),
            tuple(
                FrameworkArtifact.from_dict(item)
                for item in _array(data["frameworks"], "framework artifacts")
            ),
            tuple(
                ToolArtifact.from_dict(item)
                for item in _array(data["tools"], "tool artifacts")
            ),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    schema_version: int
    run_spec_sha256: str
    stock_apk: ArtifactRef
    intent: ArtifactRef
    resolution: ArtifactRef
    source_manifest: ArtifactRef
    toolchain_profile: ArtifactRef
    frameworks: tuple[FrameworkArtifact, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay request schema")
        _sha256(self.run_spec_sha256, "run specification SHA-256")
        for value, kind, label in (
            (self.stock_apk, "stock-apk", "stock APK"),
            (self.intent, "intent-spec", "intent"),
            (self.resolution, "resolution-spec", "resolution"),
            (self.source_manifest, "source-manifest-v1", "source manifest"),
            (self.toolchain_profile, "toolchain-profile", "toolchain profile"),
        ):
            _artifact(value, kind, label)
        if not isinstance(self.frameworks, tuple) or any(
            not isinstance(framework, FrameworkArtifact) for framework in self.frameworks
        ):
            raise TypeError("Frameworks must be a tuple of FrameworkArtifact objects")
        _sorted_unique(self.frameworks, "framework artifacts", key=lambda item: item.package_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayRequest:
        data = _keys(data, cls, "replay request")
        return cls(
            data["schema_version"],
            data["run_spec_sha256"],
            ArtifactRef.from_dict(data["stock_apk"]),
            ArtifactRef.from_dict(data["intent"]),
            ArtifactRef.from_dict(data["resolution"]),
            ArtifactRef.from_dict(data["source_manifest"]),
            ArtifactRef.from_dict(data["toolchain_profile"]),
            tuple(
                FrameworkArtifact.from_dict(item)
                for item in _array(data["frameworks"], "framework artifacts")
            ),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayRequestV2:
    schema_version: int
    run_spec_sha256: str
    gate_prepared: ArtifactRef
    stock_apk: ArtifactRef
    intent: ArtifactRef
    resolution: ArtifactRef
    source_manifest: ArtifactRef
    toolchain_profile: ArtifactRef
    frameworks: tuple[FrameworkArtifact, ...]
    tools: tuple[ToolArtifact, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported replay request schema")
        _sha256(self.run_spec_sha256, "run specification SHA-256")
        _artifact(
            self.gate_prepared,
            "replay-gate-prepared-v2",
            "gate prepared envelope",
        )
        for value, kind, label in (
            (self.stock_apk, "stock-apk", "stock APK"),
            (self.intent, "intent-spec", "intent"),
            (self.resolution, "resolution-spec", "resolution"),
            (self.source_manifest, "source-manifest-v1", "source manifest"),
            (self.toolchain_profile, "toolchain-profile", "toolchain profile"),
        ):
            _artifact(value, kind, label)
        if not isinstance(self.frameworks, tuple) or any(
            not isinstance(framework, FrameworkArtifact) for framework in self.frameworks
        ):
            raise TypeError("Frameworks must be a tuple of FrameworkArtifact objects")
        _sorted_unique(self.frameworks, "framework artifacts", key=lambda item: item.package_id)
        if not isinstance(self.tools, tuple) or any(
            not isinstance(tool, ToolArtifact) for tool in self.tools
        ):
            raise TypeError("Tools must be a tuple of ToolArtifact objects")
        if not self.tools:
            raise ValueError("Tools must not be empty")
        _sorted_unique(self.tools, "tool artifacts", key=lambda item: item.tool_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayRequestV2:
        data = _keys(data, cls, "replay request")
        return cls(
            data["schema_version"],
            data["run_spec_sha256"],
            ArtifactRef.from_dict(data["gate_prepared"]),
            ArtifactRef.from_dict(data["stock_apk"]),
            ArtifactRef.from_dict(data["intent"]),
            ArtifactRef.from_dict(data["resolution"]),
            ArtifactRef.from_dict(data["source_manifest"]),
            ArtifactRef.from_dict(data["toolchain_profile"]),
            tuple(
                FrameworkArtifact.from_dict(item)
                for item in _array(data["frameworks"], "framework artifacts")
            ),
            tuple(
                ToolArtifact.from_dict(item)
                for item in _array(data["tools"], "tool artifacts")
            ),
        )

    @property
    def direct_artifacts(self) -> tuple[ArtifactRef, ...]:
        return (
            self.gate_prepared,
            self.stock_apk,
            self.intent,
            self.resolution,
            self.source_manifest,
            self.toolchain_profile,
            *(framework.artifact for framework in self.frameworks),
            *(tool.artifact for tool in self.tools),
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def _gate_prepared_input_hashes(
    gate_prepared: GatePreparedEnvelopeV2,
) -> tuple[str, ...]:
    return (
        gate_prepared.stock_apk.sha256,
        gate_prepared.intent.sha256,
        gate_prepared.resolution.sha256,
        gate_prepared.source_manifest.sha256,
        gate_prepared.toolchain_profile.sha256,
        *(framework.artifact.sha256 for framework in gate_prepared.frameworks),
        *(tool.artifact.sha256 for tool in gate_prepared.tools),
    )


def _validate_gate_prepared_relationship(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    gate_prepared: GatePreparedEnvelopeV2,
) -> None:
    if request.gate_prepared.sha256 != run_spec.gate_prepared_sha256:
        raise ValueError("Replay run does not bind the gate prepared bytes")
    if canonical_sha256(request.gate_prepared) != run_spec.gate_prepared_ref_sha256:
        raise ValueError("Replay request does not bind the gate prepared envelope")
    if gate_prepared.sha256 != request.gate_prepared.sha256:
        raise ValueError("Gate prepared envelope bytes are not canonical")
    if request.gate_prepared.input_hashes != _gate_prepared_input_hashes(gate_prepared):
        raise ValueError("Gate prepared envelope input lineage is incomplete")
    prepared_content = (
        request.stock_apk,
        request.intent,
        request.resolution,
        request.source_manifest,
        request.toolchain_profile,
        request.frameworks,
        request.tools,
    )
    envelope_content = (
        gate_prepared.stock_apk,
        gate_prepared.intent,
        gate_prepared.resolution,
        gate_prepared.source_manifest,
        gate_prepared.toolchain_profile,
        gate_prepared.frameworks,
        gate_prepared.tools,
    )
    if prepared_content != envelope_content:
        raise ValueError("Replay request does not match the gate prepared envelope")


def _validate_admitted_relationships(
    run_spec: ReplayRunSpecV1,
    request: ReplayRequest,
    decision: GateDecision,
    intent: IntentSpecV2,
    resolution: ResolutionSpecV3,
    source_manifest: SourceManifestV1,
    profile: ToolchainProfile,
) -> None:
    if not isinstance(run_spec, ReplayRunSpecV1):
        raise TypeError("Run specification must be a ReplayRunSpecV1")
    if not isinstance(request, ReplayRequest):
        raise TypeError("Request must be a ReplayRequest")
    if not isinstance(decision, GateDecision):
        raise TypeError("Decision must be a GateDecision")
    if not isinstance(intent, IntentSpecV2):
        raise TypeError("Intent must be an IntentSpecV2")
    if not isinstance(resolution, ResolutionSpecV3):
        raise TypeError("Resolution must be a ResolutionSpecV3")
    if not isinstance(source_manifest, SourceManifestV1):
        raise TypeError("Source manifest must be a SourceManifestV1")
    if not isinstance(profile, ToolchainProfile):
        raise TypeError("Profile must be a ToolchainProfile")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    _validate_admitted_content_relationships(
        run_spec, request, intent, resolution, source_manifest, profile
    )


def _validate_decision(run_spec: ReplayRunSpecV1, decision: GateDecision) -> None:
    if decision.decision != "approve":
        raise ValueError("Replay requires an approval decision")
    if decision.actor != run_spec.allowed_actor:
        raise ValueError("Gate decision actor does not bind the replay run")
    if decision.run_id != run_spec.run_id:
        raise ValueError("Gate decision run id does not bind the replay run")
    if decision.gate_id != run_spec.gate_id:
        raise ValueError("Gate decision gate id does not bind the replay run")
    if decision.subject_sha256 != run_spec.sha256:
        raise ValueError("Gate decision subject does not bind the replay run")
    if decision.admission_sha256 != run_spec.gate_admission_sha256:
        raise ValueError("Gate decision admission does not bind the replay run")
    if decision.prepared_sha256 != run_spec.gate_prepared_sha256:
        raise ValueError("Gate decision prepared state does not bind the replay run")
    if decision.policy_revision != run_spec.policy_revision:
        raise ValueError("Gate decision policy does not bind the replay run")


def _validate_admitted_content_relationships(
    run_spec: ReplayRunSpecV1,
    request: ReplayRequest,
    intent: IntentSpecV2,
    resolution: ResolutionSpecV3,
    source_manifest: SourceManifestV1,
    profile: ToolchainProfile,
) -> None:
    if not (
        request.stock_apk.sha256
        == run_spec.subject_sha256
        == resolution.target.apk_sha256
    ):
        raise ValueError("Stock APK does not bind the admitted target")
    if run_spec.intent_sha256 != intent.sha256 or resolution.intent_sha256 != intent.sha256:
        raise ValueError("Intent does not bind the admitted run and resolution")
    if run_spec.resolution_sha256 != resolution.sha256:
        raise ValueError("Resolution does not bind the admitted run")
    if not (
        source_manifest.sha256
        == run_spec.source_manifest_sha256
        == resolution.source_bundle_sha256
    ):
        raise ValueError("Source manifest does not bind the admitted run and resolution")
    if run_spec.toolchain_profile_sha256 != profile.sha256:
        raise ValueError("Toolchain profile does not bind the admitted run")
    if profile.profile_id != resolution.backend.profile_id:
        raise ValueError("Toolchain profile id does not bind the resolution backend")
    if profile.backend_kind != resolution.backend.kind:
        raise ValueError("Toolchain backend kind does not bind the resolution backend")
    if run_spec.policy_revision != intent.policy_revision:
        raise ValueError("Policy revision does not bind the admitted intent")

    profile_capabilities = tuple(
        sorted(binding.executor_capability_sha256 for binding in profile.capability_bindings)
    )
    if run_spec.executor_capability_sha256s != profile_capabilities:
        raise ValueError("Executor capabilities do not exactly match the toolchain profile")
    requested_frameworks = tuple(
        (framework.package_id, framework.artifact.sha256) for framework in request.frameworks
    )
    required_frameworks = tuple(
        (requirement.package_id, requirement.apk_sha256) for requirement in profile.frameworks
    )
    if requested_frameworks != required_frameworks:
        raise ValueError("Framework artifacts do not exactly match the toolchain profile")
    has_installer = any(
        binding.role == "install_framework" for binding in profile.capability_bindings
    )
    if has_installer != bool(request.frameworks):
        raise ValueError("Framework artifacts do not match the installer capability")


@dataclass(frozen=True, slots=True)
class AdmittedReplay:
    schema_version: int
    run_spec: ReplayRunSpecV1
    request: ReplayRequest
    decision: GateDecision
    intent: IntentSpecV2
    resolution: ResolutionSpecV3
    source_manifest: SourceManifestV1
    profile: ToolchainProfile

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported admitted replay schema")
        _validate_admitted_relationships(
            self.run_spec,
            self.request,
            self.decision,
            self.intent,
            self.resolution,
            self.source_manifest,
            self.profile,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmittedReplay:
        data = _keys(data, cls, "admitted replay")
        source_manifest_data = _keys(
            data["source_manifest"], SourceManifestV1, "source manifest wrapper"
        )
        return cls(
            data["schema_version"],
            ReplayRunSpecV1.from_dict(data["run_spec"]),
            ReplayRequest.from_dict(data["request"]),
            GateDecision.from_dict(data["decision"]),
            IntentSpecV2.from_dict(data["intent"]),
            ResolutionSpecV3.from_dict(data["resolution"]),
            SourceManifestV1.from_json_value(source_manifest_data["records"]),
            ToolchainProfile.from_dict(data["profile"]),
        )

    @property
    def run_spec_sha256(self) -> str:
        return self.run_spec.sha256

    @property
    def replay_request_sha256(self) -> str:
        return self.request.sha256

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.decision)

    @property
    def intent_sha256(self) -> str:
        return self.intent.sha256

    @property
    def resolution_sha256(self) -> str:
        return self.resolution.sha256

    @property
    def source_manifest_sha256(self) -> str:
        return self.source_manifest.sha256

    @property
    def toolchain_profile_sha256(self) -> str:
        return self.profile.sha256

    @property
    def capability_bindings(self) -> tuple[CapabilityBinding, ...]:
        return self.profile.capability_bindings

    @property
    def decode_executor_capability_sha256(self) -> str:
        return self.profile.binding("decode")

    @property
    def build_executor_capability_sha256(self) -> str:
        return self.profile.binding("build")

    @property
    def install_framework_executor_capability_sha256(self) -> str | None:
        return self.profile.binding("install_framework") if self.profile.frameworks else None

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def admit_replay(
    run_spec: ReplayRunSpecV1,
    request: ReplayRequest,
    decision: GateDecision,
    decision_is_recorded: Callable[[GateDecision], bool],
    artifact_resolver: Callable[[ArtifactRef], bytes],
) -> AdmittedReplay:
    if not isinstance(run_spec, ReplayRunSpecV1) or not isinstance(request, ReplayRequest):
        raise TypeError("Replay admission requires replay contracts")
    if not isinstance(decision, GateDecision):
        raise TypeError("Replay admission requires a gate decision")
    if not callable(decision_is_recorded):
        raise TypeError("Decision recording predicate must be callable")
    if not callable(artifact_resolver):
        raise TypeError("Artifact resolver must be callable")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    try:
        recorded = decision_is_recorded(decision)
    except Exception as error:
        raise ValueError("Unable to verify recorded gate decision") from error
    if type(recorded) is not bool:
        raise TypeError("Decision recording predicate must return a boolean")
    if not recorded:
        raise ValueError("Gate decision is not recorded")

    _resolve_artifact(artifact_resolver, request.stock_apk)
    intent_bytes = _resolve_artifact(artifact_resolver, request.intent)
    resolution_bytes = _resolve_artifact(artifact_resolver, request.resolution)
    source_manifest_bytes = _resolve_artifact(artifact_resolver, request.source_manifest)
    profile_bytes = _resolve_artifact(artifact_resolver, request.toolchain_profile)
    for framework in request.frameworks:
        _resolve_artifact(artifact_resolver, framework.artifact)

    intent = IntentSpecV2.from_dict(_decode_json(intent_bytes, "intent"))
    resolution = ResolutionSpecV3.from_dict(_decode_json(resolution_bytes, "resolution"))
    source_manifest = SourceManifestV1.from_json_value(
        _decode_json(source_manifest_bytes, "source manifest")
    )
    profile = ToolchainProfile.from_dict(_decode_json(profile_bytes, "toolchain profile"))

    return AdmittedReplay(
        1,
        run_spec,
        request,
        decision,
        intent,
        resolution,
        source_manifest,
        profile,
    )


def _validate_admitted_relationships_v2(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    decision: GateDecision,
    intent: IntentSpecV2,
    resolution: ResolutionSpecV3,
    source_manifest: SourceManifestV1,
    profile: ToolchainProfileV2,
    gate_prepared: GatePreparedEnvelopeV2,
) -> None:
    if not isinstance(run_spec, ReplayRunSpecV2):
        raise TypeError("Run specification must be a ReplayRunSpecV2")
    if not isinstance(request, ReplayRequestV2):
        raise TypeError("Request must be a ReplayRequestV2")
    if not isinstance(decision, GateDecision):
        raise TypeError("Decision must be a GateDecision")
    if not isinstance(intent, IntentSpecV2):
        raise TypeError("Intent must be an IntentSpecV2")
    if not isinstance(resolution, ResolutionSpecV3):
        raise TypeError("Resolution must be a ResolutionSpecV3")
    if not isinstance(source_manifest, SourceManifestV1):
        raise TypeError("Source manifest must be a SourceManifestV1")
    if not isinstance(profile, ToolchainProfileV2):
        raise TypeError("Profile must be a ToolchainProfileV2")
    if not isinstance(gate_prepared, GatePreparedEnvelopeV2):
        raise TypeError("Gate prepared envelope must be a GatePreparedEnvelopeV2")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    _validate_gate_prepared_relationship(run_spec, request, gate_prepared)
    _validate_admitted_content_relationships(
        run_spec, request, intent, resolution, source_manifest, profile
    )
    requested_tools = tuple(
        (tool.tool_id, tool.artifact.kind, tool.artifact.sha256) for tool in request.tools
    )
    required_tools = tuple(
        (tool.tool_id, tool.artifact_kind, tool.artifact_sha256) for tool in profile.tools
    )
    if requested_tools != required_tools:
        raise ValueError("Tool artifacts do not exactly match the toolchain profile")


@dataclass(frozen=True, slots=True)
class AdmittedReplayV2:
    """Relationally validated replay data; ledger recording grants execution authority."""

    schema_version: int
    run_spec: ReplayRunSpecV2
    request: ReplayRequestV2
    decision: GateDecision
    intent: IntentSpecV2
    resolution: ResolutionSpecV3
    source_manifest: SourceManifestV1
    profile: ToolchainProfileV2
    gate_prepared: GatePreparedEnvelopeV2

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported admitted replay schema")
        _validate_admitted_relationships_v2(
            self.run_spec,
            self.request,
            self.decision,
            self.intent,
            self.resolution,
            self.source_manifest,
            self.profile,
            self.gate_prepared,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmittedReplayV2:
        data = _keys(data, cls, "admitted replay")
        source_manifest_data = _keys(
            data["source_manifest"], SourceManifestV1, "source manifest wrapper"
        )
        return cls(
            data["schema_version"],
            ReplayRunSpecV2.from_dict(data["run_spec"]),
            ReplayRequestV2.from_dict(data["request"]),
            GateDecision.from_dict(data["decision"]),
            IntentSpecV2.from_dict(data["intent"]),
            ResolutionSpecV3.from_dict(data["resolution"]),
            SourceManifestV1.from_json_value(source_manifest_data["records"]),
            ToolchainProfileV2.from_dict(data["profile"]),
            GatePreparedEnvelopeV2.from_dict(data["gate_prepared"]),
        )

    @property
    def run_spec_sha256(self) -> str:
        return self.run_spec.sha256

    @property
    def replay_request_sha256(self) -> str:
        return self.request.sha256

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.decision)

    @property
    def intent_sha256(self) -> str:
        return self.intent.sha256

    @property
    def resolution_sha256(self) -> str:
        return self.resolution.sha256

    @property
    def source_manifest_sha256(self) -> str:
        return self.source_manifest.sha256

    @property
    def toolchain_profile_sha256(self) -> str:
        return self.profile.sha256

    @property
    def capability_bindings(self) -> tuple[CapabilityBinding, ...]:
        return self.profile.capability_bindings

    @property
    def decode_executor_capability_sha256(self) -> str:
        return self.profile.binding("decode")

    @property
    def build_executor_capability_sha256(self) -> str:
        return self.profile.binding("build")

    @property
    def install_framework_executor_capability_sha256(self) -> str | None:
        return self.profile.binding("install_framework") if self.profile.frameworks else None

    @property
    def direct_artifacts(self) -> tuple[ArtifactRef, ...]:
        return self.request.direct_artifacts

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def admit_replay_v2(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    decision: GateDecision,
    decision_is_recorded: Callable[[GateDecision], bool],
    artifact_resolver: Callable[[ArtifactRef], bytes],
) -> AdmittedReplayV2:
    if not isinstance(run_spec, ReplayRunSpecV2) or not isinstance(request, ReplayRequestV2):
        raise TypeError("Replay admission requires replay contracts")
    if not isinstance(decision, GateDecision):
        raise TypeError("Replay admission requires a gate decision")
    if not callable(decision_is_recorded):
        raise TypeError("Decision recording predicate must be callable")
    if not callable(artifact_resolver):
        raise TypeError("Artifact resolver must be callable")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    try:
        recorded = decision_is_recorded(decision)
    except Exception as error:
        raise ValueError("Unable to verify recorded gate decision") from error
    if type(recorded) is not bool:
        raise TypeError("Decision recording predicate must return a boolean")
    if not recorded:
        raise ValueError("Gate decision is not recorded")

    if canonical_sha256(request.gate_prepared) != run_spec.gate_prepared_ref_sha256:
        raise ValueError("Replay request does not bind the gate prepared envelope")
    gate_prepared_bytes = _resolve_artifact(artifact_resolver, request.gate_prepared)
    gate_prepared = GatePreparedEnvelopeV2.from_dict(
        _decode_json(gate_prepared_bytes, "gate prepared envelope")
    )
    _validate_gate_prepared_relationship(run_spec, request, gate_prepared)
    _resolve_artifact(artifact_resolver, request.stock_apk)
    intent_bytes = _resolve_artifact(artifact_resolver, request.intent)
    resolution_bytes = _resolve_artifact(artifact_resolver, request.resolution)
    source_manifest_bytes = _resolve_artifact(artifact_resolver, request.source_manifest)
    profile_bytes = _resolve_artifact(artifact_resolver, request.toolchain_profile)
    for framework in request.frameworks:
        _resolve_artifact(artifact_resolver, framework.artifact)
    for tool in request.tools:
        _resolve_artifact(artifact_resolver, tool.artifact)

    intent = IntentSpecV2.from_dict(_decode_json(intent_bytes, "intent"))
    resolution = ResolutionSpecV3.from_dict(_decode_json(resolution_bytes, "resolution"))
    source_manifest = SourceManifestV1.from_json_value(
        _decode_json(source_manifest_bytes, "source manifest")
    )
    profile = ToolchainProfileV2.from_dict(_decode_json(profile_bytes, "toolchain profile"))

    return AdmittedReplayV2(
        2,
        run_spec,
        request,
        decision,
        intent,
        resolution,
        source_manifest,
        profile,
        gate_prepared,
    )


def _validate_admitted_relationships_v3_content(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    decision: GateDecision,
    intent: IntentSpecV2,
    resolution: ResolutionSpecV3,
    source_manifest: SourceManifestV1,
    profile: ToolchainProfileV3,
    gate_prepared: GatePreparedEnvelopeV2,
) -> None:
    if not isinstance(run_spec, ReplayRunSpecV2):
        raise TypeError("Run specification must be a ReplayRunSpecV2")
    if not isinstance(request, ReplayRequestV2):
        raise TypeError("Request must be a ReplayRequestV2")
    if not isinstance(decision, GateDecision):
        raise TypeError("Decision must be a GateDecision")
    if not isinstance(intent, IntentSpecV2):
        raise TypeError("Intent must be an IntentSpecV2")
    if not isinstance(resolution, ResolutionSpecV3):
        raise TypeError("Resolution must be a ResolutionSpecV3")
    if not isinstance(source_manifest, SourceManifestV1):
        raise TypeError("Source manifest must be a SourceManifestV1")
    if not isinstance(profile, ToolchainProfileV3):
        raise TypeError("Profile must be a ToolchainProfileV3")
    if not isinstance(gate_prepared, GatePreparedEnvelopeV2):
        raise TypeError("Gate prepared envelope must be a GatePreparedEnvelopeV2")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    _validate_gate_prepared_relationship(run_spec, request, gate_prepared)
    _validate_admitted_content_relationships(
        run_spec, request, intent, resolution, source_manifest, profile
    )
    requested_tools = tuple(
        (tool.tool_id, tool.artifact.kind, tool.artifact.sha256) for tool in request.tools
    )
    required_tools = tuple(
        (tool.tool_id, tool.artifact_kind, tool.artifact_sha256) for tool in profile.tools
    )
    if requested_tools != required_tools:
        raise ValueError("Tool artifacts do not exactly match the toolchain profile")


@dataclass(frozen=True, slots=True)
class AdmittedReplayV3:
    """Relational admission; ledger recording grants execution authority."""

    schema_version: int
    run_spec: ReplayRunSpecV2
    request: ReplayRequestV2
    decision: GateDecision
    intent: IntentSpecV2
    resolution: ResolutionSpecV3
    source_manifest: SourceManifestV1
    profile: ToolchainProfileV3
    gate_prepared: GatePreparedEnvelopeV2
    executor_capabilities: tuple[ExecutorCapability, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 3:
            raise ValueError("Unsupported admitted replay schema")
        _validate_admitted_relationships_v3_content(
            self.run_spec,
            self.request,
            self.decision,
            self.intent,
            self.resolution,
            self.source_manifest,
            self.profile,
            self.gate_prepared,
        )
        if not isinstance(self.executor_capabilities, tuple) or any(
            type(capability) is not ExecutorCapability
            for capability in self.executor_capabilities
        ):
            raise TypeError(
                "Executor capabilities must be a tuple of ExecutorCapability objects"
            )
        if len(self.executor_capabilities) != len(self.profile.capability_bindings):
            raise ValueError(
                "Executor capabilities do not exactly match the toolchain profile"
            )
        for binding, capability in zip(
            self.profile.capability_bindings,
            self.executor_capabilities,
            strict=True,
        ):
            if capability.canonical_identity != binding.executor_capability_sha256:
                raise ValueError(
                    "Executor capabilities do not exactly match the toolchain profile"
                )
            self.profile.validate_capability(binding.role, capability)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmittedReplayV3:
        data = _keys(data, cls, "admitted replay")
        source_manifest_data = _keys(
            data["source_manifest"], SourceManifestV1, "source manifest wrapper"
        )
        return cls(
            data["schema_version"],
            ReplayRunSpecV2.from_dict(data["run_spec"]),
            ReplayRequestV2.from_dict(data["request"]),
            GateDecision.from_dict(data["decision"]),
            IntentSpecV2.from_dict(data["intent"]),
            ResolutionSpecV3.from_dict(data["resolution"]),
            SourceManifestV1.from_json_value(source_manifest_data["records"]),
            ToolchainProfileV3.from_dict(data["profile"]),
            GatePreparedEnvelopeV2.from_dict(data["gate_prepared"]),
            tuple(
                ExecutorCapability.from_dict(item)
                for item in _array(
                    data["executor_capabilities"], "executor capabilities"
                )
            ),
        )

    def capability(self, role: CapabilityRole) -> ExecutorCapability:
        if role not in {"install_framework", "decode", "build"}:
            raise ValueError("Invalid capability role")
        for binding, capability in zip(
            self.profile.capability_bindings,
            self.executor_capabilities,
            strict=True,
        ):
            if binding.role == role:
                return capability
        raise KeyError(role)

    def plan(self, role: CapabilityRole) -> RoleExecutionPlan:
        return self.profile.plan(role)

    @property
    def run_spec_sha256(self) -> str:
        return self.run_spec.sha256

    @property
    def replay_request_sha256(self) -> str:
        return self.request.sha256

    @property
    def decision_sha256(self) -> str:
        return canonical_sha256(self.decision)

    @property
    def intent_sha256(self) -> str:
        return self.intent.sha256

    @property
    def resolution_sha256(self) -> str:
        return self.resolution.sha256

    @property
    def source_manifest_sha256(self) -> str:
        return self.source_manifest.sha256

    @property
    def toolchain_profile_sha256(self) -> str:
        return self.profile.sha256

    @property
    def capability_bindings(self) -> tuple[CapabilityBinding, ...]:
        return self.profile.capability_bindings

    @property
    def decode_executor_capability_sha256(self) -> str:
        return self.profile.binding("decode")

    @property
    def build_executor_capability_sha256(self) -> str:
        return self.profile.binding("build")

    @property
    def install_framework_executor_capability_sha256(self) -> str | None:
        return self.profile.binding("install_framework") if self.profile.frameworks else None

    @property
    def direct_artifacts(self) -> tuple[ArtifactRef, ...]:
        return self.request.direct_artifacts

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayDecodeCheckpointResultV1:
    schema_version: int
    admitted_replay_sha256: str
    role: Literal["decode"]
    execution_plan_sha256: str
    executor_capability_sha256: str
    tool_artifact_sha256: str
    execution_request_sha256: str
    returncode: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay decode checkpoint result schema")
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.execution_plan_sha256, "execution plan SHA-256"),
            (self.executor_capability_sha256, "executor capability SHA-256"),
            (self.tool_artifact_sha256, "tool artifact SHA-256"),
            (self.execution_request_sha256, "execution request SHA-256"),
        ):
            _sha256(value, label)
        if self.role != "decode":
            raise ValueError("Replay decode checkpoint role must be decode")
        if type(self.returncode) is not int or self.returncode != 0:
            raise ValueError("Replay decode checkpoint requires a successful execution")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayDecodeCheckpointResultV1:
        return cls(**_keys(data, cls, "replay decode checkpoint result"))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayFrameworkInstallationV1:
    package_id: int
    framework_apk: ArtifactRef
    execution_request_sha256: str

    def __post_init__(self) -> None:
        if type(self.package_id) is not int:
            raise TypeError("Framework package id must be an integer")
        if not 1 <= self.package_id <= 255:
            raise ValueError("Framework package id must be between 1 and 255")
        if type(self.framework_apk) is not ArtifactRef:
            raise TypeError("Framework APK must be an exact ArtifactRef")
        if self.framework_apk.kind != "framework-apk":
            raise ValueError("Invalid framework APK kind")
        _sha256(self.execution_request_sha256, "execution request SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayFrameworkInstallationV1:
        data = _keys(data, cls, "replay framework installation")
        return cls(
            data["package_id"],
            ArtifactRef.from_dict(data["framework_apk"]),
            data["execution_request_sha256"],
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayFrameworkCacheReceiptV1:
    schema_version: int
    admitted_replay_sha256: str
    toolchain_profile_id: str
    toolchain_profile_sha256: str
    role: Literal["install_framework"]
    execution_plan_sha256: str
    executor_capability_sha256: str
    tool_artifact_sha256: str
    installations: tuple[ReplayFrameworkInstallationV1, ...]
    framework_cache_manifest: ArtifactRef
    framework_cache_semantic_sha256: str
    operation_key: str
    success: Literal[True]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay framework cache receipt schema")
        _identifier(self.toolchain_profile_id, "toolchain profile id", lowercase=True)
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.toolchain_profile_sha256, "toolchain profile SHA-256"),
            (self.execution_plan_sha256, "execution plan SHA-256"),
            (self.executor_capability_sha256, "executor capability SHA-256"),
            (self.tool_artifact_sha256, "tool artifact SHA-256"),
            (self.framework_cache_semantic_sha256, "framework cache semantic SHA-256"),
            (self.operation_key, "operation key"),
        ):
            _sha256(value, label)
        if type(self.role) is not str or self.role != "install_framework":
            raise ValueError("Replay framework cache receipt role must be install_framework")
        if not isinstance(self.installations, tuple) or any(
            type(item) is not ReplayFrameworkInstallationV1 for item in self.installations
        ):
            raise TypeError(
                "Framework installations must be a tuple of exact installation records"
            )
        if not self.installations:
            raise ValueError("Framework installations must not be empty")
        _sorted_unique(
            self.installations,
            "framework installations",
            key=lambda item: item.package_id,
        )
        if type(self.framework_cache_manifest) is not ArtifactRef:
            raise TypeError("Framework cache manifest must be an exact ArtifactRef")
        if self.framework_cache_manifest.kind != "decoded-tree-manifest-v1":
            raise ValueError("Invalid framework cache manifest kind")
        if self.framework_cache_manifest.producer_operation_id != self.operation_key:
            raise ValueError("Framework cache manifest producer does not match operation")
        if self.framework_cache_manifest.input_hashes != self.execution_input_hashes:
            raise ValueError("Framework cache manifest input lineage is incomplete")
        if type(self.success) is not bool or self.success is not True:
            raise ValueError("Replay framework cache receipt requires success")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayFrameworkCacheReceiptV1:
        data = _keys(data, cls, "replay framework cache receipt")
        return cls(
            data["schema_version"],
            data["admitted_replay_sha256"],
            data["toolchain_profile_id"],
            data["toolchain_profile_sha256"],
            data["role"],
            data["execution_plan_sha256"],
            data["executor_capability_sha256"],
            data["tool_artifact_sha256"],
            tuple(
                ReplayFrameworkInstallationV1.from_dict(item)
                for item in _array(data["installations"], "framework installations")
            ),
            ArtifactRef.from_dict(data["framework_cache_manifest"]),
            data["framework_cache_semantic_sha256"],
            data["operation_key"],
            data["success"],
        )

    @property
    def execution_input_hashes(self) -> tuple[str, ...]:
        return (
            self.admitted_replay_sha256,
            self.toolchain_profile_sha256,
            self.execution_plan_sha256,
            self.executor_capability_sha256,
            self.tool_artifact_sha256,
            *(canonical_sha256(item) for item in self.installations),
        )

    @property
    def receipt_input_hashes(self) -> tuple[str, ...]:
        return (
            *self.execution_input_hashes,
            canonical_sha256(self.framework_cache_manifest),
            self.framework_cache_semantic_sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayDecodedTreeReceiptV1:
    schema_version: int
    decoded_apk_role: Literal["stock_input", "final_output"]
    admitted_replay_sha256: str
    input_apk: ArtifactRef
    toolchain_profile_id: str
    toolchain_profile_sha256: str
    role: Literal["decode"]
    execution_plan_sha256: str
    executor_capability_sha256: str
    tool_artifact_sha256: str
    execution_request_sha256: str
    decoded_tree_manifest: ArtifactRef
    decoded_tree_semantic_sha256: str
    operation_key: str
    success: Literal[True]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay decoded-tree receipt schema")
        if type(self.decoded_apk_role) is not str:
            raise TypeError("Decoded APK role must be a string")
        if self.decoded_apk_role not in {"stock_input", "final_output"}:
            raise ValueError("Invalid decoded APK role")
        if type(self.input_apk) is not ArtifactRef:
            raise TypeError("Input APK must be an exact ArtifactRef")
        allowed_input_kinds = {
            "stock_input": {"stock-apk"},
            "final_output": {"final-apk"},
        }
        if self.input_apk.kind not in allowed_input_kinds[self.decoded_apk_role]:
            raise ValueError("Input APK kind does not match decoded APK role")
        _identifier(self.toolchain_profile_id, "toolchain profile id", lowercase=True)
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.toolchain_profile_sha256, "toolchain profile SHA-256"),
            (self.execution_plan_sha256, "execution plan SHA-256"),
            (self.executor_capability_sha256, "executor capability SHA-256"),
            (self.tool_artifact_sha256, "tool artifact SHA-256"),
            (self.execution_request_sha256, "execution request SHA-256"),
            (self.decoded_tree_semantic_sha256, "decoded-tree semantic SHA-256"),
        ):
            _sha256(value, label)
        if type(self.role) is not str:
            raise TypeError("Replay decoded-tree receipt role must be a string")
        if self.role != "decode":
            raise ValueError("Replay decoded-tree receipt role must be decode")
        if type(self.decoded_tree_manifest) is not ArtifactRef:
            raise TypeError("Decoded-tree manifest must be an exact ArtifactRef")
        if self.decoded_tree_manifest.kind != "decoded-tree-manifest-v1":
            raise ValueError("Invalid decoded-tree manifest kind")
        _sha256(self.operation_key, "operation key")
        if self.decoded_tree_manifest.producer_operation_id != self.operation_key:
            raise ValueError("Decoded-tree manifest producer does not match operation")
        if self.decoded_tree_manifest.input_hashes != self.execution_input_hashes:
            raise ValueError("Decoded-tree manifest input lineage is incomplete")
        if type(self.success) is not bool or self.success is not True:
            raise ValueError("Replay decoded-tree receipt requires success")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayDecodedTreeReceiptV1:
        data = _keys(data, cls, "replay decoded-tree receipt")
        return cls(
            data["schema_version"],
            data["decoded_apk_role"],
            data["admitted_replay_sha256"],
            ArtifactRef.from_dict(data["input_apk"]),
            data["toolchain_profile_id"],
            data["toolchain_profile_sha256"],
            data["role"],
            data["execution_plan_sha256"],
            data["executor_capability_sha256"],
            data["tool_artifact_sha256"],
            data["execution_request_sha256"],
            ArtifactRef.from_dict(data["decoded_tree_manifest"]),
            data["decoded_tree_semantic_sha256"],
            data["operation_key"],
            data["success"],
        )

    @property
    def execution_input_hashes(self) -> tuple[str, ...]:
        return (
            self.admitted_replay_sha256,
            canonical_sha256(self.input_apk),
            self.toolchain_profile_sha256,
            self.execution_plan_sha256,
            self.executor_capability_sha256,
            self.tool_artifact_sha256,
            self.execution_request_sha256,
        )

    @property
    def receipt_input_hashes(self) -> tuple[str, ...]:
        return (
            *self.execution_input_hashes,
            canonical_sha256(self.decoded_tree_manifest),
            self.decoded_tree_semantic_sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayDecodedTreeReceiptV2:
    schema_version: int
    decoded_apk_role: Literal["stock_input", "final_output"]
    admitted_replay_sha256: str
    input_apk: ArtifactRef
    toolchain_profile_id: str
    toolchain_profile_sha256: str
    role: Literal["decode"]
    execution_plan_sha256: str
    executor_capability_sha256: str
    tool_artifact_sha256: str
    execution_request_sha256: str
    completed_framework_cache_receipt: ArtifactRef
    framework_cache_manifest: ArtifactRef
    framework_cache_semantic_sha256: str
    decoded_tree_manifest: ArtifactRef
    decoded_tree_semantic_sha256: str
    operation_key: str
    success: Literal[True]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported replay decoded-tree receipt schema")
        if type(self.decoded_apk_role) is not str:
            raise TypeError("Decoded APK role must be a string")
        if self.decoded_apk_role not in {"stock_input", "final_output"}:
            raise ValueError("Invalid decoded APK role")
        if type(self.input_apk) is not ArtifactRef:
            raise TypeError("Input APK must be an exact ArtifactRef")
        allowed_input_kinds = {
            "stock_input": {"stock-apk"},
            "final_output": {"final-apk"},
        }
        if self.input_apk.kind not in allowed_input_kinds[self.decoded_apk_role]:
            raise ValueError("Input APK kind does not match decoded APK role")
        _identifier(self.toolchain_profile_id, "toolchain profile id", lowercase=True)
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.toolchain_profile_sha256, "toolchain profile SHA-256"),
            (self.execution_plan_sha256, "execution plan SHA-256"),
            (self.executor_capability_sha256, "executor capability SHA-256"),
            (self.tool_artifact_sha256, "tool artifact SHA-256"),
            (self.execution_request_sha256, "execution request SHA-256"),
            (self.framework_cache_semantic_sha256, "framework cache semantic SHA-256"),
            (self.decoded_tree_semantic_sha256, "decoded-tree semantic SHA-256"),
            (self.operation_key, "operation key"),
        ):
            _sha256(value, label)
        if type(self.role) is not str or self.role != "decode":
            raise ValueError("Replay decoded-tree receipt role must be decode")
        for value, kind, label in (
            (
                self.completed_framework_cache_receipt,
                "replay-framework-cache-receipt-v1",
                "completed framework cache receipt",
            ),
            (
                self.framework_cache_manifest,
                "decoded-tree-manifest-v1",
                "framework cache manifest",
            ),
            (
                self.decoded_tree_manifest,
                "decoded-tree-manifest-v1",
                "decoded-tree manifest",
            ),
        ):
            if type(value) is not ArtifactRef:
                raise TypeError(f"{label.capitalize()} must be an exact ArtifactRef")
            if value.kind != kind:
                raise ValueError(f"Invalid {label} kind")
        if (
            self.framework_cache_manifest.producer_operation_id
            != self.completed_framework_cache_receipt.producer_operation_id
        ):
            raise ValueError(
                "Framework cache manifest producer does not match completed receipt"
            )
        if self.decoded_tree_manifest.producer_operation_id != self.operation_key:
            raise ValueError("Decoded-tree manifest producer does not match operation")
        if self.decoded_tree_manifest.input_hashes != self.execution_input_hashes:
            raise ValueError("Decoded-tree manifest input lineage is incomplete")
        if type(self.success) is not bool or self.success is not True:
            raise ValueError("Replay decoded-tree receipt requires success")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayDecodedTreeReceiptV2:
        data = _keys(data, cls, "replay decoded-tree receipt")
        return cls(
            data["schema_version"],
            data["decoded_apk_role"],
            data["admitted_replay_sha256"],
            ArtifactRef.from_dict(data["input_apk"]),
            data["toolchain_profile_id"],
            data["toolchain_profile_sha256"],
            data["role"],
            data["execution_plan_sha256"],
            data["executor_capability_sha256"],
            data["tool_artifact_sha256"],
            data["execution_request_sha256"],
            ArtifactRef.from_dict(data["completed_framework_cache_receipt"]),
            ArtifactRef.from_dict(data["framework_cache_manifest"]),
            data["framework_cache_semantic_sha256"],
            ArtifactRef.from_dict(data["decoded_tree_manifest"]),
            data["decoded_tree_semantic_sha256"],
            data["operation_key"],
            data["success"],
        )

    @property
    def execution_input_hashes(self) -> tuple[str, ...]:
        return (
            self.admitted_replay_sha256,
            canonical_sha256(self.input_apk),
            self.toolchain_profile_sha256,
            self.execution_plan_sha256,
            self.executor_capability_sha256,
            self.tool_artifact_sha256,
            self.execution_request_sha256,
            canonical_sha256(self.completed_framework_cache_receipt),
            canonical_sha256(self.framework_cache_manifest),
            self.framework_cache_semantic_sha256,
        )

    @property
    def receipt_input_hashes(self) -> tuple[str, ...]:
        return (
            *self.execution_input_hashes,
            canonical_sha256(self.decoded_tree_manifest),
            self.decoded_tree_semantic_sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplaySourceAdmissionEvidenceV1:
    schema_version: int
    admitted_replay_sha256: str
    source_manifest_sha256: str
    staged_tree_sha256: str
    file_count: int
    relative_destination: str
    passed: Literal[True]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported replay source admission evidence schema")
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.source_manifest_sha256, "source manifest SHA-256"),
            (self.staged_tree_sha256, "staged tree SHA-256"),
        ):
            _sha256(value, label)
        if type(self.file_count) is not int:
            raise TypeError("Replay source admission file count must be an integer")
        if self.file_count < 0:
            raise ValueError("Replay source admission file count must be nonnegative")
        if self.relative_destination != "admitted-source":
            raise ValueError("Invalid replay source admission destination")
        if type(self.passed) is not bool or self.passed is not True:
            raise ValueError("Replay source admission evidence requires success")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplaySourceAdmissionEvidenceV1:
        return cls(**_keys(data, cls, "replay source admission evidence"))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ReplayApplyOperationResultV1:
    operation_id: str
    status: Literal["applied", "already_applied"]

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "replay apply operation id")
        if type(self.status) is not str:
            raise TypeError("Replay apply operation status must be a string")
        if self.status not in {"applied", "already_applied"}:
            raise ValueError("Invalid replay apply operation status")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayApplyOperationResultV1:
        return cls(**_keys(data, cls, "replay apply operation result"))


@dataclass(frozen=True, slots=True)
class ReplayPatchedTreeReceiptV1:
    schema_version: int
    admitted_replay_sha256: str
    completed_decode_receipt: ArtifactRef
    input_decoded_tree_manifest: ArtifactRef
    input_decoded_tree_semantic_sha256: str
    intent_sha256: str
    resolution_sha256: str
    source_manifest_sha256: str
    target_port_spec_sha256: str
    source_admission: ReplaySourceAdmissionEvidenceV1
    operation_results: tuple[ReplayApplyOperationResultV1, ...]
    apply_report_sha256: str
    patched_tree_manifest: ArtifactRef
    patched_tree_semantic_sha256: str
    operation_key: str
    success: Literal[True]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported replay patched-tree receipt schema")
        if type(self.completed_decode_receipt) is not ArtifactRef:
            raise TypeError("Completed decode receipt must be an exact ArtifactRef")
        if self.completed_decode_receipt.kind not in {
            "replay-decoded-tree-receipt-v1",
            "replay-decoded-tree-receipt-v2",
        }:
            raise ValueError("Invalid completed decode receipt kind")
        for value, kind, label in (
            (
                self.input_decoded_tree_manifest,
                "decoded-tree-manifest-v1",
                "input decoded-tree manifest",
            ),
            (
                self.patched_tree_manifest,
                "decoded-tree-manifest-v1",
                "patched-tree manifest",
            ),
        ):
            if type(value) is not ArtifactRef:
                raise TypeError(f"{label.capitalize()} must be an exact ArtifactRef")
            if value.kind != kind:
                raise ValueError(f"Invalid {label} kind")
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (
                self.input_decoded_tree_semantic_sha256,
                "input decoded-tree semantic SHA-256",
            ),
            (self.intent_sha256, "intent SHA-256"),
            (self.resolution_sha256, "resolution SHA-256"),
            (self.source_manifest_sha256, "source manifest SHA-256"),
            (self.target_port_spec_sha256, "target port specification SHA-256"),
            (self.apply_report_sha256, "apply report SHA-256"),
            (self.patched_tree_semantic_sha256, "patched-tree semantic SHA-256"),
            (self.operation_key, "operation key"),
        ):
            _sha256(value, label)
        if type(self.source_admission) is not ReplaySourceAdmissionEvidenceV1:
            raise TypeError(
                "Replay source admission must be exact source admission evidence"
            )
        if not isinstance(self.operation_results, tuple) or any(
            type(result) is not ReplayApplyOperationResultV1
            for result in self.operation_results
        ):
            raise TypeError(
                "Replay apply operation results must be a tuple of exact result records"
            )
        operation_ids = tuple(result.operation_id for result in self.operation_results)
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("Duplicate replay apply operation id")
        if self.patched_tree_manifest.producer_operation_id != self.operation_key:
            raise ValueError("Patched-tree manifest producer does not match operation")
        if self.patched_tree_manifest.input_hashes != self.execution_input_hashes:
            raise ValueError("Patched-tree manifest input lineage is incomplete")
        if type(self.success) is not bool or self.success is not True:
            raise ValueError("Replay patched-tree receipt requires success")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplayPatchedTreeReceiptV1:
        data = _keys(data, cls, "replay patched-tree receipt")
        return cls(
            data["schema_version"],
            data["admitted_replay_sha256"],
            ArtifactRef.from_dict(data["completed_decode_receipt"]),
            ArtifactRef.from_dict(data["input_decoded_tree_manifest"]),
            data["input_decoded_tree_semantic_sha256"],
            data["intent_sha256"],
            data["resolution_sha256"],
            data["source_manifest_sha256"],
            data["target_port_spec_sha256"],
            ReplaySourceAdmissionEvidenceV1.from_dict(data["source_admission"]),
            tuple(
                ReplayApplyOperationResultV1.from_dict(item)
                for item in _array(data["operation_results"], "replay apply operation results")
            ),
            data["apply_report_sha256"],
            ArtifactRef.from_dict(data["patched_tree_manifest"]),
            data["patched_tree_semantic_sha256"],
            data["operation_key"],
            data["success"],
        )

    @property
    def execution_input_hashes(self) -> tuple[str, ...]:
        return (
            self.admitted_replay_sha256,
            canonical_sha256(self.completed_decode_receipt),
            canonical_sha256(self.input_decoded_tree_manifest),
            self.input_decoded_tree_semantic_sha256,
            self.intent_sha256,
            self.resolution_sha256,
            self.source_manifest_sha256,
            self.target_port_spec_sha256,
            self.source_admission.sha256,
            self.apply_report_sha256,
        )

    @property
    def receipt_input_hashes(self) -> tuple[str, ...]:
        return (
            *self.execution_input_hashes,
            canonical_sha256(self.patched_tree_manifest),
            self.patched_tree_semantic_sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def admit_replay_v3(
    run_spec: ReplayRunSpecV2,
    request: ReplayRequestV2,
    decision: GateDecision,
    decision_is_recorded: Callable[[GateDecision], bool],
    artifact_resolver: Callable[[ArtifactRef], bytes],
    capability_resolver: Callable[[str], ExecutorCapability],
) -> AdmittedReplayV3:
    if not isinstance(run_spec, ReplayRunSpecV2) or not isinstance(request, ReplayRequestV2):
        raise TypeError("Replay admission requires replay contracts")
    if not isinstance(decision, GateDecision):
        raise TypeError("Replay admission requires a gate decision")
    if not callable(decision_is_recorded):
        raise TypeError("Decision recording predicate must be callable")
    if not callable(artifact_resolver):
        raise TypeError("Artifact resolver must be callable")
    if not callable(capability_resolver):
        raise TypeError("Capability resolver must be callable")

    if request.run_spec_sha256 != run_spec.sha256:
        raise ValueError("Replay request does not bind the run specification")
    _validate_decision(run_spec, decision)
    try:
        recorded = decision_is_recorded(decision)
    except Exception as error:
        raise ValueError("Unable to verify recorded gate decision") from error
    if type(recorded) is not bool:
        raise TypeError("Decision recording predicate must return a boolean")
    if not recorded:
        raise ValueError("Gate decision is not recorded")

    if canonical_sha256(request.gate_prepared) != run_spec.gate_prepared_ref_sha256:
        raise ValueError("Replay request does not bind the gate prepared envelope")
    gate_prepared_bytes = _resolve_artifact(artifact_resolver, request.gate_prepared)
    gate_prepared = GatePreparedEnvelopeV2.from_dict(
        _decode_json(gate_prepared_bytes, "gate prepared envelope")
    )
    _validate_gate_prepared_relationship(run_spec, request, gate_prepared)
    _resolve_artifact(artifact_resolver, request.stock_apk)
    intent_bytes = _resolve_artifact(artifact_resolver, request.intent)
    resolution_bytes = _resolve_artifact(artifact_resolver, request.resolution)
    source_manifest_bytes = _resolve_artifact(artifact_resolver, request.source_manifest)
    profile_bytes = _resolve_artifact(artifact_resolver, request.toolchain_profile)
    for framework in request.frameworks:
        _resolve_artifact(artifact_resolver, framework.artifact)
    for tool in request.tools:
        _resolve_artifact(artifact_resolver, tool.artifact)

    intent = IntentSpecV2.from_dict(_decode_json(intent_bytes, "intent"))
    resolution = ResolutionSpecV3.from_dict(_decode_json(resolution_bytes, "resolution"))
    source_manifest = SourceManifestV1.from_json_value(
        _decode_json(source_manifest_bytes, "source manifest")
    )
    profile = ToolchainProfileV3.from_dict(
        _decode_json(profile_bytes, "toolchain profile")
    )
    _validate_admitted_relationships_v3_content(
        run_spec,
        request,
        decision,
        intent,
        resolution,
        source_manifest,
        profile,
        gate_prepared,
    )

    capabilities: list[ExecutorCapability] = []
    for binding in profile.capability_bindings:
        try:
            capability = capability_resolver(binding.executor_capability_sha256)
        except Exception as error:
            raise ValueError(
                f"Unable to resolve {binding.role} executor capability"
            ) from error
        if type(capability) is not ExecutorCapability:
            raise TypeError("Capability resolver must return an ExecutorCapability")
        if capability.canonical_identity != binding.executor_capability_sha256:
            raise ValueError("Resolved executor capability SHA-256 mismatch")
        profile.validate_capability(binding.role, capability)
        capabilities.append(capability)

    return AdmittedReplayV3(
        3,
        run_spec,
        request,
        decision,
        intent,
        resolution,
        source_manifest,
        profile,
        gate_prepared,
        tuple(capabilities),
    )
