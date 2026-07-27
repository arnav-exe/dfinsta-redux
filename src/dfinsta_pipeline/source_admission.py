from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .contracts import canonical_sha256
from .ledger import Ledger
from .port_contracts import SourceFile
from .replay_contracts import AdmittedReplay, AdmittedReplayV3, SourceManifestV1


DESTINATION_NAME = "admitted-source"
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class SourceAdmissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceAdmissionReport:
    schema_version: int
    admitted_replay_sha256: str
    source_manifest_sha256: str
    staged_tree_sha256: str
    file_count: int
    relative_destination: str
    passed: bool

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class SourceAdmissionReportV2:
    schema_version: int
    admitted_replay_sha256: str
    source_manifest_sha256: str
    staged_tree_sha256: str
    file_count: int
    relative_destination: str
    passed: bool

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported source admission report schema")
        for value, label in (
            (self.admitted_replay_sha256, "admitted replay SHA-256"),
            (self.source_manifest_sha256, "source manifest SHA-256"),
            (self.staged_tree_sha256, "staged tree SHA-256"),
        ):
            _report_sha256(value, label)
        if type(self.file_count) is not int:
            raise TypeError("Source admission report file count must be an integer")
        if self.file_count < 0:
            raise ValueError("Source admission report file count must be nonnegative")
        if type(self.relative_destination) is not str:
            raise TypeError("Source admission report destination must be a string")
        if self.relative_destination != DESTINATION_NAME:
            raise ValueError("Invalid source admission report destination")
        if type(self.passed) is not bool:
            raise TypeError("Source admission report passed must be a boolean")
        if not self.passed:
            raise ValueError("Source admission report must record success")

    @classmethod
    def from_dict(cls, data: object) -> SourceAdmissionReportV2:
        if type(data) is not dict or any(type(key) is not str for key in data):
            raise TypeError("Source admission report must be an object with string keys")
        expected = {
            "schema_version",
            "admitted_replay_sha256",
            "source_manifest_sha256",
            "staged_tree_sha256",
            "file_count",
            "relative_destination",
            "passed",
        }
        unknown = set(data) - expected
        missing = expected - set(data)
        if unknown:
            raise ValueError(f"Unknown source admission report field: {sorted(unknown)[0]}")
        if missing:
            raise ValueError(f"Missing source admission report field: {sorted(missing)[0]}")
        return cls(**data)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


def admit_source_bundle(
    admitted: AdmittedReplay,
    source_root: Path,
    attempt_root: Path,
    admission_is_recorded: Callable[[AdmittedReplay], bool],
) -> SourceAdmissionReport:
    _require_runtime()
    admitted = _revalidate_admitted(admitted)
    if not callable(admission_is_recorded):
        raise TypeError("admission_is_recorded must be callable")
    try:
        recorded = admission_is_recorded(admitted)
    except Exception as error:
        raise SourceAdmissionError("Could not verify ledger-owned admission") from error
    if type(recorded) is not bool:
        raise SourceAdmissionError("Admission predicate must return a bool")
    if not recorded:
        raise SourceAdmissionError("Admitted replay is not recorded")

    source = _directory_path(source_root, "source root")
    attempt = _directory_path(attempt_root, "attempt root")
    if source == attempt or source in attempt.parents or attempt in source.parents:
        raise SourceAdmissionError("Source and attempt roots must not overlap")
    _probe_noreplace(attempt)

    destination = attempt / DESTINATION_NAME
    if _lexists(destination):
        raise SourceAdmissionError(f"Destination already exists: {destination}")

    manifest = admitted.source_manifest
    if not isinstance(manifest, SourceManifestV1):
        raise TypeError("admitted source manifest must be a SourceManifestV1")
    staged_records = _preflight(source, manifest.records)
    staged_sha256 = _records_sha256(staged_records)

    temporary: Path | None = None
    try:
        if _lexists(destination):
            raise SourceAdmissionError(f"Destination already exists: {destination}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{DESTINATION_NAME}-", dir=attempt))
        _write_tree(temporary, staged_records)
        _publish_without_overwrite(temporary, destination)
        temporary = None
        _fsync_directory(attempt)
    except SourceAdmissionError:
        raise
    except OSError as error:
        raise SourceAdmissionError(f"Could not stage source bundle: {error}") from error
    finally:
        if temporary is not None:
            _remove_tree(temporary)

    return SourceAdmissionReport(
        1,
        admitted.sha256,
        manifest.sha256,
        staged_sha256,
        len(staged_records),
        DESTINATION_NAME,
        True,
    )


