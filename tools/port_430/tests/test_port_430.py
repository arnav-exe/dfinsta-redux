import json
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
REPOSITORY = TOOLS.parents[1]
SOURCE = REPOSITORY / "dfinsta_source_430"
sys.path.insert(0, str(TOOLS))

from build import GRAFT_NAMES, graft_apk
from prepare_tree import prepare
from verify_apk import (
    FORBIDDEN_CUSTOM_SYMBOLS,
    REQUIRED_CUSTOM_SYMBOLS,
    expected_dex_names,
    payload_comparison,
    signature_context,
    verify,
    verify_structural_hooks,
)


class SourcePolicyTests(unittest.TestCase):
    def test_has_only_the_four_approved_custom_classes(self) -> None:
        smali_files = sorted((SOURCE / "newCode").rglob("*.smali"))
        descriptors = {
            re.search(
                r"^\.class .* (L[^;]+;)$",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            ).group(1)
            for path in smali_files
        }

        self.assertEqual(len(smali_files), 4)
        self.assertEqual(descriptors, set(REQUIRED_CUSTOM_SYMBOLS))

    def test_has_no_manifest_or_resource_payload(self) -> None:
        self.assertFalse(any(path.is_file() for path in (SOURCE / "newRes").rglob("*")))
        self.assertFalse(any(path.is_file() for path in (SOURCE / "manifest").rglob("*")))

    def test_custom_source_excludes_forbidden_dependencies_and_fixed_app_ids(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SOURCE / "newCode").rglob("*.smali"))
        )

        for symbol in FORBIDDEN_CUSTOM_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, content)
        self.assertNotIn("Ljava/lang/System;->exit", content)
        self.assertIsNone(re.search(r"\b0x7f[0-9a-f]+\b", content))

    def test_dialog_has_exact_labels_keys_defaults_and_apply(self) -> None:
        wrapper = (SOURCE / "newCode/com/dfinstagram/SettingsWrapper.smali").read_text(
            encoding="utf-8"
        )
        labels = re.findall(r'const-string v5, "(Disable [^"]+)"', wrapper)
        keys = re.findall(r'const-string v[05], "(disable_[^"]+)"', wrapper)

        self.assertEqual(
            labels,
            [
                "Disable feed",
                "Disable Explore",
                "Disable Reels",
                "Disable Stories",
                "Disable profile ads",
            ],
        )
        self.assertEqual(
            set(keys),
            {
                "disable_feed",
                "disable_explore",
                "disable_reels",
                "disable_stories",
                "disable_adds",
            },
        )
        self.assertEqual(wrapper.count("->getBoolean(Ljava/lang/String;Z)Z"), 5)
        self.assertEqual(wrapper.count("invoke-interface {v1, v5, v6}"), 5)
        self.assertIn("const/4 v6, 0x1", wrapper)
        self.assertEqual(wrapper.count("->putBoolean(Ljava/lang/String;Z)"), 1)
        self.assertEqual(wrapper.count("SharedPreferences$Editor;->apply()V"), 1)
        self.assertIn('const-string v1, "com.instagram"', wrapper)
        self.assertIn("Landroid/app/AlertDialog$Builder;", wrapper)
        self.assertIn("Landroid/content/DialogInterface$OnMultiChoiceClickListener;", wrapper)
        self.assertIn('const-string v0, "Close"', wrapper)
        self.assertIn("restart required", wrapper)

    def test_preference_reader_uses_true_default(self) -> None:
        reader = (SOURCE / "newCode/com/dfinstagram/dfinstagram.smali").read_text(
            encoding="utf-8"
        )
        self.assertIn('const-string v1, "com.instagram"', reader)
        self.assertRegex(
            reader,
            r"const/4 v1, 0x1\s+invoke-interface \{v0, p0, v1\}.*->getBoolean",
        )

    def test_reels_hook_uses_live_430_homecoming_path(self) -> None:
        hooks = (SOURCE / "newCode/com/dfinstagram/hooks.smali").read_text(
            encoding="utf-8"
        )

        self.assertIn('const-string v1, "/api/v1/clips/homecoming/"', hooks)
        self.assertNotIn('const-string v1, "/api/v1/clips/home/"', hooks)
        self.assertIn(
            "replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;", hooks
        )
        self.assertIn('const-string p0, ""', hooks)

    def test_profile_ads_replaces_retired_shopping_rule(self) -> None:
        hooks = (SOURCE / "newCode/com/dfinstagram/hooks.smali").read_text(
            encoding="utf-8"
        )

        self.assertIn('const-string v1, "/profile_ads/get_profile_ads/"', hooks)
        self.assertIn('const-string v1, "disable_adds"', hooks)
        self.assertNotIn("minishop", hooks)
        self.assertNotIn("disable_shopping", hooks)


