import dataclasses
import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from dfinsta_pipeline.backend import BackendError, BackendReport, compose_apk
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import ApktoolFullRebuildBackend, StockDexGraftBackend


def write_zip(path: Path, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = b"archive-comment"
        for info, payload in entries:
            archive.writestr(info, payload)


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
