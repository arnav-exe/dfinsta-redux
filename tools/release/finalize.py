#!/usr/bin/env python3
"""Align, sign, verify, and promote a self-distributed APK release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


SECRET_ENV = {
    "keystore": "DFINSTA_KEYSTORE",
    "alias": "DFINSTA_KEY_ALIAS",
    "password": "DFINSTA_KEYSTORE_PASSWORD",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        policy = json.load(stream)
    if policy.get("schema_version") != 1:
        raise ValueError("Unsupported signing policy schema")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", policy.get("policy_id", "")):
        raise ValueError("Invalid signing policy id")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]+", policy.get("package", "")):
        raise ValueError("Invalid signing package")
    if not isinstance(policy.get("minimum_sdk"), int) or policy["minimum_sdk"] < 1:
        raise ValueError("Invalid signing minimum SDK")
    certificate = policy.get("expected_certificate_sha256", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", certificate):
        raise ValueError("Invalid expected signing certificate SHA-256")
    if policy.get("allowed_signer_count") != 1:
        raise ValueError("Only one release signer is supported")
    schemes = policy.get("required_signature_schemes")
    if not isinstance(schemes, list) or not schemes or not all(
        scheme in {"v2", "v3", "v3.1", "v4"} for scheme in schemes
    ):
        raise ValueError("Invalid required signature schemes")
    policy["expected_certificate_sha256"] = certificate.lower()
    return policy


def run(command: Sequence[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=True,
    )


def parse_badging(output: str) -> dict[str, Any]:
    package = re.search(
        r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
        output,
        re.MULTILINE,
    )
    minimum_sdk = re.search(r"^sdkVersion:'(\d+)'", output, re.MULTILINE)
    if not package or not minimum_sdk:
        raise ValueError("Unable to parse APK package metadata")
    return {
        "package": package.group(1),
        "version_code": package.group(2),
        "version_name": package.group(3),
        "minimum_sdk": int(minimum_sdk.group(1)),
    }


def parse_signature(output: str) -> dict[str, Any]:
    certificates = re.findall(
        r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$",
        output,
        re.MULTILINE,
    )
    signer_count = re.search(r"^Number of signers: (\d+)$", output, re.MULTILINE)
    schemes = {
        match.group(1): match.group(2) == "true"
        for match in re.finditer(
            r"^Verified using (v(?:2|3|3\.1|4)) scheme[^:]*: (true|false)$",
            output,
            re.MULTILINE,
        )
    }
    if not signer_count or not certificates:
        raise ValueError("Unable to parse APK signer identity")
    return {
        "signer_count": int(signer_count.group(1)),
        "certificate_sha256": [value.lower() for value in certificates],
        "signature_schemes": schemes,
    }


def required_secret_environment() -> tuple[Path, str, dict[str, str]]:
    missing = [name for name in SECRET_ENV.values() if not os.environ.get(name)]
    if missing:
        raise ValueError(f"Missing signing environment variable: {missing[0]}")
    keystore = Path(os.environ[SECRET_ENV["keystore"]]).expanduser()
    if not keystore.is_file():
        raise ValueError("Signing keystore does not exist")
    alias = os.environ[SECRET_ENV["alias"]]
    child_env = dict(os.environ)
    return keystore, alias, child_env


def output_paths(output_apk: Path) -> tuple[Path, Path]:
    return output_apk.with_suffix(".verification.json"), output_apk.with_suffix(".release.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unsigned_apk", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("--unsigned-build-report", required=True, type=Path)
    parser.add_argument("--unsigned-verification-report", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--zipalign", required=True, type=Path)
    parser.add_argument("--apksigner", required=True, type=Path)
    parser.add_argument("--aapt", required=True, type=Path)
    parser.add_argument("--apktool-jar", required=True, type=Path)
    parser.add_argument("--final-verifier", required=True, type=Path)
    parser.add_argument("--output-apk", required=True, type=Path)
    args = parser.parse_args(argv)

    inputs = [
        args.unsigned_apk,
        args.stock_apk,
        args.unsigned_build_report,
        args.unsigned_verification_report,
        args.policy,
        args.zipalign,
        args.apksigner,
        args.aapt,
        args.apktool_jar,
        args.final_verifier,
    ]
    missing = next((path for path in inputs if not path.is_file()), None)
    if missing:
        raise FileNotFoundError(f"Missing release input: {missing}")
    output_apk = args.output_apk.resolve()
    verification_report, release_report = output_paths(output_apk)
    for path in (output_apk, verification_report, release_report):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")
    if not output_apk.parent.is_dir():
        raise FileNotFoundError(f"Output parent does not exist: {output_apk.parent}")

    policy = load_policy(args.policy)
    keystore, alias, child_env = required_secret_environment()
    input_hashes = {str(path.resolve()): sha256_file(path) for path in inputs}

    with tempfile.TemporaryDirectory(prefix="dfinsta-release-", dir=output_apk.parent) as directory:
        temporary = Path(directory)
        aligned = temporary / "aligned.apk"
        signed = temporary / "signed.apk"
        staged_verification = temporary / "verification.json"
        staged_release_report = temporary / "release.json"

        run([str(args.zipalign), "-P", "16", "-f", "-v", "4", str(args.unsigned_apk), str(aligned)])
        run([str(args.zipalign), "-c", "-P", "16", "-v", "4", str(aligned)])
        run(
            [
                str(args.apksigner),
                "sign",
                "--ks",
                str(keystore),
                "--ks-key-alias",
                alias,
                "--ks-pass",
                f"env:{SECRET_ENV['password']}",
                "--min-sdk-version",
                str(policy["minimum_sdk"]),
                "--out",
                str(signed),
                str(aligned),
            ],
            env=child_env,
        )
        badging = parse_badging(run([str(args.aapt), "dump", "badging", str(signed)]).stdout)
        if badging["package"] != policy["package"]:
            raise ValueError("Signed APK package does not match release policy")
        if badging["minimum_sdk"] != policy["minimum_sdk"]:
            raise ValueError("Signed APK minimum SDK does not match release policy")

        signature_result = run(
            [
                str(args.apksigner),
                "verify",
                "--verbose",
                "--print-certs",
                "--min-sdk-version",
                str(policy["minimum_sdk"]),
                str(signed),
            ]
        )
        signature = parse_signature(signature_result.stdout + signature_result.stderr)
        if signature["signer_count"] != policy["allowed_signer_count"]:
            raise ValueError("Signed APK signer count does not match release policy")
        if signature["certificate_sha256"] != [policy["expected_certificate_sha256"]]:
            raise ValueError("Signed APK certificate does not match release policy")
        for scheme in policy["required_signature_schemes"]:
            if not signature["signature_schemes"].get(scheme, False):
                raise ValueError(f"Signed APK is missing required {scheme} signature")

        run(
            [
                sys.executable,
                str(args.final_verifier),
                str(signed),
                str(args.stock_apk),
                "--apktool-jar",
                str(args.apktool_jar),
                "--apksigner",
                str(args.apksigner),
                "--require-signature",
                "--expected-certificate-sha256",
                policy["expected_certificate_sha256"],
                "--output",
                str(staged_verification),
            ]
        )

        aligned_sha256 = sha256_file(aligned)
        signed_sha256 = sha256_file(signed)
        verification_sha256 = sha256_file(staged_verification)
        report = {
            "schema_version": 1,
            "policy": {
                "path": str(args.policy.resolve()),
                "sha256": sha256_file(args.policy),
                "policy_id": policy["policy_id"],
            },
            "package": badging,
            "signature": signature,
            "inputs": input_hashes,
            "stages": {
                "aligned_sha256": aligned_sha256,
                "signed_sha256": signed_sha256,
                "signed_verification_sha256": verification_sha256,
            },
            "tools": {
                "finalizer_sha256": sha256_file(Path(__file__).resolve()),
                "zipalign_sha256": sha256_file(args.zipalign),
                "apksigner_launcher_sha256": sha256_file(args.apksigner),
                "aapt_sha256": sha256_file(args.aapt),
                "apktool_jar_sha256": sha256_file(args.apktool_jar),
                "final_verifier_sha256": sha256_file(args.final_verifier),
            },
            "outputs": {
                "apk": str(output_apk),
                "apk_sha256": signed_sha256,
                "verification_report": str(verification_report),
                "verification_report_sha256": verification_sha256,
            },
            "secrets_recorded": False,
            "passed": True,
        }
        staged_release_report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        signed.replace(output_apk)
        staged_verification.replace(verification_report)
        staged_release_report.replace(release_report)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
