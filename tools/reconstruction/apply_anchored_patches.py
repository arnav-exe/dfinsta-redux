import argparse
import json
from pathlib import Path

from apply_endpoint_patches import class_index


def significant_lines(lines: list[str]) -> list[tuple[int, str]]:
    significant = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(".line") and not stripped.startswith("#"):
            significant.append((index, stripped))
    return significant


def find_anchors(lines: list[str], anchor: list[str]) -> list[tuple[int, int]]:
    significant = significant_lines(lines)
    matches = []
    width = len(anchor)
    for index in range(len(significant) - width + 1):
        candidate = [item[1] for item in significant[index : index + width]]
        if candidate == anchor:
            matches.append((significant[index][0], significant[index + width - 1][0]))
    return matches


def apply_operation(path: Path, operation: dict) -> str:
    content = path.read_text(encoding="utf-8")
    marker_count = content.count(operation["marker"])
    expected_markers = operation["expected_marker_count"]
    if marker_count == expected_markers:
        return "already_applied"
    if marker_count:
        raise ValueError(
            f"Partial patch in {path}: marker {operation['marker']} found "
            f"{marker_count}/{expected_markers} times"
        )

    lines = content.splitlines()
    matches = find_anchors(lines, operation["anchor"])
    expected_anchors = operation["expected_anchor_count"]
    if len(matches) != expected_anchors:
        raise ValueError(
            f"Anchor mismatch in {path} for {operation['id']}: "
            f"found {len(matches)}/{expected_anchors}"
        )
    occurrence = operation.get("occurrence", 0)
    start, end = matches[occurrence]
    payload = operation["payload"]
    mode = operation["mode"]
    if mode == "insert_after":
        lines[end + 1 : end + 1] = payload
    elif mode == "insert_before":
        lines[start:start] = payload
    elif mode == "replace":
        lines[start : end + 1] = payload
    else:
        raise ValueError(f"Unknown mode: {mode}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "applied"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    paths = class_index(args.target)
    results = []
    for operation in manifest["operations"]:
        descriptor = operation["descriptor"]
        if descriptor not in paths:
            raise ValueError(f"Descriptor not found: {descriptor}")
        status = apply_operation(paths[descriptor], operation)
        results.append({"id": operation["id"], "descriptor": descriptor, "status": status})

    report = {
        "operations": len(results),
        "applied": sum(item["status"] == "applied" for item in results),
        "already_applied": sum(item["status"] == "already_applied" for item in results),
        "results": results,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("operations", "applied", "already_applied")}, indent=2))


if __name__ == "__main__":
    main()
