from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeVar


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
T = TypeVar("T")


def _canonical_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _canonical_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Canonical mappings require string keys")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value), allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_id(value: str, label: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _validate_sha256(value: str, label: str) -> None:
    if not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {label}")


def _strict_keys(data: object, expected: set[str], label: str) -> None:
    if type(data) is not dict or any(type(key) is not str for key in data):
        raise TypeError(f"{label} must be an object with string keys")
    unknown = set(data) - expected
    missing = expected - set(data)
    if unknown:
        raise ValueError(f"Unknown {label} field: {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"Missing {label} field: {sorted(missing)[0]}")


def _decode_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) not in {list, tuple} or any(type(item) is not str for item in value):
        raise TypeError(f"{label} must be an array of strings")
    return tuple(value)


@dataclass(frozen=True)
class ArtifactRef:
    schema_version: int
    kind: str
    sha256: str
    size: int
    uri: str
    producer_operation_id: str
    input_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported artifact reference schema")
        if type(self.size) is not int or not isinstance(self.input_hashes, tuple):
            raise TypeError("Invalid artifact reference types")
        _validate_id(self.kind, "artifact kind")
        _validate_sha256(self.sha256, "artifact SHA-256")
        _validate_id(self.producer_operation_id, "producer operation id")
        if self.size < 0 or self.uri != f"cas://sha256/{self.sha256}":
            raise ValueError("Invalid artifact reference metadata")
        for digest in self.input_hashes:
            _validate_sha256(digest, "artifact input SHA-256")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactRef:
        expected = {field.name for field in dataclasses.fields(cls)}
        _strict_keys(data, expected, "artifact reference")
        return cls(
            **{
                **data,
                "input_hashes": _decode_string_tuple(data["input_hashes"], "artifact input hashes"),
            }
        )


@dataclass(frozen=True)
class IntentSpec:
    schema_version: int
    policy_revision: str
    intent_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported intent schema")
        if not isinstance(self.intent_ids, tuple):
            raise TypeError("Intent IDs must be a tuple")
        _validate_id(self.policy_revision, "policy revision")
        if not self.intent_ids:
            raise ValueError("Intent specification is empty")
        for intent_id in self.intent_ids:
            _validate_id(intent_id, "intent id")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentSpec:
        _strict_keys(data, {field.name for field in dataclasses.fields(cls)}, "intent")
        return cls(**{**data, "intent_ids": _decode_string_tuple(data["intent_ids"], "intent IDs")})


@dataclass(frozen=True)
class ResolutionSpec:
    schema_version: int
    target_sha256: str
    operation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported resolution schema")
        if not isinstance(self.operation_ids, tuple):
            raise TypeError("Operation IDs must be a tuple")
        _validate_sha256(self.target_sha256, "target SHA-256")
        for operation_id in self.operation_ids:
            _validate_id(operation_id, "operation id")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResolutionSpec:
        _strict_keys(data, {field.name for field in dataclasses.fields(cls)}, "resolution")
        return cls(
            **{
                **data,
                "operation_ids": _decode_string_tuple(data["operation_ids"], "operation IDs"),
            }
        )


@dataclass(frozen=True)
class RunSpec:
    schema_version: int
    run_id: str
    subject_sha256: str
    intent_sha256: str
    resolution_sha256: str
    executor_capability_sha256: str
    policy_revision: str
    allowed_actor: str
    gate_timeout_seconds: int
    apk_composition: Literal["monolithic"]
    crash_after_effect: bool = False
    apply_delay_seconds: int = 0

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported run schema")
        _validate_id(self.run_id, "run id")
        for digest, label in (
            (self.subject_sha256, "run subject SHA-256"),
            (self.intent_sha256, "intent SHA-256"),
            (self.resolution_sha256, "resolution SHA-256"),
            (self.executor_capability_sha256, "executor capability SHA-256"),
        ):
            _validate_sha256(digest, label)
        _validate_id(self.policy_revision, "policy revision")
        _validate_id(self.allowed_actor, "allowed actor")
        if type(self.gate_timeout_seconds) is not int or type(self.apply_delay_seconds) is not int:
            raise TypeError("Run timeouts must be integers")
        if type(self.crash_after_effect) is not bool:
            raise TypeError("Run crash flag must be a boolean")
        if self.gate_timeout_seconds < 1 or self.apply_delay_seconds < 0:
            raise ValueError("Invalid run timeout")
        if self.apk_composition != "monolithic":
            raise ValueError("Split APK sets are not supported")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSpec:
        _strict_keys(data, {field.name for field in dataclasses.fields(cls)}, "run")
        return cls(**data)


