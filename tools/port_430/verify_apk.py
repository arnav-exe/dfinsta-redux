import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable


REQUIRED_CUSTOM_SYMBOLS = [
    "Lcom/dfinstagram/startapp;",
    "Lcom/dfinstagram/dfinstagram;",
    "Lcom/dfinstagram/hooks;",
    "Lcom/dfinstagram/SettingsWrapper;",
]

GRAFT_DEX_NAMES = {"classes.dex", "classes3.dex", "classes4.dex", "classes6.dex", "classes20.dex"}

FORBIDDEN_CUSTOM_SYMBOLS = [
    "Lcom/instagram/",
    "DistractionFree",
    "Amplitude",
    "Lcom/acra/",
    "UniFile",
    "FeedCache",
    "Hardcore",
    "donate_",
    "welcome",
    "istring",
    "improveRemove",
    "modifyFeedResponse",
    "nativeReadBuffer",
    "Lcom/dfinstagram/preference/Preference;",
    "Lcom/dfinstagram/preference/PreferenceFragment;",
    "Landroid/app/Activity;",
    "Landroid/preference/PreferenceActivity;",
]


def expected_dex_names() -> list[str]:
    return ["classes.dex"] + [f"classes{index}.dex" for index in range(2, 21)]


def is_signature_artifact(name: str) -> bool:
    parts = name.upper().split("/")
    if len(parts) != 2 or parts[0] != "META-INF":
        return False
    return parts[1] == "MANIFEST.MF" or parts[1].endswith((".SF", ".RSA", ".DSA", ".EC"))


def payload_comparison(final_entries: dict[str, Any], stock_entries: dict[str, Any]) -> tuple[bool, bool]:
    retained_final = {
        name: value
        for name, value in final_entries.items()
        if name not in GRAFT_DEX_NAMES and not is_signature_artifact(name)
    }
    retained_stock = {
        name: value
        for name, value in stock_entries.items()
        if name not in GRAFT_DEX_NAMES and not is_signature_artifact(name)
    }
    names_equal = set(retained_final) == set(retained_stock)
    return names_equal, names_equal and retained_final == retained_stock


def method_body(smali: str, signature: str) -> str:
    match = re.search(
        rf"(?ms)^\.method [^\n]*\b{re.escape(signature)}\s*$.*?^\.end method$",
        smali,
    )
    if not match:
        raise ValueError(f"Method not found: {signature}")
    return match.group(0)


def endpoint_replacement_present(method: str, endpoint: str) -> bool:
    marker = re.escape(
        "Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;"
    )
    pattern = (
        rf'const-string ([vp]\d+), "{re.escape(endpoint)}"\s+'
        rf"invoke-static \{{\1\}}, {marker}\s+move-result-object \1"
    )
    return re.search(pattern, method) is not None


