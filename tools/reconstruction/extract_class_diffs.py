import argparse
import difflib
import json
import re
from pathlib import Path

from inventory import normalized_smali


def safe_name(descriptor: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", descriptor.removeprefix("L").removesuffix(";"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("class_summary", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--direct-only", action="store_true")
    args = parser.parse_args()

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    summary = json.loads(args.class_summary.read_text(encoding="utf-8"))
    if args.direct_only:
        descriptors = [item["descriptor"] for item in summary["direct_hook_hosts"]]
    else:
        descriptors = inventory["classes"]["changed"]

    stock_root = Path(inventory["stock"])
    modified_root = Path(inventory["modified"])
    stock_paths = inventory["classes"]["stock_paths"]
    modified_paths = inventory["classes"]["modified_paths"]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for descriptor in descriptors:
        stock_path = stock_root / stock_paths[descriptor]
        modified_path = modified_root / modified_paths[descriptor]
        stock_lines = normalized_smali(stock_path).decode("utf-8").splitlines(keepends=True)
        modified_lines = normalized_smali(modified_path).decode("utf-8").splitlines(keepends=True)
        diff = difflib.unified_diff(
            stock_lines,
            modified_lines,
            fromfile=f"stock/{stock_paths[descriptor]}",
            tofile=f"modified/{modified_paths[descriptor]}",
        )
        output = args.output_dir / f"{safe_name(descriptor)}.diff"
        output.write_text("".join(diff), encoding="utf-8")

    print(f"Wrote {len(descriptors)} normalized class diffs to {args.output_dir}")


if __name__ == "__main__":
    main()
