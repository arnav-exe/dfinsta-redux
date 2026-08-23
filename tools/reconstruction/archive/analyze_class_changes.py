import argparse
import sys
import json
from collections import Counter
from pathlib import Path

# `inventory.py` stayed in the parent directory when these scripts were
# archived on 2026-08-23: it is live library code that `apply_endpoint_patches`
# imports on every port, while this script is a completed one-time job.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory import normalized_smali  # noqa: E402


def lines(path: Path) -> list[str]:
    return normalized_smali(path).decode("utf-8").splitlines()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    stock_root = Path(inventory["stock"])
    modified_root = Path(inventory["modified"])
    stock_paths = inventory["classes"]["stock_paths"]
    modified_paths = inventory["classes"]["modified_paths"]

    changes = []
    for descriptor in inventory["classes"]["changed"]:
        stock_lines = lines(stock_root / stock_paths[descriptor])
        modified_lines = lines(modified_root / modified_paths[descriptor])
        stock_counter = Counter(stock_lines)
        modified_counter = Counter(modified_lines)
        added = list((modified_counter - stock_counter).elements())
        removed = list((stock_counter - modified_counter).elements())
        dfinsta_added = Counter(line for line in added if "Lcom/dfinstagram/" in line)
        changes.append(
            {
                "descriptor": descriptor,
                "stock_path": stock_paths[descriptor],
                "modified_path": modified_paths[descriptor],
                "added_line_count": len(added),
                "removed_line_count": len(removed),
                "dfinsta_added_call_count": sum(dfinsta_added.values()),
                "dfinsta_added_lines": dict(sorted(dfinsta_added.items())),
            }
        )

    direct_hosts = [change for change in changes if change["dfinsta_added_lines"]]
    ranked = sorted(
        changes,
        key=lambda change: change["added_line_count"] + change["removed_line_count"],
        reverse=True,
    )
    result = {
        "direct_hook_host_count": len(direct_hosts),
        "direct_hook_hosts": direct_hosts,
        "changed_classes_ranked": ranked,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"Direct hook hosts: {len(direct_hosts)}")
    for host in direct_hosts:
        print(f"  {host['descriptor']} ({host['modified_path']})")
        for line, count in host["dfinsta_added_lines"].items():
            print(f"    {count}x {line}")
    print("Largest normalized changes:")
    for change in ranked[:15]:
        total = change["added_line_count"] + change["removed_line_count"]
        print(f"  {total:6} lines  {change['descriptor']}")


if __name__ == "__main__":
    main()
