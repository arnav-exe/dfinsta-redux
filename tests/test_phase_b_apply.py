import dataclasses
import copy
import hashlib
import inspect
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.apply import ApplyError, apply_port
from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import (
    AppendManifestComponents,
    AppendResourceEntries,
    DeletePath,
    ManifestComponent,
    OverlayTree,
    ReplaceResourceEntry,
    ResourceEntry,
    IntentSpecV2,
    ResolutionSpecV2,
    SmaliEdit,
    SourceFile,
)
from tests.test_phase_b_contracts import intent_data, resolution_340, resolution_430


@dataclasses.dataclass(frozen=True)
class Spec:
    operations: tuple[object, ...]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def smali_edit(
    operation_id: str = "edit",
    *,
    mode: str = "replace",
    policy: str = "all",
    occurrence: int | None = None,
    precondition: tuple[str, ...] = ("const/4 v0, 0x0",),
    pre_count: int = 1,
    payload: tuple[str, ...] = ("const/4 v0, 0x1",),
    final: tuple[str, ...] = ("const/4 v0, 0x1",),
    final_count: int = 1,
    descriptor: str = "Lsample/Worker;",
    method: str = "run()V",
) -> SmaliEdit:
    return SmaliEdit(
        operation_id,
        "smali_edit",
        ("intent",),
        descriptor,
        method,
        mode,
        policy,
        occurrence,
        precondition,
        pre_count,
        payload,
        final,
        final_count,
    )


class ApplyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="apply-test-")
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.source = self.root / "source"
        self.work.mkdir()
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str, *, source: bool = False) -> Path:
        path = (self.source if source else self.work) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_bytes(self, relative: str, data: bytes, *, source: bool = False) -> Path:
        path = (self.source if source else self.work) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def make_smali(self, body: str, *, relative: str = "smali/sample/Worker.smali") -> Path:
        return self.write(
            relative,
            ".class public Lsample/Worker;\n"
            ".super Ljava/lang/Object;\n\n"
            ".method public run()V\n"
            ".registers 2\n"
            f"{body}"
            "    return-void\n"
            ".end method\n",
        )

    def apply(self, *operations: object):
        return apply_port(Spec(tuple(operations)), self.work, self.source)  # type: ignore[arg-type]


