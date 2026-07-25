import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
REPOSITORY = TOOLS.parents[1]
SOURCE = REPOSITORY / "dfinsta_source_430"
sys.path.insert(0, str(TOOLS))

from verify_apk import FORBIDDEN_CUSTOM_SYMBOLS, REQUIRED_CUSTOM_SYMBOLS, verify


ANDROID_NS = "http://schemas.android.com/apk/res/android"


class SourcePolicyTests(unittest.TestCase):
    def test_has_only_the_six_approved_custom_classes(self) -> None:
        smali_files = sorted((SOURCE / "newCode").rglob("*.smali"))
        descriptors = {
            re.search(r"^\.class .* (L[^;]+;)$", path.read_text(encoding="utf-8"), re.MULTILINE).group(1)
            for path in smali_files
        }

        self.assertEqual(len(smali_files), 6)
        self.assertEqual(
            descriptors,
            {
                "Lcom/dfinstagram/startapp;",
                "Lcom/dfinstagram/dfinstagram;",
                "Lcom/dfinstagram/hooks;",
                "Lcom/dfinstagram/SettingsWrapper;",
                "Lcom/dfinstagram/preference/Preference;",
                "Lcom/dfinstagram/preference/PreferenceFragment;",
            },
        )

    def test_custom_source_excludes_forbidden_dependencies_and_fixed_app_ids(self) -> None:
        content = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((SOURCE / "newCode").rglob("*.smali"))
        )

        for symbol in FORBIDDEN_CUSTOM_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, content)
        self.assertNotIn("/qp/batch_fetch/", content)
        self.assertNotIn("Ljava/lang/System;->exit", content)
        self.assertNotIn("isrestart", content)
        self.assertIsNone(re.search(r"\b0x7f[0-9a-f]+\b", content))

    def test_ui_and_blocker_share_upgrade_compatible_preferences(self) -> None:
        utility = (SOURCE / "newCode" / "com" / "dfinstagram" / "dfinstagram.smali").read_text(
            encoding="utf-8"
        )
        fragment = (
            SOURCE / "newCode" / "com" / "dfinstagram" / "preference" / "PreferenceFragment.smali"
        ).read_text(encoding="utf-8")

        self.assertIn('const-string v1, "com.instagram"', utility)
        self.assertIn('const-string v2, "com.instagram"', fragment)


class ManifestAndPayloadTests(unittest.TestCase):
    def test_manifest_adds_one_non_exported_activity(self) -> None:
        activity = ET.parse(SOURCE / "manifest" / "added_components.xml").getroot()

        self.assertEqual(activity.tag, "activity")
        self.assertEqual(
            activity.attrib[f"{{{ANDROID_NS}}}name"],
            "com.dfinstagram.preference.Preference",
        )
        self.assertEqual(activity.attrib[f"{{{ANDROID_NS}}}exported"], "false")
        self.assertEqual(
            activity.attrib[f"{{{ANDROID_NS}}}theme"],
            "@android:style/Theme.Material.Light.NoActionBar",
        )
        self.assertEqual(len(activity.attrib), 3)

    def test_preferences_have_exactly_five_symbolic_switches(self) -> None:
        root = ET.parse(SOURCE / "newRes" / "xml" / "dfinsta_settings.xml").getroot()
        switches = list(root)
        key = f"{{{ANDROID_NS}}}key"
        title = f"{{{ANDROID_NS}}}title"
        summary = f"{{{ANDROID_NS}}}summary"

        self.assertEqual([item.tag for item in switches], ["SwitchPreference"] * 5)
        self.assertEqual(
            {item.attrib[key] for item in switches},
            {
                "disable_feed",
                "disable_explore",
                "disable_reels",
                "disable_stories",
                "disable_shopping",
            },
        )
        self.assertTrue(all(item.attrib[title].startswith("@string/") for item in switches))
        self.assertTrue(all(item.attrib[summary].startswith("@string/") for item in switches))

    def test_has_three_exact_430_anchored_patches(self) -> None:
        manifest = json.loads(
            (SOURCE / "patches" / "anchored_patches.json").read_text(encoding="utf-8")
        )
        operations = {operation["id"]: operation for operation in manifest["operations"]}

        self.assertEqual(set(operations), {
            "set_app_context",
            "tigon_url_block",
            "install_settings_long_click",
        })
        self.assertEqual(
            operations["set_app_context"]["payload"],
            ["", "    invoke-static {v0}, Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V"],
        )
        self.assertEqual(
            operations["tigon_url_block"]["anchor"],
            [":try_start_0", "iget-object v1, p1, LX/05ez;->A08:Ljava/net/URI;"],
        )
        self.assertEqual(
            operations["tigon_url_block"]["payload"],
            ["", "    invoke-static {v1}, Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V"],
        )
        self.assertEqual(
            operations["install_settings_long_click"]["payload"],
            [
                "    new-instance v13, Lcom/dfinstagram/SettingsWrapper;",
                "",
                "    invoke-direct {v13}, Lcom/dfinstagram/SettingsWrapper;-><init>()V",
            ],
        )


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dex_names = ["classes.dex"] + [
            f"classes{index}.dex" for index in range(2, 21)
        ]
        self.custom_dex = " ".join(REQUIRED_CUSTOM_SYMBOLS).encode("utf-8")

    def test_accepts_exact_20_dex_contract(self) -> None:
        result = verify(self.dex_names, self.custom_dex)

        self.assertTrue(result["passed"])

    def test_rejects_wrong_dex_count(self) -> None:
        result = verify(self.dex_names[:-1], self.custom_dex)

        self.assertFalse(result["passed"])

    def test_rejects_forbidden_custom_symbol(self) -> None:
        result = verify(self.dex_names, self.custom_dex + b" Lcom/instagram/example;")

        self.assertFalse(result["passed"])
        self.assertTrue(result["forbidden_custom_symbols_present"]["Lcom/instagram/"])


if __name__ == "__main__":
    unittest.main()