@dataclass(frozen=True)
class GateRequest:
    schema_version: int
    run_id: str
    gate_id: str
    subject_sha256: str
    admission_sha256: str
    prepared_sha256: str
    policy_revision: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported gate request schema")
        _validate_id(self.run_id, "gate run id")
        _validate_id(self.gate_id, "gate id")
        _validate_sha256(self.subject_sha256, "gate subject SHA-256")
        _validate_sha256(self.admission_sha256, "gate admission SHA-256")
        _validate_sha256(self.prepared_sha256, "gate prepared SHA-256")
        _validate_id(self.policy_revision, "gate policy revision")
        if type(self.issued_at) is not str or type(self.expires_at) is not str:
            raise TypeError("Gate request timestamps must be strings")
        if not self.issued_at or not self.expires_at:
            raise ValueError("Gate request requires timestamps")


@dataclass(frozen=True)
class GateDecision:
    schema_version: int
    decision_id: str
    idempotency_id: str
    actor: str
    run_id: str
    gate_id: str
    subject_sha256: str
    admission_sha256: str
    prepared_sha256: str
    policy_revision: str
    decision: Literal["approve", "reject", "defer"]
    rationale: str
    issued_at: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported gate decision schema")
        for value, label in (
            (self.decision_id, "decision id"),
            (self.idempotency_id, "idempotency id"),
            (self.actor, "actor"),
            (self.run_id, "decision run id"),
            (self.gate_id, "decision gate id"),
            (self.policy_revision, "decision policy revision"),
        ):
            _validate_id(value, label)
        _validate_sha256(self.subject_sha256, "decision subject SHA-256")
        _validate_sha256(self.admission_sha256, "decision admission SHA-256")
        _validate_sha256(self.prepared_sha256, "decision prepared SHA-256")
        if self.decision not in {"approve", "reject", "defer"}:
            raise ValueError("Invalid gate decision")
        if type(self.rationale) is not str or type(self.issued_at) is not str:
            raise TypeError("Gate decision rationale and timestamp must be strings")
        if not self.rationale.strip() or len(self.rationale) > 2048 or not self.issued_at.strip():
            raise ValueError("Gate decision requires rationale and timestamp")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateDecision:
        _strict_keys(data, {field.name for field in dataclasses.fields(cls)}, "gate decision")
        return cls(**data)


@dataclass(frozen=True)
class StageInput:
    schema_version: int
    spec: RunSpec
    upstream: tuple[ArtifactRef, ...]
    decision: GateDecision | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported stage input schema")
        if not isinstance(self.spec, RunSpec) or not isinstance(self.upstream, tuple):
            raise TypeError("Invalid stage input")
        if any(not isinstance(reference, ArtifactRef) for reference in self.upstream):
            raise TypeError("Invalid stage input artifact")
        if self.decision is not None and not isinstance(self.decision, GateDecision):
            raise TypeError("Invalid stage decision")

    @property
    def input_hashes(self) -> tuple[str, ...]:
        hashes = [canonical_sha256(self.spec), *(reference.sha256 for reference in self.upstream)]
        if self.decision is not None:
            hashes.append(canonical_sha256(self.decision))
        return tuple(hashes)


@dataclass(frozen=True)
class GateReceipt:
    decision_id: str
    accepted: bool


@dataclass(frozen=True)
class WorkflowStatus:
    state: str
    gate: GateRequest | None
    decision_id: str | None


@dataclass(frozen=True)
class RunResult:
    schema_version: int
    run_id: str
    state: Literal["completed", "rejected", "deferred", "blocked", "cancelled"]
    prepared: ArtifactRef | None
    output: ArtifactRef | None
    decision_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported run result schema")
        _validate_id(self.run_id, "result run id")
        if self.state not in {"completed", "rejected", "deferred", "blocked", "cancelled"}:
            raise ValueError("Invalid run result state")
        if self.prepared is not None and not isinstance(self.prepared, ArtifactRef):
            raise TypeError("Invalid prepared result artifact")
        if self.output is not None and not isinstance(self.output, ArtifactRef):
            raise TypeError("Invalid output result artifact")
        if self.state == "completed" and (self.output is None or self.decision_id is None):
            raise ValueError("Completed result lacks output or decision")
        if self.state != "completed" and self.output is not None:
            raise ValueError("Non-completed result cannot contain output")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResult:
        _strict_keys(data, {field.name for field in dataclasses.fields(cls)}, "run result")
        converted = dict(data)
        for name in ("prepared", "output"):
            if converted[name] is not None:
                converted[name] = ArtifactRef.from_dict(converted[name])
        return cls(**converted)