class SmaliApplyTests(ApplyFixture):
    def test_case_distinct_filenames_are_selected_by_exact_descriptor(self) -> None:
        template = (
            ".class public {descriptor}\n"
            ".super Ljava/lang/Object;\n\n"
            ".method public run()V\n"
            ".registers 2\n"
            "    const/4 v0, 0x0\n"
            "    return-void\n"
            ".end method\n"
        )
        upper = self.write(
            "smali_classes10/X/QeB.smali", template.format(descriptor="LX/QeB;")
        )
        lower = self.write(
            "smali_classes10/X/Qeb.smali", template.format(descriptor="LX/Qeb;")
        )

        report = self.apply(
            smali_edit(
                "upper",
                descriptor="LX/QeB;",
                payload=("const/4 v0, 0x1",),
                final=("const/4 v0, 0x1",),
            ),
            smali_edit(
                "lower",
                descriptor="LX/Qeb;",
                payload=("const/4 v0, 0x2",),
                final=("const/4 v0, 0x2",),
            ),
        )

        self.assertEqual(
            tuple(result.status for result in report.results), ("applied", "applied")
        )
        self.assertIn("const/4 v0, 0x1", upper.read_text(encoding="utf-8"))
        self.assertIn("const/4 v0, 0x2", lower.read_text(encoding="utf-8"))

    def test_retained_anchor_replace_shapes_are_idempotent(self) -> None:
        cases = (
            (
                'const-string v2, "com.bloks.www.minishops.storefront.ig"',
                (
                    'const-string v2, "com.bloks.www.minishops.storefront.ig"',
                    "invoke-static {v2}, Lcom/dfinstagram/DistractionFree;->improveRemoveShopping(Ljava/lang/String;)Ljava/lang/String;",
                    "move-result-object v2",
                ),
            ),
            (
                'const-string v8, "clips/discover/"',
                (
                    ":dfinsta_reels_discover_endpoint",
                    'const-string v8, "clips/discover/"',
                    "invoke-static {v8}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;",
                    "move-result-object v8",
                ),
            ),
        )
        for precondition, final in cases:
            with self.subTest(final=final):
                self.make_smali(f"    {precondition}\n")
                operation = smali_edit(
                    precondition=(precondition,), payload=final, final=final
                )
                self.assertEqual(self.apply(operation).results[0].status, "applied")
                self.assertEqual(self.apply(operation).results[0].status, "already_applied")

    def test_insert_before_after_replace_and_removal_are_idempotent(self) -> None:
        cases = (
            (
                "insert_before",
                ("invoke-static {}, Lsample/Hook;->call()V",),
                (
                    "invoke-static {}, Lsample/Hook;->call()V",
                    "const/4 v0, 0x0",
                ),
                1,
            ),
            (
                "insert_after",
                ("invoke-static {}, Lsample/Hook;->call()V",),
                (
                    "const/4 v0, 0x0",
                    "invoke-static {}, Lsample/Hook;->call()V",
                ),
                1,
            ),
            ("replace", ("const/4 v0, 0x1",), ("const/4 v0, 0x1",), 1),
            ("replace", (), ("const/4 v0, 0x0",), 0),
        )
        for mode, payload, final, final_count in cases:
            with self.subTest(mode=mode, removal=not payload):
                self.make_smali("    const/4 v0, 0x0\n")
                operation = smali_edit(
                    mode=mode,
                    payload=payload,
                    final=final,
                    final_count=final_count,
                )
                self.assertEqual(self.apply(operation).results[0].status, "applied")
                self.assertEqual(self.apply(operation).results[0].status, "already_applied")
                text = (self.work / "smali/sample/Worker.smali").read_text(encoding="utf-8")
                if payload:
                    self.assertIn(f"    {payload[0]}" if not payload[0][0].isspace() else payload[0], text)

    def test_all_matching_ignores_lines_blanks_and_comments(self) -> None:
        self.make_smali(
            "    const/4 v0, 0x0\n"
            "    .line 7\n"
            "\n"
            "    # generated location\n"
            "    move v1, v0\n"
            "    const/4 v0, 0x0\n"
            "    .line 8\n"
            "    move v1, v0\n"
        )
        operation = smali_edit(
            policy="all",
            precondition=("const/4 v0, 0x0", "move v1, v0"),
            pre_count=2,
            payload=("const/4 v0, 0x1", "move v1, v0"),
            final=("const/4 v0, 0x1", "move v1, v0"),
            final_count=2,
        )
        self.assertEqual(self.apply(operation).results[0].status, "applied")

    def test_occurrence_checks_total_and_leaves_other_register_separated_match(self) -> None:
        path = self.make_smali(
            "    const/4 v0, 0x0\n"
            "    move v1, v0\n"
            "    const/4 v0, 0x0\n"
        )
        operation = smali_edit(
            policy="occurrence",
            occurrence=1,
            pre_count=2,
            payload=("const/4 v0, 0x2",),
            final=("const/4 v0, 0x2",),
        )
        self.assertEqual(self.apply(operation).results[0].status, "applied")
        self.assertLess(path.read_text(encoding="utf-8").index("0x0"), path.read_text(encoding="utf-8").index("0x2"))
        self.assertEqual(self.apply(operation).results[0].status, "already_applied")

    def test_matching_is_scoped_to_exact_method(self) -> None:
        path = self.make_smali("    const/4 v0, 0x0\n")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n.method public other()V\n"
                "    const/4 v0, 0x0\n"
                "    return-void\n"
                ".end method\n"
            )
        self.apply(smali_edit())
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("0x1"), 1)
        self.assertEqual(text.count("0x0"), 1)

    def test_duplicate_descriptor_method_cardinality_and_partial_states_fail(self) -> None:
        self.make_smali("    const/4 v0, 0x0\n")
        self.write(
            "smali_more/duplicate.smali",
            ".class Lsample/Worker;\n.super Ljava/lang/Object;\n",
        )
        with self.assertRaisesRegex(ApplyError, "Duplicate smali descriptor"):
            self.apply(smali_edit())

        (self.work / "smali_more/duplicate.smali").unlink()
        path = self.work / "smali/sample/Worker.smali"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n.method run()V\n    return-void\n.end method\n")
        with self.assertRaisesRegex(ApplyError, "resolved to 2"):
            self.apply(smali_edit())

        self.make_smali("    const/4 v0, 0x0\n    const/4 v0, 0x0\n")
        with self.assertRaisesRegex(ApplyError, "cardinality"):
            self.apply(smali_edit())

        self.make_smali("    const/4 v0, 0x1\n    const/4 v0, 0x1\n")
        with self.assertRaisesRegex(ApplyError, "Partial final"):
            self.apply(smali_edit())

    def test_existing_final_rejects_unexpected_extra_precondition(self) -> None:
        self.make_smali(
            "    const/4 v0, 0x1\n"
            "    const/4 v0, 0x0\n"
        )
        with self.assertRaisesRegex(ApplyError, "unexpected preconditions"):
            self.apply(smali_edit())

    def test_crlf_is_preserved_and_insignificant_contract_sequences_fail(self) -> None:
        path = self.work / "smali/sample/Worker.smali"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b".class Lsample/Worker;\r\n.super Ljava/lang/Object;\r\n"
            b".method run()V\r\n    const/4 v0, 0x0\r\n.end method\r\n"
        )
        self.apply(smali_edit())
        data = path.read_bytes()
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

        for sequence in (("# comment",), (".line 7",)):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(
                ValueError, "no significant lines"
            ):
                smali_edit(precondition=sequence)


