import argparse
import copy
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
REPOSITORY = TOOLS.parents[1]
ANCHORED_PATCHER = REPOSITORY / "tools" / "reconstruction" / "apply_anchored_patches.py"
GRAFT_NAMES = {"classes.dex", "classes3.dex", "classes6.dex", "classes20.dex"}


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
    refused_paths = [
        args.framework_path,
        args.work_tree,
        args.output_apk,
        intermediate_apk,
        anchored_report,
        verification_report,
    ]
    for path in refused_paths:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")

    python = sys.executable
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
            "--output",
            str(verification_report),
        ]
    )


if __name__ == "__main__":
    main()
