from __future__ import annotations

import copy
import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .contracts import canonical_sha256
from .port_contracts import ApktoolFullRebuildBackend, Backend, StockDexGraftBackend


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackendReport:
    kind: str
    stock_sha256: str
    intermediate_sha256: str
    output_sha256: str
    final_dex_entries: tuple[str, ...]
    replaced_entries: tuple[str, ...]
    added_entries: tuple[str, ...]
    retained_entry_count: int
    stripped_signature_entries: tuple[str, ...]
    passed: bool

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class _Entry:
    info: zipfile.ZipInfo
    data: bytes


@dataclass(frozen=True, slots=True)
class _Archive:
    entries: tuple[_Entry, ...]
    comment: bytes

    @property
    def by_name(self) -> dict[str, _Entry]:
        return {entry.info.filename: entry for entry in self.entries}


_DEX_ENTRY = re.compile(r"classes(?:[2-9]|[1-9][0-9]+)?\.dex")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with _open_regular(path) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise BackendError(f"Could not hash {path}: {exc}") from exc
    return digest.hexdigest()


def _open_regular(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BackendError(f"Path is not a regular file: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _load_archive(path: Path, label: str) -> tuple[_Archive, str]:
    try:
        with _open_regular(path) as stream:
            data = stream.read()
        digest = hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BackendError(f"{label} archive contains duplicate entry names")
            entries = tuple(_Entry(info, archive.read(info)) for info in infos)
            return _Archive(entries, archive.comment), digest
    except BackendError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise BackendError(f"Could not read {label} archive {path}: {exc}") from exc


def _read_archive(path: Path, label: str) -> _Archive:
    return _load_archive(path, label)[0]


def _is_signature(name: str) -> bool:
    parts = name.upper().split("/")
    return len(parts) == 2 and parts[0] == "META-INF" and (
        parts[1] == "MANIFEST.MF"
        or parts[1].startswith("SIG-")
        or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))
    )


def _dex_entries(archive: _Archive) -> tuple[str, ...]:
    return tuple(entry.info.filename for entry in archive.entries if _DEX_ENTRY.fullmatch(entry.info.filename))


def _require_exact_dex(
    archive: _Archive, expected: tuple[str, ...], label: str
) -> None:
    actual = _dex_entries(archive)
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise BackendError(
            f"{label} DEX topology mismatch: expected {expected!r}, found {actual!r}"
        )


def _validate_paths(stock: object, intermediate: object, output: object) -> tuple[Path, Path, Path]:
    for value, label in (
        (stock, "stock APK"),
        (intermediate, "intermediate APK"),
        (output, "output APK"),
    ):
        if not isinstance(value, Path):
            raise TypeError(f"{label} path must be a pathlib.Path")
    assert isinstance(stock, Path) and isinstance(intermediate, Path) and isinstance(output, Path)
    for path in (stock, intermediate, output.parent):
        for component in (path.absolute(), *path.absolute().parents):
            if component.is_symlink() or (
                hasattr(component, "is_junction") and component.is_junction()
            ):
                raise BackendError(f"Path contains a symlink or junction: {path}")
    for path, label in ((stock, "stock APK"), (intermediate, "intermediate APK")):
        if path.is_symlink() or not path.is_file():
            raise BackendError(f"{label} must be an existing regular non-symlink file: {path}")
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BackendError(f"Output parent must be an existing non-symlink directory: {parent}")
    if output.is_symlink() or output.exists():
        raise BackendError(f"Output must not already exist: {output}")
    return stock, intermediate, output


def _publish_temp(temp_path: Path, output: Path) -> None:
    try:
        os.link(temp_path, output)
    except OSError as exc:
        raise BackendError(f"Could not publish output APK {output}: {exc}") from exc