def verify_structural_hooks(smali_root: Path) -> dict[str, bool]:
    paths = {
        "tigon": smali_root / "smali/com/instagram/api/tigon/TigonServiceLayer.smali",
        "context": smali_root / "smali_classes3/com/instagram/app/InstagramAppShell.smali",
        "reels": smali_root / "smali_classes4/X/05t2.smali",
        "settings": smali_root / "smali_classes6/X/077K.smali",
    }
    sources = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    on_create = method_body(sources["context"], "onCreate()V")
    start_request = method_body(
        sources["tigon"], "startRequest(LX/05ez;LX/05fq;LX/05gu;)LX/0Fcm;"
    )
    reels_a07 = method_body(
        sources["reels"],
        "A07(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Lkotlin/jvm/functions/Function0;ZZZZ)LX/017H;",
    )
    reels_a09 = method_body(
        sources["reels"],
        "A09(Landroid/content/Context;LX/0HSu;LX/0Jin;LX/0Jej;Lcom/instagram/common/session/UserSession;LX/0Jae;Ljava/lang/Integer;Ljava/lang/Long;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/util/List;Lkotlin/jvm/functions/Function0;ZZZZZZZZZZZ)LX/03xp;",
    )
    settings_a00 = method_body(
        sources["settings"],
        "A00(Landroid/content/Context;Lcom/instagram/common/session/UserSession;LX/077F;LX/0JxZ;)Landroid/widget/ImageView;",
    )
    reels_marker = "Lcom/dfinstagram/hooks;->replaceReelsEndpoint(Ljava/lang/String;)Ljava/lang/String;"
    context_pattern = re.compile(
        r"invoke-super \{v0\}, Landroid/app/Application;->onCreate\(\)V\s+"
        r"invoke-static \{v0\}, Lcom/dfinstagram/startapp;->setContext\(Landroid/app/Application;\)V"
    )
    tigon_pattern = re.compile(
        r"iget-object ([vp]\d+), p1, LX/05ez;->A08:Ljava/net/URI;\s+"
        r"invoke-static \{\1\}, Lcom/dfinstagram/hooks;->throwIfBlocked\(Ljava/net/URI;\)V"
    )
    settings_pattern = re.compile(
        r"invoke-static \{v0, v6\}, LX/00ZY;->A00\(Landroid/view/View\$OnClickListener;Landroid/view/View;\)V\s+"
        r"instance-of v0, p3, LX/077N;\s+"
        r"if-eqz v0, (:[A-Za-z0-9_]+)\s+"
        r"new-instance v0, Lcom/dfinstagram/SettingsWrapper;\s+"
        r"invoke-direct \{v0\}, Lcom/dfinstagram/SettingsWrapper;-><init>\(\)V\s+"
        r"invoke-virtual \{v6, v0\}, Landroid/view/View;->setOnLongClickListener\(Landroid/view/View\$OnLongClickListener;\)V\s+"
        r"\1"
    )
    return {
        "context_on_create_sequence": context_pattern.search(on_create) is not None
        and on_create.count(
            "Lcom/dfinstagram/startapp;->setContext(Landroid/app/Application;)V"
        )
        == 1,
        "tigon_start_request_sequence": tigon_pattern.search(start_request) is not None
        and start_request.count(
            "Lcom/dfinstagram/hooks;->throwIfBlocked(Ljava/net/URI;)V"
        )
        == 1,
        "reels_a07_discover": endpoint_replacement_present(reels_a07, "clips/discover/"),
        "reels_a09_homecoming": endpoint_replacement_present(reels_a09, "clips/homecoming/"),
        "reels_a09_stream": endpoint_replacement_present(reels_a09, "clips/discover/stream/"),
        "reels_call_count_three": (reels_a07 + reels_a09).count(reels_marker) == 3,
        "settings_guarded_after_stock_click": settings_pattern.search(settings_a00) is not None,
        "settings_long_click_call_once": settings_a00.count(
            "Landroid/view/View;->setOnLongClickListener(Landroid/view/View$OnLongClickListener;)V"
        )
        == 1,
    }


def archive_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt APK: {path}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate ZIP entry in {path}")
        hashes = {}
        for name in names:
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[name] = digest.hexdigest()
        return hashes


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_sources(apk: Path, apktool_jar: Path, output: Path) -> None:
    subprocess.run(
        ["java", "-jar", str(apktool_jar), "decode", "-r", "-o", str(output), str(apk)],
        check=True,
    )


