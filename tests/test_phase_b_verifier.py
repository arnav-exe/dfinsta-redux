import dataclasses
import hashlib
import struct
import tempfile
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path

from dfinsta_pipeline.compiler import TargetPortSpecV2
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import (
    ApktoolFullRebuildBackend,
    AppendManifestComponents,
    AppendResourceEntries,
    ArchiveEntriesAbsent,
    ArchiveEntryNamesAndBytesPreservedExcept,
    BytesAbsent,
    BytesPresent,
    DeletePath,
    DescriptorSetEquality,
    DescriptorsPresent,
    DexEntrySetEquality,
    DexStringSubstringsAbsent,
    DexStringsAbsent,
    DexStringsPresent,
    ExactSmaliSequenceCount,
    ManifestComponent,
    OperationPostcondition,
    OverlayTree,
    ReplaceResourceEntry,
    ResourceEntry,
    SmaliEdit,
    SourceFile,
    StockDexGraftBackend,
    TargetIdentity,
)
from dfinsta_pipeline.verifier import (
    DecodedArtifactReceipt,
    VerificationError,
    decoded_tree_sha256,
    verify_apk,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uleb128(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def mutf8(value: str) -> tuple[int, bytes]:
    raw = value.encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[index : index + 2], "little") for index in range(0, len(raw), 2)]
    encoded = bytearray()
    for unit in units:
        if unit == 0:
            encoded.extend(b"\xc0\x80")
        elif unit <= 0x7F:
            encoded.append(unit)
        elif unit <= 0x7FF:
            encoded.extend((0xC0 | unit >> 6, 0x80 | unit & 0x3F))
        else:
            encoded.extend(
                (0xE0 | unit >> 12, 0x80 | unit >> 6 & 0x3F, 0x80 | unit & 0x3F)
            )
    return len(units), bytes(encoded)


def finalize_dex(data: bytearray) -> bytes:
    data[12:32] = hashlib.sha1(data[32:]).digest()
    struct.pack_into("<I", data, 8, zlib.adler32(data[12:]) & 0xFFFFFFFF)
    return bytes(data)


def minimal_dex(strings: tuple[str, ...]) -> bytes:
    table_offset = 112 if strings else 0
    table_end = 112 + 4 * len(strings)
    data_offset = (table_end + 3) & ~3
    encoded = [uleb128(size) + value + b"\0" for size, value in map(mutf8, strings)]
    offsets = []
    cursor = data_offset
    for value in encoded:
        offsets.append(cursor)
        cursor += len(value)
    map_offset = (cursor + 3) & ~3
    map_entries = [(0x0000, 1, 0)]
    if strings:
        map_entries.extend(
            ((0x0001, len(strings), table_offset), (0x2002, len(strings), data_offset))
        )
    map_entries.append((0x1000, 1, map_offset))
    file_size = map_offset + 4 + 12 * len(map_entries)
    data = bytearray(file_size)
    data[:8] = b"dex\n035\0"
    struct.pack_into("<I", data, 32, file_size)
    struct.pack_into("<I", data, 36, 112)
    struct.pack_into("<I", data, 40, 0x12345678)
    struct.pack_into("<I", data, 52, map_offset)
    struct.pack_into("<I", data, 56, len(strings))
    struct.pack_into("<I", data, 60, table_offset)
    struct.pack_into("<I", data, 104, file_size - data_offset)
    struct.pack_into("<I", data, 108, data_offset)
    for index, offset in enumerate(offsets):
        struct.pack_into("<I", data, table_offset + index * 4, offset)
    cursor = data_offset
    for value in encoded:
        data[cursor : cursor + len(value)] = value
        cursor += len(value)
    struct.pack_into("<I", data, map_offset, len(map_entries))
    for index, (section_type, size, offset) in enumerate(map_entries):
        struct.pack_into("<HHII", data, map_offset + 4 + 12 * index, section_type, 0, size, offset)
    return finalize_dex(data)


def write_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


class VerifierFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="verifier-test-")
        self.root = Path(self.temporary.name)
        self.decoded = self.root / "decoded"
        self.source = self.root / "source"
        self.decoded.mkdir()
        self.source.mkdir()
        self.stock = self.root / "stock.apk"
        self.output = self.root / "output.apk"

        self.dex = minimal_dex(("alpha", "needle"))
        write_zip(
            self.stock,
            [
                ("classes.dex", b"stock-dex"),
                ("assets/common.bin", b"common"),
                ("META-INF/MANIFEST.MF", b"signature"),
            ],
        )
        write_zip(
            self.output,
            [("classes.dex", self.dex), ("assets/common.bin", b"common")],
        )

        self.write_decoded(
            "smali/sample/Worker.smali",
            ".class public Lsample/Worker;\n"
            ".super Ljava/lang/Object;\n"
            ".method public run()V\n"
            "    .line 7\n"
            "    const/4 v0, 0x1\n"
            "    # ignored\n"
            "    return-void\n"
            ".end method\n",
        )
        overlay_smali = (
            b".class public Laddon/Feature;\n.super Ljava/lang/Object;\n"
            b".method public go()V\n    return-void\n.end method\n"
        )
        overlay_xml = b'<node xmlns:p="urn:test" p:value="one"><child> value </child></node>'
        overlay_binary = b"overlay-bytes"
        self.write_source("bundle/addon/Feature.smali", overlay_smali)
        self.write_source("bundle/config.xml", overlay_xml)
        self.write_source("bundle/data.bin", overlay_binary)
        self.write_decoded(
            "smali_extra/addon/Feature.smali",
            b"# generated\n" + overlay_smali.replace(b"\n", b"\r\n"),
        )
        self.write_decoded(
            "smali_extra/config.xml",
            b'<node xmlns:q="urn:test" q:value="one"><!-- note --><child>value</child></node>',
        )
        self.write_decoded("smali_extra/data.bin", overlay_binary)
        self.write_decoded(
            "res/values/values.xml",
            '<resources><string name="added">Added</string>'
            '<string name="changed">After</string></resources>',
        )
        self.write_decoded(
            "AndroidManifest.xml",
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
            '<application><activity android:name="sample.Settings" /></application></manifest>',
        )

        self.edit = SmaliEdit(
            "edit",
            "smali_edit",
            ("intent",),
            "Lsample/Worker;",
            "run()V",
            "replace",
            "all",
            None,
            ("const/4 v0, 0x0",),
            1,
            ("const/4 v0, 0x1",),
            ("const/4 v0, 0x1",),
            1,
        )
        source_files = (
            SourceFile("addon/Feature.smali", digest(overlay_smali)),
            SourceFile("config.xml", digest(overlay_xml)),
            SourceFile("data.bin", digest(overlay_binary)),
        )
        self.overlay = OverlayTree(
            "overlay",
            "overlay_tree",
            ("intent",),
            "bundle",
            "smali_extra",
            source_files,
            canonical_sha256(source_files),
            "forbid",
        )
        self.append = AppendResourceEntries(
            "append",
            "append_resource_entries",
            ("intent",),
            "res/values/values.xml",
            (ResourceEntry("string", "added", '<string name="added">Added</string>'),),
        )
        self.replace = ReplaceResourceEntry(
            "replace",
            "replace_resource_entry",
            ("intent",),
            "res/values/values.xml",
            ResourceEntry("string", "changed", '<string name="changed">Before</string>'),
            ResourceEntry("string", "changed", '<string name="changed">After</string>'),
        )
        self.manifest = AppendManifestComponents(
            "manifest",
            "append_manifest_components",
            ("intent",),
            "AndroidManifest.xml",
            (
                ManifestComponent(
                    "activity",
                    "sample.Settings",
                    '<activity android:name="sample.Settings" />',
                ),
            ),
        )
        self.delete = DeletePath("delete", "delete_path", ("intent",), "assets/gone.bin", True)
        operations = (
            self.edit,
            self.overlay,
            self.append,
            self.replace,
            self.manifest,
            self.delete,
        )
        proofs = tuple(
            OperationPostcondition(
                f"proof-{operation.operation_id}",
                "operation_postcondition",
                operation.operation_id,
                canonical_sha256(operation),
            )
            for operation in operations
        )
        assertions = (
            proofs[2],
            proofs[0],
            proofs[5],
            proofs[1],
            proofs[4],
            proofs[3],
            ExactSmaliSequenceCount(
                "smali-count",
                "exact_smali_sequence_count",
                "Lsample/Worker;",
                "run()V",
                ("const/4 v0, 0x1", "return-void"),
                1,
            ),
            DexEntrySetEquality(
                "backend.final-dex-entries", "dex_entry_set_equality", ("classes.dex",)
            ),
            DescriptorSetEquality(
                "descriptor-set",
                "descriptor_set_equality",
                "classes.dex",
                ("Lsample/Worker;",),
            ),
            DescriptorsPresent(
                "descriptors-present",
                "descriptors_present",
                "classes.dex",
                ("Lsample/Worker;",),
            ),
            DexStringsPresent(
                "strings-present", "dex_strings_present", "classes.dex", ("needle",)
            ),
            DexStringsAbsent(
                "strings-absent", "dex_strings_absent", "classes.dex", ("missing",)
            ),
            DexStringSubstringsAbsent(
                "substrings-absent",
                "dex_string_substrings_absent",
                "classes.dex",
                ("forbidden",),
            ),
            ArchiveEntriesAbsent(
                "entries-absent", "archive_entries_absent", ("META-INF/MANIFEST.MF",)
            ),
            ArchiveEntryNamesAndBytesPreservedExcept(
                "archive-preserved",
                "archive_preservation_except",
                ("META-INF/MANIFEST.MF", "classes.dex"),
            ),
            BytesPresent("bytes-present", "bytes_present", "assets/common.bin", "636f6d"),
            BytesAbsent("bytes-absent", "bytes_absent", "assets/common.bin", "ffff"),
        )
        self.spec = TargetPortSpecV2(
            schema_version=2,
            intent_sha256="a" * 64,
            resolution_sha256="b" * 64,
            target=TargetIdentity(
                "sample.package",
                "fixture",
                1,
                digest(self.stock.read_bytes()),
                "monolithic",
            ),
            backend=ApktoolFullRebuildBackend(
                "apktool_full_rebuild", "fixture-full", ("classes.dex",)
            ),
            intent_statuses=(),
            operations=operations,
            assertions=assertions,
        )
        self.receipt = self.make_receipt()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_decoded(self, relative: str, data: str | bytes) -> Path:
        path = self.decoded / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return path

    def write_source(self, relative: str, data: bytes) -> Path:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def make_receipt(self) -> DecodedArtifactReceipt:
        return DecodedArtifactReceipt(
            output_apk_sha256=digest(self.output.read_bytes()),
            decoded_tree_sha256=decoded_tree_sha256(self.decoded),
            decoder_profile_id="fixture-decoder",
            decoder_capability_sha256="c" * 64,
        )

    def verify(
        self,
        spec: TargetPortSpecV2 | None = None,
        receipt: DecodedArtifactReceipt | None = None,
        *,
        refresh_receipt: bool = True,
    ):
        return verify_apk(
            spec or self.spec,
            self.stock,
            self.output,
            self.decoded,
            self.source,
            self.make_receipt() if refresh_receipt and receipt is None else receipt or self.receipt,
        )


