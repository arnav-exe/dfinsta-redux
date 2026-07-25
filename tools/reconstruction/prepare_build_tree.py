import argparse
import copy
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)


def overlay(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def parse_fragments(path: Path) -> list[ET.Element]:
    content = path.read_text(encoding="utf-8")
    wrapped = f'<resources xmlns:android="{ANDROID_NS}">{content}</resources>'
    return list(ET.fromstring(wrapped))


def resource_identity(element: ET.Element) -> tuple[str, str | None]:
    return element.attrib.get("type", element.tag), element.attrib.get("name")


def append_resources(source: Path, target_root: Path) -> None:
    for fragment_path in source.rglob("*.xml"):
        relative = fragment_path.relative_to(source)
        target_path = target_root / relative
        tree = ET.parse(target_path)
        root = tree.getroot()
        existing = {resource_identity(element) for element in root}
        for element in parse_fragments(fragment_path):
            identity = resource_identity(element)
            if identity in existing:
                raise ValueError(f"Resource already exists in clean target: {relative}::{identity}")
            root.append(element)
            existing.add(identity)
        ET.indent(tree, space="    ")
        tree.write(target_path, encoding="utf-8", xml_declaration=True)


def apply_value_changes(source: Path, target_root: Path) -> None:
    changes = json.loads(source.read_text(encoding="utf-8"))
    by_file = {}
    for key, change in changes.items():
        relative, identity = key.split("::", 1)
        by_file.setdefault(relative, []).append((identity, change["modified"]))

    for relative, entries in by_file.items():
        target_path = target_root.parent / relative
        tree = ET.parse(target_path)
        root = tree.getroot()
        for identity, modified_xml in entries:
            expected_type, expected_name = identity.split("/", 1)
            matches = [
                (index, element)
                for index, element in enumerate(root)
                if resource_identity(element) == (expected_type, expected_name)
            ]
            if len(matches) != 1:
                raise ValueError(f"Expected one resource {relative}::{identity}, found {len(matches)}")
            index, old = matches[0]
            replacement = parse_fragments_from_text(modified_xml)[0]
            replacement.tail = old.tail
            root.remove(old)
            root.insert(index, replacement)
        ET.indent(tree, space="    ")
        tree.write(target_path, encoding="utf-8", xml_declaration=True)


def parse_fragments_from_text(content: str) -> list[ET.Element]:
    wrapped = f'<resources xmlns:android="{ANDROID_NS}">{content}</resources>'
    return list(ET.fromstring(wrapped))


def apply_manifest_components(source: Path, manifest: Path) -> None:
    tree = ET.parse(manifest)
    application = tree.getroot().find("application")
    if application is None:
        raise ValueError("Manifest has no application element")
    name_key = f"{{{ANDROID_NS}}}name"
    existing = {(child.tag, child.attrib.get(name_key)) for child in application}
    for component in parse_fragments(source):
        identity = component.tag, component.attrib.get(name_key)
        if identity in existing:
            raise ValueError(f"Manifest component already exists: {identity}")
        application.append(copy.deepcopy(component))
        existing.add(identity)
    ET.indent(tree, space="    ")
    tree.write(manifest, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_decode", type=Path)
    parser.add_argument("patch_source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite {args.output}")

    shutil.copytree(args.stock_decode, args.output)
    overlay(args.patch_source / "newCode", args.output / "smali_classes11")
    overlay(args.patch_source / "thirdPartyCode", args.output / "smali_classes11")
    overlay(args.patch_source / "newRes", args.output / "res")
    append_resources(args.patch_source / "appendRes", args.output / "res")
    apply_value_changes(
        args.patch_source / "resourcePatches" / "changed_values.json", args.output / "res"
    )
    apply_manifest_components(
        args.patch_source / "manifest" / "added_components.xml",
        args.output / "AndroidManifest.xml",
    )
    (args.output / "assets" / "drawables.bin").unlink(missing_ok=True)
    print(f"Prepared build tree at {args.output}")


if __name__ == "__main__":
    main()