class PrepareAndPatchTests(unittest.TestCase):
    def test_prepare_only_adds_classes20(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock"
            source = root / "source"
            output = root / "output"
            (stock / "res/raw").mkdir(parents=True)
            (stock / "AndroidManifest.xml").write_text("stock", encoding="utf-8")
            (stock / "res/raw/item").write_bytes(b"resource")
            (source / "newCode/pkg").mkdir(parents=True)
            (source / "newCode/pkg/Only.smali").write_text("class", encoding="utf-8")

            prepare(stock, source, output)

            self.assertEqual((output / "AndroidManifest.xml").read_bytes(), b"stock")
            self.assertEqual((output / "res/raw/item").read_bytes(), b"resource")
            self.assertEqual((output / "smali_classes20/pkg/Only.smali").read_bytes(), b"class")

    def test_has_six_exact_430_anchored_patches(self) -> None:
        manifest = json.loads(
            (SOURCE / "patches/anchored_patches.json").read_text(encoding="utf-8")
        )
        operations = {operation["id"]: operation for operation in manifest["operations"]}

        self.assertEqual(
            set(operations),
            {
                "set_app_context",
                "tigon_url_block",
                "install_settings_long_click",
                "replace_reels_discover_endpoint",
                "replace_reels_homecoming_endpoint",
                "replace_reels_stream_endpoint",
            },
        )
        self.assertEqual(
            {
                operations[operation_id]["marker"]
                for operation_id in (
                    "set_app_context",
                    "tigon_url_block",
                    "install_settings_long_click",
                )
            },
            {
                "Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V",
                "Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V",
                "Lcom/dfinstagram/SettingsWrapper;",
            },
        )
        self.assertEqual(
            operations["tigon_url_block"]["anchor"],
            [":try_start_0", "iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;"],
        )
        self.assertEqual(
            operations["install_settings_long_click"]["anchor"],
            [
                "new-instance v0, LX/0417;",
                "invoke-direct {v0, v3, p2, v6, p3}, LX/0417;-><init>(ILjava/lang/Object;Ljava/lang/Object;Ljava/lang/Object;)V",
                "invoke-static {v0, v6}, LX/00ZY;->A00(Landroid/view/View$OnClickListener;Landroid/view/View;)V",
            ],
        )
        payload = operations["install_settings_long_click"]["payload"]
        self.assertIn("    instance-of v0, p3, LX/077N;", payload)
        self.assertIn(
            "    invoke-virtual {v6, v0}, Landroid/view/View;->setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V",
            payload,
        )
        for operation_id, register, endpoint in (
            ("replace_reels_discover_endpoint", "v8", "clips/discover/"),
            ("replace_reels_homecoming_endpoint", "v9", "clips/homecoming/"),
            ("replace_reels_stream_endpoint", "v9", "clips/discover/stream/"),
        ):
            operation = operations[operation_id]
            self.assertEqual(operation["descriptor"], "LX/05t2;")
            self.assertEqual(operation["anchor"], [f'const-string {register}, "{endpoint}"'])
            self.assertIn(
                f"    invoke-static {{{register}}}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;",
                operation["payload"],
            )


class GraftTests(unittest.TestCase):
    @staticmethod
    def write_zip(path: Path, entries: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def test_replaces_only_graft_set_and_preserves_stock_entry_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock.apk"
            intermediate = root / "intermediate.apk"
            output = root / "output.apk"
            stock_entries = {
                "AndroidManifest.xml": b"manifest",
                "resources.arsc": b"resources",
                "res/raw/a": b"a",
                "classes.dex": b"stock-1",
                "classes2.dex": b"stock-2",
                "classes3.dex": b"stock-3",
                "classes4.dex": b"stock-4",
                "classes6.dex": b"stock-6",
                "META-INF/MANIFEST.MF": b"signature",
                "META-INF/CERT.RSA": b"signature",
                "META-INF/keep.txt": b"keep",
            }
            replacements = {name: f"new-{name}".encode("utf-8") for name in GRAFT_NAMES}
            self.write_zip(stock, stock_entries)
            self.write_zip(intermediate, replacements)

            graft_apk(stock, intermediate, output)

            with zipfile.ZipFile(output) as archive:
                final_entries = {name: archive.read(name) for name in archive.namelist()}
            self.assertEqual(
                set(final_entries),
                set(stock_entries) - {"META-INF/MANIFEST.MF", "META-INF/CERT.RSA"}
                | {"classes20.dex"},
            )
            for name, data in stock_entries.items():
                if name not in GRAFT_NAMES and not name.startswith("META-INF/MANIFEST") and not name.endswith(".RSA"):
                    self.assertEqual(final_entries[name], data)
            for name, data in replacements.items():
                self.assertEqual(final_entries[name], data)

    def test_refuses_existing_output_and_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock.apk"
            intermediate = root / "intermediate.apk"
            output = root / "output.apk"
            self.write_zip(stock, {name: b"stock" for name in GRAFT_NAMES - {"classes20.dex"}})
            self.write_zip(intermediate, {name: b"new" for name in GRAFT_NAMES})
            output.write_bytes(b"exists")
            with self.assertRaises(FileExistsError):
                graft_apk(stock, intermediate, output)

            output.unlink()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(stock, "a") as archive:
                    archive.writestr("classes.dex", b"duplicate")
            with self.assertRaises(ValueError):
                graft_apk(stock, intermediate, output)


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dex_names = expected_dex_names()
        self.dex_content = {name: b"stock" for name in self.dex_names}
        self.dex_content["classes20.dex"] = " ".join(REQUIRED_CUSTOM_SYMBOLS).encode("utf-8")
        self.structural_hooks = {"hook": True}
        self.stock_entries = {
            "AndroidManifest.xml": b"manifest",
            "resources.arsc": b"arsc",
            "res/a": b"resource",
            "assets/a": b"asset",
        }
        self.final_entries = dict(self.stock_entries)

    def verify(self) -> dict:
        return verify(
            self.dex_names,
            self.dex_content,
            self.final_entries,
            self.stock_entries,
            self.structural_hooks,
            *payload_comparison(self.final_entries, self.stock_entries),
        )

    def test_accepts_exact_dex_symbols_hooks_and_resources(self) -> None:
        self.assertTrue(self.verify()["passed"])

    def test_rejects_wrong_dex_set_or_extra_custom_class(self) -> None:
        self.dex_names.pop()
        self.assertFalse(self.verify()["passed"])
        self.dex_names = expected_dex_names()
        self.dex_content["classes20.dex"] += b" Lcom/dfinstagram/Extra;"
        self.assertFalse(self.verify()["passed"])

    def test_rejects_missing_hook_activity_or_resource_change(self) -> None:
        self.structural_hooks["hook"] = False
        self.assertFalse(self.verify()["passed"])
        self.structural_hooks["hook"] = True
        self.dex_content["classes20.dex"] += b" Landroid/app/Activity;"
        self.assertFalse(self.verify()["passed"])
        self.dex_content["classes20.dex"] = " ".join(REQUIRED_CUSTOM_SYMBOLS).encode("utf-8")
        self.final_entries["res/a"] = b"changed"
        self.assertFalse(self.verify()["passed"])

    def test_rejects_changed_retained_payload(self) -> None:
        self.final_entries["assets/a"] = b"changed"
        result = self.verify()
        self.assertFalse(result["retained_payload_entry_bytes_equal"])
        self.assertFalse(result["passed"])

    def test_structural_hooks_require_exact_methods_and_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "smali/com/instagram/api/tigon/TigonServiceLayer.smali": """
.method public startRequest()V
    invoke-static {v1}, Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V
.end method
""",
                "smali_classes3/com/instagram/app/InstagramAppShell.smali": """
.method public onCreate()V
    invoke-static {v0}, Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V
.end method
""",
                "smali_classes4/X/05t2.smali": """
.method public A07()V
    const-string v8, "clips/discover/"
    invoke-static {v8}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v8
.end method
.method public A09()V
    const-string v9, "clips/homecoming/"
    invoke-static {v9}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v9
    const-string v9, "clips/discover/stream/"
    invoke-static {v9}, Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v9
.end method
""",
                "smali_classes6/X/077K.smali": """
.method public A00()V
    invoke-static {v0, v6}, LX/00ZY;->A00(Landroid/view/View$OnClickListener;Landroid/view/View;)V
    instance-of v0, p3, LX/077N;
    if-eqz v0, :cond_0
    new-instance v0, Lcom/dfinstagram/SettingsWrapper;
    invoke-direct {v0}, Lcom/dfinstagram/SettingsWrapper;-><init>()V
    invoke-virtual {v6, v0}, Landroid/view/View;->setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V
.end method
""",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            checks = verify_structural_hooks(root)
            self.assertTrue(all(checks.values()))
            settings = root / "smali_classes6/X/077K.smali"
            settings.write_text(
                files["smali_classes6/X/077K.smali"].replace("LX/077N;", "LX/077e;"),
                encoding="utf-8",
            )
            self.assertFalse(verify_structural_hooks(root)["settings_guarded_after_stock_click"])

    @patch("verify_apk.subprocess.run")
    def test_records_signature_verification(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk = root / "app.apk"
            apksigner = root / "apksigner"
            apk.write_bytes(b"apk")
            apksigner.write_bytes(b"tool")
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, "Signer #1 certificate SHA-256 digest: abc\n", ""
            )
            result = signature_context(apk, apksigner)
            self.assertTrue(result["verified"])
            self.assertEqual(result["output"], ["Signer #1 certificate SHA-256 digest: abc"])


if __name__ == "__main__":
    unittest.main()
