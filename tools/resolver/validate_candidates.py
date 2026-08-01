"""Mechanically validate agent-proposed anchors against a decoded APK.

An agent that maps hooks onto a new Instagram version produces *candidates*, not
answers. This tool is the guardrail between the two: it re-derives every checkable
claim from the decode itself, so a confident-sounding proposal cannot reach a build
without surviving deterministic checks.

Every check here exists because the corresponding mistake actually happened while
porting 430 and 439:

  descriptor_resolves   obfuscated names are recycled between versions, so a name
                        that exists may be an unrelated class
  anchor_matches        three reels anchors were submitted with leading whitespace;
                        the applier compares against line.strip() so they matched
                        zero times
  anchor_unique         `iput-object ... A0H` occurred twice in one file, once for
                        the Options button and once for the follow button
  marker_absent         a leftover marker means a partially applied patch
  registers_safe        a payload that clobbers a register read later corrupts the
                        method while still assembling cleanly

It reports, it does not repair. Anything it cannot decide is reported as UNKNOWN
rather than assumed good.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reconstruction"))

from apply_anchored_patches import find_anchors, significant_lines  # noqa: E402
from apply_endpoint_patches import class_index  # noqa: E402

# registers written by an instruction, keyed by the opcode prefix
WRITES = re.compile(
    r"^\s*(?:move|move-wide|move-object|move-result|move-result-wide|move-result-object"
    r"|move-exception|const|const/4|const/16|const-wide|const-string|const-class"
    r"|new-instance|new-array|iget|iget-wide|iget-object|iget-boolean|iget-byte"
    r"|iget-char|iget-short|sget|sget-wide|sget-object|aget|aget-object|instance-of"
    r"|array-length|add-int|sub-int|mul-int|div-int|rem-int)[^\s]*\s+([vp]\d+)"
)
READS = re.compile(r"[vp]\d+")


def payload_written_registers(payload: list[str]) -> set[str]:
    written = set()
    for line in payload:
        m = WRITES.match(line)
        if m:
            written.add(m.group(1))
    return written


def registers_safe(lines: list[str], end_index: int, written: set[str]) -> tuple[bool, str]:
    """Best-effort liveness: is any register the payload writes read before being rewritten?"""
    if not written:
        return True, "payload writes no register"
    pending = set(written)
    for raw in lines[end_index + 1 : end_index + 60]:
        line = raw.strip()
        if not line or line.startswith((".line", "#")):
            continue
        m = WRITES.match(raw)
        rewritten = m.group(1) if m else None
        operands = set(READS.findall(line))
        # a read of a pending register before it is rewritten is unsafe
        for reg in sorted(pending):
            if reg in operands and reg != rewritten:
                return False, f"{reg} is read at {line!r} before being rewritten"
        if rewritten in pending:
            pending.discard(rewritten)
        if not pending:
            return True, "all written registers are rewritten before any read"
        if line.startswith(("return", "goto", "throw")):
            break
    return True, "no conflicting read found in the following window"


def validate(decode: Path, operations: list[dict]) -> list[dict]:
    index = class_index(decode)
    results = []
    for op in operations:
        row: dict[str, object] = {"id": op.get("id"), "descriptor": op.get("descriptor")}
        path = index.get(op.get("descriptor", ""))
        row["descriptor_resolves"] = path is not None
        if path is None:
            row["verdict"] = "BROKEN"
            row["reason"] = "descriptor not found in this decode"
            results.append(row)
            continue

        row["smali_path"] = str(Path(path).relative_to(decode))
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()

        raw_anchor = op.get("anchor", [])
        stripped = [a.strip() for a in raw_anchor if a.strip()]
        row["anchor_whitespace_clean"] = raw_anchor == stripped
        matches = find_anchors(lines, stripped)
        row["anchor_occurrences"] = len(matches)
        expected = op.get("expected_anchor_count", 1)
        row["anchor_matches"] = len(matches) > 0
        row["anchor_unique"] = len(matches) == expected

        marker = op.get("marker")
        row["marker_absent"] = (text.count(marker) == 0) if marker else None

        if matches:
            written = payload_written_registers(op.get("payload", []))
            safe, why = registers_safe(lines, matches[0][1], written)
            row["payload_writes"] = sorted(written)
            row["registers_safe"] = safe
            row["registers_note"] = why
        else:
            row["registers_safe"] = None
            row["registers_note"] = "not evaluated: anchor did not match"

        ok = (
            row["descriptor_resolves"]
            and row["anchor_matches"]
            and row["anchor_unique"]
            and (row["marker_absent"] in (True, None))
            and row["registers_safe"] is not False
        )
        row["verdict"] = "OK" if ok else "BROKEN"
        if not ok:
            reasons = []
            if not row["anchor_matches"]:
                reasons.append(
                    "anchor matched 0 times"
                    + ("; anchor has leading whitespace" if not row["anchor_whitespace_clean"] else "")
                )
            elif not row["anchor_unique"]:
                reasons.append(f"anchor matched {len(matches)} times, expected {expected}")
            if row["marker_absent"] is False:
                reasons.append("marker already present (partially applied?)")
            if row["registers_safe"] is False:
                reasons.append(row["registers_note"])
            row["reason"] = "; ".join(reasons)
        results.append(row)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("decode", type=Path, help="clean stock apktool decode of the target")
    parser.add_argument("candidates", type=Path, help="anchored_patches.json-shaped candidates")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    operations = payload["operations"] if isinstance(payload, dict) else payload
    results = validate(args.decode, operations)

    width = max((len(str(r["id"])) for r in results), default=10)
    for r in results:
        print(f"  {r['verdict']:<7} {str(r['id']):<{width}}  {r.get('reason', '')}")
    broken = [r for r in results if r["verdict"] != "OK"]
    print(f"\n{len(results) - len(broken)}/{len(results)} candidates valid")

    if args.output:
        args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
