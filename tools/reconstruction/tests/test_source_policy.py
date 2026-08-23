import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
SOURCE = REPOSITORY / "tests/fixtures/dfinsta_source_340"
NEW_CODE = SOURCE / "newCode" / "com" / "dfinstagram"


def parse_public_fragment() -> ET.Element:
    path = SOURCE / "appendRes" / "values" / "public.xml"
    return ET.fromstring(f"<resources>{path.read_text(encoding='utf-8')}</resources>")


class SourcePolicyTests(unittest.TestCase):
    def test_removed_classes_are_absent(self) -> None:
        removed = (
            NEW_CODE / "dfinstagram$1.smali",
            NEW_CODE / "preference" / "Preference$1.smali",
        )

        for path in removed:
            with self.subTest(path=path):
                self.assertFalse(path.exists())

    def test_removed_smali_residue_is_absent(self) -> None:
        forbidden = (
            "PrefsBackupHelper",
            "com/dfinstagram/followers/Tracker",
            "bufferStreamField",
            "disable_comments",
            "disable_suggested_posts",
            "getRealPathFromURI",
            "improveRemoveComments",
            "improveRemoveLimitedComments",
            "improveRemoveStreamComments",
            "readBufferField",
            "restore_backup",
            "save_backup",
            "str2Bytes",
            "updateList",
        )
        content = "\n".join(
            path.read_text(encoding="utf-8") for path in NEW_CODE.rglob("*.smali")
        )

        for symbol in forbidden:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, content)

    def test_removed_manifest_component_is_absent(self) -> None:
        manifest = ET.parse(SOURCE / "manifest" / "added_components.xml").getroot()
        android_name = "{http://schemas.android.com/apk/res/android}name"

        self.assertEqual(
            manifest.attrib[android_name], "com.dfinstagram.preference.Preference"
        )
        self.assertNotIn("IconChoose", ET.tostring(manifest, encoding="unicode"))

    def test_removed_resource_keys_are_absent(self) -> None:
        removed = {"bug_report", "dfinstagram_disable_suggested_posts"}
        istrings = ET.parse(SOURCE / "newRes" / "values" / "istrings.xml")
        public = parse_public_fragment()

        self.assertTrue(
            removed.isdisjoint(item.attrib["name"] for item in istrings.iter("item"))
        )
        self.assertTrue(
            removed.isdisjoint(item.attrib["name"] for item in public.iter("public"))
        )

    def test_active_settings_essentials_remain(self) -> None:
        settings = ET.parse(SOURCE / "newRes" / "xml" / "instander_settings.xml")
        android_key = "{http://schemas.android.com/apk/res/android}key"
        keys = {
            item.attrib[android_key]
            for item in settings.iter()
            if android_key in item.attrib
        }
        required_keys = {
            "disable_explore",
            "disable_feed",
            "disable_reels",
            "disable_shopping",
            "disable_stories",
            "donate_btc",
            "donate_eth",
            "enable_hardcore",
        }
        public = parse_public_fragment()
        public_names = {item.attrib["name"] for item in public.iter("public")}
        fragment = (NEW_CODE / "preference" / "PreferenceFragment.smali").read_text(
            encoding="utf-8"
        )

        self.assertTrue(required_keys.issubset(keys))
        self.assertIn('const-string v11, "donate_btc"', fragment)
        self.assertIn('const-string v11, "donate_eth"', fragment)
        self.assertIn("DfInstagramPreference", public_names)
        self.assertIn("instander_settings", public_names)
        self.assertTrue((NEW_CODE / "SettingsWrapper.smali").is_file())
        self.assertTrue((NEW_CODE / "preference" / "Preference.smali").is_file())


if __name__ == "__main__":
    unittest.main()