def signature_context(
    apk: Path, apksigner: Path, expected_certificate_sha256: str | None = None
) -> dict[str, Any]:
    result = subprocess.run(
        [str(apksigner), "verify", "--verbose", "--print-certs", str(apk)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (result.stdout + result.stderr).strip().splitlines()
    certificates = [
        match.group(1).lower()
        for line in output
        if (match := re.fullmatch(r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})", line))
    ]
    expected = expected_certificate_sha256.lower() if expected_certificate_sha256 else None
    return {
        "tool": str(apksigner),
        "tool_sha256": file_sha256(apksigner),
        "verified": result.returncode == 0,
        "certificate_sha256": certificates,
        "expected_certificate_sha256": expected,
        "approved_signer": expected is None or certificates == [expected],
        "output": output,
    }


def verify(
    dex_names: list[str],
    dex_content: dict[str, bytes],
    final_entries: dict[str, bytes],
    stock_entries: dict[str, bytes],
    structural_hooks: dict[str, bool],
    payload_names_equal: bool,
    payload_bytes_equal: bool,
) -> dict:
    custom_dex = dex_content.get("classes20.dex", b"")
    custom_symbols = {
        match.decode("utf-8")
        for match in re.findall(rb"Lcom/dfinstagram/[A-Za-z0-9_$/]+;", custom_dex)
    }
    required = {symbol: symbol in custom_symbols for symbol in REQUIRED_CUSTOM_SYMBOLS}
    forbidden = {
        symbol: symbol.encode("utf-8") in custom_dex for symbol in FORBIDDEN_CUSTOM_SYMBOLS
    }
    exact_dex_set = len(dex_names) == 20 and set(dex_names) == set(expected_dex_names())
    final_res = {name for name in final_entries if name.startswith("res/")}
    stock_res = {name for name in stock_entries if name.startswith("res/")}
    resource_names_equal = final_res == stock_res
    resource_bytes_equal = resource_names_equal and all(
        final_entries[name] == stock_entries[name] for name in stock_res
    )
    manifest_equal = (
        "AndroidManifest.xml" in final_entries
        and "AndroidManifest.xml" in stock_entries
        and final_entries["AndroidManifest.xml"] == stock_entries["AndroidManifest.xml"]
    )
    resources_arsc_equal = (
        "resources.arsc" in final_entries
        and "resources.arsc" in stock_entries
        and final_entries["resources.arsc"] == stock_entries["resources.arsc"]
    )
    exact_custom_symbols = custom_symbols == set(REQUIRED_CUSTOM_SYMBOLS)
    return {
        "dex_files": sorted(dex_names),
        "dex_count": len(dex_names),
        "exact_dex_set": exact_dex_set,
        "exact_custom_symbols": exact_custom_symbols,
        "required_custom_symbols": required,
        "structural_host_hooks": structural_hooks,
        "forbidden_custom_symbols_present": forbidden,
        "android_manifest_equal": manifest_equal,
        "resources_arsc_equal": resources_arsc_equal,
        "res_entry_names_equal": resource_names_equal,
        "res_entry_bytes_equal": resource_bytes_equal,
        "retained_payload_entry_names_equal": payload_names_equal,
        "retained_payload_entry_bytes_equal": payload_bytes_equal,
        "passed": all(
            [
                exact_dex_set,
                exact_custom_symbols,
                all(required.values()),
                all(structural_hooks.values()),
                not any(forbidden.values()),
                manifest_equal,
                resources_arsc_equal,
                resource_bytes_equal,
                payload_names_equal,
                payload_bytes_equal,
            ]
        ),
    }


def read_apk(
    path: Path, include: Callable[[str], bool]
) -> tuple[list[str], dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt APK: {path}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate ZIP entry in {path}")
        return names, {name: archive.read(name) for name in names if include(name)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("--apktool-jar", required=True, type=Path)
    parser.add_argument("--apksigner", type=Path)
    parser.add_argument("--require-signature", action="store_true")
    parser.add_argument("--expected-certificate-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.require_signature and args.apksigner is None:
        parser.error("--require-signature requires --apksigner")
    if args.expected_certificate_sha256:
        if args.apksigner is None:
            parser.error("--expected-certificate-sha256 requires --apksigner")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_certificate_sha256):
            parser.error("--expected-certificate-sha256 must be 64 hexadecimal characters")

    relevant = lambda name: (
        name in {"AndroidManifest.xml", "resources.arsc"}
        or name.startswith("res/")
        or (name.startswith("classes") and name.endswith(".dex") and "/" not in name)
    )
    resources = lambda name: name in {"AndroidManifest.xml", "resources.arsc"} or name.startswith(
        "res/"
    )
    final_names, final_entries = read_apk(args.apk, relevant)
    _, stock_entries = read_apk(args.stock_apk, resources)
    final_hashes = archive_hashes(args.apk)
    stock_hashes = archive_hashes(args.stock_apk)
    payload_names_equal, payload_bytes_equal = payload_comparison(final_hashes, stock_hashes)
    dex_names = [
        name
        for name in final_names
        if name.startswith("classes") and name.endswith(".dex") and "/" not in name
    ]
    dex_content = {name: final_entries[name] for name in dex_names}
    with tempfile.TemporaryDirectory(prefix="dfinsta-verify-") as directory:
        decoded = Path(directory) / "decoded"
        decode_sources(args.apk, args.apktool_jar, decoded)
        structural_hooks = verify_structural_hooks(decoded)
        result = {
            "apk": str(args.apk),
            "apk_sha256": file_sha256(args.apk),
            "stock_apk": str(args.stock_apk),
            "stock_apk_sha256": file_sha256(args.stock_apk),
            "apktool_jar": str(args.apktool_jar),
            "apktool_jar_sha256": file_sha256(args.apktool_jar),
            "verifier_sha256": file_sha256(Path(__file__).resolve()),
            **verify(
                dex_names,
                dex_content,
                final_entries,
                stock_entries,
                structural_hooks,
                payload_names_equal,
                payload_bytes_equal,
            ),
        }
        signature = (
            signature_context(args.apk, args.apksigner, args.expected_certificate_sha256)
            if args.apksigner
            else None
        )
        result["signature"] = signature
        if signature is not None:
            result["passed"] = (
                result["passed"] and signature["verified"] and signature["approved_signer"]
            )
        elif args.require_signature:
            result["passed"] = False
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise FileExistsError(f"Refusing to overwrite {args.output}")
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
