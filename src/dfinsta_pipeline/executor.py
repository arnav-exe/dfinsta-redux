from __future__ import annotations

import asyncio
import hashlib
import os
import re
import string
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from .contracts import ArtifactRef, canonical_sha256


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
Launcher = Callable[..., Awaitable[Any]]


def _require_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} must be a tuple")


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"Invalid {label}")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{label} must be sorted and unique")


def _require_pairs(values: tuple[tuple[str, str], ...], label: str) -> None:
    _require_tuple(values, label)
    if any(
        not isinstance(pair, tuple)
        or len(pair) != 2
        or not isinstance(pair[0], str)
        or not isinstance(pair[1], str)
        for pair in values
    ):
        raise TypeError(f"{label} must contain string pairs")
    names = tuple(pair[0] for pair in values)
    if tuple(sorted(set(names))) != names:
        raise ValueError(f"{label} names must be sorted and unique")


def _relative_parts(value: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"Invalid {label}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Invalid {label}")
    return path.parts


def _strict_fields(data: dict[str, Any], contract: type[Any]) -> None:
    expected = {field.name for field in fields(contract)}
    if set(data) != expected:
        unknown = sorted(set(data) - expected)
        missing = sorted(expected - set(data))
        detail = unknown[0] if unknown else missing[0]
        raise ValueError(f"Invalid {contract.__name__} field: {detail}")