def admit_source_bundle_v2(
    candidate: AdmittedReplayV3,
    source_root: Path,
    attempt_root: Path,
    ledger: Ledger,
) -> SourceAdmissionReportV2:
    admitted = _require_v3_authority(candidate, ledger)
    _require_runtime()

    source = _directory_path(source_root, "source root")
    attempt = _directory_path(attempt_root, "attempt root")
    if source == attempt or source in attempt.parents or attempt in source.parents:
        raise SourceAdmissionError("Source and attempt roots must not overlap")
    _probe_noreplace(attempt)

    destination = attempt / DESTINATION_NAME
    if _lexists(destination):
        raise SourceAdmissionError(f"Destination already exists: {destination}")

    manifest = admitted.source_manifest
    staged_records = _preflight(source, manifest.records)
    staged_sha256 = _records_sha256(staged_records)

    temporary: Path | None = None
    try:
        if _lexists(destination):
            raise SourceAdmissionError(f"Destination already exists: {destination}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{DESTINATION_NAME}-", dir=attempt))
        _write_tree(temporary, staged_records)
        _publish_without_overwrite(temporary, destination)
        temporary = None
        _fsync_directory(attempt)
    except SourceAdmissionError:
        raise
    except OSError as error:
        raise SourceAdmissionError(f"Could not stage source bundle: {error}") from error
    finally:
        if temporary is not None:
            _remove_tree(temporary)

    return SourceAdmissionReportV2(
        2,
        admitted.sha256,
        manifest.sha256,
        staged_sha256,
        len(staged_records),
        DESTINATION_NAME,
        True,
    )


def verify_staged_source(
    report: SourceAdmissionReport, admitted: AdmittedReplay, destination: Path
) -> None:
    _require_runtime()
    if not isinstance(report, SourceAdmissionReport):
        raise TypeError("report must be a SourceAdmissionReport")
    admitted = _revalidate_admitted(admitted)
    if (
        report.schema_version != 1
        or not report.passed
        or report.relative_destination != DESTINATION_NAME
        or report.admitted_replay_sha256 != admitted.sha256
        or report.source_manifest_sha256 != admitted.source_manifest.sha256
        or report.file_count != len(admitted.source_manifest.records)
    ):
        raise SourceAdmissionError("Source admission report does not match admitted replay")
    tree = _directory_path(destination, "staged source")
    if tree.name != report.relative_destination:
        raise SourceAdmissionError("Staged source destination does not match report")
    records = _tree_records(tree)
    if len(records) != report.file_count:
        raise SourceAdmissionError("Staged source file count mismatch")
    expected = admitted.source_manifest.records
    if tuple(relative for relative, _ in records) != tuple(
        record.relative_path for record in expected
    ) or any(
        hashlib.sha256(data).hexdigest() != source.sha256
        for (relative, data), source in zip(records, expected, strict=True)
    ):
        raise SourceAdmissionError("Staged source does not match source manifest")
    if _records_sha256(records) != report.staged_tree_sha256:
        raise SourceAdmissionError("Staged source tree SHA-256 mismatch")


