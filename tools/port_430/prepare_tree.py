import argparse
import shutil
from pathlib import Path


def copy_overlay(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite payload target {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def prepare(stock_decode: Path, patch_source: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite work tree {output}")
    if not (stock_decode / "AndroidManifest.xml").is_file():
        raise FileNotFoundError(f"Not an apktool decode: {stock_decode}")
    if (stock_decode / "smali_classes20").exists():
        raise ValueError("Stock tree already has smali_classes20")

    shutil.copytree(stock_decode, output)
    copy_overlay(patch_source / "newCode", output / "smali_classes20")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    prepare(args.stock_decode, args.patch_source, args.output)
    print(f"Prepared build tree at {args.output}")


if __name__ == "__main__":
    main()