class PhaseBVerifierTests(VerifierFixture):
    def test_success_report_preserves_assertion_order_and_has_canonical_hash(self) -> None:
        report = self.verify()
        self.assertTrue(report.passed)
        self.assertEqual(report.operation_proof_count, len(self.spec.operations))
        self.assertEqual(
            tuple(result.assertion_id for result in report.assertion_results),
            (*tuple(assertion.assertion_id for assertion in self.spec.assertions), "backend.signature-policy"),
        )
        self.assertTrue(all(result.passed for result in report.assertion_results))
        self.assertEqual(report.output_sha256, digest(self.output.read_bytes()))
        self.assertEqual(report.stock_sha256, digest(self.stock.read_bytes()))
        self.assertEqual(report.decoded_artifact_receipt, self.receipt)
        self.assertEqual(report.decoded_tree_sha256, self.receipt.decoded_tree_sha256)
        self.assertEqual(report.decoder_profile_id, "fixture-decoder")
        self.assertEqual(report.decoder_capability_sha256, "c" * 64)
        self.assertEqual(report.sha256, canonical_sha256(report))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.passed = False

    def test_receipt_rejects_stale_unrelated_and_tampered_bindings(self) -> None:
        self.write_decoded("unrelated/new.bin", b"new")
        with self.assertRaisesRegex(VerificationError, "receipt tree"):
            self.verify(refresh_receipt=False)

        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "only.bin").write_bytes(b"other")
        unrelated_receipt = dataclasses.replace(
            self.receipt, decoded_tree_sha256=decoded_tree_sha256(unrelated)
        )
        with self.assertRaisesRegex(VerificationError, "receipt tree"):
            self.verify(receipt=unrelated_receipt, refresh_receipt=False)

        tampered_output = dataclasses.replace(self.receipt, output_apk_sha256="0" * 64)
        with self.assertRaisesRegex(VerificationError, "receipt output"):
            self.verify(receipt=tampered_output, refresh_receipt=False)
        with self.assertRaises(ValueError):
            dataclasses.replace(self.receipt, decoder_capability_sha256="C" * 64)
        with self.assertRaises(ValueError):
            dataclasses.replace(self.receipt, decoder_profile_id="Uppercase")

    def test_decoded_tree_hash_is_ordered_and_rejects_symlinks(self) -> None:
        first = decoded_tree_sha256(self.decoded)
        self.assertEqual(first, decoded_tree_sha256(self.decoded))
        link = self.decoded / "linked"
        link.symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(VerificationError, "Symlink or junction"):
            decoded_tree_sha256(self.decoded)

    def test_each_operation_intrinsic_mismatch_fails_its_proof(self) -> None:
        mutations = (
            ("edit", lambda: self.write_decoded(
                "smali/sample/Worker.smali",
                ".class Lsample/Worker;\n.method run()V\n return-void\n.end method\n",
            )),
            ("overlay", lambda: self.write_decoded("smali_extra/data.bin", b"changed")),
            ("append", lambda: self.write_decoded(
                "res/values/values.xml",
                '<resources><string name="changed">After</string></resources>',
            )),
            ("replace", lambda: self.write_decoded(
                "res/values/values.xml",
                '<resources><string name="added">Added</string>'
                '<string name="changed">Wrong</string></resources>',
            )),
            ("manifest", lambda: self.write_decoded(
                "AndroidManifest.xml",
                '<manifest xmlns:android="http://schemas.android.com/apk/res/android">'
                '<application /></manifest>',
            )),
            ("delete", lambda: self.write_decoded("assets/gone.bin", b"present")),
        )
        for operation_id, mutate in mutations:
            with self.subTest(operation=operation_id):
                self.tearDown()
                self.setUp()
                mutate()
                report = self.verify()
                result = next(
                    item
                    for item in report.assertion_results
                    if item.assertion_id == f"proof-{operation_id}"
                )
                self.assertFalse(result.passed)
                self.assertFalse(report.passed)

    def test_overlay_source_hash_and_xml_semantics_are_verified(self) -> None:
        self.write_source("bundle/data.bin", b"tampered")
        self.assertFalse(
            next(
                result
                for result in self.verify().assertion_results
                if result.assertion_id == "proof-overlay"
            ).passed
        )
        self.write_source("bundle/data.bin", b"overlay-bytes")
        self.write_decoded(
            "smali_extra/config.xml",
            b'<node xmlns:q="urn:test" q:value="two"><child>value</child></node>',
        )
        self.assertFalse(
            next(
                result
                for result in self.verify().assertion_results
                if result.assertion_id == "proof-overlay"
            ).passed
        )

    def test_smali_labels_are_normalized_for_operation_and_overlay_proofs(self) -> None:
        final = (
            ":branch_source",
            "if-eqz v0, :done_source",
            "goto :branch_source",
            ":done_source",
        )
        edit = dataclasses.replace(self.edit, payload=final, final_sequence=final)
        self.write_decoded(
            "smali/sample/Worker.smali",
            ".class Lsample/Worker;\n.method run()V\n"
            ":branch_generated\n"
            "if-eqz v0, :done_generated\n"
            "goto :branch_generated\n"
            ":done_generated\n"
            "return-void\n.end method\n",
        )
        operations = tuple(edit if operation is self.edit else operation for operation in self.spec.operations)
        assertions = tuple(
            dataclasses.replace(assertion, operation_sha256=canonical_sha256(edit))
            if isinstance(assertion, OperationPostcondition) and assertion.operation_id == "edit"
            else assertion
            for assertion in self.spec.assertions
        )
        spec = dataclasses.replace(self.spec, operations=operations, assertions=assertions)
        result = next(
            item for item in self.verify(spec).assertion_results if item.assertion_id == "proof-edit"
        )
        self.assertTrue(result.passed)

        source_smali = (
            b".class Laddon/Feature;\n.method go()V\n:start_source\n"
            b"goto :end_source\n:end_source\nreturn-void\n.end method\n"
        )
        target_smali = source_smali.replace(b"source", b"generated")
        self.write_source("bundle/addon/Feature.smali", source_smali)
        self.write_decoded("smali_extra/addon/Feature.smali", target_smali)
        source_files = tuple(
            dataclasses.replace(source_file, sha256=digest(source_smali))
            if source_file.relative_path == "addon/Feature.smali"
            else source_file
            for source_file in self.overlay.source_files
        )
        overlay = dataclasses.replace(
            self.overlay,
            source_files=source_files,
            source_manifest_sha256=canonical_sha256(source_files),
        )
        operations = tuple(
            overlay if operation is self.overlay else operation for operation in self.spec.operations
        )
        assertions = tuple(
            dataclasses.replace(assertion, operation_sha256=canonical_sha256(overlay))
            if isinstance(assertion, OperationPostcondition) and assertion.operation_id == "overlay"
            else assertion
            for assertion in self.spec.assertions
        )
        spec = dataclasses.replace(self.spec, operations=operations, assertions=assertions)
        result = next(
            item
            for item in self.verify(spec).assertion_results
            if item.assertion_id == "proof-overlay"
        )
        self.assertTrue(result.passed)

    def test_smali_operation_checks_residual_preconditions_and_missing_removal_scope(self) -> None:
        path = self.decoded / "smali/sample/Worker.smali"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "    return-void", "    const/4 v0, 0x0\n    return-void"
            ),
            encoding="utf-8",
        )
        result = next(
            item for item in self.verify().assertion_results if item.assertion_id == "proof-edit"
        )
        self.assertFalse(result.passed)
        self.assertIn("residual precondition", result.detail)

        removal = dataclasses.replace(
            self.edit,
            payload=(),
            final_sequence=self.edit.precondition_sequence,
            expected_final_count=0,
        )
        operations = tuple(
            removal if operation is self.edit else operation for operation in self.spec.operations
        )
        assertions = tuple(
            dataclasses.replace(assertion, operation_sha256=canonical_sha256(removal))
            if isinstance(assertion, OperationPostcondition) and assertion.operation_id == "edit"
            else assertion
            for assertion in self.spec.assertions
        )
        spec = dataclasses.replace(self.spec, operations=operations, assertions=assertions)

        path.unlink()
        result = next(
            item for item in self.verify(spec).assertion_results if item.assertion_id == "proof-edit"
        )
        self.assertFalse(result.passed)
        self.assertIn("descriptor is absent", result.detail)

        self.write_decoded(
            "smali/sample/Worker.smali",
            ".class Lsample/Worker;\n.method other()V\nreturn-void\n.end method\n",
        )
        result = next(
            item for item in self.verify(spec).assertion_results if item.assertion_id == "proof-edit"
        )
        self.assertFalse(result.passed)
        self.assertIn("method is absent", result.detail)

    def test_descriptors_present_allows_extras_but_requires_declared_descriptors(self) -> None:
        self.write_decoded(
            "smali/sample/Extra.smali",
            ".class Lsample/Extra;\n.super Ljava/lang/Object;\n",
        )
        report = self.verify()
        present = next(
            result
            for result in report.assertion_results
            if result.assertion_id == "descriptors-present"
        )
        equality = next(
            result
            for result in report.assertion_results
            if result.assertion_id == "descriptor-set"
        )
        self.assertTrue(present.passed)
        self.assertFalse(equality.passed)

        (self.decoded / "smali/sample/Worker.smali").unlink()
        present = next(
            result
            for result in self.verify().assertion_results
            if result.assertion_id == "descriptors-present"
        )
        self.assertFalse(present.passed)
        self.assertIn("Worker", present.detail)

    def test_operation_proofs_are_exhaustive_bound_and_known(self) -> None:
        assertions = self.spec.assertions
        missing = dataclasses.replace(self.spec, assertions=assertions[1:])
        with self.assertRaisesRegex(VerificationError, "Missing operation proof"):
            self.verify(missing)

        proof = next(item for item in assertions if item.assertion_id == "proof-edit")
        bad_hash = dataclasses.replace(proof, operation_sha256="0" * 64)
        spoofed = dataclasses.replace(
            self.spec,
            assertions=tuple(bad_hash if item is proof else item for item in assertions),
        )
        with self.assertRaisesRegex(VerificationError, "hash mismatch"):
            self.verify(spoofed)

        unknown = dataclasses.replace(proof, operation_id="unknown")
        unknown_spec = dataclasses.replace(
            self.spec,
            assertions=tuple(unknown if item is proof else item for item in assertions),
        )
        with self.assertRaisesRegex(VerificationError, "unknown operation"):
            self.verify(unknown_spec)

    def test_smali_method_and_cardinality_and_descriptor_failures(self) -> None:
        path = self.decoded / "smali/sample/Worker.smali"
        path.write_text(
            path.read_text(encoding="utf-8")
            + ".method run()V\n return-void\n.end method\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(VerificationError, "resolved to 2"):
            self.verify()

        self.tearDown()
        self.setUp()
        self.write_decoded(
            "smali/duplicate.smali", ".class Lsample/Worker;\n.super Ljava/lang/Object;\n"
        )
        with self.assertRaisesRegex(VerificationError, "Duplicate smali descriptor"):
            self.verify()

        self.tearDown()
        self.setUp()
        path = self.decoded / "smali/sample/Worker.smali"
        path.write_text(".super Ljava/lang/Object;\n", encoding="utf-8")
        with self.assertRaisesRegex(VerificationError, "exactly one class"):
            self.verify()

    def test_assertion_mismatches_cover_sets_strings_archive_and_legacy_bytes(self) -> None:
        replacements = {
            "smali-count": dataclasses.replace(
                next(a for a in self.spec.assertions if a.assertion_id == "smali-count"),
                expected_count=2,
            ),
            "descriptor-set": DescriptorSetEquality(
                "descriptor-set",
                "descriptor_set_equality",
                "classes.dex",
                ("Lmissing/Class;",),
            ),
            "strings-present": DexStringsPresent(
                "strings-present", "dex_strings_present", "classes.dex", ("not-there",)
            ),
            "strings-absent": DexStringsAbsent(
                "strings-absent", "dex_strings_absent", "classes.dex", ("alpha",)
            ),
            "substrings-absent": DexStringSubstringsAbsent(
                "substrings-absent",
                "dex_string_substrings_absent",
                "classes.dex",
                ("need",),
            ),
            "entries-absent": ArchiveEntriesAbsent(
                "entries-absent", "archive_entries_absent", ("assets/common.bin",)
            ),
            "archive-preserved": ArchiveEntryNamesAndBytesPreservedExcept(
                "archive-preserved", "archive_preservation_except", ("classes.dex",)
            ),
            "bytes-present": BytesPresent(
                "bytes-present", "bytes_present", "assets/common.bin", "ffff"
            ),
            "bytes-absent": BytesAbsent(
                "bytes-absent", "bytes_absent", "assets/common.bin", "636f6d"
            ),
        }
        spec = dataclasses.replace(
            self.spec,
            assertions=tuple(replacements.get(item.assertion_id, item) for item in self.spec.assertions),
        )
        failed = {
            result.assertion_id for result in self.verify(spec).assertion_results if not result.passed
        }
        self.assertEqual(failed, set(replacements))

    def test_archive_preservation_detects_name_and_payload_differences_after_exclusions(self) -> None:
        write_zip(
            self.output,
            [
                ("classes.dex", self.dex),
                ("assets/common.bin", b"changed"),
                ("assets/extra.bin", b"extra"),
            ],
        )
        result = next(
            item
            for item in self.verify().assertion_results
            if item.assertion_id == "archive-preserved"
        )
        self.assertFalse(result.passed)
        self.assertIn("extra", result.detail)

    def test_dex_parser_rejects_bounds_uleb_and_non_string_raw_bytes(self) -> None:
        raw_only = minimal_dex(("prefixneedle",))
        write_zip(self.output, [("classes.dex", raw_only), ("assets/common.bin", b"common")])
        report = self.verify()
        self.assertFalse(
            next(r for r in report.assertion_results if r.assertion_id == "strings-present").passed
        )

        malformed = bytearray(minimal_dex(("alpha",)))
        struct.pack_into("<I", malformed, 112, len(malformed) + 1)
        write_zip(
            self.output,
            [("classes.dex", finalize_dex(malformed)), ("assets/common.bin", b"common")],
        )
        with self.assertRaisesRegex(VerificationError, "out of bounds"):
            self.verify()

        malformed = bytearray(minimal_dex(("alpha",)))
        string_offset = struct.unpack_from("<I", malformed, 112)[0]
        malformed[string_offset : string_offset + 5] = b"\x80\x80\x80\x80\x80"
        write_zip(
            self.output,
            [("classes.dex", finalize_dex(malformed)), ("assets/common.bin", b"common")],
        )
        with self.assertRaisesRegex(VerificationError, "ULEB128"):
            self.verify()

    def test_dex_structural_checksum_signature_map_and_version_validation(self) -> None:
        cases = []
        checksum = bytearray(minimal_dex(("alpha",)))
        checksum[8] ^= 1
        cases.append((checksum, "checksum"))

        signature = bytearray(minimal_dex(("alpha",)))
        signature[12] ^= 1
        cases.append((signature, "signature"))

        data_bounds = bytearray(minimal_dex(("alpha",)))
        struct.pack_into("<I", data_bounds, 104, struct.unpack_from("<I", data_bounds, 104)[0] - 1)
        cases.append((bytearray(finalize_dex(data_bounds)), "data section bounds"))

        duplicate_map = bytearray(minimal_dex(("alpha",)))
        map_offset = struct.unpack_from("<I", duplicate_map, 52)[0]
        struct.pack_into("<H", duplicate_map, map_offset + 16, 0x0000)
        cases.append((bytearray(finalize_dex(duplicate_map)), "map list"))

        unsupported = bytearray(minimal_dex(("alpha",)))
        unsupported[:8] = b"dex\n042\0"
        cases.append((unsupported, "unsupported"))

        for data, message in cases:
            with self.subTest(message=message):
                write_zip(
                    self.output,
                    [("classes.dex", bytes(data)), ("assets/common.bin", b"common")],
                )
                with self.assertRaisesRegex(VerificationError, message):
                    self.verify()

    def test_dex_modified_utf8_nul_surrogate_and_malformed_encodings(self) -> None:
        values = ("a\0b", "\ud800")
        write_zip(
            self.output,
            [("classes.dex", minimal_dex(values)), ("assets/common.bin", b"common")],
        )
        present = next(
            assertion for assertion in self.spec.assertions if assertion.assertion_id == "strings-present"
        )
        spec = dataclasses.replace(
            self.spec,
            assertions=tuple(
                dataclasses.replace(present, strings=values) if assertion is present else assertion
                for assertion in self.spec.assertions
            ),
        )
        self.assertTrue(
            next(
                result
                for result in self.verify(spec).assertion_results
                if result.assertion_id == "strings-present"
            ).passed
        )

        four_byte = bytearray(minimal_dex(("alpha",)))
        string_offset = struct.unpack_from("<I", four_byte, 112)[0]
        four_byte[string_offset + 1 : string_offset + 6] = b"\xf0\x90\x80\x80\0"
        write_zip(
            self.output,
            [("classes.dex", finalize_dex(four_byte)), ("assets/common.bin", b"common")],
        )
        with self.assertRaisesRegex(VerificationError, "lead byte"):
            self.verify()

        overlong = bytearray(minimal_dex(("alpha",)))
        string_offset = struct.unpack_from("<I", overlong, 112)[0]
        overlong[string_offset + 1 : string_offset + 4] = b"\xc0\x81\0"
        write_zip(
            self.output,
            [("classes.dex", finalize_dex(overlong)), ("assets/common.bin", b"common")],
        )
        with self.assertRaisesRegex(VerificationError, "Overlong"):
            self.verify()

        truncated = bytearray(minimal_dex(("alpha", "beta")))
        first_offset = struct.unpack_from("<I", truncated, 112)[0]
        second_offset = struct.unpack_from("<I", truncated, 116)[0]
        self.assertLess(first_offset, second_offset)
        truncated[second_offset - 1] = ord("x")
        write_zip(
            self.output,
            [("classes.dex", finalize_dex(truncated)), ("assets/common.bin", b"common")],
        )
        with self.assertRaisesRegex(VerificationError, "Unterminated"):
            self.verify()

    def test_backend_topology_and_graft_signatures_are_mandatory(self) -> None:
        write_zip(
            self.output,
            [
                ("classes.dex", self.dex),
                ("classes2.dex", minimal_dex(())),
                ("assets/common.bin", b"common"),
            ],
        )
        topology_report = self.verify()
        self.assertFalse(topology_report.passed)
        topology_result = next(
            result
            for result in topology_report.assertion_results
            if result.assertion_id == "backend.final-dex-entries"
        )
        self.assertFalse(topology_result.passed)
        self.assertIn("classes2.dex", topology_result.detail)

        write_zip(
            self.output,
            [
                ("classes.dex", self.dex),
                ("assets/common.bin", b"common"),
                ("meta-inf/CERT.RSA", b"signature"),
            ],
        )
        graft = StockDexGraftBackend(
            "stock_dex_graft", "fixture-graft", ("classes.dex",), (), ()
        )
        graft_report = self.verify(dataclasses.replace(self.spec, backend=graft))
        self.assertFalse(graft_report.passed)
        signature_result = graft_report.assertion_results[-1]
        self.assertEqual(signature_result.assertion_id, "backend.signature-policy")
        self.assertFalse(signature_result.passed)
        self.assertIn("CERT.RSA", signature_result.detail)
        full_without_signature_assertion = dataclasses.replace(
            self.spec,
            assertions=tuple(
                assertion
                for assertion in self.spec.assertions
                if assertion.assertion_id not in {"archive-preserved", "entries-absent"}
            ),
        )
        self.assertTrue(self.verify(full_without_signature_assertion).passed)

    def test_duplicate_and_corrupt_archives_are_rejected(self) -> None:
        with zipfile.ZipFile(self.output, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("classes.dex", self.dex)
            archive.writestr("classes.dex", self.dex)
        with self.assertRaisesRegex(VerificationError, "duplicate"):
            self.verify()

        self.output.write_bytes(b"not a zip")
        with self.assertRaisesRegex(VerificationError, "Could not read output archive"):
            self.verify()

    def test_path_types_symlinks_and_confinement_are_strict(self) -> None:
        arguments = (self.stock, self.output, self.decoded, self.source)
        for index in range(4):
            values = list(arguments)
            values[index] = str(values[index])
            with self.subTest(index=index), self.assertRaises(TypeError):
                verify_apk(self.spec, *values, self.receipt)

        output_link = self.root / "output-link.apk"
        output_link.symlink_to(self.output)
        with self.assertRaisesRegex(VerificationError, "symlink"):
            verify_apk(
                self.spec, self.stock, output_link, self.decoded, self.source, self.receipt
            )

        source_link = self.source / "bundle/data.bin"
        source_link.unlink()
        source_link.symlink_to(self.root / "outside.bin")
        with self.assertRaisesRegex(VerificationError, "Symlink"):
            self.verify()

    def test_stock_hash_binding_is_enforced(self) -> None:
        bad = dataclasses.replace(
            self.spec,
            target=dataclasses.replace(self.spec.target, apk_sha256="0" * 64),
        )
        with self.assertRaisesRegex(VerificationError, "target identity"):
            self.verify(bad)

    def test_source_has_no_target_specific_literals(self) -> None:
        source = (Path(__file__).parents[1] / "src/dfinsta_pipeline/verifier.py").read_text(
            encoding="utf-8"
        )
        for literal in ("340", "430", "LX/", "clips/", "classes20"):
            self.assertNotIn(literal, source)


if __name__ == "__main__":
    unittest.main()