def verify_staged_source_v2(
    report: SourceAdmissionReportV2,
    candidate: AdmittedReplayV3,
    destination: Path,
    ledger: Ledger,
) -> None:
    admitted = _require_v3_authority(candidate, ledger)
    if type(report) is not SourceAdmissionReportV2:
        raise TypeError("report must be an exact SourceAdmissionReportV2")
    report = SourceAdmissionReportV2.from_dict(asdict(report))
    _require_runtime()
    if (
        report.schema_version != 2
        or not report.passed
        or report.relative_destination != DESTINATION_NAME
        or report.admitted_replay_sha256 != admitted.sha256
        or report.source_manifest_sha256 != admitted.source_manifest.sha256
        or report.file_count != len(admitted.source_manifest.records)
    ):
        raise SourceAdmissionError("Source admission report does not match admitted replay")
    tree = _directory_path(destination, "staged source")
    if tree.name != report.relative_destination:
        raise SourceAdmissionError("Staged source destination does not match report")
    records = _tree_records(tree)
    if len(records) != report.file_count:
        raise SourceAdmissionError("Staged source file count mismatch")
    expected = admitted.source_manifest.records
    if tuple(relative for relative, _ in records) != tuple(
        record.relative_path for record in expected
    ) or any(
        hashlib.sha256(data).hexdigest() != source.sha256
        for (relative, data), source in zip(records, expected, strict=True)
    ):
        raise SourceAdmissionError("Staged source does not match source manifest")
    if _records_sha256(records) != report.staged_tree_sha256:
        raise SourceAdmissionError("Staged source tree SHA-256 mismatch")


def source_tree_sha256(root: Path) -> str:
    _require_runtime()
    tree = _directory_path(root, "source tree")
    return _records_sha256(_tree_records(tree))


def _tree_records(tree: Path) -> tuple[tuple[str, bytes], ...]:
    records: list[tuple[str, bytes]] = []
    for directory, directory_names, file_names in os.walk(
        tree, followlinks=False, onerror=_raise_walk_error
    ):
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            path = parent / name
            if _is_link_or_junction(path):
                raise SourceAdmissionError(f"Symlink or junction in source tree: {path}")
        for name in file_names:
            path = parent / name
            relative = path.relative_to(tree).as_posix()
            records.append((relative, _read_relative(tree, relative)))
    return tuple(sorted(records))


def _preflight(
    source_root: Path, records: tuple[SourceFile, ...]
) -> tuple[tuple[str, bytes], ...]:
    paths = tuple(_relative_path(record.relative_path) for record in records)
    folded_paths: dict[str, str] = {}
    folded_parts = sorted(tuple(part.casefold() for part in path.parts) for path in paths)
    for record, path in zip(records, paths, strict=True):
        folded = path.as_posix().casefold()
        if folded in folded_paths:
            raise SourceAdmissionError(
                f"Case-insensitive source path collision: {record.relative_path}"
            )
        folded_paths[folded] = record.relative_path
    for previous, current in zip(folded_parts, folded_parts[1:]):
        if current[: len(previous)] == previous:
            raise SourceAdmissionError("Source file paths have an ancestor conflict")

    result: list[tuple[str, bytes]] = []
    for record in records:
        data = _read_relative(source_root, record.relative_path)
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise SourceAdmissionError(
                f"Source file SHA-256 mismatch: {record.relative_path}"
            )
        result.append((record.relative_path, data))
    return tuple(result)


def _relative_path(value: object) -> PurePosixPath:
    if type(value) is not str:
        raise TypeError("Source file path must be a string")
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in parts)
        or any(not _portable_component(part) for part in parts)
        or path.as_posix() != value
    ):
        raise SourceAdmissionError(f"Unsafe or noncanonical source file path: {value}")
    return path


def _portable_component(component: str) -> bool:
    device_name = component.split(".", 1)[0].rstrip(" .").casefold()
    return not (
        any(character in '<>:"|?*' for character in component)
        or component.endswith((".", " "))
        or device_name in WINDOWS_RESERVED_NAMES
        or any(unicodedata.category(character) == "Cc" for character in component)
    )


