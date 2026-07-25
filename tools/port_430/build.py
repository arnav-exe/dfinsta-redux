import argparse
import shlex
import subprocess
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
REPOSITORY = TOOLS.parents[1]
ANCHORED_PATCHER = REPOSITORY / "tools" / "reconstruction" / "apply_anchored_patches.py"


def run(command: list[str]) -> None:
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("apktool_jar", type=Path)
    parser.add_argument("framework_apk", type=Path)
    parser.add_argument("--framework-path", required=True, type=Path)
    parser.add_argument("--work-tree", required=True, type=Path)
    parser.add_argument("--output-apk", required=True, type=Path)
    args = parser.parse_args()

    anchored_report = args.work_tree.parent / f"{args.work_tree.name}-anchored-report.json"
    verification_report = args.output_apk.with_suffix(".verification.json")
    refused_paths = [
        args.framework_path,
        args.work_tree,
        args.output_apk,
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
            str(args.output_apk),
        ]
    )
    run(
        [
            python,
            str(TOOLS / "verify_apk.py"),
            str(args.output_apk),
            "--output",
            str(verification_report),
        ]
    )


if __name__ == "__main__":
    main()
