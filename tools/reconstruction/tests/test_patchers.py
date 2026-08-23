import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from apply_anchored_patches import apply_operation as apply_anchored
from apply_endpoint_patches import apply_operation as apply_endpoint
import rebuild
from verify_apk import HARDENED_FORBIDDEN_SYMBOLS, REQUIRED_SYMBOLS, verify


REPOSITORY = TOOLS.parents[1]


class EndpointPatchTests(unittest.TestCase):
    def test_replaces_const_string_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Host.smali"
            path.write_text('    const-string/jumbo v2, "feed/timeline/"\n', encoding="utf-8")
            operation = {
                "literal": "feed/timeline/",
                "helper": "improveRemovePosts",
                "mode": "replace",
                "expected_count": 1,
            }

            self.assertEqual(apply_endpoint(path, operation), "applied")
            self.assertIn("improveRemovePosts", path.read_text(encoding="utf-8"))
            self.assertEqual(apply_endpoint(path, operation), "already_applied")

    def test_wraps_existing_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Host.smali"
            original = '    const-string p0, "com.bloks.www.minishops.ad.storefront"\n'
            path.write_text(original, encoding="utf-8")
            operation = {
                "literal": "com.bloks.www.minishops.ad.storefront",
                "helper": "improveRemoveShopping",
                "mode": "wrap",
                "expected_count": 1,
            }

            self.assertEqual(apply_endpoint(path, operation), "applied")
            content = path.read_text(encoding="utf-8")
            self.assertIn(original.strip(), content)
            self.assertIn("invoke-static {p0}", content)

    def test_rejects_anchor_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Host.smali"
            path.write_text('    const-string v0, "other"\n', encoding="utf-8")
            operation = {
                "literal": "feed/timeline/",
                "helper": "improveRemovePosts",
                "mode": "replace",
                "expected_count": 1,
            }

            with self.assertRaisesRegex(ValueError, "Anchor mismatch"):
                apply_endpoint(path, operation)


class AnchoredPatchTests(unittest.TestCase):
    def test_matches_across_debug_lines_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Host.smali"
            path.write_text(
                "    invoke-virtual {v0}, Lexample;->call()V\n"
                "\n"
                "    .line 42\n"
                "    move-result-object v1\n",
                encoding="utf-8",
            )
            operation = {
                "id": "test_insert",
                "mode": "insert_after",
                "anchor": [
                    "invoke-virtual {v0}, Lexample;->call()V",
                    "move-result-object v1",
                ],
                "expected_anchor_count": 1,
                "marker": "Lhook;->run()V",
                "expected_marker_count": 1,
                "payload": ["", "    invoke-static {}, Lhook;->run()V"],
            }

            self.assertEqual(apply_anchored(path, operation), "applied")
            self.assertEqual(apply_anchored(path, operation), "already_applied")

    def test_replaces_significant_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Host.smali"
            path.write_text(
                "    old-one\n\n    .line 7\n    old-two\n    keep\n", encoding="utf-8"
            )
            operation = {
                "id": "test_replace",
                "mode": "replace",
                "anchor": ["old-one", "old-two"],
                "expected_anchor_count": 1,
                "marker": "new-one",
                "expected_marker_count": 1,
                "payload": ["    new-one"],
            }

            self.assertEqual(apply_anchored(path, operation), "applied")
            self.assertEqual(path.read_text(encoding="utf-8"), "    new-one\n    keep\n")

    def test_hardened_manifest_only_sets_application_context_at_startup(self) -> None:
        path = REPOSITORY / "tests/fixtures/dfinsta_source_340" / "patches" / "anchored_patches.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        operations = {operation["id"]: operation for operation in manifest["operations"]}
        rendered = json.dumps(manifest)

        self.assertNotIn("app_crash_annotation", operations)
        self.assertNotIn("AmplitudeEventsSender", rendered)
        self.assertNotIn("com/acra", rendered)
        self.assertNotIn("ReportsCrashes", rendered)
        startup = operations["set_app_context"]
        invocation = (
            "    invoke-static {v9}, Lcom/dfinstagram/startapp;"
            "->setContext(Landroid/app/Application;)V"
        )
        self.assertEqual(
            startup["marker"],
            "Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V",
        )
        self.assertEqual(startup["expected_marker_count"], 1)
        self.assertEqual(startup["payload"], ["", invocation])


class ApkVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dex_names = ["classes.dex"] + [f"classes{index}.dex" for index in range(2, 12)]
        self.required_content = " ".join(REQUIRED_SYMBOLS).encode("utf-8")

    def test_default_verification_remains_compatible_with_oracle_privacy_symbols(self) -> None:
        content = self.required_content + b" AmplitudeEventsSender Lcom/acra/ACRA; ReportsCrashes"

        result = verify(self.dex_names, content)

        self.assertTrue(result["passed"])
        self.assertNotIn("hardened_forbidden_symbols_present", result)

    def test_hardened_verification_forbids_privacy_symbols(self) -> None:
        for symbol in HARDENED_FORBIDDEN_SYMBOLS:
            with self.subTest(symbol=symbol):
                result = verify(
                    self.dex_names,
                    self.required_content + b" " + symbol.encode("utf-8"),
                    hardened=True,
                )
                self.assertFalse(result["passed"])
                self.assertTrue(result["hardened_forbidden_symbols_present"][symbol])

    def test_hardened_verification_passes_without_privacy_symbols(self) -> None:
        result = verify(self.dex_names, self.required_content, hardened=True)

        self.assertTrue(result["passed"])
        self.assertFalse(any(result["hardened_forbidden_symbols_present"].values()))


class RebuildTests(unittest.TestCase):
    @patch("rebuild.run")
    def test_invokes_hardened_apk_verification(self, run_mock) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = [
                "rebuild.py",
                str(root / "stock"),
                str(root / "patch"),
                str(root / "apktool.jar"),
                "--work-tree",
                str(root / "work"),
                "--output-apk",
                str(root / "output.apk"),
            ]
            with patch.object(sys, "argv", arguments):
                rebuild.main()

        verify_command = run_mock.call_args_list[-1].args[0]
        self.assertIn("verify_apk.py", verify_command[1])
        self.assertIn("--hardened", verify_command)


if __name__ == "__main__":
    unittest.main()
