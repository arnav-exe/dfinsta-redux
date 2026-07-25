import argparse
import hashlib
import json
import re
from pathlib import Path


CLASS_RE = re.compile(r"^\.class\b.*\s(L[^;]+;)$")


def strip_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "#" and not quoted:
            return line[:index]
    return line


def normalized_smali(path: Path) -> bytes:
    normalized = []
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            stripped = line.strip()
            if not stripped or stripped.startswith(".line") or stripped.startswith("#"):
                continue
            line = strip_comment(line).strip()
            if line:
                normalized.append(line)
    return ("\n".join(normalized) + "\n").encode("utf-8")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def class_descriptor(path: Path) -> str:
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            match = CLASS_RE.match(line.strip())
            if match:
                return match.group(1)
    raise ValueError(f"No .class descriptor in {path}")


def smali_inventory(root: Path) -> dict[str, dict[str, str]]:
    classes = {}
    for path in root.glob("smali*/**/*.smali"):
        descriptor = class_descriptor(path)
        if descriptor in classes:
            raise ValueError(f"Duplicate descriptor {descriptor}: {path}")
        classes[descriptor] = {
            "path": path.relative_to(root).as_posix(),
            "normalized_sha256": digest(normalized_smali(path)),
        }
    return classes


def file_inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not relative.startswith("smali"):
            files[relative] = digest(path.read_bytes())
    return files


def compare(left: dict, right: dict) -> dict[str, list]:
    left_keys = set(left)
    right_keys = set(right)
    return {
        "added": sorted(right_keys - left_keys),
        "removed": sorted(left_keys - right_keys),
        "changed": sorted(key for key in left_keys & right_keys if left[key] != right[key]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock", type=Path)
    parser.add_argument("modified", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    stock_classes = smali_inventory(args.stock)
    modified_classes = smali_inventory(args.modified)
    class_changes = compare(stock_classes, modified_classes)

    stock_files = file_inventory(args.stock)
    modified_files = file_inventory(args.modified)
    file_changes = compare(stock_files, modified_files)

    result = {
        "stock": str(args.stock),
        "modified": str(args.modified),
        "counts": {
            "stock_classes": len(stock_classes),
            "modified_classes": len(modified_classes),
            "added_classes": len(class_changes["added"]),
            "removed_classes": len(class_changes["removed"]),
            "changed_classes": len(class_changes["changed"]),
            "stock_non_smali_files": len(stock_files),
            "modified_non_smali_files": len(modified_files),
            "added_files": len(file_changes["added"]),
            "removed_files": len(file_changes["removed"]),
            "changed_files": len(file_changes["changed"]),
        },
        "classes": {
            **class_changes,
            "added_paths": {key: modified_classes[key]["path"] for key in class_changes["added"]},
            "removed_paths": {key: stock_classes[key]["path"] for key in class_changes["removed"]},
            "stock_paths": {key: stock_classes[key]["path"] for key in class_changes["changed"]},
            "modified_paths": {
                key: modified_classes[key]["path"] for key in class_changes["changed"]
            },
        },
        "files": file_changes,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["counts"], indent=2))


if __name__ == "__main__":
    main()
