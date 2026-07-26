import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from runner import (
    Adb,
    UiNode,
    accepted_startup_anchor_set,
    bounds_center,
    evaluate_text_assertions,
    fatal_log_lines,
    find_selector_nodes,
    foreground_state_valid,
    parse_ui_xml,
    physical_display_size,
    resumed_activity,
    selector_criteria,
    sha256_file,
    startup_intent_arguments,
)


XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="Join Instagram" resource-id="title" class="android.widget.TextView"
        content-desc="" clickable="false" long-clickable="false" checked="false"
        bounds="[10,20][110,220]" />
  <node text="" resource-id="com.instagram.android:id/profile_tab"
         class="android.widget.FrameLayout" content-desc="Profile" clickable="true"
         long-clickable="true" checked="true" selected="true" bounds="[900,2100][1080,2280]" />
  <node text="I already have a profile" resource-id="login" class="android.widget.Button"
        content-desc="" clickable="true" long-clickable="false" checked="false"
        bounds="[20,300][500,400]" />
</hierarchy>
"""


class XmlParsingTests(unittest.TestCase):
    def test_parses_semantic_attributes(self) -> None:
        nodes = parse_ui_xml(XML)
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[1].resource_id, "com.instagram.android:id/profile_tab")
        self.assertEqual(nodes[1].content_desc, "Profile")
        self.assertTrue(nodes[1].clickable)
        self.assertTrue(nodes[1].long_clickable)
        self.assertTrue(nodes[1].checked)
        self.assertTrue(nodes[1].selected)

    def test_rejects_malformed_xml(self) -> None:
        with self.assertRaises(Exception):
            parse_ui_xml("<hierarchy><node></hierarchy>")


class BoundsTests(unittest.TestCase):
    def test_returns_integer_center(self) -> None:
        self.assertEqual(bounds_center("[10,20][111,221]"), (60, 120))

    def test_rejects_invalid_or_inverted_bounds(self) -> None:
        for bounds in ("10,20,30,40", "[20,20][10,40]"):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                bounds_center(bounds)


class StartupAnchorTests(unittest.TestCase):
    def test_accepts_first_complete_anchor_set(self) -> None:
        anchors = [["Join Instagram", "I already have a profile"], ["Password"]]
        self.assertEqual(accepted_startup_anchor_set(parse_ui_xml(XML), anchors), anchors[0])

    def test_accepts_historical_password_set_and_rejects_partial_set(self) -> None:
        password = UiNode("Password", "", "", "", "[0,0][1,1]", False, False, False, False)
        join_only = UiNode("Join Instagram", "", "", "", "[0,0][1,1]", False, False, False, False)
        anchors = [["Join Instagram", "I already have a profile"], ["Password"]]
        self.assertEqual(accepted_startup_anchor_set([password], anchors), ["Password"])
        self.assertIsNone(accepted_startup_anchor_set([join_only], anchors))


class FatalLogTests(unittest.TestCase):
    def test_only_returns_actual_fatal_markers(self) -> None:
        logcat = "\n".join(
            [
                "I/AndroidRuntime: VM running normally",
                "E/AndroidRuntime: FATAL EXCEPTION: main",
                "E/ACRA: ACRA caught a RuntimeException",
            ]
        )
        self.assertEqual(
            fatal_log_lines(logcat),
            ["E/AndroidRuntime: FATAL EXCEPTION: main", "E/ACRA: ACRA caught a RuntimeException"],
        )


class SelectorTests(unittest.TestCase):
    def test_matches_resource_id_and_content_desc_conjunctively(self) -> None:
        nodes = parse_ui_xml(XML)
        selector = {
            "resource_id": "com.instagram.android:id/profile_tab",
            "content_desc": "Profile",
        }
        self.assertEqual(find_selector_nodes(nodes, selector), [nodes[1]])
        self.assertEqual(find_selector_nodes(nodes, {**selector, "content_desc": "Home"}), [])

    def test_rejects_empty_or_unsupported_selectors(self) -> None:
        for selector in ({}, {"class_name": "android.view.View"}, {"resource_id": ""}):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                selector_criteria(selector)


class TextAssertionTests(unittest.TestCase):
    def test_evaluates_visible_and_absent_anchors(self) -> None:
        assertions = [
            {"name": "shell", "kind": "visible_text", "anchors": ["Join Instagram"], "severity": "required"},
            {
                "name": "feed_content",
                "kind": "absent_text",
                "anchors": ["Suggested for you", "Follow"],
                "match": "all",
                "severity": "evidence",
            },
        ]
        results = evaluate_text_assertions(parse_ui_xml(XML), assertions)
        self.assertTrue(results[0]["passed"])
        self.assertTrue(results[1]["passed"])
        self.assertEqual(results[1]["anchor_visible"], {"Suggested for you": False, "Follow": False})

    def test_any_visible_and_any_absent_semantics(self) -> None:
        assertions = [
            {"kind": "visible_text", "anchors": ["missing", "Password"], "match": "any"},
            {"kind": "absent_text", "anchors": ["Join Instagram", "missing"], "match": "any"},
        ]
        password = UiNode("Password", "", "", "", "[0,0][1,1]", False, False, False, False)
        results = evaluate_text_assertions([*parse_ui_xml(XML), password], assertions)
        self.assertTrue(results[0]["passed"])
        self.assertTrue(results[1]["passed"])

    def test_rejects_invalid_assertion_contract(self) -> None:
        invalid = {"kind": "contains_text", "anchors": ["Home"]}
        with self.assertRaises(ValueError):
            evaluate_text_assertions(parse_ui_xml(XML), [invalid])


class CommandConstructionTests(unittest.TestCase):
    def test_hashes_provenance_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.apk"
            path.write_bytes(b"artifact")
            self.assertEqual(
                sha256_file(path),
                "c7c5c1d70c5dec4416ab6158afd0b223ef40c29b1dc1f97ed9428b94d4cadb1c",
            )

    def test_builds_contract_launcher_intent(self) -> None:
        self.assertEqual(
            startup_intent_arguments(
                {
                    "component": "com.example/.Launcher",
                    "action": "android.intent.action.MAIN",
                    "categories": ["android.intent.category.LAUNCHER"],
                }
            ),
            [
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-n",
                "com.example/.Launcher",
            ],
        )

    def test_parses_top_resumed_activity(self) -> None:
        self.assertEqual(
            resumed_activity(
                "topResumedActivity=ActivityRecord{abc u0 com.example/.MainActivity t42}"
            ),
            "com.example/.MainActivity",
        )
        self.assertIsNone(resumed_activity("ResumedActivity: null"))

    def test_parses_physical_display_size(self) -> None:
        self.assertEqual(physical_display_size("Physical size: 1080x2400\n"), (1080, 2400))
        with self.assertRaises(ValueError):
            physical_display_size("Override size: 720x1600\n")

    def test_modal_foreground_requires_onboarding_anchors(self) -> None:
        config = {
            "foreground_states": [
                {"activity": "com.example/.Main"},
                {
                    "activity": "com.example/.Modal",
                    "requires_logged_out_anchor_set": True,
                },
            ]
        }
        self.assertTrue(foreground_state_valid(config, "com.example/.Main", None))
        self.assertFalse(foreground_state_valid(config, "com.example/.Modal", None))
        self.assertTrue(
            foreground_state_valid(config, "com.example/.Modal", ["Join", "Sign in"])
        )
        self.assertFalse(foreground_state_valid(config, "com.other/.Main", None))

    def test_builds_serialized_adb_command(self) -> None:
        adb = Adb("C:/Android/platform-tools/adb.exe", "device-123")
        self.assertEqual(
            adb.command("shell", "pidof", "com.instagram.android"),
            [
                "C:/Android/platform-tools/adb.exe",
                "-s",
                "device-123",
                "shell",
                "pidof",
                "com.instagram.android",
            ],
        )

    @patch("runner.subprocess.run")
    def test_run_uses_argument_list_and_utf8(self, run_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess([], 0, "device\n", "")
        result = Adb("adb").run("get-state", timeout=7)
        self.assertEqual(result.stdout, "device\n")
        run_mock.assert_called_once_with(
            ["adb", "get-state"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=7,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
