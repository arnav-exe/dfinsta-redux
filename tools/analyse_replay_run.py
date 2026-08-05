#!/usr/bin/env python3
"""Turn a registered-replay run record into the numbers a design note quotes.

    python tools/analyse_replay_run.py <run-root>/success.json

`tests/integration/test_registered_replay_harness.py` writes `success.json` after
a real port. It carries, per target, a `worker_query_responsiveness` sample every
15 s and a `stage_heartbeats` sample beside it. Every loop-blocking figure in
`docs/WORKFLOW_REGISTRATION_DESIGN.md` -- "23% of query samples answered", "apply
92% blocked", "longest unbroken stretch 28 samples" -- is a reduction of those
two arrays.

**This script exists because those reductions were being done by hand.** The
design note already records the same regret twice: a standalone threading
benchmark whose figures are "not reproducible from the tree" because no script
was committed, and a heartbeat gap read off `temporal activity describe`. A
number nobody can recompute is a number nobody can check, and this project's
whole method is that measurement beats argument.

Reads the record and nothing else -- no ledger, no Temporal connection, no
network. Safe to point at an old run root.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def query_availability(samples: list[dict[str, Any]]) -> tuple[int, int]:
    """(answered, total). The headline number: could the worker respond at all?"""

    return sum(1 for sample in samples if sample["query_answered"]), len(samples)


def blocked_by_activity(samples: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """`activity -> (samples, blocked)`.

    Attributed per activity rather than reported as one percentage, because the
    per-stage split is what corrected the first reading of this measurement: the
    blocking was not the decode's tree capture specifically, it was every stage,
    and the worst offender already ran its subprocess in a thread.
    """

    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for sample in samples:
        name = sample["running_activity"] or "(between stages)"
        tally[name][0] += 1
        tally[name][1] += 0 if sample["query_answered"] else 1
    return {name: (total, blocked) for name, (total, blocked) in tally.items()}


def longest_blocked_stretch(samples: list[dict[str, Any]]) -> int:
    """The most consecutive unanswered samples.

    Matters more than the percentage. A worker that is blocked half the time but
    never for two samples running is responsive; one blocked in half-hour slabs
    is not, and it is the slab that expires a heartbeat.
    """

    longest = current = 0
    for sample in samples:
        current = 0 if sample["query_answered"] else current + 1
        longest = max(longest, current)
    return longest


def worst_heartbeat_gaps(samples: list[dict[str, Any]]) -> dict[str, float]:
    """`stage -> the largest gap it ever reported`. What a timeout is set from.

    The maximum over the run, not the last reading: a stage that reported 30 s
    once and 12 s afterwards is a stage whose timeout must clear 30.
    """

    worst: dict[str, float] = {}
    for sample in samples:
        details = sample.get("details")
        if not isinstance(details, dict):
            continue
        stage = details.get("stage")
        gap = details.get("worst_gap_seconds")
        if isinstance(stage, str) and isinstance(gap, (int, float)):
            worst[stage] = max(worst.get(stage, 0.0), float(gap))
    return worst


def describe(target: dict[str, Any]) -> str:
    samples = target.get("worker_query_responsiveness", [])
    answered, total = query_availability(samples)
    share = f"{answered * 100 // total}%" if total else "n/a"
    lines = [
        f"=== target {target.get('target')} ===",
        f"query samples answered      {answered} of {total} ({share})",
        f"longest blocked stretch     {longest_blocked_stretch(samples)} samples",
        "",
        f"{'activity':<48}{'samples':>9}{'blocked':>9}",
    ]
    for name, (count, blocked) in sorted(blocked_by_activity(samples).items()):
        lines.append(f"{name:<48}{count:>9}{blocked * 100 // count:>8}%")

    heartbeats = target.get("stage_heartbeats", [])
    recorded = target.get("worst_heartbeat_gap_seconds") or worst_heartbeat_gaps(heartbeats)
    lines.append("")
    if recorded:
        lines.append(f"{'stage':<24}{'worst gap (s)':>15}{'beats':>8}")
        beats: dict[str, int] = defaultdict(int)
        for sample in heartbeats:
            details = sample.get("details")
            if isinstance(details, dict) and isinstance(details.get("stage"), str):
                stage = details["stage"]
                beats[stage] = max(beats[stage], int(details.get("beats", 0)))
        for stage, gap in sorted(recorded.items()):
            lines.append(f"{stage:<24}{gap:>15}{beats.get(stage, 0):>8}")
    else:
        # Said rather than shown as zeros. Runs before 2026-08-05 carry no
        # heartbeat samples at all, and "no gaps recorded" must not read as
        # "no gaps occurred" -- the same three-state distinction
        # `verify_build.py` draws between not-asked, refused and checked.
        lines.append("no heartbeat samples in this record (run predates the sampler)")

    history = target.get("history", {})
    lines.append("")
    lines.append(
        f"history {history.get('json_bytes')} bytes, "
        f"within budget {history.get('within_budget')}"
    )
    return "\n".join(lines)


def targets_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    if "target_evidence" in record:
        return list(record["target_evidence"])
    return [record]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("record", type=Path, help="a run root's success.json")
    args = parser.parse_args(argv)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"refused: cannot read {args.record}: {error}", file=sys.stderr)
        return 2
    if not isinstance(record, dict):
        print(f"refused: {args.record} is not a run record", file=sys.stderr)
        return 2
    for target in targets_of(record):
        print(describe(target))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
