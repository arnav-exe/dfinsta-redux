import argparse
import shlex
import subprocess
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent


def run(command: list[str]) -> None:
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("apktool_jar", type=Path)
    parser.add_argument("--work-tree", required=True, type=Path)
    parser.add_argument("--output-apk", required=True, type=Path)
    args = parser.parse_args()

    if args.work_tree.exists():
        raise FileExistsError(f"Refusing to overwrite work tree {args.work_tree}")
    if args.output_apk.exists():
        raise FileExistsError(f"Refusing to overwrite APK {args.output_apk}")

    python = sys.executable
    endpoint_report = args.work_tree.parent / f"{args.work_tree.name}-endpoint-report.json"
    anchored_report = args.work_tree.parent / f"{args.work_tree.name}-anchored-report.json"
    verification = args.output_apk.with_suffix(".verification.json")

    run(
        [
            python,
            str(TOOLS / "prepare_build_tree.py"),
            str(args.stock_decode),
            str(args.patch_source),
            "--output",
            str(args.work_tree),
        ]
    )
    run(
        [
            python,
            str(TOOLS / "apply_endpoint_patches.py"),
            str(args.work_tree),
            str(args.patch_source / "patches" / "endpoint_replacements.json"),
            "--output",
            str(endpoint_report),
        ]
    )
    run(
        [
            python,
            str(TOOLS / "apply_anchored_patches.py"),
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
            str(verification),
        ]
    )


if __name__ == "__main__":
    main()