class TreeAndXmlApplyTests(ApplyFixture):
    def overlay(self, files: tuple[tuple[str, bytes], ...], policy: str = "forbid") -> OverlayTree:
        records = []
        for relative, data in files:
            self.write_bytes(f"bundle/{relative}", data, source=True)
            records.append(SourceFile(relative, digest(data)))
        source_files = tuple(records)
        return OverlayTree(
            "overlay",
            "overlay_tree",
            ("intent",),
            "bundle",
            "smali_extra",
            source_files,
            canonical_sha256(source_files),
            policy,
        )

    def test_overlay_fresh_exact_hash_collision_partial_and_require_exact(self) -> None:
        operation = self.overlay((("a.bin", b"a"), ("b.bin", b"b")))
        self.assertEqual(self.apply(operation).results[0].status, "applied")
        self.assertEqual(self.apply(operation).results[0].status, "already_applied")

        (self.work / "smali_extra/a.bin").write_bytes(b"wrong")
        with self.assertRaisesRegex(ApplyError, "partial or collision"):
            self.apply(operation)

        (self.work / "smali_extra").rename(self.work / "old")
        (self.work / "smali_extra").mkdir()
        (self.work / "smali_extra/a.bin").write_bytes(b"a")
        with self.assertRaisesRegex(ApplyError, "partial or collision"):
            self.apply(operation)

        exact = self.overlay((("c.bin", b"c"),), "require_exact")
        with self.assertRaisesRegex(ApplyError, "require_exact"):
            self.apply(exact)
        self.write_bytes("smali_extra/c.bin", b"c")
        self.assertEqual(self.apply(exact).results[0].status, "already_applied")

        source_file = self.source / "bundle/c.bin"
        source_file.write_bytes(b"changed")
        with self.assertRaisesRegex(ApplyError, "hash mismatch"):
            self.apply(exact)

    def test_overlay_rejects_source_and_destination_symlinks(self) -> None:
        operation = self.overlay((("a.bin", b"a"),))
        source_file = self.source / "bundle/a.bin"
        source_file.unlink()
        source_file.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ApplyError, "Symlink"):
            self.apply(operation)

        source_file.unlink()
        source_file.write_bytes(b"a")
        (self.work / "smali_extra").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ApplyError, "Symlink"):
            self.apply(operation)

    def test_overlay_rejects_existing_and_cross_overlay_smali_descriptors(self) -> None:
        self.make_smali("    const/4 v0, 0x0\n")
        duplicate = b".class Lsample/Worker;\n.super Ljava/lang/Object;\n"
        operation = self.overlay((("duplicate.smali", duplicate),))
        with self.assertRaisesRegex(ApplyError, "descriptor already exists"):
            self.apply(operation)

        first_data = b".class Lsample/Added;\n.super Ljava/lang/Object;\n"
        first = self.overlay((("first.smali", first_data),))
        second_path = self.write_bytes("second/same.smali", first_data, source=True)
        second_files = (SourceFile("same.smali", digest(first_data)),)
        second = OverlayTree(
            "second-overlay", "overlay_tree", ("intent",), "second", "smali_other",
            second_files, canonical_sha256(second_files), "forbid",
        )
        self.assertTrue(second_path.is_file())
        with self.assertRaisesRegex(ApplyError, "descriptor already exists"):
            self.apply(first, second)

    def test_resources_append_replace_before_after_mixed_and_duplicates(self) -> None:
        path = self.write(
            "res/values/items.xml",
            "<resources><string name=\"old\">Before</string><string name=\"tail\">Tail</string></resources>",
        )
        append = AppendResourceEntries(
            "append",
            "append_resource_entries",
            ("intent",),
            "res/values/items.xml",
            (
                ResourceEntry("string", "alpha", '<string name="alpha">A</string>'),
                ResourceEntry("string", "beta", '<string name="beta">B</string>'),
            ),
        )
        replace = ReplaceResourceEntry(
            "replace",
            "replace_resource_entry",
            ("intent",),
            "res/values/items.xml",
            ResourceEntry("string", "old", '<string name="old">Before</string>'),
            ResourceEntry("string", "old", '<string name="old">After</string>'),
        )
        self.assertEqual(self.apply(append, replace).results[0].status, "applied")
        self.assertEqual(tuple(result.status for result in self.apply(append, replace).results), ("already_applied", "already_applied"))
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index('name="old"'), text.index('name="tail"'))

        path.write_text(
            '<resources><string name="alpha">A</string><string name="old">After</string></resources>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ApplyError, "partial"):
            self.apply(append)
        path.write_text(
            '<resources><string name="alpha">A</string><string name="alpha">A</string></resources>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ApplyError, "Duplicate resource"):
            self.apply(append)

    def test_resource_declared_identity_and_semantic_noop_fail_atomically(self) -> None:
        path = self.write(
            "res/values/items.xml", '<resources><string name="old">Before</string></resources>'
        )
        original = path.read_bytes()
        bad_append = AppendResourceEntries(
            "bad-append", "append_resource_entries", ("intent",), "res/values/items.xml",
            (ResourceEntry("string", "declared", '<string name="other">Value</string>'),),
        )
        with self.assertRaisesRegex(ApplyError, "identity mismatch"):
            self.apply(bad_append)
        self.assertEqual(path.read_bytes(), original)

        bad_replace = ReplaceResourceEntry(
            "bad-replace", "replace_resource_entry", ("intent",), "res/values/items.xml",
            ResourceEntry("string", "old", '<string name="other">Before</string>'),
            ResourceEntry("string", "old", '<string name="old">After</string>'),
        )
        with self.assertRaisesRegex(ApplyError, "identity mismatch"):
            self.apply(bad_replace)
        self.assertEqual(path.read_bytes(), original)

        noop = ReplaceResourceEntry(
            "noop", "replace_resource_entry", ("intent",), "res/values/items.xml",
            ResourceEntry("string", "old", '<string name="old">Before</string>'),
            ResourceEntry("string", "old", '<string name="old">Before</string>'),
        )
        with self.assertRaisesRegex(ApplyError, "semantic no-op"):
            self.apply(noop)
        self.assertEqual(path.read_bytes(), original)

    def test_manifest_identity_exactness_and_partial_state(self) -> None:
        path = self.write(
            "AndroidManifest.xml",
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application /></manifest>',
        )
        operation = AppendManifestComponents(
            "manifest",
            "append_manifest_components",
            ("intent",),
            "AndroidManifest.xml",
            (
                ManifestComponent("activity", "sample.One", '<activity android:name="sample.One" />'),
                ManifestComponent("service", "sample.Two", '<service android:name="sample.Two" />'),
            ),
        )
        self.assertEqual(self.apply(operation).results[0].status, "applied")
        self.assertEqual(self.apply(operation).results[0].status, "already_applied")
        self.assertIn("xmlns:android", path.read_text(encoding="utf-8"))
        path.write_text(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application><activity android:name="sample.One" /></application></manifest>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ApplyError, "partial"):
            self.apply(operation)

    def test_xml_comments_namespaces_and_namespaced_application_are_preserved(self) -> None:
        resource = self.write(
            "res/values/namespaced.xml",
            '<resources xmlns:tools="urn:test"><!--keep--><string name="old" tools:flag="yes">Old</string></resources>',
        )
        append = AppendResourceEntries(
            "append-ns", "append_resource_entries", ("intent",), "res/values/namespaced.xml",
            (ResourceEntry("string", "new", '<string name="new">New</string>'),),
        )
        self.apply(append)
        resource_text = resource.read_text(encoding="utf-8")
        self.assertIn("<!--keep-->", resource_text)
        self.assertIn('xmlns:tools="urn:test"', resource_text)

        manifest = self.write(
            "NamespacedManifest.xml",
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android" xmlns:m="urn:manifest"><!--keep--><m:application /></manifest>',
        )
        operation = AppendManifestComponents(
            "manifest-ns", "append_manifest_components", ("intent",), "NamespacedManifest.xml",
            (ManifestComponent("activity", "sample.One", '<activity android:name="sample.One" />'),),
        )
        self.apply(operation)
        manifest_text = manifest.read_text(encoding="utf-8")
        self.assertIn("<!--keep-->", manifest_text)
        self.assertIn('xmlns:m="urn:manifest"', manifest_text)

    def test_delete_file_directory_absence_and_expected_absent(self) -> None:
        self.write_bytes("assets/file.bin", b"x")
        delete_file = DeletePath("delete-file", "delete_path", ("intent",), "assets/file.bin", True)
        self.assertEqual(self.apply(delete_file).results[0].status, "applied")
        self.assertEqual(self.apply(delete_file).results[0].status, "already_applied")
        self.write("cache/nested/item", "x")
        delete_dir = DeletePath("delete-dir", "delete_path", ("intent",), "cache", True)
        with self.assertRaisesRegex(ApplyError, "no content manifest"):
            self.apply(delete_dir)
        self.assertTrue((self.work / "cache/nested/item").is_file())
        self.write_bytes("assets/unexpected.bin", b"x")
        unexpected = DeletePath(
            "unexpected", "delete_path", ("intent",), "assets/unexpected.bin", False
        )
        with self.assertRaisesRegex(ApplyError, "Unexpected"):
            self.apply(unexpected)


class ApplyPortTests(ApplyFixture):
    def test_all_six_variants_report_in_spec_order_and_second_run_is_idempotent(self) -> None:
        self.make_smali("    const/4 v0, 0x0\n")
        overlay_data = b".class Lsample/Added;\n"
        self.write_bytes("bundle/Added.smali", overlay_data, source=True)
        source_files = (SourceFile("Added.smali", digest(overlay_data)),)
        self.write(
            "res/values/items.xml",
            '<resources><string name="old">Before</string></resources>',
        )
        self.write(
            "AndroidManifest.xml",
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application /></manifest>',
        )
        self.write_bytes("assets/remove.bin", b"remove")
        operations = (
            smali_edit("one"),
            OverlayTree(
                "two", "overlay_tree", ("intent",), "bundle", "smali_extra", source_files,
                canonical_sha256(source_files), "forbid",
            ),
            AppendResourceEntries(
                "three", "append_resource_entries", ("intent",), "res/values/items.xml",
                (ResourceEntry("string", "added", '<string name="added">Added</string>'),),
            ),
            ReplaceResourceEntry(
                "four", "replace_resource_entry", ("intent",), "res/values/items.xml",
                ResourceEntry("string", "old", '<string name="old">Before</string>'),
                ResourceEntry("string", "old", '<string name="old">After</string>'),
            ),
            AppendManifestComponents(
                "five", "append_manifest_components", ("intent",), "AndroidManifest.xml",
                (ManifestComponent("activity", "sample.Settings", '<activity android:name="sample.Settings" />'),),
            ),
            DeletePath("six", "delete_path", ("intent",), "assets/remove.bin", True),
        )
        first = self.apply(*operations)
        second = self.apply(*operations)
        self.assertTrue(dataclasses.is_dataclass(first))
        self.assertEqual(tuple(result.operation_id for result in first.results), tuple(operation.operation_id for operation in operations))
        self.assertEqual({result.status for result in first.results}, {"applied"})
        self.assertEqual({result.status for result in second.results}, {"already_applied"})
        self.assertEqual(first.sha256, canonical_sha256(first))

    def test_roots_and_destination_paths_are_confined(self) -> None:
        missing = self.root / "missing"
        with self.assertRaisesRegex(ApplyError, "existing"):
            apply_port(Spec(()), missing, self.source)  # type: ignore[arg-type]
        linked_root = self.root / "linked"
        linked_root.symlink_to(self.work)
        with self.assertRaisesRegex(ApplyError, "non-symlink"):
            apply_port(Spec(()), linked_root, self.source)  # type: ignore[arg-type]

        outside = self.root / "outside"
        outside.mkdir()
        (self.work / "assets").symlink_to(outside)
        operation = DeletePath("delete", "delete_path", ("intent",), "assets/item", True)
        with self.assertRaisesRegex(ApplyError, "Symlink"):
            self.apply(operation)

    def test_interpreter_source_contains_no_target_constants(self) -> None:
        import dfinsta_pipeline.apply as module

        source = inspect.getsource(module)
        forbidden = ("3" + "40", "4" + "30", "L" + "X/", "clips" + "/", "classes" + "20")
        for literal in forbidden:
            self.assertNotIn(literal, source)

    def test_compiler_rejects_smali_after_overlay_and_casefolded_aliases(self) -> None:
        intent = IntentSpecV2.from_dict(intent_data())
        reordered = resolution_430()
        reordered["operations"] = list(reversed(reordered["operations"]))
        with self.assertRaisesRegex(ValueError, "Smali edits must precede"):
            compile_port(intent, ResolutionSpecV2.from_dict(reordered))

        aliased = resolution_340()
        duplicate = copy.deepcopy(aliased["operations"][1])
        duplicate["operation_id"] = "case-alias"
        duplicate["archive_path"] = duplicate["archive_path"].upper()
        aliased["operations"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "same destination"):
            compile_port(intent, ResolutionSpecV2.from_dict(aliased))


if __name__ == "__main__":
    unittest.main()
