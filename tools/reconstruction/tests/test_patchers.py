import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from apply_anchored_patches import apply_operation as apply_anchored
from apply_endpoint_patches import apply_operation as apply_endpoint


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


if __name__ == "__main__":
    unittest.main()
