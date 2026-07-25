import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from runner import Adb, UiNode, accepted_startup_anchor_set, bounds_center, fatal_log_lines, parse_ui_xml


XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node text="Join Instagram" resource-id="title" class="android.widget.TextView"
        content-desc="" clickable="false" long-clickable="false" checked="false"
        bounds="[10,20][110,220]" />
  <node text="" resource-id="com.instagram.android:id/profile_tab"
        class="android.widget.FrameLayout" content-desc="Profile" clickable="true"
        long-clickable="true" checked="true" bounds="[900,2100][1080,2280]" />
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
        password = UiNode("Password", "", "", "", "[0,0][1,1]", False, False, False)
        join_only = UiNode("Join Instagram", "", "", "", "[0,0][1,1]", False, False, False)
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


class CommandConstructionTests(unittest.TestCase):
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
