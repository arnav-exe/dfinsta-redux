import argparse
import json
import re
from pathlib import Path

from inventory import class_descriptor


CONST_RE_TEMPLATE = r'(?m)^(?P<indent>[ \t]*)const-string(?:/jumbo)?\s+(?P<register>[vp]\d+),\s+"{literal}"[ \t]*$'


def class_index(root: Path) -> dict[str, Path]:
    result = {}
    for path in root.glob("smali*/**/*.smali"):
        descriptor = class_descriptor(path)
        if descriptor in result:
            raise ValueError(f"Duplicate descriptor {descriptor}")
        result[descriptor] = path
    return result


def apply_operation(path: Path, operation: dict) -> str:
    content = path.read_text(encoding="utf-8")
    helper = operation["helper"]
    expected = operation["expected_count"]
    helper_marker = f"Lcom/dfinstagram/DistractionFree;->{helper}("
    existing = content.count(helper_marker)
    if existing == expected:
        return "already_applied"
    if existing:
        raise ValueError(f"Partial patch in {path}: {helper} found {existing}/{expected} times")

    pattern = re.compile(CONST_RE_TEMPLATE.format(literal=re.escape(operation["literal"])))
    matches = list(pattern.finditer(content))
    if len(matches) != expected:
        raise ValueError(
            f"Anchor mismatch in {path}: {operation['literal']} found {len(matches)}/{expected} times"
        )

    def replacement(match: re.Match) -> str:
        indent = match.group("indent")
        register = match.group("register")
        if operation["mode"] == "replace":
            invoke = (
                f"{indent}invoke-static {{}}, Lcom/dfinstagram/DistractionFree;"
                f"->{helper}()Ljava/lang/String;"
            )
            return f"{invoke}\n\n{indent}move-result-object {register}"
        if operation["mode"] == "wrap":
            invoke = (
                f"{indent}invoke-static {{{register}}}, Lcom/dfinstagram/DistractionFree;"
                f"->{helper}(Ljava/lang/String;)Ljava/lang/String;"
            )
            return f"{match.group(0)}\n\n{invoke}\n\n{indent}move-result-object {register}"
        raise ValueError(f"Unknown mode: {operation['mode']}")

    updated, count = pattern.subn(replacement, content)
    if count != expected:
        raise AssertionError(f"Applied {count}/{expected} replacements in {path}")
    path.write_text(updated, encoding="utf-8")
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
        results.append({**operation, "path": str(paths[descriptor]), "status": status})

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
