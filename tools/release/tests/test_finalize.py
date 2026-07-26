import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from finalize import load_policy, output_paths, parse_badging, parse_signature


class PolicyTests(unittest.TestCase):
    def test_loads_valid_public_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(
                '{"schema_version":1,"policy_id":"release-v1","package":"com.example.app",'
                '"minimum_sdk":28,"expected_certificate_sha256":"' + "a" * 64 + '",'
                '"allowed_signer_count":1,"required_signature_schemes":["v3"],'
                '"key_rotation_allowed":false}',
                encoding="utf-8",
            )
            self.assertEqual(load_policy(path)["expected_certificate_sha256"], "a" * 64)

    def test_rejects_missing_certificate_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_policy(path)


class ParsingTests(unittest.TestCase):
    def test_parses_package_and_signature(self) -> None:
        self.assertEqual(
            parse_badging(
                "package: name='com.example.app' versionCode='3' versionName='1.2'\n"
                "sdkVersion:'28'\n"
            ),
            {
                "package": "com.example.app",
                "version_code": "3",
                "version_name": "1.2",
                "minimum_sdk": 28,
            },
        )
        digest = "b" * 64
        parsed = parse_signature(
            "Verified using v2 scheme (APK Signature Scheme v2): false\n"
            "Verified using v3 scheme (APK Signature Scheme v3): true\n"
            "Number of signers: 1\n"
            f"Signer #1 certificate SHA-256 digest: {digest}\n"
        )
        self.assertEqual(parsed["certificate_sha256"], [digest])
        self.assertTrue(parsed["signature_schemes"]["v3"])

    def test_derives_refuse_overwrite_reports(self) -> None:
        verification, report = output_paths(Path("release.apk"))
        self.assertEqual(verification, Path("release.verification.json"))
        self.assertEqual(report, Path("release.release.json"))


if __name__ == "__main__":
    unittest.main()