@dataclass(frozen=True, slots=True)
class ExecutorCapability:
    schema_version: int
    capability_id: str
    executable_sha256: str
    argv_template: tuple[str, ...]
    path_arguments: tuple[str, ...]
    input_kinds: tuple[str, ...]
    output_kind: str
    allowed_environment: tuple[str, ...]
    fixed_environment: tuple[tuple[str, str], ...]
    allowed_mutation_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported executor capability schema")
        if not self.capability_id or not _SHA256_RE.fullmatch(self.executable_sha256):
            raise ValueError("Invalid executor capability identity")
        for value, label in (
            (self.argv_template, "argv template"),
            (self.path_arguments, "path arguments"),
            (self.input_kinds, "input kinds"),
            (self.allowed_environment, "allowed environment"),
            (self.fixed_environment, "fixed environment"),
            (self.allowed_mutation_paths, "allowed mutation paths"),
        ):
            _require_tuple(value, label)
        if not self.argv_template or any(not isinstance(item, str) or "\x00" in item for item in self.argv_template):
            raise ValueError("Invalid argv template")
        _require_sorted_unique(self.path_arguments, "path arguments")
        _require_sorted_unique(self.input_kinds, "input kinds")
        _require_sorted_unique(self.allowed_environment, "allowed environment")
        _require_pairs(self.fixed_environment, "fixed environment")
        _require_sorted_unique(self.allowed_mutation_paths, "allowed mutation paths")
        if not isinstance(self.output_kind, str) or not self.output_kind:
            raise ValueError("Invalid output kind")
        if any(not _ENVIRONMENT_NAME_RE.fullmatch(name) for name in self.allowed_environment):
            raise ValueError("Invalid allowed environment name")
        fixed_names = {name for name, value in self.fixed_environment if _ENVIRONMENT_NAME_RE.fullmatch(name) and "\x00" not in value}
        if len(fixed_names) != len(self.fixed_environment) or fixed_names.intersection(self.allowed_environment):
            raise ValueError("Invalid fixed environment")
        for path in self.allowed_mutation_paths:
            _relative_parts(path, "allowed mutation path")

        placeholders: set[str] = set()
        formatter = string.Formatter()
        try:
            parsed = (formatter.parse(item) for item in self.argv_template)
            for item in parsed:
                for _, field_name, format_spec, conversion in item:
                    if field_name is None:
                        continue
                    if not field_name.isidentifier() or format_spec or conversion:
                        raise ValueError("Invalid argv placeholder")
                    placeholders.add(field_name)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid argv template") from error
        if not placeholders or not set(self.path_arguments).issubset(placeholders):
            raise ValueError("Invalid argv placeholders")

    @property
    def canonical_identity(self) -> str:
        return canonical_sha256(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutorCapability:
        _strict_fields(data, cls)
        converted = dict(data)
        for name in (
            "argv_template",
            "path_arguments",
            "input_kinds",
            "allowed_environment",
            "fixed_environment",
            "allowed_mutation_paths",
        ):
            converted[name] = tuple(tuple(item) if name == "fixed_environment" else item for item in data[name])
        return cls(**converted)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    schema_version: int
    capability_id: str
    input_artifact: ArtifactRef
    output_kind: str
    arguments: tuple[tuple[str, str], ...]
    environment: tuple[tuple[str, str], ...]
    apk_composition: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported execution request schema")
        if not isinstance(self.capability_id, str) or not self.capability_id:
            raise ValueError("Invalid capability id")
        if not isinstance(self.input_artifact, ArtifactRef):
            raise TypeError("input_artifact must be an ArtifactRef")
        if not isinstance(self.output_kind, str) or not self.output_kind:
            raise ValueError("Invalid output kind")
        _require_pairs(self.arguments, "arguments")
        _require_pairs(self.environment, "environment")
        if any("\x00" in name or "\x00" in value or "=" in name for name, value in self.environment):
            raise ValueError("Invalid environment")
        if self.apk_composition != "monolithic":
            raise ValueError("Split APK sets are not supported")

    @property
    def canonical_identity(self) -> str:
        return canonical_sha256(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRequest:
        _strict_fields(data, cls)
        converted = dict(data)
        converted["input_artifact"] = ArtifactRef.from_dict(data["input_artifact"])
        converted["arguments"] = tuple(tuple(pair) for pair in data["arguments"])
        converted["environment"] = tuple(tuple(pair) for pair in data["environment"])
        return cls(**converted)


@dataclass(frozen=True, slots=True)
class ExecutionMetadata:
    executable_path: Path
    workspace_root: Path
    cwd: Path

    def __post_init__(self) -> None:
        for value, label in (
            (self.executable_path, "executable path"),
            (self.workspace_root, "workspace root"),
            (self.cwd, "working directory"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{label} must be absolute")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str


def _contained(root: Path, candidate: Path, label: str) -> Path:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes workspace root") from error
    return candidate


def _workspace_path(root: Path, relative: str, label: str) -> Path:
    candidate = root.joinpath(*_relative_parts(relative, label)).resolve(strict=False)
    return _contained(root, candidate, label)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[str, str]]:
    snapshot: dict[str, tuple[str, str]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            snapshot[relative] = ("file", _file_sha256(path))
    return snapshot


async def _clean_up(process: Any) -> None:
    if process.returncode is None:
        process.kill()
    await process.communicate()


def _unexpected_mutations(
    before: dict[str, tuple[str, str]],
    after: dict[str, tuple[str, str]],
    allowed: tuple[str, ...],
) -> set[str]:
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    return {
        path
        for path in changed
        if not any(path == allowed_path or path.startswith(f"{allowed_path}/") for allowed_path in allowed)
    }


async def execute(
    capability: ExecutorCapability,
    request: ExecutionRequest,
    metadata: ExecutionMetadata,
    *,
    timeout_seconds: float,
    launcher: Launcher | None = None,
) -> ExecutionResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if request.capability_id != capability.capability_id:
        raise ValueError("Request capability does not match grant")
    if request.input_artifact.kind not in capability.input_kinds:
        raise ValueError("Input artifact kind is not allowed")
    if request.output_kind != capability.output_kind:
        raise ValueError("Output artifact kind is not allowed")

    argument_values = dict(request.arguments)
    placeholders = {
        field_name
        for template in capability.argv_template
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }
    if set(argument_values) != placeholders:
        raise ValueError("Arguments do not exactly match argv template")

    workspace_root = metadata.workspace_root.resolve(strict=True)
    if not workspace_root.is_dir():
        raise ValueError("Workspace root is not a directory")
    cwd = metadata.cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("Working directory is not a directory")
    _contained(workspace_root, cwd, "Working directory")
    for name in capability.path_arguments:
        _workspace_path(workspace_root, argument_values[name], f"Path argument {name}")
    for relative in capability.allowed_mutation_paths:
        _workspace_path(workspace_root, relative, "Allowed mutation path")

    supplied_environment = dict(request.environment)
    if not set(supplied_environment).issubset(capability.allowed_environment):
        raise ValueError("Environment contains a non-allowlisted name")
    environment = {**dict(capability.fixed_environment), **supplied_environment}
    argv = tuple(template.format_map(argument_values) for template in capability.argv_template)
    before = _snapshot(workspace_root)

    executable = metadata.executable_path.resolve(strict=True)
    if not executable.is_file():
        raise ValueError("Executable is not a regular file")
    if _file_sha256(executable) != capability.executable_sha256:
        raise ValueError("Executable SHA-256 does not match capability")

    launch = launcher if launcher is not None else asyncio.create_subprocess_exec
    process = await launch(
        str(executable),
        *argv,
        cwd=str(cwd),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except (TimeoutError, asyncio.CancelledError):
        await _clean_up(process)
        unexpected = _unexpected_mutations(
            before, _snapshot(workspace_root), capability.allowed_mutation_paths
        )
        if unexpected:
            raise PermissionError(f"Subprocess mutated undeclared path: {sorted(unexpected)[0]}")
        raise

    after = _snapshot(workspace_root)
    unexpected = _unexpected_mutations(before, after, capability.allowed_mutation_paths)
    if unexpected:
        raise PermissionError(f"Subprocess mutated undeclared path: {sorted(unexpected)[0]}")
    return ExecutionResult(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8"),
        stderr=stderr.decode("utf-8"),
    )
