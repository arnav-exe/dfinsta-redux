"""Canonical authority for quiescent, owner-private decoded trees.

Capture publishes immutable CAS bytes after a validated sequential scan; it does not
provide an atomic filesystem snapshot or defend against a hostile process with the
same OS identity. Real decoder integration requires separate process confinement.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .contracts import ArtifactRef, ID_PATTERN, SHA256_PATTERN, canonical_json
from .store import ContentStore


MANIFEST_KIND = "decoded-tree-manifest-v1"
MAX_ENTRIES = 500_000
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 16 * 1024 * 1024 * 1024
MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 16 * 1024
MAX_DEPTH = 64
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
READ_SIZE = 1024 * 1024
_MANIFEST_FIXED_OVERHEAD = 192
_MANIFEST_ENTRY_OVERHEAD = 192
_MANIFEST_PATH_EXPANSION = 6

_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_HAS_DESCRIPTOR_RUNTIME = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


class DecodedArtifactError(ValueError):
    pass


def _strict_keys(data: object, expected: set[str], label: str) -> None:
    if type(data) is not dict or any(type(key) is not str for key in data):
        raise TypeError(f"{label} must be an object with string keys")
    unknown = set(data) - expected
    missing = expected - set(data)
    if unknown:
        raise ValueError(f"Unknown {label} field: {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"Missing {label} field: {sorted(missing)[0]}")


def _validate_sha256(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Invalid {label}")


def _component_bytes(component: str) -> bytes:
    try:
        return component.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DecodedArtifactError("Decoded-tree paths must be valid UTF-8") from error


def _validate_component(component: object) -> str:
    if type(component) is not str:
        raise TypeError("Decoded-tree path components must be strings")
    encoded = _component_bytes(component)
    device_name = component.split(".", 1)[0].rstrip(" .").casefold()
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or unicodedata.normalize("NFC", component) != component
        or len(encoded) > MAX_COMPONENT_BYTES
        or any(character in _WINDOWS_FORBIDDEN for character in component)
        or component.endswith((".", " "))
        or device_name in _WINDOWS_RESERVED_NAMES
        or any(unicodedata.category(character) == "Cc" for character in component)
    ):
        raise DecodedArtifactError(f"Unsafe or noncanonical decoded-tree component: {component!r}")
    return component


def _path_parts(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise TypeError("Decoded-tree path must be a string")
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise DecodedArtifactError(f"Unsafe or noncanonical decoded-tree path: {value!r}")
    parts = tuple(value.split("/"))
    if len(parts) > MAX_DEPTH:
        raise DecodedArtifactError("Decoded-tree path exceeds the V1 depth limit")
    for part in parts:
        _validate_component(part)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DecodedArtifactError("Decoded-tree paths must be valid UTF-8") from error
    if len(encoded) > MAX_PATH_BYTES or PurePosixPath(value).as_posix() != value:
        raise DecodedArtifactError("Decoded-tree path exceeds V1 limits or is noncanonical")
    return parts


@dataclass(frozen=True, slots=True)
class DecodedTreeEntryV1:
    path: str
    kind: Literal["directory", "file"]
    size: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        _path_parts(self.path)
        if type(self.kind) is not str:
            raise TypeError("Decoded-tree entry kind must be a string")
        if self.kind == "directory":
            if self.size is not None or self.sha256 is not None:
                raise ValueError("Decoded-tree directories cannot have size or hash metadata")
        elif self.kind == "file":
            if type(self.size) is not int:
                raise TypeError("Decoded-tree file size must be an integer")
            if self.size < 0:
                raise ValueError("Decoded-tree file size must be nonnegative")
            _validate_sha256(self.sha256, "decoded-tree file SHA-256")
        else:
            raise ValueError("Decoded-tree entry kind must be directory or file")

    @classmethod
    def from_dict(cls, data: object) -> DecodedTreeEntryV1:
        _strict_keys(data, {"path", "kind", "size", "sha256"}, "decoded-tree entry")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class DecodedTreeManifestV1:
    schema_version: int
    decoded_tree_sha256: str
    entries: tuple[DecodedTreeEntryV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise TypeError("Decoded-tree manifest schema version must be an integer")
        if self.schema_version != 1:
            raise ValueError("Unsupported decoded-tree manifest schema")
        _validate_sha256(self.decoded_tree_sha256, "decoded-tree SHA-256")
        if type(self.entries) is not tuple:
            raise TypeError("Decoded-tree manifest entries must be a tuple")
        if len(self.entries) > MAX_ENTRIES:
            raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 entry limit")
        if any(type(entry) is not DecodedTreeEntryV1 for entry in self.entries):
            raise TypeError("Decoded-tree manifest entries must be exact DecodedTreeEntryV1 values")

        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(paths, key=lambda path: path.encode("utf-8"))):
            raise DecodedArtifactError("Decoded-tree entries must be sorted by UTF-8 path bytes")
        if len(paths) != len(set(paths)):
            raise DecodedArtifactError("Decoded-tree entry paths must be unique")

        by_path = {entry.path: entry for entry in self.entries}
        folded: dict[str, str] = {}
        for entry in self.entries:
            parts = _path_parts(entry.path)
            folded_path = "/".join(part.casefold() for part in parts)
            previous = folded.get(folded_path)
            if previous is not None and previous != entry.path:
                raise DecodedArtifactError("Decoded-tree paths have a casefold collision")
            folded[folded_path] = entry.path
            for depth in range(1, len(parts)):
                parent = "/".join(parts[:depth])
                parent_entry = by_path.get(parent)
                if parent_entry is None:
                    raise DecodedArtifactError("Decoded-tree entries require explicit parent directories")
                if parent_entry.kind != "directory":
                    raise DecodedArtifactError("Decoded-tree file is an ancestor of another entry")

        folded_kinds = {path.casefold(): entry.kind for path, entry in by_path.items()}
        for folded_path, kind in folded_kinds.items():
            parts = folded_path.split("/")
            if any(folded_kinds.get("/".join(parts[:depth])) == "file" for depth in range(1, len(parts))):
                raise DecodedArtifactError("Decoded-tree paths have a casefold ancestor conflict")

    @classmethod
    def from_dict(cls, data: object) -> DecodedTreeManifestV1:
        _strict_keys(
            data,
            {"schema_version", "decoded_tree_sha256", "entries"},
            "decoded-tree manifest",
        )
        entries = data["entries"]
        if type(entries) is not list:
            raise TypeError("Decoded-tree manifest entries must be an array")
        if len(entries) > MAX_ENTRIES:
            raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 entry limit")
        return cls(
            schema_version=data["schema_version"],
            decoded_tree_sha256=data["decoded_tree_sha256"],
            entries=tuple(DecodedTreeEntryV1.from_dict(entry) for entry in entries),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json(self).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _require_runtime() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if (
        os.name != "posix"
        or not sys.platform.startswith("linux")
        or any(not isinstance(getattr(os, name, None), int) for name in required)
        or not _HAS_DESCRIPTOR_RUNTIME
    ):
        raise RuntimeError("Decoded-tree artifacts require Linux descriptor-relative no-follow support")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _open_absolute_directory(path: Path) -> tuple[int, tuple[int, int]]:
    absolute = _absolute_path(path)
    descriptor = os.open("/", _directory_flags())
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DecodedArtifactError("Path is not a directory")
        return descriptor, (metadata.st_dev, metadata.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _reopen_matches(path: Path, identity: tuple[int, int]) -> bool:
    descriptor = -1
    try:
        descriptor, current = _open_absolute_directory(path)
        return current == identity
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_stat(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _read_stable_file(
    parent_fd: int, name: str, before: os.stat_result
) -> tuple[bytes, os.stat_result]:
    if before.st_nlink != 1:
        raise DecodedArtifactError("Decoded-tree regular files must have exactly one link")
    if before.st_size > MAX_FILE_BYTES:
        raise DecodedArtifactError("Decoded-tree file exceeds the V1 file-size limit")
    descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_stat(before, opened):
            raise DecodedArtifactError("Decoded-tree file changed while being opened")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(READ_SIZE, remaining))
            if not chunk:
                raise DecodedArtifactError("Decoded-tree file was truncated while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DecodedArtifactError("Decoded-tree file grew while being read")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_stat(before, after) or not _same_stat(before, current):
            raise DecodedArtifactError("Decoded-tree file changed while being captured")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _semantic_update(digest: Any, path: str, data: bytes) -> None:
    encoded = path.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _manifest_entry_budget(path: str) -> int:
    return _MANIFEST_ENTRY_OVERHEAD + _MANIFEST_PATH_EXPANSION * len(path.encode("utf-8"))


def _bounded_directory_names(directory_fd: int) -> list[str]:
    names: list[str] = []
    with os.scandir(directory_fd) as iterator:
        for item in iterator:
            if len(names) >= MAX_ENTRIES:
                raise DecodedArtifactError("Decoded-tree directory exceeds the V1 entry limit")
            names.append(item.name)
    return sorted(names, key=lambda item: item.encode("utf-8"))


def capture_decoded_tree(
    store: ContentStore,
    root: Path,
    producer_operation_id: str,
    input_hashes: tuple[str, ...],
) -> ArtifactRef:
    _require_runtime()
    if type(store) is not ContentStore:
        raise TypeError("store must be an exact ContentStore")
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if type(producer_operation_id) is not str or type(input_hashes) is not tuple:
        raise TypeError("Invalid decoded-tree artifact lineage types")
    if ID_PATTERN.fullmatch(producer_operation_id) is None:
        raise ValueError("Invalid decoded-tree producer operation ID")
    for input_hash in input_hashes:
        _validate_sha256(input_hash, "decoded-tree input SHA-256")
    manifest_budget = _MANIFEST_FIXED_OVERHEAD
    if manifest_budget > MAX_MANIFEST_BYTES:
        raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 byte limit")

    root_fd, root_identity = _open_absolute_directory(root)
    root_metadata = os.fstat(root_fd)
    entries: list[DecodedTreeEntryV1] = []
    file_stats: dict[str, os.stat_result] = {}
    total_bytes = 0
    try:
        def reserve_manifest_entry(relative: str) -> None:
            nonlocal manifest_budget
            manifest_budget += _manifest_entry_budget(relative)
            if manifest_budget > MAX_MANIFEST_BYTES:
                raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 byte limit")

        def scan(directory_fd: int, prefix: tuple[str, ...]) -> None:
            nonlocal total_bytes
            directory_before = os.fstat(directory_fd)
            for name in _bounded_directory_names(directory_fd):
                if len(entries) >= MAX_ENTRIES:
                    raise DecodedArtifactError("Decoded tree exceeds the V1 entry limit")
                _validate_component(name)
                parts = (*prefix, name)
                relative = "/".join(parts)
                _path_parts(relative)
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise DecodedArtifactError(f"Symlink or junction in decoded tree: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                            raise DecodedArtifactError("Decoded-tree directory changed while being opened")
                        reserve_manifest_entry(relative)
                        entries.append(DecodedTreeEntryV1(relative, "directory", None, None))
                        scan(child_fd, parts)
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if not _same_stat(metadata, current):
                            raise DecodedArtifactError("Decoded-tree directory changed while being captured")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    reserve_manifest_entry(relative)
                    data, stable_metadata = _read_stable_file(directory_fd, name, metadata)
                    total_bytes += len(data)
                    if total_bytes > MAX_TOTAL_FILE_BYTES:
                        raise DecodedArtifactError("Decoded tree exceeds the V1 total-size limit")
                    # Earlier blobs may remain after a later failure, but without a
                    # published manifest reference they are non-authoritative CAS orphans.
                    blob_sha256, blob_size = store.put_blob(data)
                    file_stats[relative] = stable_metadata
                    entries.append(DecodedTreeEntryV1(relative, "file", blob_size, blob_sha256))
                else:
                    raise DecodedArtifactError(f"Special file in decoded tree: {relative}")
            if not _same_stat(directory_before, os.fstat(directory_fd)):
                raise DecodedArtifactError("Decoded-tree directory changed while being enumerated")

        scan(root_fd, ())
        _verify_saved_file_stats(root_fd, file_stats)
        if not _same_stat(root_metadata, os.fstat(root_fd)):
            raise DecodedArtifactError("Decoded-tree root changed during stability sweep")
    finally:
        os.close(root_fd)
    if not _reopen_matches(root, root_identity):
        raise DecodedArtifactError("Decoded-tree root identity changed during capture")

    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    digest = hashlib.sha256()
    for entry in entries:
        if entry.kind == "file":
            data = store.read_blob(entry.sha256, entry.size)
            _semantic_update(digest, entry.path, data)
    manifest = DecodedTreeManifestV1(1, digest.hexdigest(), tuple(entries))
    payload = manifest.canonical_bytes()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 byte limit")
    return store.put_bytes(
        kind=MANIFEST_KIND,
        data=payload,
        producer_operation_id=producer_operation_id,
        input_hashes=input_hashes,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_decoded_tree(store: ContentStore, manifest_ref: ArtifactRef) -> DecodedTreeManifestV1:
    if type(store) is not ContentStore:
        raise TypeError("store must be an exact ContentStore")
    if type(manifest_ref) is not ArtifactRef:
        raise TypeError("manifest_ref must be an exact ArtifactRef")
    if manifest_ref.kind != MANIFEST_KIND:
        raise ValueError("Artifact is not a V1 decoded-tree manifest")
    if manifest_ref.size > MAX_MANIFEST_BYTES:
        raise DecodedArtifactError("Decoded-tree manifest exceeds the V1 byte limit")
    payload = store.read_bytes(manifest_ref)
    try:
        text = payload.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecodedArtifactError("Invalid decoded-tree manifest JSON") from error
    manifest = DecodedTreeManifestV1.from_dict(decoded)
    if manifest.canonical_bytes() != payload:
        raise DecodedArtifactError("Decoded-tree manifest JSON is not canonical")

    semantic = hashlib.sha256()
    total_bytes = 0
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        total_bytes += entry.size
        if entry.size > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_FILE_BYTES:
            raise DecodedArtifactError("Decoded-tree manifest exceeds V1 content limits")
        data = store.read_blob(entry.sha256, entry.size)
        _semantic_update(semantic, entry.path, data)
    if semantic.hexdigest() != manifest.decoded_tree_sha256:
        raise DecodedArtifactError("Decoded-tree semantic SHA-256 mismatch")
    return manifest


def _open_child_directory(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DecodedArtifactError("Materialized decoded-tree path is not a directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_manifest_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = _open_child_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_saved_file_stats(
    root_fd: int, file_stats: dict[str, os.stat_result]
) -> None:
    for relative in sorted(file_stats, key=lambda path: path.encode("utf-8")):
        parts = _path_parts(relative)
        parent_fd = _open_manifest_parent(root_fd, parts[:-1])
        try:
            current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(parent_fd)
        if not stat.S_ISREG(current.st_mode) or not _same_stat(file_stats[relative], current):
            raise DecodedArtifactError("Decoded-tree file changed after being read")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Could not complete decoded-tree file write")
        view = view[written:]


@dataclass(slots=True)
class _ScanDirectory:
    descriptor: int
    prefix: tuple[str, ...]
    before: os.stat_result
    parent: _ScanDirectory | None
    name: str | None
    parent_metadata: os.stat_result | None
    names: list[str]
    next_name: int = 0
    pending_name: bool = False
    open_children: int = 0
    closed: bool = False


def _verify_manifest_fd(manifest: DecodedTreeManifestV1, root_fd: int) -> None:
    if type(manifest) is not DecodedTreeManifestV1:
        raise TypeError("manifest must be an exact DecodedTreeManifestV1")
    if type(root_fd) is not int:
        raise TypeError("root_fd must be an integer descriptor")

    root_metadata = os.fstat(root_fd)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise DecodedArtifactError("Materialized decoded-tree root is not a directory")

    actual: list[DecodedTreeEntryV1] = []
    file_stats: dict[str, os.stat_result] = {}
    semantic = hashlib.sha256()
    total_bytes = 0
    discovered = 0
    queue: list[tuple[bytes, str, _ScanDirectory, str]] = []
    contexts: list[_ScanDirectory] = []

    def new_context(
        descriptor: int,
        prefix: tuple[str, ...],
        before: os.stat_result,
        parent: _ScanDirectory | None,
        name: str | None,
        parent_metadata: os.stat_result | None,
    ) -> _ScanDirectory:
        nonlocal discovered
        names = _bounded_directory_names(descriptor)
        discovered += len(names)
        if discovered > MAX_ENTRIES:
            raise DecodedArtifactError("Materialized decoded tree exceeds V1 limits")
        context = _ScanDirectory(
            descriptor,
            prefix,
            before,
            parent,
            name,
            parent_metadata,
            names,
        )
        contexts.append(context)
        return context

    def finish_if_complete(context: _ScanDirectory) -> None:
        if (
            context.closed
            or context.pending_name
            or context.next_name != len(context.names)
            or context.open_children
        ):
            return
        if not _same_stat(context.before, os.fstat(context.descriptor)):
            raise DecodedArtifactError("Materialized directory changed during verification")
        if context.parent is None:
            context.closed = True
            return
        current = os.stat(
            context.name,
            dir_fd=context.parent.descriptor,
            follow_symlinks=False,
        )
        if not _same_stat(context.parent_metadata, current):
            raise DecodedArtifactError("Materialized directory changed during verification")
        os.close(context.descriptor)
        context.closed = True
        context.parent.open_children -= 1
        finish_if_complete(context.parent)

    def queue_next(context: _ScanDirectory) -> None:
        if context.next_name == len(context.names):
            finish_if_complete(context)
            return
        name = context.names[context.next_name]
        context.next_name += 1
        context.pending_name = True
        relative = "/".join((*context.prefix, name))
        heapq.heappush(queue, (relative.encode("utf-8"), relative, context, name))

    root = new_context(root_fd, (), root_metadata, None, None, None)
    try:
        queue_next(root)
        while queue:
            _, relative, parent, name = heapq.heappop(queue)
            parent.pending_name = False
            _validate_component(name)
            parts = (*parent.prefix, name)
            _path_parts(relative)
            metadata = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, _directory_flags(), dir_fd=parent.descriptor)
                try:
                    opened = os.fstat(child_fd)
                    if not _same_stat(metadata, opened):
                        raise DecodedArtifactError(
                            "Materialized directory changed during verification"
                        )
                    actual.append(DecodedTreeEntryV1(relative, "directory", None, None))
                    parent.open_children += 1
                    try:
                        child = new_context(
                            child_fd,
                            parts,
                            opened,
                            parent,
                            name,
                            metadata,
                        )
                    except BaseException:
                        parent.open_children -= 1
                        raise
                    child_fd = -1
                    queue_next(child)
                finally:
                    if child_fd >= 0:
                        os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                data, stable_metadata = _read_stable_file(parent.descriptor, name, metadata)
                total_bytes += len(data)
                if total_bytes > MAX_TOTAL_FILE_BYTES:
                    raise DecodedArtifactError("Materialized decoded tree exceeds V1 limits")
                file_hash = hashlib.sha256(data).hexdigest()
                file_stats[relative] = stable_metadata
                actual.append(DecodedTreeEntryV1(relative, "file", len(data), file_hash))
                _semantic_update(semantic, relative, data)
            elif stat.S_ISLNK(metadata.st_mode):
                raise DecodedArtifactError("Symlink or junction in materialized decoded tree")
            else:
                raise DecodedArtifactError("Special file in materialized decoded tree")
            queue_next(parent)

        finish_if_complete(root)
        if not root.closed:
            raise DecodedArtifactError("Materialized decoded-tree scan did not complete")
        _verify_saved_file_stats(root_fd, file_stats)
        if not _same_stat(root_metadata, os.fstat(root_fd)):
            raise DecodedArtifactError("Materialized root changed during stability sweep")
        if tuple(actual) != manifest.entries:
            raise DecodedArtifactError("Materialized decoded-tree topology or bytes mismatch")
        if semantic.hexdigest() != manifest.decoded_tree_sha256:
            raise DecodedArtifactError("Materialized decoded-tree semantic SHA-256 mismatch")
    finally:
        for context in reversed(contexts):
            if context.parent is not None and not context.closed:
                os.close(context.descriptor)
                context.closed = True


def materialize_decoded_tree(
    store: ContentStore,
    manifest_ref: ArtifactRef,
    parent: Path,
    name: str,
) -> Path:
    _require_runtime()
    manifest = load_decoded_tree(store, manifest_ref)
    if not isinstance(parent, Path):
        raise TypeError("parent must be a Path")
    _validate_component(name)
    parent_fd, parent_identity = _open_absolute_directory(parent)
    root_fd = -1
    directory_paths = [""]
    destination = parent / name
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        root_fd = _open_child_directory(parent_fd, name)
        os.fchmod(root_fd, 0o700)
        root_metadata = os.fstat(root_fd)
        for entry in manifest.entries:
            parts = _path_parts(entry.path)
            parent_descriptor = _open_manifest_parent(root_fd, parts[:-1])
            try:
                if entry.kind == "directory":
                    os.mkdir(parts[-1], mode=0o700, dir_fd=parent_descriptor)
                    child_fd = _open_child_directory(parent_descriptor, parts[-1])
                    try:
                        os.fchmod(child_fd, 0o700)
                    finally:
                        os.close(child_fd)
                    directory_paths.append(entry.path)
                else:
                    data = store.read_blob(entry.sha256, entry.size)
                    file_fd = os.open(
                        parts[-1],
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    try:
                        _write_all(file_fd, data)
                        os.fchmod(file_fd, 0o600)
                        os.fsync(file_fd)
                    finally:
                        os.close(file_fd)
            finally:
                os.close(parent_descriptor)

        _verify_manifest_fd(manifest, root_fd)
        for path in sorted(
            directory_paths,
            key=lambda value: 0 if not value else value.count("/") + 1,
            reverse=True,
        ):
            parts = tuple(path.split("/")) if path else ()
            descriptor = _open_manifest_parent(root_fd, parts)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(parent_fd)
        if not _reopen_matches(parent, parent_identity):
            raise DecodedArtifactError("Decoded-tree materialization parent identity changed")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (root_metadata.st_dev, root_metadata.st_ino):
            raise DecodedArtifactError("Decoded-tree materialization root identity changed")
        return destination
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def verify_materialized_decoded_tree(
    manifest: DecodedTreeManifestV1, destination: Path
) -> None:
    _require_runtime()
    if type(manifest) is not DecodedTreeManifestV1:
        raise TypeError("manifest must be an exact DecodedTreeManifestV1")
    if not isinstance(destination, Path):
        raise TypeError("destination must be a Path")
    root_fd, root_identity = _open_absolute_directory(destination)
    try:
        _verify_manifest_fd(manifest, root_fd)
    finally:
        os.close(root_fd)
    if not _reopen_matches(destination, root_identity):
        raise DecodedArtifactError("Materialized decoded-tree root identity changed")
