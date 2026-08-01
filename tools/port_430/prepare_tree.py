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


def prepare(
    stock_decode: Path,
    patch_source: Path,
    output: Path,
    custom_tree: str = "smali_classes20",
) -> None:
    """Overlay the custom classes into a fresh copy of a stock decode.

    `custom_tree` is a parameter because the free index is target-specific:
    Instagram 430 shipped 19 DEX files so smali_classes20 was free, while 439
    ships 20 and needs smali_classes21. Defaulting to the 430 value keeps every
    existing caller and test unchanged.
    """
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite work tree {output}")
    if not (stock_decode / "AndroidManifest.xml").is_file():
        raise FileNotFoundError(f"Not an apktool decode: {stock_decode}")
    if (stock_decode / custom_tree).exists():
        raise ValueError(f"Stock tree already has {custom_tree}")

    shutil.copytree(stock_decode, output)
    copy_overlay(patch_source / "newCode", output / custom_tree)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--custom-tree", default="smali_classes20")
    args = parser.parse_args()

    prepare(args.stock_decode, args.patch_source, args.output, args.custom_tree)
    print(f"Prepared build tree at {args.output}")


if __name__ == "__main__":
    main()
