import dataclasses
import hashlib
import os
import shutil
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dfinsta_pipeline.decoded_artifact as decoded_artifact_module
from dfinsta_pipeline.contracts import ArtifactRef
from dfinsta_pipeline.decoded_artifact import (
    MANIFEST_KIND,
    DecodedArtifactError,
    DecodedTreeEntryV1,
    DecodedTreeManifestV1,
    capture_decoded_tree,
    load_decoded_tree,
    materialize_decoded_tree,
    verify_materialized_decoded_tree,
)
from dfinsta_pipeline.store import ContentStore
from dfinsta_pipeline.verifier import decoded_tree_sha256


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class DecodedArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = ContentStore(self.base / "cas")

    def make_tree(self, name: str = "decoded") -> Path:
        root = self.base / name
        (root / "empty").mkdir(parents=True)
        (root / "a").mkdir()
        (root / "a" / "x").write_bytes(b"nested\x00bytes")
        (root / "a-").write_bytes(bytes(range(256)))
        (root / "zero").write_bytes(b"")
        return root

    def capture(self, root: Path) -> ArtifactRef:
        return capture_decoded_tree(self.store, root, "decode-1", ("a" * 64,))

    def blob_path(self, digest: str) -> Path:
        return self.store.root / "sha256" / digest[:2] / digest

    def test_capture_is_deterministic_and_matches_verifier_semantics(self) -> None:
        first_root = self.make_tree("first")
        second_root = self.make_tree("second")
        for path in second_root.rglob("*"):
            os.chmod(path, 0o777 if path.is_dir() else 0o600)
            os.utime(path, (1_000_000, 1_000_000))

        first = self.capture(first_root)
        second = self.capture(second_root)
        manifest = load_decoded_tree(self.store, first)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(manifest.decoded_tree_sha256, decoded_tree_sha256(first_root))
        self.assertEqual(
            tuple((entry.path, entry.kind) for entry in manifest.entries),
            (
                ("a", "directory"),
                ("a-", "file"),
                ("a/x", "file"),
                ("empty", "directory"),
                ("zero", "file"),
            ),
        )
        self.assertEqual(manifest.entries[-1].sha256, EMPTY_SHA256)
        self.assertEqual(manifest.sha256, first.sha256)

    def test_entry_and_manifest_types_are_exact(self) -> None:
        with self.assertRaises(TypeError):
            DecodedTreeEntryV1("file", "file", True, "a" * 64)
        with self.assertRaises(TypeError):
            DecodedTreeEntryV1("file", "file", 0, None)
        with self.assertRaises(ValueError):
            DecodedTreeEntryV1("dir", "directory", 0, None)
        with self.assertRaises(TypeError):
            DecodedTreeManifestV1(True, "a" * 64, ())
        with self.assertRaises(TypeError):
            DecodedTreeManifestV1(1, "a" * 64, [])

    def test_manifest_requires_order_parents_and_collision_free_paths(self) -> None:
        directory = DecodedTreeEntryV1("d", "directory", None, None)
        child = DecodedTreeEntryV1("d/f", "file", 0, EMPTY_SHA256)
        with self.assertRaisesRegex(DecodedArtifactError, "sorted"):
            DecodedTreeManifestV1(1, "a" * 64, (child, directory))
        with self.assertRaisesRegex(DecodedArtifactError, "parent"):
            DecodedTreeManifestV1(1, "a" * 64, (child,))
        with self.assertRaisesRegex(DecodedArtifactError, "casefold"):
            DecodedTreeManifestV1(
                1,
                "a" * 64,
                (
                    DecodedTreeEntryV1("A", "directory", None, None),
                    DecodedTreeEntryV1("a", "directory", None, None),
                ),
            )
        with self.assertRaisesRegex(DecodedArtifactError, "ancestor"):
            DecodedTreeManifestV1(
                1,
                "a" * 64,
                (
                    DecodedTreeEntryV1("d", "file", 0, EMPTY_SHA256),
                    child,
                ),
            )

    def test_linux_worker_path_policy(self) -> None:
        invalid = (
            "",
            "/absolute",
            "a\\b",
            "a//b",
            "a/./b",
            "a/../b",
            "trailing/",
            "bad:name",
            "bad.",
            "bad ",
            "control\x00name",
            "e\u0301",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                DecodedTreeEntryV1(path, "directory", None, None)
        DecodedTreeEntryV1("é/valid.bin", "file", 0, EMPTY_SHA256)

    def test_windows_device_names_roundtrip_as_linux_tree_entries(self) -> None:
        root = self.base / "device-names"
        root.mkdir()
        (root / "AUX.smali").write_bytes(b".class public LAUX;\n")
        (root / "CON.txt").write_bytes(b"content")

        reference = self.capture(root)
        parent = self.base / "materialized"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")

        self.assertEqual((destination / "AUX.smali").read_bytes(), b".class public LAUX;\n")
        self.assertEqual((destination / "CON.txt").read_bytes(), b"content")
        verify_materialized_decoded_tree(load_decoded_tree(self.store, reference), destination)

    def test_materialization_name_is_one_safe_component(self) -> None:
        reference = self.capture(self.make_tree())
        parent = self.base / "names"
        parent.mkdir()
        for name in ("a/b", "a\\b", ".", "..", "NUL", "AUX.smali", "bad "):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                materialize_decoded_tree(self.store, reference, parent, name)

    def test_load_rejects_noncanonical_duplicate_and_nonfinite_json(self) -> None:
        payloads = (
            b'{"decoded_tree_sha256":"' + b"0" * 64 + b'","entries":[],"schema_version":1 }',
            b'{"schema_version":1,"schema_version":1,"decoded_tree_sha256":"'
            + b"0" * 64
            + b'","entries":[]}',
            b'{"decoded_tree_sha256":"'
            + b"0" * 64
            + b'","entries":[],"schema_version":NaN}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                reference = self.store.put_bytes(
                    kind=MANIFEST_KIND,
                    data=payload,
                    producer_operation_id="test",
                    input_hashes=(),
                )
                with self.assertRaises((TypeError, ValueError)):
                    load_decoded_tree(self.store, reference)

    def test_load_requires_exact_store_reference_and_kind(self) -> None:
        reference = self.capture(self.make_tree())

        class RefSubclass(ArtifactRef):
            pass

        with self.assertRaises(TypeError):
            load_decoded_tree(self.store, RefSubclass(**reference.__dict__))
        with self.assertRaises(ValueError):
            load_decoded_tree(self.store, dataclasses.replace(reference, kind="other"))

    def test_capture_rejects_symlinks_special_files_and_hardlinks(self) -> None:
        cases = ("file-link", "dir-link", "fifo", "socket", "hardlink")
        for case in cases:
            with self.subTest(case=case):
                root = self.base / case
                root.mkdir()
                target = root / "target"
                target.write_bytes(b"value")
                open_socket = None
                if case == "file-link":
                    (root / "bad").symlink_to(target)
                elif case == "dir-link":
                    directory = root / "directory"
                    directory.mkdir()
                    (root / "bad").symlink_to(directory, target_is_directory=True)
                elif case == "fifo":
                    os.mkfifo(root / "bad")
                elif case == "socket":
                    open_socket = socket.socket(socket.AF_UNIX)
                    open_socket.bind(str(root / "bad"))
                else:
                    os.link(target, root / "bad")
                try:
                    with self.assertRaises((OSError, ValueError)):
                        self.capture(root)
                finally:
                    if open_socket is not None:
                        open_socket.close()

    def test_capture_limits_fail_before_publication(self) -> None:
        root = self.base / "limited"
        root.mkdir()
        (root / "large").write_bytes(b"12")
        with mock.patch("dfinsta_pipeline.decoded_artifact.MAX_FILE_BYTES", 1):
            with self.assertRaisesRegex(DecodedArtifactError, "file-size"):
                self.capture(root)

    def test_manifest_budget_fails_before_next_blob_publication(self) -> None:
        root = self.base / "manifest-budget"
        root.mkdir()
        (root / "a").write_bytes(b"first")
        (root / "z").write_bytes(b"second")
        first_budget = (
            decoded_artifact_module._MANIFEST_FIXED_OVERHEAD
            + decoded_artifact_module._manifest_entry_budget("a")
        )
        with mock.patch(
            "dfinsta_pipeline.decoded_artifact.MAX_MANIFEST_BYTES", first_budget
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "manifest"):
                self.capture(root)
        first_digest = hashlib.sha256(b"first").hexdigest()
        second_digest = hashlib.sha256(b"second").hexdigest()
        self.assertTrue(self.blob_path(first_digest).is_file())
        self.assertFalse(self.blob_path(second_digest).exists())

    def test_capture_rejects_invalid_lineage_before_reading_root(self) -> None:
        missing = self.base / "missing"
        with self.assertRaises(ValueError):
            capture_decoded_tree(self.store, missing, "bad operation", ())
        with self.assertRaises(TypeError):
            capture_decoded_tree(self.store, missing, "decode", (True,))

    def test_capture_detects_file_replacement_race(self) -> None:
        root = self.base / "replacement"
        root.mkdir()
        target = root / "file"
        target.write_bytes(b"original")
        target_inode = target.stat().st_ino
        real_read = os.read
        replaced = False

        def replace_after_read(descriptor: int, size: int) -> bytes:
            nonlocal replaced
            data = real_read(descriptor, size)
            if not replaced and os.fstat(descriptor).st_ino == target_inode:
                replaced = True
                target.rename(root / "old")
                target.write_bytes(b"competitor")
            return data

        with mock.patch("dfinsta_pipeline.decoded_artifact.os.read", side_effect=replace_after_read):
            with self.assertRaisesRegex(DecodedArtifactError, "changed"):
                self.capture(root)

    def test_capture_detects_same_inode_overwrite_after_earlier_read(self) -> None:
        root = self.base / "capture-overwrite"
        root.mkdir()
        first = root / "a"
        first.write_bytes(b"first")
        (root / "z").write_bytes(b"later")
        real_read = decoded_artifact_module._read_stable_file
        reads = 0

        def overwrite_first(
            parent_fd: int, name: str, metadata: os.stat_result
        ) -> tuple[bytes, os.stat_result]:
            nonlocal reads
            result = real_read(parent_fd, name, metadata)
            reads += 1
            if reads == 1:
                inode = first.stat().st_ino
                first.write_bytes(b"other")
                self.assertEqual(first.stat().st_ino, inode)
            return result

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._read_stable_file",
            side_effect=overwrite_first,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "changed after being read"):
                self.capture(root)
        self.assertEqual(reads, 2)
        with mock.patch("dfinsta_pipeline.decoded_artifact.MAX_ENTRIES", 0):
            with self.assertRaisesRegex(DecodedArtifactError, "entry"):
                self.capture(root)

    def test_load_rejects_missing_corrupt_writable_and_hardlinked_blob(self) -> None:
        for failure in ("missing", "corrupt", "writable", "hardlinked"):
            with self.subTest(failure=failure):
                store = ContentStore(self.base / f"cas-{failure}")
                root = self.base / f"tree-{failure}"
                root.mkdir()
                (root / "file").write_bytes(b"value")
                reference = capture_decoded_tree(store, root, "decode", ())
                manifest = load_decoded_tree(store, reference)
                blob = store.root / "sha256" / manifest.entries[0].sha256[:2] / manifest.entries[0].sha256
                if failure == "missing":
                    blob.unlink()
                elif failure == "corrupt":
                    os.chmod(blob, 0o644)
                    blob.write_bytes(b"other")
                    os.chmod(blob, 0o444)
                elif failure == "writable":
                    os.chmod(blob, 0o644)
                else:
                    os.link(blob, self.base / f"extra-{failure}")
                with self.assertRaises((OSError, ValueError)):
                    load_decoded_tree(store, reference)

    def test_materialize_roundtrip_modes_no_overwrite_and_recapture(self) -> None:
        source = self.make_tree()
        reference = self.capture(source)
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "output"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")
        verify_materialized_decoded_tree(manifest, destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "a" / "x").stat().st_mode), 0o600)
        recaptured = self.capture(destination)
        self.assertEqual(reference.sha256, recaptured.sha256)
        with self.assertRaises(FileExistsError):
            materialize_decoded_tree(self.store, reference, parent, "tree")

        for kind in ("file", "directory", "symlink"):
            name = f"existing-{kind}"
            path = parent / name
            if kind == "file":
                path.write_bytes(b"competitor")
            elif kind == "directory":
                path.mkdir()
            else:
                path.symlink_to(destination, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                materialize_decoded_tree(self.store, reference, parent, name)

    def test_materialize_rejects_parent_symlink_replacement_and_inner_competitor(self) -> None:
        reference = self.capture(self.make_tree())
        parent = self.base / "parent"
        parent.mkdir()
        parent_link = self.base / "parent-link"
        parent_link.symlink_to(parent, target_is_directory=True)
        with self.assertRaises(OSError):
            materialize_decoded_tree(self.store, reference, parent_link, "tree")

        moved = self.base / "moved-parent"
        real_mkdir = os.mkdir
        replaced = False

        def replace_parent(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            nonlocal replaced
            real_mkdir(name, mode, dir_fd=dir_fd)
            if name == "tree" and dir_fd is not None and not replaced:
                replaced = True
                parent.rename(moved)
                parent.mkdir()

        with mock.patch("dfinsta_pipeline.decoded_artifact.os.mkdir", side_effect=replace_parent):
            with self.assertRaises((OSError, ValueError)):
                materialize_decoded_tree(self.store, reference, parent, "tree")
        self.assertTrue((moved / "tree").is_dir())
        self.assertFalse((parent / "tree").exists())

        competitor_parent = self.base / "competitor-parent"
        competitor_parent.mkdir()
        inserted = False

        def insert_competitor(
            name: object, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            nonlocal inserted
            real_mkdir(name, mode, dir_fd=dir_fd)
            if name == "a" and dir_fd is not None and not inserted:
                inserted = True
                child_fd = os.open("a", os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
                try:
                    descriptor = os.open(
                        "x", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=child_fd
                    )
                    os.close(descriptor)
                finally:
                    os.close(child_fd)

        with mock.patch("dfinsta_pipeline.decoded_artifact.os.mkdir", side_effect=insert_competitor):
            with self.assertRaises(FileExistsError):
                materialize_decoded_tree(
                    self.store, reference, competitor_parent, "quarantine"
                )
        self.assertTrue((competitor_parent / "quarantine" / "a" / "x").exists())

    def test_materializer_verifies_held_root_not_pathname_decoy(self) -> None:
        reference = self.capture(self.make_tree())
        parent = self.base / "decoy-parent"
        parent.mkdir()
        destination = parent / "tree"
        quarantined = parent / "held-root"
        real_verify = decoded_artifact_module._verify_manifest_fd
        verified_inodes: list[int] = []

        def install_decoy(manifest: DecodedTreeManifestV1, root_fd: int) -> None:
            destination.rename(quarantined)
            shutil.copytree(quarantined, destination)
            os.mkdir("unexpected", dir_fd=root_fd)
            verified_inodes.append(os.fstat(root_fd).st_ino)
            real_verify(manifest, root_fd)

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._verify_manifest_fd",
            side_effect=install_decoy,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "topology"):
                materialize_decoded_tree(self.store, reference, parent, "tree")
        self.assertEqual(verified_inodes, [quarantined.stat().st_ino])
        self.assertNotEqual(quarantined.stat().st_ino, destination.stat().st_ino)

    def test_single_pass_detects_extra_inserted_during_semantic_hash(self) -> None:
        reference = self.capture(self.make_tree())
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "single-pass"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")
        real_update = decoded_artifact_module._semantic_update
        inserted = False

        def insert_after_semantic_update(digest: object, path: str, data: bytes) -> None:
            nonlocal inserted
            real_update(digest, path, data)
            if not inserted:
                inserted = True
                (destination / "late-extra").write_bytes(b"competitor")

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._semantic_update",
            side_effect=insert_after_semantic_update,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "directory changed"):
                verify_materialized_decoded_tree(manifest, destination)

    def test_single_pass_reads_each_file_once(self) -> None:
        reference = self.capture(self.make_tree())
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "one-read"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")
        real_read = decoded_artifact_module._read_stable_file
        reads = 0

        def count_read(
            parent_fd: int, name: str, metadata: os.stat_result
        ) -> tuple[bytes, os.stat_result]:
            nonlocal reads
            reads += 1
            return real_read(parent_fd, name, metadata)

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._read_stable_file",
            side_effect=count_read,
        ):
            verify_materialized_decoded_tree(manifest, destination)
        self.assertEqual(reads, sum(entry.kind == "file" for entry in manifest.entries))

    def test_verify_detects_same_inode_overwrite_after_semantic_update(self) -> None:
        root = self.base / "verify-overwrite-source"
        root.mkdir()
        (root / "a").write_bytes(b"first")
        (root / "z").write_bytes(b"later")
        reference = self.capture(root)
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "verify-overwrite"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")
        first = destination / "a"
        real_update = decoded_artifact_module._semantic_update
        updates = 0

        def overwrite_first(digest: object, path: str, data: bytes) -> None:
            nonlocal updates
            real_update(digest, path, data)
            updates += 1
            if updates == 1:
                inode = first.stat().st_ino
                first.write_bytes(b"other")
                self.assertEqual(first.stat().st_ino, inode)

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._semantic_update",
            side_effect=overwrite_first,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "changed after being read"):
                verify_materialized_decoded_tree(manifest, destination)
        self.assertEqual(updates, 2)

    def test_open_child_directory_closes_descriptor_when_fstat_fails(self) -> None:
        parent = self.base / "fd-cleanup"
        child = parent / "child"
        child.mkdir(parents=True)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        real_fstat = os.fstat
        opened: list[int] = []

        def fail_child_fstat(descriptor: int) -> os.stat_result:
            if descriptor != parent_fd:
                opened.append(descriptor)
                raise OSError("injected fstat failure")
            return real_fstat(descriptor)

        try:
            with mock.patch(
                "dfinsta_pipeline.decoded_artifact.os.fstat",
                side_effect=fail_child_fstat,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    decoded_artifact_module._open_child_directory(parent_fd, "child")
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                real_fstat(opened[0])
        finally:
            os.close(parent_fd)

    def test_destination_replacement_after_scan_fails_final_identity(self) -> None:
        reference = self.capture(self.make_tree())
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "post-scan"
        parent.mkdir()
        destination = materialize_decoded_tree(self.store, reference, parent, "tree")
        displaced = parent / "displaced"
        real_verify = decoded_artifact_module._verify_manifest_fd

        def replace_after_scan(verified: DecodedTreeManifestV1, root_fd: int) -> None:
            real_verify(verified, root_fd)
            destination.rename(displaced)
            destination.mkdir()

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._verify_manifest_fd",
            side_effect=replace_after_scan,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "root identity"):
                verify_materialized_decoded_tree(manifest, destination)

    def test_materializer_replacement_after_scan_fails_final_parent_stat(self) -> None:
        reference = self.capture(self.make_tree())
        parent = self.base / "materialize-post-scan"
        parent.mkdir()
        destination = parent / "tree"
        displaced = parent / "displaced"
        real_verify = decoded_artifact_module._verify_manifest_fd

        def replace_after_scan(manifest: DecodedTreeManifestV1, root_fd: int) -> None:
            real_verify(manifest, root_fd)
            destination.rename(displaced)
            destination.mkdir()

        with mock.patch(
            "dfinsta_pipeline.decoded_artifact._verify_manifest_fd",
            side_effect=replace_after_scan,
        ):
            with self.assertRaisesRegex(DecodedArtifactError, "root identity"):
                materialize_decoded_tree(self.store, reference, parent, "tree")
        self.assertTrue(displaced.is_dir())
        self.assertTrue(destination.is_dir())

    def test_verify_rejects_extras_changed_bytes_symlinks_and_hardlinks(self) -> None:
        reference = self.capture(self.make_tree())
        manifest = load_decoded_tree(self.store, reference)
        parent = self.base / "verify"
        parent.mkdir()
        for failure in ("extra", "changed", "symlink", "hardlink"):
            destination = materialize_decoded_tree(
                self.store, reference, parent, f"tree-{failure}"
            )
            if failure == "extra":
                (destination / "extra").write_bytes(b"extra")
            elif failure == "changed":
                (destination / "zero").write_bytes(b"changed")
            elif failure == "symlink":
                (destination / "link").symlink_to(destination / "zero")
            else:
                os.link(destination / "zero", destination / "hardlink")
            with self.subTest(failure=failure), self.assertRaises((OSError, ValueError)):
                verify_materialized_decoded_tree(manifest, destination)

    def test_short_write_is_completed_and_fsync_failure_leaves_private_tree(self) -> None:
        reference = self.capture(self.make_tree())
        parent = self.base / "short-write"
        parent.mkdir()
        real_write = os.write

        def short_write(descriptor: int, data: object) -> int:
            return real_write(descriptor, memoryview(data)[: max(1, len(data) // 2)])

        with mock.patch("dfinsta_pipeline.decoded_artifact.os.write", side_effect=short_write):
            destination = materialize_decoded_tree(self.store, reference, parent, "complete")
        self.assertEqual((destination / "a-").read_bytes(), bytes(range(256)))

        real_fsync = os.fsync
        calls = 0

        def fail_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected fsync failure")
            real_fsync(descriptor)

        with mock.patch("dfinsta_pipeline.decoded_artifact.os.fsync", side_effect=fail_fsync):
            with self.assertRaisesRegex(OSError, "injected"):
                materialize_decoded_tree(self.store, reference, parent, "quarantine")
        self.assertTrue((parent / "quarantine").is_dir())


if __name__ == "__main__":
    unittest.main()
