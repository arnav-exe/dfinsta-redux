import copy
import hashlib
import inspect
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from dfinsta_pipeline.compiler import compile_port
from dfinsta_pipeline.contracts import canonical_sha256
from dfinsta_pipeline.port_contracts import (
    ArchiveEntriesAbsent,
    ArchiveEntryNamesAndBytesPreservedExcept,
    BytesAbsent,
    BytesPresent,
    DescriptorsPresent,
    DexStringSubstringsAbsent,
    DexStringsPresent,
    IntentSpecV2,
    OperationPostcondition,
    OverlayTree,
    ResolutionSpecV3,
    SmaliEdit,
)
from tools.phase_b.generate_specs import anchored_operations_340, resolve_classes


ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "pipeline_specs"
STOCK_340_DECODE = ROOT / "work" / "1.4.1-reconstruction" / "stock-340"
STOCK_340_APK = (
    ROOT
    / "apks"
    / "com.instagram.android_340.0.0.22.109-374010893_minAPI28(arm64-v8a)(nodpi).apk"
)
STOCK_430_DECODE = ROOT / "work" / "430-clean-build-v2" / "stock-430"
STOCK_430_APK = (
    ROOT
    / "apks"
    / "com.instagram.android_430.0.0.53.80-383611248_minAPI28(arm64-v8a)(360,400,420,480dpi).apk"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")

METHODS_430 = {
    "430.anchor.install_settings_long_click": (
        "LX/077K;",
        "A00(Landroid/content/Context;Lcom/instagram/common/session/UserSession;"
        "LX/077F;LX/0JxZ;)Landroid/widget/ImageView;",
    ),
    "430.anchor.replace_reels_discover_endpoint": (
        "LX/05t2;",
        "A07(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Lkotlin/jvm/functions/Function0;ZZZZ)LX/017H;",
    ),
    "430.anchor.replace_reels_homecoming_endpoint": (
        "LX/05t2;",
        "A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;"
        "ZZZZZZZZZZZ)LX/03xp;",
    ),
    "430.anchor.replace_reels_stream_endpoint": (
        "LX/05t2;",
        "A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;"
        "Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;"
        "Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;"
        "Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;"
        "ZZZZZZZZZZZ)LX/03xp;",
    ),
    "430.anchor.set_app_context": ("Lcom/instagram/app/InstagramAppShell;", "onCreate()V"),
    "430.anchor.tigon_url_block": (
        "Lcom/instagram/api/tigon/TigonServiceLayer;",
        "startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;",
    ),
}

ANCHORS_430 = {
    "430.anchor.install_settings_long_click": (
        "new-instance v0, LX/0417;",
        "invoke-direct {v0, v3, p2, v6, p3}, LX/0417;-><init>(ILjava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)V",
        "invoke-static {v0, v6}, LX/00ZY;->A00(Landroid/view/View$OnClickListener;Landroid/view/View;)V",
    ),
    "430.anchor.replace_reels_discover_endpoint": ('const-string v8, "clips/discover/"',),
    "430.anchor.replace_reels_homecoming_endpoint": ('const-string v9, "clips/homecoming/"',),
    "430.anchor.replace_reels_stream_endpoint": ('const-string v9, "clips/discover/stream/"',),
    "430.anchor.set_app_context": (
        "invoke-super {v0}, Landroid/app/Application;->onCreate()V",
    ),
    "430.anchor.tigon_url_block": (
        ":try_start_0",
        "iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;",
    ),
}


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


class PhaseBFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.intent = IntentSpecV2.from_dict(load(SPECS / "intent_v2.json"))
        cls.resolutions = {
            target: ResolutionSpecV3.from_dict(
                load(SPECS / "resolutions" / f"instagram_{target}.json")
            )
            for target in (340, 430)
        }
        cls.source_manifests = {
            target: load(SPECS / "source_manifests" / f"instagram_{target}.json")
            for target in (340, 430)
        }
        cls.compiled = {
            target: compile_port(cls.intent, resolution)
            for target, resolution in cls.resolutions.items()
        }

    def test_shared_intent_statuses_and_narrow_strategies(self) -> None:
        expected_ids = (
            "app-context",
            "block-explore",
            "block-feed",
            "block-profile-ads",
            "block-reels",
            "block-shopping",
            "block-stories",
            "cache-lifecycle",
            "privacy-hardening",
            "settings-entry",
            "welcome-flow",
        )
        self.assertEqual(tuple(hook.hook_id for hook in self.intent.hooks), expected_ids)
        hooks = {hook.hook_id: hook for hook in self.intent.hooks}
        for intent_id in (
            "app-context",
            "block-explore",
            "block-feed",
            "block-profile-ads",
            "block-reels",
            "block-shopping",
            "block-stories",
            "cache-lifecycle",
        ):
            self.assertEqual(hooks[intent_id].allowed_strategies, ("smali_edit",))
        self.assertEqual(
            hooks["privacy-hardening"].allowed_strategies,
            ("delete_path", "overlay_tree", "replace_resource_entry"),
        )
        for resolution in self.resolutions.values():
            self.assertEqual(resolution.intent_sha256, self.intent.sha256)
        self.assertTrue(
            all(status.status == "implemented" for status in self.resolutions[340].intent_statuses)
        )
        omitted = {
            status.intent_id
            for status in self.resolutions[430].intent_statuses
            if status.status == "omitted"
        }
        self.assertEqual(omitted, {"block-shopping", "cache-lifecycle", "welcome-flow"})

    def test_exact_target_identities_and_backend_topology(self) -> None:
        target_340 = self.resolutions[340].target
        self.assertEqual(
            (target_340.package_name, target_340.version_name, target_340.version_code),
            ("com.instagram.android", "340.0.0.22.109", 374010893),
        )
        self.assertEqual(
            target_340.apk_sha256,
            "68f4546f8cb597a668d6033916200ef99191a9006350fcd986fd33392aea5113",
        )
        self.assertEqual(
            self.resolutions[340].backend.final_dex_entries,
            ("classes.dex", *(f"classes{index}.dex" for index in range(2, 12))),
        )
        target_430 = self.resolutions[430].target
        self.assertEqual(
            (target_430.package_name, target_430.version_name, target_430.version_code),
            ("com.instagram.android", "430.0.0.53.80", 383611248),
        )
        self.assertEqual(
            target_430.apk_sha256,
            "38ae9861b9ca89f60f41767324e1c3d54a4e3a00ed5555b92660a08e6db14754",
        )
        backend = self.resolutions[430].backend
        self.assertEqual(backend.profile_id, "apktool-2.9.3-aapt1-api36")
        self.assertEqual(len(backend.stock_dex_entries), 19)
        self.assertEqual(
            backend.replace_dex_entries,
            ("classes.dex", "classes3.dex", "classes4.dex", "classes6.dex"),
        )
        self.assertEqual(backend.add_dex_entries, ("classes20.dex",))

    def test_smali_counts_order_and_endpoint_cardinality(self) -> None:
        edits_340 = [
            operation
            for operation in self.resolutions[340].operations
            if isinstance(operation, SmaliEdit)
        ]
        self.assertEqual(len(edits_340), 45)
        self.assertEqual(len([edit for edit in edits_340 if edit.operation_id.startswith("340.endpoint.")]), 38)
        endpoint_edits = edits_340[:38]
        self.assertTrue(all(edit.operation_id.startswith("340.endpoint.") for edit in endpoint_edits))
        self.assertTrue(all(edit.match_policy == "all" and edit.occurrence is None for edit in endpoint_edits))
        self.assertEqual(sum(edit.expected_precondition_count for edit in endpoint_edits), 39)
        self.assertTrue(all(edit.operation_id.startswith("340.anchor.") for edit in edits_340[38:]))
        ids = [operation.operation_id for operation in self.resolutions[340].operations]
        self.assertLess(
            ids.index("340.anchor.remove_stock_settings_long_click"),
            ids.index("340.anchor.install_dfinsta_settings_long_click"),
        )
        self.assertEqual(
            sum(isinstance(operation, SmaliEdit) for operation in self.resolutions[430].operations),
            6,
        )
        self.assertTrue(
            all(
                isinstance(operation, SmaliEdit)
                for operation in self.resolutions[430].operations[:6]
            )
        )

    def test_430_exact_ownership_payload_and_final_sequences(self) -> None:
        edits = {
            operation.operation_id: operation
            for operation in self.resolutions[430].operations
            if isinstance(operation, SmaliEdit)
        }
        self.assertEqual(set(edits), set(METHODS_430))
        for operation_id, (descriptor, method) in METHODS_430.items():
            edit = edits[operation_id]
            self.assertEqual((edit.descriptor, edit.method_signature), (descriptor, method))
            self.assertEqual(edit.precondition_sequence, ANCHORS_430[operation_id])
            self.assertTrue(edit.payload)
            if edit.mode == "insert_after":
                self.assertEqual(
                    edit.final_sequence,
                    (*edit.precondition_sequence, *edit.payload),
                    operation_id,
                )
                for anchor_line in edit.precondition_sequence:
                    self.assertNotIn(anchor_line, edit.payload, operation_id)
            elif edit.mode == "insert_before":
                self.assertEqual(
                    edit.final_sequence,
                    (*edit.payload, *edit.precondition_sequence),
                    operation_id,
                )
            else:
                self.assertEqual(edit.final_sequence, edit.payload, operation_id)

    def test_overlay_counts_and_narrow_intent_links(self) -> None:
        overlays_340 = {
            operation.target_prefix: operation
            for operation in self.resolutions[340].operations
            if isinstance(operation, OverlayTree)
        }
        overlays_430 = {
            operation.target_prefix: operation
            for operation in self.resolutions[430].operations
            if isinstance(operation, OverlayTree)
        }
        self.assertEqual(len(overlays_340["smali_classes11"].source_files), 9)
        self.assertEqual(overlays_340["smali_classes11"].intent_ids, ("privacy-hardening",))
        self.assertEqual(len(overlays_340["res"].source_files), 91)
        self.assertEqual(overlays_340["res"].intent_ids, ("settings-entry", "welcome-flow"))
        self.assertEqual(len(overlays_430["smali_classes20"].source_files), 4)
        self.assertEqual(overlays_430["smali_classes20"].intent_ids, ("privacy-hardening",))

    def test_source_manifests_are_strict_and_bind_resolutions(self) -> None:
        for target, manifest in self.source_manifests.items():
            self.assertIs(type(manifest), list)
            self.assertEqual(len(manifest), {340: 112, 430: 5}[target])
            paths = []
            for record in manifest:
                self.assertIs(type(record), dict)
                self.assertEqual(set(record), {"relative_path", "sha256"})
                path = record["relative_path"]
                digest = record["sha256"]
                self.assertIs(type(path), str)
                self.assertIs(type(digest), str)
                self.assertEqual(PurePosixPath(path).as_posix(), path)
                self.assertNotIn("..", PurePosixPath(path).parts)
                self.assertRegex(digest, SHA256)
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest)
                paths.append(path)
            self.assertEqual(paths, sorted(paths))
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(self.resolutions[target].source_bundle_sha256, canonical_sha256(manifest))

    def test_closed_string_and_signature_assertions(self) -> None:
        for resolution in self.resolutions.values():
            self.assertFalse(
                any(
                    isinstance(assertion, (BytesPresent, BytesAbsent))
                    for assertion in resolution.additional_assertions
                )
            )
            self.assertTrue(
                any(isinstance(assertion, DexStringsPresent) for assertion in resolution.additional_assertions)
            )
            self.assertTrue(
                any(
                    isinstance(assertion, DexStringSubstringsAbsent)
                    for assertion in resolution.additional_assertions
                )
            )
        self.assertTrue(
            any(
                isinstance(assertion, DescriptorsPresent)
                for assertion in self.resolutions[340].additional_assertions
            )
        )
        absent = next(
            assertion
            for assertion in self.resolutions[430].additional_assertions
            if isinstance(assertion, ArchiveEntriesAbsent)
        )
        preservation = next(
            assertion
            for assertion in self.resolutions[430].additional_assertions
            if isinstance(assertion, ArchiveEntryNamesAndBytesPreservedExcept)
        )
        self.assertTrue(absent.entries)
        self.assertTrue(all(entry.startswith("META-INF/") for entry in absent.entries))
        self.assertTrue(set(absent.entries) <= set(preservation.exclusions))

    def test_compiler_rejects_dex_string_assertion_outside_topology(self) -> None:
        data = copy.deepcopy(load(SPECS / "resolutions" / "instagram_430.json"))
        assertion = next(
            item
            for item in data["additional_assertions"]
            if item["kind"] == "dex_strings_present"
        )
        assertion["dex_entry"] = "classes99.dex"
        with self.assertRaisesRegex(ValueError, "outside backend topology"):
            compile_port(self.intent, ResolutionSpecV3.from_dict(data))

        substring_data = copy.deepcopy(load(SPECS / "resolutions" / "instagram_430.json"))
        assertion = next(
            item
            for item in substring_data["additional_assertions"]
            if item["kind"] == "dex_string_substrings_absent"
        )
        assertion["dex_entry"] = "classes99.dex"
        with self.assertRaisesRegex(ValueError, "outside backend topology"):
            compile_port(self.intent, ResolutionSpecV3.from_dict(substring_data))

    def test_operation_ids_and_compiler_postconditions_are_exhaustive(self) -> None:
        for target, resolution in self.resolutions.items():
            operation_ids = [operation.operation_id for operation in resolution.operations]
            self.assertEqual(len(operation_ids), len(set(operation_ids)), target)
            proofs = {
                assertion.operation_id
                for assertion in self.compiled[target].assertions
                if isinstance(assertion, OperationPostcondition)
            }
            self.assertEqual(proofs, set(operation_ids), target)

    def test_generator_anchor_cardinality_mutations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase-b-anchor-") as directory:
            path = Path(directory) / "Worker.smali"
            descriptor = "Lsample/Worker;"
            manifest = {
                "operations": [
                    {
                        "id": "mutated-anchor",
                        "descriptor": descriptor,
                        "anchor": ["const/4 v0, 0x0"],
                        "expected_anchor_count": 1,
                    }
                ]
            }
            bodies = (
                "    const/4 v0, 0x1\n",
                "    const/4 v0, 0x0\n    const/4 v0, 0x0\n",
            )
            for body in bodies:
                with self.subTest(body=body):
                    path.write_text(
                        ".class public Lsample/Worker;\n"
                        ".method public run()V\n"
                        f"{body}"
                        "    return-void\n"
                        ".end method\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "Anchor cardinality drift"):
                        anchored_operations_340(manifest, {descriptor: path})

    def test_generator_resolves_moved_descriptor_and_rejects_duplicate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase-b-descriptor-") as directory:
            decode = Path(directory)
            wanted = decode / "smali_classes3/X/QeB.smali"
            decoy = decode / "smali/X/QeB.2.smali"
            wanted.parent.mkdir(parents=True)
            decoy.parent.mkdir(parents=True)
            wanted.write_text(".class public LX/QeB;\n", encoding="utf-8")
            decoy.write_text(".class public LX/Qeb;\n", encoding="utf-8")

            self.assertEqual(resolve_classes(decode, {"LX/QeB;"}), {"LX/QeB;": wanted})

            duplicate = decode / "smali_classes4/X/QeB.3.smali"
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text(".class public LX/QeB;\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "resolved to 2 files"):
                resolve_classes(decode, {"LX/QeB;"})

    def test_generic_compiler_contains_no_target_literals(self) -> None:
        source = inspect.getsource(__import__("dfinsta_pipeline.compiler", fromlist=["*"]))
        for target_literal in ("340", "430", "LX/", "clips/", "classes20"):
            self.assertNotIn(target_literal, source)

    @unittest.skipUnless(
        STOCK_340_DECODE.is_dir()
        and STOCK_340_APK.is_file()
        and STOCK_430_DECODE.is_dir()
        and STOCK_430_APK.is_file(),
        "provisioned stock decodes/APK are unavailable",
    )
    def test_generator_output_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase-b-specs-") as directory:
            output = Path(directory) / "pipeline_specs"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "phase_b" / "generate_specs.py"),
                    "--repo-root",
                    str(ROOT),
                    "--output",
                    str(output),
                    "--stock-340-decode",
                    str(STOCK_340_DECODE),
                    "--stock-340-apk",
                    str(STOCK_340_APK),
                    "--stock-430-decode",
                    str(STOCK_430_DECODE),
                    "--stock-430-apk",
                    str(STOCK_430_APK),
                ],
                check=True,
                timeout=30,
            )
            for relative in (
                Path("intent_v2.json"),
                Path("resolutions/instagram_340.json"),
                Path("resolutions/instagram_430.json"),
                Path("source_manifests/instagram_340.json"),
                Path("source_manifests/instagram_430.json"),
            ):
                self.assertEqual((output / relative).read_bytes(), (SPECS / relative).read_bytes())


if __name__ == "__main__":
    unittest.main()