def _copy_to_temp(source: Path, output: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temp_path = Path(name)
        shutil.copyfile(source, temp_path)
        os.chmod(temp_path, 0o644)
        return temp_path
    except OSError as exc:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
        raise BackendError(f"Could not stage output APK for {output}: {exc}") from exc


def _write_graft(stock: _Archive, intermediate: _Archive, backend: StockDexGraftBackend, output: Path) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temp_path = Path(name)
        replacements = set(backend.replace_dex_entries)
        intermediate_by_name = intermediate.by_name
        with zipfile.ZipFile(temp_path, "w") as archive:
            archive.comment = stock.comment
            for entry in stock.entries:
                name = entry.info.filename
                if _is_signature(name):
                    continue
                payload = intermediate_by_name[name].data if name in replacements else entry.data
                archive.writestr(copy.copy(entry.info), payload)
            for name in backend.add_dex_entries:
                entry = intermediate_by_name[name]
                archive.writestr(copy.copy(entry.info), entry.data)
        os.chmod(temp_path, 0o644)
        return temp_path
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
        raise BackendError(f"Could not stage grafted APK for {output}: {exc}") from exc


def _verify_graft(
    output: _Archive,
    stock: _Archive,
    intermediate: _Archive,
    backend: StockDexGraftBackend,
) -> None:
    _require_exact_dex(output, backend.final_dex_entries, "Output")
    if any(_is_signature(entry.info.filename) for entry in output.entries):
        raise BackendError("Output still contains signature artifacts")
    output_by_name = output.by_name
    stock_by_name = stock.by_name
    intermediate_by_name = intermediate.by_name
    excluded = set(backend.replace_dex_entries) | set(backend.add_dex_entries)
    retained = {
        entry.info.filename: entry.data
        for entry in stock.entries
        if not _is_signature(entry.info.filename) and entry.info.filename not in excluded
    }
    expected_names = set(retained) | set(backend.replace_dex_entries) | set(backend.add_dex_entries)
    if set(output_by_name) != expected_names:
        raise BackendError("Output entry names do not match the declared graft composition")
    expected_order = tuple(
        entry.info.filename for entry in stock.entries if not _is_signature(entry.info.filename)
    ) + backend.add_dex_entries
    if tuple(entry.info.filename for entry in output.entries) != expected_order:
        raise BackendError("Output entry order does not match the declared graft composition")
    if output.comment != stock.comment:
        raise BackendError("Output archive comment changed")
    if any(output_by_name[name].data != data for name, data in retained.items()):
        raise BackendError("Output changed retained stock entry bytes")
    for name in (*backend.replace_dex_entries, *backend.add_dex_entries):
        if output_by_name[name].data != intermediate_by_name[name].data:
            raise BackendError(f"Output payload does not match intermediate entry: {name}")
    for name, entry in output_by_name.items():
        expected = intermediate_by_name[name] if name in backend.add_dex_entries else stock_by_name[name]
        if _metadata(entry.info) != _metadata(expected.info):
            raise BackendError(f"Output entry metadata changed: {name}")


def _metadata(info: zipfile.ZipInfo) -> tuple[object, ...]:
    return (
        info.date_time,
        info.compress_type,
        info.comment,
        info.extra,
        info.internal_attr,
        info.external_attr,
        info.create_system,
        info.create_version,
        info.extract_version,
        info.flag_bits,
        info.volume,
    )


def compose_apk(
    backend: Backend, stock_apk: Path, intermediate_apk: Path, output_apk: Path
) -> BackendReport:
    if not isinstance(backend, (ApktoolFullRebuildBackend, StockDexGraftBackend)):
        raise TypeError("backend must be a supported Backend contract")
    stock_apk, intermediate_apk, output_apk = _validate_paths(
        stock_apk, intermediate_apk, output_apk
    )
    intermediate, intermediate_sha256 = _load_archive(intermediate_apk, "intermediate")
    temp_path: Path | None = None
    try:
        if isinstance(backend, ApktoolFullRebuildBackend):
            stock_sha256 = _sha256(stock_apk)
            _require_exact_dex(intermediate, backend.final_dex_entries, "Intermediate")
            temp_path = _copy_to_temp(intermediate_apk, output_apk)
            staged = _read_archive(temp_path, "staged output")
            _require_exact_dex(staged, backend.final_dex_entries, "Output")
            if _sha256(temp_path) != intermediate_sha256:
                raise BackendError("Staged output does not match the intermediate APK")
            replaced_entries: tuple[str, ...] = ()
            added_entries: tuple[str, ...] = ()
            stripped_signatures: tuple[str, ...] = ()
            retained_count = 0
        else:
            stock, stock_sha256 = _load_archive(stock_apk, "stock")
            _require_exact_dex(stock, backend.stock_dex_entries, "Stock")
            stock_names = set(stock.by_name)
            intermediate_names = set(intermediate.by_name)
            for name in backend.replace_dex_entries:
                if name not in stock_names or name not in intermediate_names:
                    raise BackendError(f"Replacement entry must exist in both archives: {name}")
            for name in backend.add_dex_entries:
                if name in stock_names:
                    raise BackendError(f"Added entry collides with stock archive: {name}")
                if name not in intermediate_names:
                    raise BackendError(f"Added entry is missing from intermediate archive: {name}")
            temp_path = _write_graft(stock, intermediate, backend, output_apk)
            staged = _read_archive(temp_path, "staged output")
            _verify_graft(staged, stock, intermediate, backend)
            replaced_entries = backend.replace_dex_entries
            added_entries = backend.add_dex_entries
            stripped_signatures = tuple(
                entry.info.filename for entry in stock.entries if _is_signature(entry.info.filename)
            )
            retained_count = sum(
                not _is_signature(entry.info.filename)
                and entry.info.filename not in set(backend.replace_dex_entries)
                for entry in stock.entries
            )
        output_sha256 = _sha256(temp_path)
        _publish_temp(temp_path, output_apk)
        return BackendReport(
            kind=backend.kind,
            stock_sha256=stock_sha256,
            intermediate_sha256=intermediate_sha256,
            output_sha256=output_sha256,
            final_dex_entries=backend.final_dex_entries,
            replaced_entries=replaced_entries,
            added_entries=added_entries,
            retained_entry_count=retained_count,
            stripped_signature_entries=stripped_signatures,
            passed=True,
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