def _read_relative(root: Path, relative: str) -> bytes:
    parts = _relative_path(relative).parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    current = -1
    try:
        current = os.open(root, directory_flags)
        for part in parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        if not stat.S_ISREG(
            os.stat(parts[-1], dir_fd=current, follow_symlinks=False).st_mode
        ):
            raise SourceAdmissionError(f"Source path is not a regular file: {relative}")
        descriptor = os.open(parts[-1], flags, dir_fd=current)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SourceAdmissionError(f"Source path is not a regular file: {relative}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except SourceAdmissionError:
        raise
    except OSError as error:
        raise SourceAdmissionError(f"Could not read source file {relative}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current >= 0:
            os.close(current)


def _write_tree(root: Path, records: tuple[tuple[str, bytes], ...]) -> None:
    directories = {root}
    for relative, data in records:
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directories.update((destination.parent, *destination.parent.parents))
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    contained = {path for path in directories if path == root or root in path.parents}
    for directory in sorted(contained, key=lambda path: len(path.parts), reverse=True):
        os.chmod(directory, 0o555)
        _fsync_directory(directory)


def _records_sha256(records: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for relative, data in sorted(records):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _publish_without_overwrite(source: Path, destination: Path) -> None:
    renameat2 = _load_renameat2()
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise SourceAdmissionError(f"Destination already exists: {destination}")
        raise OSError(error_number, os.strerror(error_number), destination)


def _directory_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    absolute = value.absolute()
    for component in (absolute, *absolute.parents):
        if _is_link_or_junction(component):
            raise SourceAdmissionError(f"{label} contains a symlink or junction")
    try:
        mode = value.stat().st_mode
    except OSError as error:
        raise SourceAdmissionError(f"{label} must be an existing directory") from error
    if not stat.S_ISDIR(mode):
        raise SourceAdmissionError(f"{label} must be an existing directory")
    return value.resolve()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _raise_walk_error(error: OSError) -> None:
    raise SourceAdmissionError(f"Could not scan source tree: {error}") from error


def _remove_tree(path: Path) -> None:
    if not _lexists(path):
        return
    if _is_link_or_junction(path):
        raise SourceAdmissionError(
            f"Unexpected symlink or junction at private source tree: {path}"
        )
    directories: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        path, followlinks=False, onerror=_raise_walk_error
    ):
        current = Path(directory)
        directories.append(current)
        for name in (*directory_names, *file_names):
            entry = current / name
            if _is_link_or_junction(entry):
                raise SourceAdmissionError(
                    f"Unexpected symlink or junction in private source tree: {entry}"
                )
    for directory in directories:
        descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    shutil.rmtree(path)


def _revalidate_admitted(admitted: object) -> AdmittedReplay:
    if not isinstance(admitted, AdmittedReplay):
        raise TypeError("admitted must be an AdmittedReplay")
    try:
        validated = AdmittedReplay.from_dict(asdict(admitted))
    except Exception as error:
        raise SourceAdmissionError("Admitted replay failed relational revalidation") from error
    if validated != admitted:
        raise SourceAdmissionError("Admitted replay changed during relational revalidation")
    return validated


def _require_v3_authority(candidate: object, ledger: object) -> AdmittedReplayV3:
    if type(ledger) is not Ledger:
        raise TypeError("ledger must be an exact Ledger")
    if type(candidate) is not AdmittedReplayV3:
        raise TypeError("candidate must be an exact AdmittedReplayV3")
    return Ledger.require_admitted_replay_v3(ledger, candidate)


def _report_sha256(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"Invalid {label}")


def _load_renameat2():
    try:
        return ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise SourceAdmissionError("Linux renameat2(RENAME_NOREPLACE) is required") from error


def _probe_noreplace(root: Path) -> None:
    probe = Path(tempfile.mkdtemp(prefix=".source-admission-probe-", dir=root))
    source = probe / "source"
    destination = probe / "destination"
    source.mkdir()
    destination.mkdir()
    try:
        renameat2 = _load_renameat2()
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
            raise SourceAdmissionError("renameat2 probe unexpectedly replaced a destination")
        error_number = ctypes.get_errno()
        if error_number != errno.EEXIST:
            raise SourceAdmissionError(
                f"Attempt filesystem does not support RENAME_NOREPLACE: {os.strerror(error_number)}"
            )
    finally:
        _remove_tree(probe)


def _require_runtime() -> None:
    required_flags = all(
        type(getattr(os, name, None)) is int and getattr(os, name) != 0
        for name in ("O_DIRECTORY", "O_NOFOLLOW")
    )
    descriptor_relative = os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
    nofollow_stat = os.stat in os.supports_follow_symlinks
    safe_rmtree = bool(getattr(shutil.rmtree, "avoids_symlink_attacks", False))
    if (
        os.name != "posix"
        or not required_flags
        or not descriptor_relative
        or not nofollow_stat
        or not safe_rmtree
    ):
        raise SourceAdmissionError(
            "Source admission requires Linux descriptor-relative no-follow filesystem support"
        )
    _load_renameat2()
