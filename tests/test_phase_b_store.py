import hashlib
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import mock

from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.store import ContentStore


class ContentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.store = ContentStore(self.base / "cas")

    def blob_path(self, digest: str) -> Path:
        return self.store.root / "sha256" / digest[:2] / digest

    def temporary_blobs(self, digest: str) -> list[Path]:
        prefix = self.store.root / "sha256" / digest[:2]
        return list(prefix.glob("*.tmp")) if prefix.exists() else []

    def test_put_bytes_preserves_artifact_identity(self) -> None:
        digest = hashlib.sha256(b"value").hexdigest()
        reference = self.store.put_bytes(
            kind="test",
            data=b"value",
            producer_operation_id="operation-1",
            input_hashes=("a" * 64,),
        )
        self.assertEqual(
            reference,
            ArtifactRef(
                1,
                "test",
                digest,
                5,
                f"cas://sha256/{digest}",
                "operation-1",
                ("a" * 64,),
            ),
        )
        self.assertEqual(self.store.read_bytes(reference), b"value")

        class ArtifactRefSubclass(ArtifactRef):
            pass

        subclass = ArtifactRefSubclass(**reference.__dict__)
        with self.assertRaises(TypeError):
            self.store.read_bytes(subclass)

    def test_round_trips_zero_binary_and_large_blobs(self) -> None:
        values = (b"", bytes(range(256)), bytes(range(256)) * 16385)
        for data in values:
            with self.subTest(size=len(data)):
                digest, size = self.store.put_blob(data)
                self.assertEqual((digest, size), (hashlib.sha256(data).hexdigest(), len(data)))
                self.assertEqual(self.store.read_blob(digest, size), data)

    def test_duplicate_put_is_idempotent_and_read_only(self) -> None:
        digest, size = self.store.put_blob(b"same")
        first_inode = self.blob_path(digest).stat().st_ino
        self.assertEqual(self.store.put_blob(b"same"), (digest, size))
        metadata = self.blob_path(digest).stat()
        self.assertEqual(metadata.st_ino, first_inode)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o444)
        self.assertEqual(self.temporary_blobs(digest), [])

    def test_read_blob_rejects_malformed_digest_and_size(self) -> None:
        invalid = (
            ("A" * 64, 0),
            ("a" * 63, 0),
            ("g" * 64, 0),
            (None, 0),
            ("a" * 64, -1),
            ("a" * 64, True),
            ("a" * 64, 1.0),
        )
        for digest, size in invalid:
            with self.subTest(digest=digest, size=size), self.assertRaises((TypeError, ValueError)):
                self.store.read_blob(digest, size)
        with self.assertRaises(TypeError):
            self.store.put_blob(bytearray(b"not exact bytes"))

    def test_rejects_symlink_or_non_directory_root(self) -> None:
        target = self.base / "target"
        target.mkdir()
        symlink = self.base / "root-link"
        symlink.symlink_to(target, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            ContentStore(symlink)

        regular = self.base / "regular"
        regular.write_bytes(b"file")
        with self.assertRaises((OSError, ValueError)):
            ContentStore(regular)

        ancestor_target = self.base / "ancestor-target"
        ancestor_target.mkdir()
        ancestor_link = self.base / "ancestor-link"
        ancestor_link.symlink_to(ancestor_target, target_is_directory=True)
        with self.assertRaises(OSError):
            ContentStore(ancestor_link / "cas")

    def test_relative_root_is_preserved_exactly(self) -> None:
        relative = Path(os.path.relpath(self.base / "relative-cas", Path.cwd()))
        store = ContentStore(relative)
        self.assertIs(store.root, relative)
        digest, size = store.put_blob(b"relative")
        self.assertEqual(store.read_blob(digest, size), b"relative")

    def test_rejects_replaced_root(self) -> None:
        original = self.base / "cas-original"
        self.store.root.rename(original)
        self.store.root.symlink_to(original, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.put_blob(b"value")

    def test_rejects_root_replaced_by_real_directory(self) -> None:
        original = self.base / "cas-original-directory"
        self.store.root.rename(original)
        self.store.root.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.store.put_blob(b"value")
        with self.assertRaises(ValueError):
            self.store.read_blob("a" * 64, 0)

    def test_rejects_symlink_or_non_directory_hash_directories(self) -> None:
        for level, is_symlink in (
            ("sha256", True),
            ("sha256", False),
            ("prefix", True),
            ("prefix", False),
        ):
            with self.subTest(level=level, is_symlink=is_symlink):
                root = self.base / f"{level}-{is_symlink}"
                store = ContentStore(root)
                digest = hashlib.sha256(b"value").hexdigest()
                parent = root if level == "sha256" else root / "sha256"
                parent.mkdir(mode=0o700, exist_ok=True)
                parent.chmod(0o700)
                name = "sha256" if level == "sha256" else digest[:2]
                path = parent / name
                if is_symlink:
                    target = self.base / f"target-{level}-{is_symlink}"
                    target.mkdir(exist_ok=True)
                    path.symlink_to(target, target_is_directory=True)
                else:
                    path.write_bytes(b"not a directory")
                with self.assertRaises(OSError):
                    store.put_blob(b"value")

    def test_rejects_group_or_other_writable_hash_directory(self) -> None:
        sha256 = self.store.root / "sha256"
        sha256.mkdir(mode=0o700)
        sha256.chmod(0o777)
        with self.assertRaises(ValueError):
            self.store.put_blob(b"value")

    def test_read_rejects_symlink_directory_and_special_blob(self) -> None:
        digest = hashlib.sha256(b"value").hexdigest()
        prefix = self.store.root / "sha256" / digest[:2]
        prefix.mkdir(mode=0o700, parents=True)
        (self.store.root / "sha256").chmod(0o700)
        prefix.chmod(0o700)
        destination = prefix / digest

        target = self.base / "target-blob"
        target.write_bytes(b"value")
        destination.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.read_blob(digest, 5)
        destination.unlink()

        destination.mkdir()
        with self.assertRaises(ValueError):
            self.store.read_blob(digest, 5)
        destination.rmdir()

        os.mkfifo(destination)
        with self.assertRaises(ValueError):
            self.store.read_blob(digest, 5)

    def test_rejects_corrupt_truncated_and_oversized_existing_blob(self) -> None:
        cases = (b"VALUE", b"valu", b"value!")
        for replacement in cases:
            with self.subTest(replacement=replacement):
                data = b"value"
                digest, size = self.store.put_blob(data)
                path = self.blob_path(digest)
                path.chmod(0o644)
                path.write_bytes(replacement)
                with self.assertRaises(ValueError):
                    self.store.read_blob(digest, size)
                with self.assertRaises(ValueError):
                    self.store.put_blob(data)
                path.unlink()

    def test_publication_collision_never_overwrites_destination(self) -> None:
        data = b"expected"
        digest = hashlib.sha256(data).hexdigest()
        destination = self.store.root / "sha256" / digest[:2] / digest
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"attacker")
        with self.assertRaises(ValueError):
            self.store.put_blob(data)
        self.assertEqual(destination.read_bytes(), b"attacker")
        self.assertEqual(self.temporary_blobs(digest), [])

    def test_link_source_replacement_cannot_publish_attacker_bytes(self) -> None:
        data = b"expected"
        digest = hashlib.sha256(data).hexdigest()
        real_link = os.link

        def replace_then_link(source: str, destination: str, **kwargs: object) -> None:
            source_dir_fd = kwargs["src_dir_fd"]
            os.unlink(source, dir_fd=source_dir_fd)
            descriptor = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o444,
                dir_fd=source_dir_fd,
            )
            try:
                os.write(descriptor, b"attacker")
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
            real_link(source, destination, **kwargs)

        with mock.patch("dfinsta_pipeline.store.os.link", side_effect=replace_then_link):
            with self.assertRaises(ValueError):
                self.store.put_blob(data)
        self.assertFalse(self.blob_path(digest).exists())
        self.assertEqual(self.temporary_blobs(digest), [])
        with self.assertRaises(FileNotFoundError):
            self.store.read_blob(digest, len(data))

    def test_rejects_writable_or_persistently_hardlinked_existing_blob(self) -> None:
        digest, size = self.store.put_blob(b"value")
        path = self.blob_path(digest)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.read_blob(digest, size)
        with self.assertRaises(ValueError):
            self.store.put_blob(b"value")

        path.chmod(0o444)
        external_link = self.base / "external-link"
        os.link(path, external_link)
        with self.assertRaises(ValueError):
            self.store.read_blob(digest, size)
        with self.assertRaises(ValueError):
            self.store.put_blob(b"value")

    def test_short_writes_are_retried(self) -> None:
        real_write = os.write

        def short_write(descriptor: int, data: memoryview) -> int:
            return real_write(descriptor, data[:3])

        with mock.patch("dfinsta_pipeline.store.os.write", side_effect=short_write):
            digest, size = self.store.put_blob(b"a value requiring several writes")
        self.assertEqual(self.store.read_blob(digest, size), b"a value requiring several writes")

    def test_write_fsync_and_link_failures_clean_temporary_blobs(self) -> None:
        failures = (
            ("write", mock.patch("dfinsta_pipeline.store.os.write", side_effect=OSError("write"))),
            ("fchmod", mock.patch("dfinsta_pipeline.store.os.fchmod", side_effect=OSError("fchmod"))),
            ("fsync", mock.patch("dfinsta_pipeline.store.os.fsync", side_effect=OSError("fsync"))),
            ("link", mock.patch("dfinsta_pipeline.store.os.link", side_effect=OSError("link"))),
        )
        for label, patcher in failures:
            with self.subTest(label=label):
                data = label.encode()
                digest = hashlib.sha256(data).hexdigest()
                with patcher, self.assertRaises(OSError):
                    self.store.put_blob(data)
                self.assertEqual(self.temporary_blobs(digest), [])
                self.assertFalse(self.blob_path(digest).exists())

    def test_post_publication_directory_fsync_failure_is_ambiguous_but_clean(self) -> None:
        data = b"directory-fsync"
        digest = hashlib.sha256(data).hexdigest()
        real_fsync = os.fsync
        calls = 0

        def fail_second_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("directory fsync")
            real_fsync(descriptor)

        with mock.patch("dfinsta_pipeline.store.os.fsync", side_effect=fail_second_fsync):
            with self.assertRaises(OSError):
                self.store.put_blob(data)
        self.assertEqual(self.temporary_blobs(digest), [])
        self.assertEqual(self.blob_path(digest).read_bytes(), data)

    def test_unlink_failure_retries_cleanup_without_masking_failure(self) -> None:
        data = b"unlink-failure"
        digest = hashlib.sha256(data).hexdigest()
        real_unlink = os.unlink
        calls = 0

        def fail_first_unlink(path: str, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("unlink")
            real_unlink(path, **kwargs)

        with mock.patch("dfinsta_pipeline.store.os.unlink", side_effect=fail_first_unlink):
            with self.assertRaisesRegex(OSError, "unlink"):
                self.store.put_blob(data)
        self.assertEqual(self.temporary_blobs(digest), [])
        self.assertEqual(self.store.read_blob(digest, len(data)), data)

    def test_destination_post_open_failure_cleans_temporary_blob(self) -> None:
        data = b"post-open"
        digest = hashlib.sha256(data).hexdigest()
        real_fstat = os.fstat
        calls = 0

        def fail_destination_fstat(descriptor: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("destination fstat")
            return real_fstat(descriptor)

        with mock.patch("dfinsta_pipeline.store.os.fstat", side_effect=fail_destination_fstat):
            with self.assertRaisesRegex(OSError, "destination fstat"):
                self.store.put_blob(data)
        self.assertEqual(self.temporary_blobs(digest), [])

    def test_temporary_fstat_failure_cleans_path_and_descriptor(self) -> None:
        data = b"temporary-fstat"
        digest = hashlib.sha256(data).hexdigest()
        descriptor_directory = Path("/proc/self/fd")
        before = len(list(descriptor_directory.iterdir()))
        real_fstat = os.fstat
        calls = 0

        def fail_temporary_fstat(descriptor: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("temporary fstat")
            return real_fstat(descriptor)

        with mock.patch("dfinsta_pipeline.store.os.fstat", side_effect=fail_temporary_fstat):
            with self.assertRaisesRegex(OSError, "temporary fstat"):
                self.store.put_blob(data)
        after = len(list(descriptor_directory.iterdir()))
        self.assertEqual(self.temporary_blobs(digest), [])
        self.assertFalse(self.blob_path(digest).exists())
        self.assertLessEqual(after, before + 1)

    def test_concurrent_identical_writers_publish_one_blob(self) -> None:
        data = bytes(range(256)) * 4096
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(self.store.put_blob, (data,) * 24))
        self.assertEqual(set(results), {(hashlib.sha256(data).hexdigest(), len(data))})
        digest, size = results[0]
        self.assertEqual(self.store.read_blob(digest, size), data)
        self.assertEqual(self.temporary_blobs(digest), [])

    def test_reader_waits_for_visible_publication_to_reach_one_link(self) -> None:
        data = b"visible-publication"
        digest = hashlib.sha256(data).hexdigest()
        link_visible = Event()
        reader_waiting = Event()
        continue_writer = Event()
        continue_reader = Event()
        real_link = os.link

        def paused_link(source: str, destination: str, **kwargs: object) -> None:
            real_link(source, destination, **kwargs)
            link_visible.set()
            if not continue_writer.wait(timeout=5):
                raise TimeoutError("writer was not released")

        def paused_sleep(_: float) -> None:
            reader_waiting.set()
            if not continue_reader.wait(timeout=5):
                raise TimeoutError("reader was not released")

        with (
            mock.patch("dfinsta_pipeline.store.os.link", side_effect=paused_link),
            mock.patch("dfinsta_pipeline.store.time.sleep", side_effect=paused_sleep),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            writer = executor.submit(self.store.put_blob, data)
            self.assertTrue(link_visible.wait(timeout=5))
            reader = executor.submit(self.store.read_blob, digest, len(data))
            self.assertTrue(reader_waiting.wait(timeout=5))
            self.assertFalse(reader.done())
            continue_writer.set()
            self.assertEqual(writer.result(timeout=5), (digest, len(data)))
            continue_reader.set()
            self.assertEqual(reader.result(timeout=5), data)

    def test_repeated_read_failures_do_not_leak_descriptors(self) -> None:
        descriptor_directory = Path("/proc/self/fd")
        before = len(list(descriptor_directory.iterdir()))
        for _ in range(100):
            with self.assertRaises(FileNotFoundError):
                self.store.read_blob("a" * 64, 0)
        after = len(list(descriptor_directory.iterdir()))
        self.assertLessEqual(after, before + 1)

        digest, size = self.store.put_blob(b"unsafe-prefix")
        prefix = self.store.root / "sha256" / digest[:2]
        prefix.chmod(0o777)
        before = len(list(descriptor_directory.iterdir()))
        for _ in range(100):
            with self.assertRaises(ValueError):
                self.store.read_blob(digest, size)
        after = len(list(descriptor_directory.iterdir()))
        self.assertLessEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
