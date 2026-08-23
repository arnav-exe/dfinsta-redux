import argparse
import copy
import json
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


ANDROID_NAME = "{http://schemas.android.com/apk/res/android}name"


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def class_relative_path(path: str) -> Path:
    parts = Path(path).parts
    if not parts or not parts[0].startswith("smali"):
        raise ValueError(f"Unexpected smali path: {path}")
    return Path(*parts[1:])


def clean_element(element: ET.Element) -> ET.Element:
    element = copy.deepcopy(element)
    for node in element.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return element


def added_manifest_components(stock: Path, modified: Path) -> list[ET.Element]:
    stock_application = ET.parse(stock).getroot().find("application")
    modified_application = ET.parse(modified).getroot().find("application")
    if stock_application is None or modified_application is None:
        raise ValueError("Manifest has no application element")

    stock_keys = {(child.tag, child.attrib.get(ANDROID_NAME)) for child in stock_application}
    return [
        clean_element(child)
        for child in modified_application
        if (child.tag, child.attrib.get(ANDROID_NAME)) not in stock_keys
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("value_delta", type=Path)
    parser.add_argument("direct_diffs", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite {args.output}")

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    value_delta = json.loads(args.value_delta.read_text(encoding="utf-8"))
    stock_root = Path(inventory["stock"])
    modified_root = Path(inventory["modified"])

    copied_classes = defaultdict(int)
    for descriptor, path in inventory["classes"]["added_paths"].items():
        relative = class_relative_path(path)
        if descriptor.startswith("Lcom/dfinstagram/"):
            destination = args.output / "newCode" / relative
            copied_classes["dfinstagram"] += 1
        elif descriptor.startswith("Lcom/acra/"):
            destination = args.output / "thirdPartyCode" / relative
            copied_classes["acra"] += 1
        else:
            raise ValueError(f"Unclassified added class: {descriptor}")
        copy_file(modified_root / path, destination)

    added_files = set(inventory["files"]["added"])
    copied_resources = 0
    for path in sorted(added_files):
        if not path.startswith("res/"):
            continue
        copy_file(modified_root / path, args.output / "newRes" / Path(path).relative_to("res"))
        copied_resources += 1

    fragments = defaultdict(list)
    for key, xml in value_delta["added"].items():
        relative, _ = key.split("::", 1)
        if relative not in added_files:
            fragments[Path(relative).relative_to("res")].append(xml)
    for relative, entries in fragments.items():
        destination = args.output / "appendRes" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(entries) + "\n", encoding="utf-8")

    patches = args.output / "resourcePatches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / "changed_values.json").write_text(
        json.dumps(value_delta["changed"], indent=2) + "\n", encoding="utf-8"
    )

    manifest_components = added_manifest_components(
        stock_root / "AndroidManifest.xml", modified_root / "AndroidManifest.xml"
    )
    manifest_dir = args.output / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "added_components.xml").write_text(
        "\n".join(ET.tostring(element, encoding="unicode") for element in manifest_components)
        + "\n",
        encoding="utf-8",
    )

    oracle_diffs = args.output / "oracleDeltas" / "host"
    oracle_diffs.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.direct_diffs.glob("*.diff")):
        copy_file(path, oracle_diffs / path.name)

    metadata = {
        "base_instagram": "340.0.0.22.109",
        "dfinsta_version": "1.4.1",
        "copied_classes": dict(copied_classes),
        "copied_resource_files": copied_resources,
        "append_resource_files": len(fragments),
        "changed_value_entries": len(value_delta["changed"]),
        "added_manifest_components": len(manifest_components),
        "direct_hook_diffs": len(list(args.direct_diffs.glob("*.diff"))),
    }
    (args.output / "reconstruction.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
