from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from pathlib import Path

from .contracts import ArtifactRef


_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_READ_SIZE = 1024 * 1024
_ADOPTION_ATTEMPTS = 100
_ADOPTION_DELAY_SECONDS = 0.005


def _require_secure_storage() -> None:
    required_dir_fd = (os.open, os.mkdir, os.link, os.unlink)
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
        or os.link not in os.supports_follow_symlinks
        or not all(hasattr(os, name) for name in ("fchmod", "fsync", "read", "write"))
    ):
        raise RuntimeError("Secure descriptor-relative content storage is unavailable")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _validate_digest(digest: str) -> None:
    if type(digest) is not str:
        raise TypeError("Blob digest must be a string")
    if len(digest) != 64 or any(character not in _SHA256_CHARACTERS for character in digest):
        raise ValueError("Blob digest must be a lowercase SHA-256")


def _validate_size(size: int) -> None:
    if type(size) is not int:
        raise TypeError("Blob size must be an integer")
    if size < 0:
        raise ValueError("Blob size must be nonnegative")


def _close_descriptors(descriptors: list[int]) -> OSError | None:
    first_error: OSError | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    descriptors.clear()
    return first_error


def _walk_absolute_directory(path: Path, *, create: bool) -> int:
    descriptor = os.open("/", _directory_flags())
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            parent = descriptor
            descriptor = child
            try:
                os.close(parent)
            except BaseException:
                try:
                    os.close(child)
                finally:
                    descriptor = parent
                raise
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _validate_owned_directory(descriptor: int, label: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError(f"Unsafe {label} directory")
    return metadata


def _open_or_create_directory(parent_fd: int, name: str, label: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    else:
        os.fsync(parent_fd)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _validate_owned_directory(descriptor, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_and_verify(
    parent_fd: int,
    digest: str,
    size: int,
    *,
    expected_data: bytes | None = None,
    expected_inode: tuple[int, int] | None = None,
    allowed_link_counts: frozenset[int] = frozenset({1}),
) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(digest, _file_flags(), dir_fd=parent_fd)
    primary_failure = False
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != size
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink not in allowed_link_counts
            or (expected_inode is not None and identity != expected_inode)
        ):
            raise ValueError("Blob verification failed")

        chunks: list[bytes] = []
        remaining = size
        hasher = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(_READ_SIZE, remaining))
            if not chunk:
                raise ValueError("Blob verification failed")
            chunks.append(chunk)
            hasher.update(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        if (
            os.read(descriptor, 1)
            or hasher.hexdigest() != digest
            or (expected_data is not None and data != expected_data)
            or (final_metadata.st_dev, final_metadata.st_ino) != identity
            or final_metadata.st_size != size
            or stat.S_IMODE(final_metadata.st_mode) != 0o444
            or final_metadata.st_uid != os.geteuid()
            or final_metadata.st_nlink not in allowed_link_counts
        ):
            raise ValueError("Blob verification failed")
        return data, final_metadata
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if not primary_failure:
                raise


def _unlink_if_identity(parent_fd: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != identity or not stat.S_ISREG(metadata.st_mode):
            return False
        os.unlink(name, dir_fd=parent_fd)
        return True
    finally:
        os.close(descriptor)


def _unlink_current_regular(parent_fd: int, name: str) -> None:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def _require_missing(parent_fd: int, name: str) -> None:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    else:
        os.close(descriptor)
        raise ValueError("Substituted content-store publication remains visible")


class ContentStore:
    def __init__(self, root: Path):
        _require_secure_storage()
        if not isinstance(root, Path):
            raise TypeError("Content store root must be a Path")
        self.root = root
        self._absolute_root = Path(os.path.abspath(root))
        descriptor = _walk_absolute_directory(self._absolute_root, create=True)
        try:
            metadata = _validate_owned_directory(descriptor, "content store root")
            self._root_identity = (metadata.st_dev, metadata.st_ino)
        finally:
            os.close(descriptor)

    def _open_pinned_root(self) -> int:
        descriptor = _walk_absolute_directory(self._absolute_root, create=False)
        try:
            metadata = _validate_owned_directory(descriptor, "content store root")
            if (metadata.st_dev, metadata.st_ino) != self._root_identity:
                raise ValueError("Content store root identity changed")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_blob_directory(self, descriptors: list[int], digest: str, *, create: bool) -> int:
        root_fd = self._open_pinned_root()
        descriptors.append(root_fd)
        if create:
            sha256_fd = _open_or_create_directory(root_fd, "sha256", "SHA-256")
            descriptors.append(sha256_fd)
            prefix_fd = _open_or_create_directory(sha256_fd, digest[:2], "blob prefix")
        else:
            sha256_fd = os.open("sha256", _directory_flags(), dir_fd=root_fd)
            descriptors.append(sha256_fd)
            _validate_owned_directory(sha256_fd, "SHA-256")
            prefix_fd = os.open(digest[:2], _directory_flags(), dir_fd=sha256_fd)
            try:
                _validate_owned_directory(prefix_fd, "blob prefix")
            except BaseException:
                os.close(prefix_fd)
                raise
        descriptors.append(prefix_fd)
        return prefix_fd

    def _adopt_existing(self, prefix_fd: int, digest: str, size: int, data: bytes) -> None:
        for attempt in range(_ADOPTION_ATTEMPTS):
            try:
                _read_and_verify(
                    prefix_fd,
                    digest,
                    size,
                    expected_data=data,
                    allowed_link_counts=frozenset({1}),
                )
                return
            except ValueError:
                _, metadata = _read_and_verify(
                    prefix_fd,
                    digest,
                    size,
                    expected_data=data,
                    allowed_link_counts=frozenset({1, 2}),
                )
                if metadata.st_nlink == 1:
                    return
                if metadata.st_nlink != 2 or attempt + 1 == _ADOPTION_ATTEMPTS:
                    raise ValueError("Content-addressed blob collision")
                time.sleep(_ADOPTION_DELAY_SECONDS)
        raise ValueError("Content-addressed blob collision")

    def _read_stable_blob(self, prefix_fd: int, digest: str, size: int) -> bytes:
        for attempt in range(_ADOPTION_ATTEMPTS):
            try:
                data, _ = _read_and_verify(prefix_fd, digest, size)
                return data
            except ValueError:
                data, metadata = _read_and_verify(
                    prefix_fd,
                    digest,
                    size,
                    allowed_link_counts=frozenset({1, 2}),
                )
                if metadata.st_nlink == 1:
                    return data
                if metadata.st_nlink != 2 or attempt + 1 == _ADOPTION_ATTEMPTS:
                    raise ValueError("Blob did not reach a stable link count")
                time.sleep(_ADOPTION_DELAY_SECONDS)
        raise ValueError("Blob did not reach a stable link count")

    def put_blob(self, data: bytes) -> tuple[str, int]:
        if type(data) is not bytes:
            raise TypeError("Blob data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        descriptors: list[int] = []
        temporary_name: str | None = None
        temporary_identity: tuple[int, int] | None = None
        temporary_created = False
        primary_failure = False
        cleanup_error: OSError | None = None
        published_by_this_writer = False
        try:
            prefix_fd = self._open_blob_directory(descriptors, digest, create=True)
            temporary_name = f".{digest}.{secrets.token_hex(16)}.tmp"
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=prefix_fd,
            )
            descriptors.append(temporary_fd)
            temporary_created = True
            temporary_metadata = os.fstat(temporary_fd)
            temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)

            view = memoryview(data)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("Unable to write content-store blob")
                view = view[written:]
            os.fchmod(temporary_fd, 0o444)
            os.fsync(temporary_fd)

            try:
                os.link(
                    temporary_name,
                    digest,
                    src_dir_fd=prefix_fd,
                    dst_dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._adopt_existing(prefix_fd, digest, size, data)
            else:
                published_by_this_writer = True
                destination_fd = os.open(digest, _file_flags(), dir_fd=prefix_fd)
                descriptors.append(destination_fd)
                destination_metadata = os.fstat(destination_fd)
                linked_temporary_metadata = os.fstat(temporary_fd)
                linked_identity = (destination_metadata.st_dev, destination_metadata.st_ino)
                publication_matches = (
                    stat.S_ISREG(destination_metadata.st_mode)
                    and stat.S_ISREG(linked_temporary_metadata.st_mode)
                    and linked_identity == temporary_identity
                    and linked_identity
                    == (linked_temporary_metadata.st_dev, linked_temporary_metadata.st_ino)
                    and destination_metadata.st_size == linked_temporary_metadata.st_size == size
                    and stat.S_IMODE(destination_metadata.st_mode)
                    == stat.S_IMODE(linked_temporary_metadata.st_mode)
                    == 0o444
                    and destination_metadata.st_uid
                    == linked_temporary_metadata.st_uid
                    == os.geteuid()
                    and destination_metadata.st_nlink == linked_temporary_metadata.st_nlink == 2
                )
                if not publication_matches:
                    try:
                        _unlink_if_identity(prefix_fd, digest, linked_identity)
                    except OSError:
                        pass
                    try:
                        _unlink_current_regular(prefix_fd, temporary_name)
                    except OSError:
                        pass
                    _require_missing(prefix_fd, digest)
                    temporary_name = None
                    raise ValueError("Content-store publication source changed")

            if not _unlink_if_identity(prefix_fd, temporary_name, temporary_identity):
                raise OSError("Content-store temporary blob changed")
            temporary_name = None
            os.fsync(prefix_fd)
            _read_and_verify(
                prefix_fd,
                digest,
                size,
                expected_data=data,
                expected_inode=temporary_identity if published_by_this_writer else None,
            )
            return digest, size
        except BaseException:
            primary_failure = True
            raise
        finally:
            if temporary_name is not None and descriptors:
                try:
                    prefix_fd = descriptors[2]
                    if temporary_identity is not None:
                        _unlink_if_identity(prefix_fd, temporary_name, temporary_identity)
                    elif temporary_created:
                        os.unlink(temporary_name, dir_fd=prefix_fd)
                except OSError as error:
                    cleanup_error = error
            close_error = _close_descriptors(descriptors)
            if not primary_failure:
                if cleanup_error is not None:
                    raise cleanup_error
                if close_error is not None:
                    raise close_error

    def read_blob(self, digest: str, size: int) -> bytes:
        _validate_digest(digest)
        _validate_size(size)
        descriptors: list[int] = []
        primary_failure = False
        try:
            prefix_fd = self._open_blob_directory(descriptors, digest, create=False)
            return self._read_stable_blob(prefix_fd, digest, size)
        except BaseException:
            primary_failure = True
            raise
        finally:
            close_error = _close_descriptors(descriptors)
            if close_error is not None and not primary_failure:
                raise close_error

    def put_bytes(
        self,
        *,
        kind: str,
        data: bytes,
        producer_operation_id: str,
        input_hashes: tuple[str, ...],
    ) -> ArtifactRef:
        digest, size = self.put_blob(data)
        return ArtifactRef(
            schema_version=1,
            kind=kind,
            sha256=digest,
            size=size,
            uri=f"cas://sha256/{digest}",
            producer_operation_id=producer_operation_id,
            input_hashes=input_hashes,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if type(reference) is not ArtifactRef:
            raise TypeError("reference must be an exact ArtifactRef")
        return self.read_blob(reference.sha256, reference.size)
