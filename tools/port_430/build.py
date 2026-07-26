import argparse
import copy
import hashlib
import json
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
REPOSITORY = TOOLS.parents[1]
ANCHORED_PATCHER = REPOSITORY / "tools" / "reconstruction" / "apply_anchored_patches.py"
GRAFT_NAMES = {"classes.dex", "classes3.dex", "classes4.dex", "classes6.dex", "classes20.dex"}


def is_signature_artifact(name: str) -> bool:
    path = name.upper().split("/")
    if len(path) != 2 or path[0] != "META-INF":
        return False
    return path[1] == "MANIFEST.MF" or path[1].endswith((".SF", ".RSA", ".DSA", ".EC"))


def checked_entries(archive: zipfile.ZipFile, label: str) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.filename in entries:
            raise ValueError(f"Duplicate ZIP entry in {label}: {info.filename}")
        entries[info.filename] = info
    return entries


def graft_apk(stock_apk: Path, intermediate_apk: Path, output_apk: Path) -> None:
    if output_apk.exists():
        raise FileExistsError(f"Refusing to overwrite {output_apk}")

    with zipfile.ZipFile(stock_apk) as stock, zipfile.ZipFile(intermediate_apk) as intermediate:
        stock_entries = checked_entries(stock, "stock APK")
        intermediate_entries = checked_entries(intermediate, "intermediate APK")
        if "classes20.dex" in stock_entries:
            raise ValueError("Stock APK already contains classes20.dex")
        missing_stock = (GRAFT_NAMES - {"classes20.dex"}) - stock_entries.keys()
        missing_intermediate = GRAFT_NAMES - intermediate_entries.keys()
        if missing_stock or missing_intermediate:
            raise ValueError(
                f"Missing graft entries: stock={sorted(missing_stock)}, "
                f"intermediate={sorted(missing_intermediate)}"
            )

        with zipfile.ZipFile(output_apk, "x") as output:
            for info in stock.infolist():
                if is_signature_artifact(info.filename):
                    continue
                data_source = intermediate if info.filename in GRAFT_NAMES else stock
                output.writestr(copy.copy(info), data_source.read(info.filename))

            info = copy.copy(intermediate_entries["classes20.dex"])
            output.writestr(info, intermediate.read("classes20.dex"))


def run(command: list[str]) -> None:
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def command_output(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return (result.stdout + result.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("stock_apk", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("apktool_jar", type=Path)
    parser.add_argument("framework_apk", type=Path)
    parser.add_argument("--framework-path", required=True, type=Path)
    parser.add_argument("--work-tree", required=True, type=Path)
    parser.add_argument("--output-apk", required=True, type=Path)
    args = parser.parse_args()

    anchored_report = args.work_tree.parent / f"{args.work_tree.name}-anchored-report.json"
    intermediate_apk = args.output_apk.with_name(f"{args.output_apk.stem}-intermediate.apk")
    verification_report = args.output_apk.with_suffix(".verification.json")
    build_report = args.output_apk.with_suffix(".build.json")
    refused_paths = [
        args.framework_path,
        args.stock_decode,
        args.work_tree,
        args.output_apk,
        intermediate_apk,
        anchored_report,
        verification_report,
        build_report,
    ]
    for path in refused_paths:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")

    python = sys.executable
    input_provenance = {
        "stock_apk": str(args.stock_apk.resolve()),
        "stock_apk_sha256": sha256_file(args.stock_apk),
        "stock_decode": str(args.stock_decode.resolve()),
        "stock_decode_mode": "fresh_apktool_decode",
        "patch_source": str(args.patch_source.resolve()),
        "patch_source_sha256": sha256_tree(args.patch_source),
        "apktool_jar": str(args.apktool_jar.resolve()),
        "apktool_jar_sha256": sha256_file(args.apktool_jar),
        "framework_apk": str(args.framework_apk.resolve()),
        "framework_apk_sha256": sha256_file(args.framework_apk),
        "python": sys.version,
        "java": command_output(["java", "-version"]),
        "source_commit": command_output(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"]),
    }
    run(
        [
            "java",
            "-jar",
            str(args.apktool_jar),
            "if",
            str(args.framework_apk),
            "-p",
            str(args.framework_path),
        ]
    )
    run(
        [
            "java",
            "-jar",
            str(args.apktool_jar),
            "decode",
            "-p",
            str(args.framework_path),
            "-o",
            str(args.stock_decode),
            str(args.stock_apk),
        ]
    )
    run(
        [
            python,
            str(TOOLS / "prepare_tree.py"),
            str(args.stock_decode),
            str(args.patch_source),
            "--output",
            str(args.work_tree),
        ]
    )
    run(
        [
            python,
            str(ANCHORED_PATCHER),
            str(args.work_tree),
            str(args.patch_source / "patches" / "anchored_patches.json"),
            "--output",
            str(anchored_report),
        ]
    )
    run(
        [
            "java",
            "-jar",
            str(args.apktool_jar),
            "b",
            str(args.work_tree),
            "--use-aapt1",
            "-p",
            str(args.framework_path),
            "-o",
            str(intermediate_apk),
        ]
    )
    graft_apk(args.stock_apk, intermediate_apk, args.output_apk)
    run(
        [
            python,
            str(TOOLS / "verify_apk.py"),
            str(args.output_apk),
            str(args.stock_apk),
            "--apktool-jar",
            str(args.apktool_jar),
            "--output",
            str(verification_report),
        ]
    )
    report = {
        "schema_version": 1,
        **input_provenance,
        "anchored_report": str(anchored_report.resolve()),
        "anchored_report_sha256": sha256_file(anchored_report),
        "verification_report": str(verification_report.resolve()),
        "verification_report_sha256": sha256_file(verification_report),
        "unsigned_apk": str(args.output_apk.resolve()),
        "unsigned_apk_sha256": sha256_file(args.output_apk),
        "passed": True,
    }
    build_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
