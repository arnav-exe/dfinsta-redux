import copy
import dataclasses
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import dfinsta_pipeline.backend as backend_module
from dfinsta_pipeline.backend import (
    BackendError,
    BackendReport,
    compose_apk,
    validate_composed_apk,
    validate_composed_apk_bytes,
)
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import ApktoolFullRebuildBackend, StockDexGraftBackend


def write_zip(path: Path, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = b"archive-comment"
        for info, payload in entries:
            archive.writestr(info, payload)


def zip_bytes(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = b"archive-comment"
        for entry, payload in entries:
            archive.writestr(entry, payload)
    return buffer.getvalue()


def info(name: str, compression: int, marker: int) -> zipfile.ZipInfo:
    value = zipfile.ZipInfo(name, (2024, 1, marker, 0, 0, 0))
    value.compress_type = compression
    value.external_attr = marker << 16
    value.comment = f"entry-{marker}".encode("utf-8")
    value.extra = bytes((0xCA, 0xFE, 0, 0))
    return value


def names_and_data(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        return [(entry.filename, archive.read(entry)) for entry in archive.infolist()]


def rewrite_zip(
    source: Path,
    target: Path,
    *,
    payloads: dict[str, bytes] | None = None,
    drop: set[str] | None = None,
    append: list[tuple[zipfile.ZipInfo | str, bytes]] | None = None,
    reverse: bool = False,
    comment: bytes | None = None,
    change_metadata: str | None = None,
) -> None:
    payloads = payloads or {}
    drop = drop or set()
    append = append or []
    with zipfile.ZipFile(source) as archive:
        entries = [
            (copy.copy(entry), payloads.get(entry.filename, archive.read(entry)))
            for entry in archive.infolist()
            if entry.filename not in drop
        ]
        archive_comment = archive.comment
    if reverse:
        entries.reverse()
    with zipfile.ZipFile(target, "w") as archive:
        archive.comment = archive_comment if comment is None else comment
        for entry, payload in entries:
            if entry.filename == change_metadata:
                entry.external_attr ^= 1 << 16
            archive.writestr(entry, payload)
        for entry, payload in append:
            archive.writestr(entry, payload)


class PhaseBBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stock = self.root / "stock.apk"
        self.intermediate = self.root / "intermediate.apk"
        self.output = self.root / "output.apk"
        self.full = ApktoolFullRebuildBackend(
            kind="apktool_full_rebuild", profile_id="synthetic-full", dex_entries=("classes.dex",)
        )
        self.graft = StockDexGraftBackend(
            kind="stock_dex_graft",
            profile_id="synthetic-graft",
            stock_dex_entries=("classes.dex", "classes2.dex"),
            replace_dex_entries=("classes2.dex",),
            add_dex_entries=("classes3.dex",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def basic_full_archives(self) -> None:
        write_zip(self.stock, [("classes.dex", b"stock"), ("res/a", b"old")])
        write_zip(self.intermediate, [("res/a", b"new"), ("classes.dex", b"rebuilt")])

    def basic_graft_archives(self) -> None:
        write_zip(
            self.stock,
            [
                (info("classes.dex", zipfile.ZIP_STORED, 2), b"stock-one"),
                (info("res/raw/value", zipfile.ZIP_DEFLATED, 4), b"retained"),
                ("META-INF/MANIFEST.MF", b"manifest"),
                ("meta-inf/CERT.sf", b"signature"),
                ("META-INF/SIG-CUSTOM", b"signature-block"),
                ("META-INF/services/provider", b"preserve-meta"),
                (info("classes2.dex", zipfile.ZIP_DEFLATED, 6), b"stock-two"),
            ],
        )
        write_zip(
            self.intermediate,
            [
                (info("classes2.dex", zipfile.ZIP_STORED, 8), b"replacement"),
                (info("classes3.dex", zipfile.ZIP_DEFLATED, 10), b"addition"),
            ],
        )

    def assert_no_staged_temps(self) -> None:
        self.assertFalse(
            any(path.name.startswith(f".{self.output.name}.") for path in self.root.iterdir())
        )

    def test_full_rebuild_adopts_intermediate_bytes_and_reports(self) -> None:
        self.basic_full_archives()
        report = compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), self.intermediate.read_bytes())
        self.assertEqual(report.kind, self.full.kind)
        self.assertEqual(report.output_sha256, report.intermediate_sha256)
        self.assertEqual(report.final_dex_entries, self.full.final_dex_entries)
        self.assertEqual(report.replaced_entries, ())
        self.assertEqual(report.added_entries, ())
        self.assertEqual(report.stripped_signature_entries, ())
        self.assertEqual(report.retained_entry_count, 0)
        self.assertTrue(report.passed)
        self.assertTrue(dataclasses.is_dataclass(report))
        self.assertEqual(report.sha256, canonical_sha256(report))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.passed = False

    def test_full_rebuild_treats_stock_as_provenance_only(self) -> None:
        self.stock.write_bytes(b"opaque-stock-provenance")
        write_zip(self.intermediate, [("classes.dex", b"rebuilt")])
        report = compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(
            report.stock_sha256,
            hashlib.sha256(b"opaque-stock-provenance").hexdigest(),
        )
        self.assertEqual(self.output.read_bytes(), self.intermediate.read_bytes())
        self.assertEqual(
            validate_composed_apk(self.full, self.stock, self.intermediate, self.output),
            report,
        )
        self.assertEqual(
            validate_composed_apk_bytes(
                self.full,
                self.stock.read_bytes(),
                self.intermediate.read_bytes(),
                self.output.read_bytes(),
            ),
            report,
        )

    def test_full_rebuild_refuses_overwrite(self) -> None:
        self.basic_full_archives()
        compose_apk(self.full, self.stock, self.intermediate, self.output)
        before = self.output.read_bytes()
        with self.assertRaisesRegex(BackendError, "already exist"):
            compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), before)

    def test_graft_replaces_adds_strips_preserves_order_and_metadata(self) -> None:
        self.basic_graft_archives()
        report = compose_apk(self.graft, self.stock, self.intermediate, self.output)
        self.assertEqual(
            names_and_data(self.output),
            [
                ("classes.dex", b"stock-one"),
                ("res/raw/value", b"retained"),
                ("META-INF/services/provider", b"preserve-meta"),
                ("classes2.dex", b"replacement"),
                ("classes3.dex", b"addition"),
            ],
        )
        with zipfile.ZipFile(self.output) as archive, zipfile.ZipFile(self.stock) as stock:
            output_infos = {entry.filename: entry for entry in archive.infolist()}
            stock_infos = {entry.filename: entry for entry in stock.infolist()}
            for name in ("classes.dex", "res/raw/value", "META-INF/services/provider", "classes2.dex"):
                self.assertEqual(output_infos[name].compress_type, stock_infos[name].compress_type)
                self.assertEqual(output_infos[name].date_time, stock_infos[name].date_time)
                self.assertEqual(output_infos[name].external_attr, stock_infos[name].external_attr)
                self.assertEqual(output_infos[name].comment, stock_infos[name].comment)
            with zipfile.ZipFile(self.intermediate) as intermediate:
                added = intermediate.getinfo("classes3.dex")
                self.assertEqual(output_infos["classes3.dex"].compress_type, added.compress_type)
                self.assertEqual(output_infos["classes3.dex"].date_time, added.date_time)
                self.assertEqual(output_infos["classes3.dex"].external_attr, added.external_attr)
                self.assertEqual(output_infos["classes3.dex"].comment, added.comment)
            self.assertEqual(archive.comment, stock.comment)
        self.assertEqual(report.replaced_entries, ("classes2.dex",))
        self.assertEqual(report.added_entries, ("classes3.dex",))
        self.assertEqual(
            report.stripped_signature_entries,
            ("META-INF/MANIFEST.MF", "meta-inf/CERT.sf", "META-INF/SIG-CUSTOM"),
        )
        self.assertEqual(report.retained_entry_count, 3)
        self.assertEqual(report.output_sha256, hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_read_only_validation_matches_compose_for_full_and_graft(self) -> None:
        self.basic_full_archives()
        with zipfile.ZipFile(self.intermediate, "a") as archive:
            archive.writestr("META-INF/CERT.RSA", b"full-rebuild-signature")
        full_report = compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(
            validate_composed_apk(self.full, self.stock, self.intermediate, self.output),
            full_report,
        )
        self.assertEqual(
            validate_composed_apk_bytes(
                self.full,
                self.stock.read_bytes(),
                self.intermediate.read_bytes(),
                self.output.read_bytes(),
            ),
            full_report,
        )
        self.assertEqual(full_report.stripped_signature_entries, ())

        stock = self.root / "graft-stock.apk"
        intermediate = self.root / "graft-intermediate.apk"
        output = self.root / "graft-output.apk"
        self.stock, self.intermediate = stock, intermediate
        self.basic_graft_archives()
        graft_report = compose_apk(self.graft, stock, intermediate, output)
        self.assertEqual(
            validate_composed_apk(self.graft, stock, intermediate, output),
            graft_report,
        )
        self.assertEqual(
            validate_composed_apk_bytes(
                self.graft,
                stock.read_bytes(),
                intermediate.read_bytes(),
                output.read_bytes(),
            ),
            graft_report,
        )
        self.assertEqual(
            graft_report.stripped_signature_entries,
            ("META-INF/MANIFEST.MF", "meta-inf/CERT.sf", "META-INF/SIG-CUSTOM"),
        )

    def test_validation_does_not_mutate_filesystem_or_modes(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)

        def snapshot() -> dict[str, tuple[bytes, int, int]]:
            return {
                path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in self.root.iterdir()
                if path.is_file()
            }

        before = snapshot()
        parent_mode = self.root.stat().st_mode
        with (
            mock.patch.object(backend_module.tempfile, "mkstemp", side_effect=AssertionError),
            mock.patch.object(backend_module.shutil, "copyfile", side_effect=AssertionError),
            mock.patch.object(backend_module.os, "chmod", side_effect=AssertionError),
            mock.patch.object(backend_module.os, "link", side_effect=AssertionError),
            mock.patch.object(Path, "unlink", side_effect=AssertionError),
        ):
            validate_composed_apk(self.graft, self.stock, self.intermediate, self.output)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.root.stat().st_mode, parent_mode)

    def test_byte_validation_makes_no_filesystem_calls(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)
        values = (
            self.stock.read_bytes(),
            self.intermediate.read_bytes(),
            self.output.read_bytes(),
        )
        forbidden = AssertionError("byte validation accessed the filesystem")
        with (
            mock.patch.object(backend_module.os, "open", side_effect=forbidden),
            mock.patch.object(backend_module.os, "stat", side_effect=forbidden),
            mock.patch.object(backend_module.os, "link", side_effect=forbidden),
            mock.patch.object(backend_module.os, "chmod", side_effect=forbidden),
            mock.patch.object(backend_module.os, "unlink", side_effect=forbidden),
            mock.patch.object(backend_module.tempfile, "mkstemp", side_effect=forbidden),
            mock.patch.object(backend_module.shutil, "copyfile", side_effect=forbidden),
            mock.patch.object(Path, "unlink", side_effect=forbidden),
        ):
            report = validate_composed_apk_bytes(self.graft, *values)
        self.assertEqual(report.output_sha256, hashlib.sha256(values[2]).hexdigest())

    def test_byte_validation_requires_exact_bytes(self) -> None:
        self.basic_full_archives()
        compose_apk(self.full, self.stock, self.intermediate, self.output)
        values = [self.stock.read_bytes(), self.intermediate.read_bytes(), self.output.read_bytes()]

        class BytesSubclass(bytes):
            pass

        for index in range(3):
            for invalid in (
                bytearray(values[index]),
                memoryview(values[index]),
                BytesSubclass(values[index]),
            ):
                candidates = values.copy()
                candidates[index] = invalid
                with self.subTest(index=index, type=type(invalid)), self.assertRaises(TypeError):
                    validate_composed_apk_bytes(self.full, *candidates)

    def test_byte_validation_rejects_malformed_duplicate_and_topology(self) -> None:
        self.basic_full_archives()
        compose_apk(self.full, self.stock, self.intermediate, self.output)
        stock = self.stock.read_bytes()
        intermediate = self.intermediate.read_bytes()
        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("classes.dex", b"first")
                archive.writestr("classes.dex", b"second")
        candidates = (
            b"not-a-zip",
            duplicate.getvalue(),
            zip_bytes([("classes.dex", b"one"), ("classes2.dex", b"extra")]),
        )
        for output in candidates:
            with self.subTest(output=output[:16]), self.assertRaises(BackendError):
                validate_composed_apk_bytes(self.full, stock, intermediate, output)

    def test_byte_validation_rejects_duplicate_stock_and_intermediate(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)
        duplicate_stock = io.BytesIO()
        duplicate_intermediate = io.BytesIO()
        for buffer, entries in (
            (
                duplicate_stock,
                (("classes.dex", b"one"), ("classes.dex", b"two"), ("classes2.dex", b"two")),
            ),
            (
                duplicate_intermediate,
                (("classes2.dex", b"one"), ("classes2.dex", b"two"), ("classes3.dex", b"three")),
            ),
        ):
            with zipfile.ZipFile(buffer, "w") as archive:
                with self.assertWarns(UserWarning):
                    for name, payload in entries:
                        archive.writestr(name, payload)
        output = self.output.read_bytes()
        with self.assertRaisesRegex(BackendError, "stock archive contains duplicate"):
            validate_composed_apk_bytes(
                self.graft,
                duplicate_stock.getvalue(),
                self.intermediate.read_bytes(),
                output,
            )
        with self.assertRaisesRegex(BackendError, "intermediate archive contains duplicate"):
            validate_composed_apk_bytes(
                self.graft,
                self.stock.read_bytes(),
                duplicate_intermediate.getvalue(),
                output,
            )

    def test_byte_validation_rejects_graft_signature_metadata_and_payload(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)
        variants = (
            ("signature", {"append": [("meta-inf/CERT.ec", b"signature")]}),
            ("metadata", {"change_metadata": "res/raw/value"}),
            ("replacement", {"payloads": {"classes2.dex": b"forged"}}),
            ("addition", {"payloads": {"classes3.dex": b"forged"}}),
        )
        for name, changes in variants:
            candidate = self.root / f"byte-bad-{name}.apk"
            rewrite_zip(self.output, candidate, **changes)
            with self.subTest(name=name), self.assertRaises(BackendError):
                validate_composed_apk_bytes(
                    self.graft,
                    self.stock.read_bytes(),
                    self.intermediate.read_bytes(),
                    candidate.read_bytes(),
                )

    def test_validation_rejects_invalid_full_outputs_and_paths(self) -> None:
        self.basic_full_archives()
        compose_apk(self.full, self.stock, self.intermediate, self.output)
        corrupt = self.root / "corrupt-output.apk"
        corrupt.write_bytes(b"not a zip")
        forged = self.root / "forged-output.apk"
        write_zip(forged, [("res/a", b"forged"), ("classes.dex", b"rebuilt")])
        topology = self.root / "topology-output.apk"
        write_zip(topology, [("classes.dex", b"rebuilt"), ("classes2.dex", b"extra")])
        duplicate = self.root / "duplicate-output.apk"
        with zipfile.ZipFile(duplicate, "w") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("classes.dex", b"first")
                archive.writestr("classes.dex", b"second")
        for candidate in (corrupt, forged, topology, duplicate):
            with self.subTest(candidate=candidate.name), self.assertRaises(BackendError):
                validate_composed_apk(self.full, self.stock, self.intermediate, candidate)

        missing = self.root / "missing-output.apk"
        with self.assertRaises(BackendError):
            validate_composed_apk(self.full, self.stock, self.intermediate, missing)
        output_link = self.root / "output-link.apk"
        output_link.symlink_to(self.output)
        with self.assertRaises(BackendError):
            validate_composed_apk(self.full, self.stock, self.intermediate, output_link)
        with self.assertRaises(TypeError):
            validate_composed_apk(self.full, self.stock, self.intermediate, str(self.output))

    def test_validation_rejects_every_protected_graft_property(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)
        variants = (
            ("retained-bytes", {"payloads": {"res/raw/value": b"changed"}}),
            ("replacement", {"payloads": {"classes2.dex": b"forged"}}),
            ("addition", {"payloads": {"classes3.dex": b"forged"}}),
            ("missing", {"drop": {"res/raw/value"}}),
            ("metadata", {"change_metadata": "res/raw/value"}),
            ("order", {"reverse": True}),
            ("comment", {"comment": b"changed-comment"}),
            ("signature", {"append": [("META-INF/CERT.RSA", b"signature")]}),
        )
        for name, changes in variants:
            candidate = self.root / f"bad-{name}.apk"
            rewrite_zip(self.output, candidate, **changes)
            with self.subTest(name=name), self.assertRaises(BackendError):
                validate_composed_apk(self.graft, self.stock, self.intermediate, candidate)

        wrong_topology = self.root / "bad-topology.apk"
        rewrite_zip(self.output, wrong_topology, drop={"classes3.dex"})
        with self.assertRaisesRegex(BackendError, "topology"):
            validate_composed_apk(self.graft, self.stock, self.intermediate, wrong_topology)

    def test_compose_does_not_publish_when_shared_validation_fails(self) -> None:
        self.basic_full_archives()
        staged = self.root / "forced-corrupt-stage.tmp"

        def corrupt_stage(_source: Path, _output: Path) -> Path:
            staged.write_bytes(b"not a zip")
            return staged

        with mock.patch.object(backend_module, "_copy_to_temp", side_effect=corrupt_stage):
            with self.assertRaises(BackendError):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(staged.exists())

    def test_link_collision_preserves_competitor_and_cleans_temp(self) -> None:
        self.basic_full_archives()
        competitor = b"competitor"
        real_link = os.link

        def collide(source: Path, output: Path) -> None:
            Path(output).write_bytes(competitor)
            real_link(source, output)

        with mock.patch.object(backend_module.os, "link", side_effect=collide):
            with self.assertRaisesRegex(BackendError, "publish"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), competitor)
        self.assert_no_staged_temps()

    def test_published_inode_mismatch_preserves_replacement_and_cleans_temp(self) -> None:
        self.basic_full_archives()
        competitor = b"replacement"
        real_publish = backend_module._publish_temp

        def replace_after_link(temp_path: Path, output: Path) -> None:
            real_publish(temp_path, output)
            output.unlink()
            output.write_bytes(competitor)

        with mock.patch.object(backend_module, "_publish_temp", side_effect=replace_after_link):
            with self.assertRaisesRegex(BackendError, "identity"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), competitor)
        self.assert_no_staged_temps()

    def test_post_link_identity_failure_removes_published_inode(self) -> None:
        self.basic_full_archives()
        real_identity = backend_module._regular_identity
        staged_checks = 0

        def fail_post_link_check(path: Path, label: str):
            nonlocal staged_checks
            if label == "staged output":
                staged_checks += 1
                if staged_checks == 2:
                    raise BackendError("forced post-link identity failure")
            return real_identity(path, label)

        with mock.patch.object(
            backend_module, "_regular_identity", side_effect=fail_post_link_check
        ):
            with self.assertRaisesRegex(BackendError, "post-link identity failure"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertFalse(self.output.exists())
        self.assert_no_staged_temps()

    def test_linked_output_validation_failure_removes_only_published_inode(self) -> None:
        self.basic_full_archives()
        real_validate = backend_module._validate_composed_apk

        def fail_linked(*args, **kwargs):
            if args[5] == self.output:
                raise BackendError("forced linked-output failure")
            return real_validate(*args, **kwargs)

        with mock.patch.object(backend_module, "_validate_composed_apk", side_effect=fail_linked):
            with self.assertRaisesRegex(BackendError, "linked-output failure"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertFalse(self.output.exists())
        self.assert_no_staged_temps()

    def test_guarded_cleanup_does_not_unlink_post_validation_replacement(self) -> None:
        self.basic_full_archives()
        competitor = b"post-link replacement"
        real_validate = backend_module._validate_composed_apk

        def replace_and_fail(*args, **kwargs):
            if args[5] == self.output:
                self.output.unlink()
                self.output.write_bytes(competitor)
                raise BackendError("forced replacement failure")
            return real_validate(*args, **kwargs)

        with mock.patch.object(
            backend_module, "_validate_composed_apk", side_effect=replace_and_fail
        ):
            with self.assertRaisesRegex(BackendError, "replacement failure"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertEqual(self.output.read_bytes(), competitor)
        self.assert_no_staged_temps()

    def test_linked_output_report_mismatch_removes_published_inode(self) -> None:
        self.basic_full_archives()
        real_validate = backend_module._validate_composed_apk

        def mismatch_linked(*args, **kwargs):
            report = real_validate(*args, **kwargs)
            return dataclasses.replace(report, passed=False) if args[5] == self.output else report

        with mock.patch.object(backend_module, "_validate_composed_apk", side_effect=mismatch_linked):
            with self.assertRaisesRegex(BackendError, "report"):
                compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertFalse(self.output.exists())
        self.assert_no_staged_temps()

    def test_validation_rejects_non_regular_paths(self) -> None:
        self.basic_full_archives()
        compose_apk(self.full, self.stock, self.intermediate, self.output)
        stock_directory = self.root / "stock-directory"
        intermediate_directory = self.root / "intermediate-directory"
        output_directory = self.root / "output-directory"
        stock_directory.mkdir()
        intermediate_directory.mkdir()
        output_directory.mkdir()
        for paths in (
            (stock_directory, self.intermediate, self.output),
            (self.stock, intermediate_directory, self.output),
            (self.stock, self.intermediate, output_directory),
        ):
            with self.subTest(path=paths), self.assertRaises(BackendError):
                validate_composed_apk(self.full, *paths)

    def test_validation_rejects_duplicate_stock_and_intermediate(self) -> None:
        self.basic_graft_archives()
        compose_apk(self.graft, self.stock, self.intermediate, self.output)
        duplicate_stock = self.root / "duplicate-stock.apk"
        duplicate_intermediate = self.root / "duplicate-intermediate.apk"
        for path, entries in (
            (
                duplicate_stock,
                (("classes.dex", b"one"), ("classes.dex", b"two"), ("classes2.dex", b"two")),
            ),
            (
                duplicate_intermediate,
                (("classes2.dex", b"one"), ("classes2.dex", b"two"), ("classes3.dex", b"three")),
            ),
        ):
            with zipfile.ZipFile(path, "w") as archive:
                with self.assertWarns(UserWarning):
                    for name, payload in entries:
                        archive.writestr(name, payload)
        with self.assertRaisesRegex(BackendError, "stock archive contains duplicate"):
            validate_composed_apk(self.graft, duplicate_stock, self.intermediate, self.output)
        with self.assertRaisesRegex(BackendError, "intermediate archive contains duplicate"):
            validate_composed_apk(self.graft, self.stock, duplicate_intermediate, self.output)

    def test_wrong_topology_missing_entries_and_add_collision_leave_no_output(self) -> None:
        cases = []
        self.basic_graft_archives()
        cases.append((self.graft, self.stock, self.intermediate))

        wrong_stock = self.root / "wrong-stock.apk"
        write_zip(wrong_stock, [("classes.dex", b"only")])
        cases[0] = (self.graft, wrong_stock, self.intermediate)

        missing_replace = self.root / "missing-replace.apk"
        write_zip(missing_replace, [("classes3.dex", b"addition")])
        cases.append((self.graft, self.stock, missing_replace))

        missing_add = self.root / "missing-add.apk"
        write_zip(missing_add, [("classes2.dex", b"replacement")])
        cases.append((self.graft, self.stock, missing_add))

        colliding_stock = self.root / "colliding-stock.apk"
        write_zip(
            colliding_stock,
            [("classes.dex", b"one"), ("classes2.dex", b"two"), ("classes3.dex", b"collision")],
        )
        cases.append((self.graft, colliding_stock, self.intermediate))

        for index, (backend, stock, intermediate) in enumerate(cases):
            output = self.root / f"failed-{index}.apk"
            with self.subTest(index=index), self.assertRaises(BackendError):
                compose_apk(backend, stock, intermediate, output)
            self.assertFalse(output.exists())

    def test_corrupt_and_duplicate_archives_are_rejected(self) -> None:
        self.basic_full_archives()
        corrupt = self.root / "corrupt.apk"
        corrupt.write_bytes(b"not-a-zip")
        duplicate = self.root / "duplicate.apk"
        with zipfile.ZipFile(duplicate, "w") as archive:
            with self.assertWarns(UserWarning):
                archive.writestr("classes.dex", b"first")
                archive.writestr("classes.dex", b"second")
        for index, candidate in enumerate((corrupt, duplicate)):
            output = self.root / f"invalid-{index}.apk"
            with self.subTest(index=index), self.assertRaises(BackendError):
                compose_apk(self.full, self.stock, candidate, output)
            self.assertFalse(output.exists())

        malformed = self.root / "malformed-deflate.apk"
        write_zip(
            malformed,
            [(info("classes.dex", zipfile.ZIP_DEFLATED, 2), b"compressed payload" * 100)],
        )
        with zipfile.ZipFile(malformed) as archive:
            entry = archive.getinfo("classes.dex")
            raw = bytearray(malformed.read_bytes())
            name_length = int.from_bytes(raw[entry.header_offset + 26 : entry.header_offset + 28], "little")
            extra_length = int.from_bytes(raw[entry.header_offset + 28 : entry.header_offset + 30], "little")
            payload_offset = entry.header_offset + 30 + name_length + extra_length
        raw[payload_offset] ^= 0xFF
        malformed.write_bytes(raw)
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.stock, malformed, self.root / "malformed-output.apk")

    def test_path_types_symlinks_and_output_parent_are_strict(self) -> None:
        self.basic_full_archives()
        for values in (
            (str(self.stock), self.intermediate, self.output),
            (self.stock, str(self.intermediate), self.output),
            (self.stock, self.intermediate, str(self.output)),
        ):
            with self.assertRaises(TypeError):
                compose_apk(self.full, *values)

        stock_link = self.root / "stock-link.apk"
        stock_link.symlink_to(self.stock)
        with self.assertRaises(BackendError):
            compose_apk(self.full, stock_link, self.intermediate, self.output)
        intermediate_link = self.root / "intermediate-link.apk"
        intermediate_link.symlink_to(self.intermediate)
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.stock, intermediate_link, self.output)
        output_link = self.root / "output-link.apk"
        output_link.symlink_to(self.root / "missing.apk")
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.stock, self.intermediate, output_link)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.stock, self.intermediate, parent_link / "new.apk")
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.root, self.intermediate, self.output)
        with self.assertRaises(BackendError):
            compose_apk(self.full, self.stock, self.intermediate, self.root / "missing" / "new.apk")

    def test_full_rebuild_rejects_wrong_dex_topology(self) -> None:
        write_zip(self.stock, [("classes.dex", b"stock")])
        write_zip(self.intermediate, [("classes.dex", b"one"), ("classes2.dex", b"extra")])
        with self.assertRaisesRegex(BackendError, "topology"):
            compose_apk(self.full, self.stock, self.intermediate, self.output)
        self.assertFalse(self.output.exists())

    def test_source_has_no_target_specific_literals(self) -> None:
        source = (Path(__file__).parents[1] / "src/dfinsta_pipeline/backend.py").read_text(
            encoding="utf-8"
        )
        for literal in ("340", "430", "LX/", "clips/", "classes20"):
            self.assertNotIn(literal, source)


if __name__ == "__main__":
    unittest.main()
