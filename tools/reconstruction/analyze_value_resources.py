import argparse
import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def canonical(element: ET.Element) -> str:
    element = copy.deepcopy(element)
    for node in element.iter():
        node.attrib = dict(sorted(node.attrib.items()))
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return ET.tostring(element, encoding="unicode", short_empty_elements=True)


def resource_key(relative: Path, element: ET.Element, index: int) -> str:
    resource_type = element.attrib.get("type", element.tag)
    name = element.attrib.get("name", f"unnamed-{index}")
    return f"{relative.as_posix()}::{resource_type}/{name}"


def inventory(root: Path) -> dict[str, str]:
    resources = {}
    for path in root.glob("res/values*/*.xml"):
        relative = path.relative_to(root)
        parsed = ET.parse(path)
        for index, element in enumerate(parsed.getroot()):
            key = resource_key(relative, element, index)
            if key in resources:
                raise ValueError(f"Duplicate resource key {key}")
            resources[key] = canonical(element)
    return resources


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock", type=Path)
    parser.add_argument("modified", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    stock = inventory(args.stock)
    modified = inventory(args.modified)
    stock_keys = set(stock)
    modified_keys = set(modified)
    added = sorted(modified_keys - stock_keys)
    removed = sorted(stock_keys - modified_keys)
    changed = sorted(key for key in stock_keys & modified_keys if stock[key] != modified[key])
    result = {
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "added": {key: modified[key] for key in added},
        "removed": {key: stock[key] for key in removed},
        "changed": {
            key: {"stock": stock[key], "modified": modified[key]} for key in changed
        },
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["counts"], indent=2))
    print("Changed entries:")
    for key in changed:
        print(f"  {key}")


if __name__ == "__main__":
    main()
