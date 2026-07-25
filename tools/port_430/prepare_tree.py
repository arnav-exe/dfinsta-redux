import argparse
import copy
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)


def copy_overlay(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite payload target {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def merge_manifest(fragment_path: Path, manifest_path: Path) -> None:
    fragment = ET.fromstring(fragment_path.read_text(encoding="utf-8"))
    if fragment.tag != "activity":
        raise ValueError("430 manifest payload must contain exactly one activity")

    tree = ET.parse(manifest_path)
    application = tree.getroot().find("application")
    if application is None:
        raise ValueError("Stock manifest has no application element")

    name_key = f"{{{ANDROID_NS}}}name"
    component_name = fragment.attrib.get(name_key)
    if any(
        child.tag == "activity" and child.attrib.get(name_key) == component_name
        for child in application
    ):
        raise ValueError(f"Manifest activity already exists: {component_name}")

    application.append(copy.deepcopy(fragment))
    ET.indent(tree, space="    ")
    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)


def prepare(stock_decode: Path, patch_source: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite work tree {output}")
    if not (stock_decode / "AndroidManifest.xml").is_file():
        raise FileNotFoundError(f"Not an apktool decode: {stock_decode}")
    if (stock_decode / "smali_classes20").exists():
        raise ValueError("Stock tree already has smali_classes20")

    shutil.copytree(stock_decode, output)
    copy_overlay(patch_source / "newCode", output / "smali_classes20")
    copy_overlay(patch_source / "newRes", output / "res")
    merge_manifest(
        patch_source / "manifest" / "added_components.xml",
        output / "AndroidManifest.xml",
    )


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
