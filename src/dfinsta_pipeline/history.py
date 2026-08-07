"""The port history as a series, pinned at 439 and extending forward.

    python -m dfinsta_pipeline.history

Everything else in this pipeline reads **pairwise**. `differential` compares N to
N-1; `agent_cost report` compares N to N-1. Neither can answer "is this getting
worse", because a comparison of two points is a change and nothing more.

**This exists because of a specific mistake.** On 2026-08-07 the Reels
selectivity margins read `5->1` (439), `7->1` (440), `4->1` (441) and I called it
a trend that had turned. Three points, two of them adjacent minor releases, is
not a trend: `4->1` could as easily be a refactor artefact. The owner's
correction is the standing rule this module implements — *pin the history at 439,
extend it forward, and track decisions across versions rather than off the most
recent one*.

**Why 439 is the floor and not an accident.** It is where the durable evidence
begins: `manifest/runtime_evidence/`, `manifest/differentials/` and the cost
ledger all start there. Reaching further back mixes architectures — the docs
already establish that 430 and 439 are "closer to one data point" than two,
because both keys of the self-profile rule fail together on 340 as consequences
of one rewrite. A series that spans that boundary is not a series.

===============================================================================
  WHAT THIS REFUSES TO DO
===============================================================================

**It will not call anything a trend.** It prints the points and the count of
points, and says in the output how many a direction would need. Naming a
direction from three samples is the error it was written for; a tool that did it
automatically would be that error with a command line.

**It computes no verdicts.** Every value is read from a durable record —
`agent_cost.jsonl`, `manifest/runtime_evidence/`, `manifest/differentials/`. The
per-version verdicts belong to the modules that own them.

**It reports gaps as gaps.** A version with no runtime evidence prints as absent,
not as zero. The difference matters: 439 recorded no identity claims at all, and
reading that as "no hooks ran" rather than "that shape was never captured" is
what made the first differential compare 2 of 7.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["HistoryError", "PortPoint", "series", "render", "BASELINE_VERSION", "main"]


class HistoryError(RuntimeError):
    """Raised when the history cannot be read honestly."""


#: The first version of the comparable series. See the module docstring: this is
#: an architectural boundary, not a convenience.
#: Anything that is not a release number is refused rather than compared.
_NUMERIC = re.compile(r"\d+")

BASELINE_VERSION = "439"

#: How many points before a direction is worth naming. Three adjacent releases is
#: a change; this is the threshold at which the word "trend" stops being an
#: impression. Deliberately a constant with a reason rather than a rule of thumb
#: buried in prose.
POINTS_FOR_A_DIRECTION = 5


@dataclass(frozen=True)
class PortPoint:
    """One version's durable record, as far as it goes."""

    version: str
    agent_invocations: int | None = None
    hooks_costed: int | None = None
    #: `runtime_probe` verdicts, or None when no evidence file exists for this
    #: version at all — absent and zero are different facts.
    runtime: Counter | None = None
    shapes: dict[str, set[str]] = field(default_factory=dict)
    #: `differential` verdicts against the previous version, keyed `N-1 -> N`.
    differential: Counter | None = None
    selectivity: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "agent_invocations": self.agent_invocations,
            "hooks_costed": self.hooks_costed,
            "runtime": dict(self.runtime) if self.runtime is not None else None,
            "shapes": {hook: sorted(kinds) for hook, kinds in sorted(self.shapes.items())},
            "differential": (
                dict(self.differential) if self.differential is not None else None
            ),
            "selectivity": dict(self.selectivity),
        }


def _shape_of(detail: dict[str, Any]) -> str:
    """Which probe shape a claim is, read the way `differential` reads it.

    Duplicated deliberately rather than imported: `differential.probe_shape`
    takes a claim object and this reads raw rows straight off disk, and the
    alternative -- constructing claims to classify them -- would make a reporting
    tool fail on a corpus the ledger would refuse for unrelated reasons.
    """

    if "hooks_that_ran" in detail:
        return "identity"
    if "signal" in detail:
        return "delta"
    if "control" in detail:
        return "absence"
    return "unknown"


def _field(row: dict[str, Any], name: str, path: Path, number: int) -> Any:
    """Read a required field, naming where it was missing.

    `_rows` already refuses unparseable JSON with a path and a line number, and a
    row that parses but lacks `hook_id` came back as a bare `KeyError` with
    neither. Two ways to be malformed, one of them findable.
    """

    try:
        return row[name]
    except KeyError:
        raise HistoryError(f"{path}:{number}: claim has no {name!r}") from None


def _instant(stamp: str) -> str:
    """A timestamp comparable across the two spellings the ledger actually holds."""

    return stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp


def _rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise HistoryError(f"{path}:{number}: {error}") from error
    return out


def _cost_points(ledger: Path) -> dict[str, tuple[int, int]]:
    """`version -> (agent invocations, hooks)` for the LATEST run of each version.

    The latest, not the aggregate: two attempts at one version are not two ports,
    and a metric that inflates with retries flatters or damns by accident.
    """

    if not ledger.is_file():
        return {}
    runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _rows(ledger):
        record = row.get("record", row)
        runs.setdefault((record["version"], record["recorded_at"]), []).append(record)
    # Normalised before comparing. The ledger holds BOTH spellings -- 439's runs
    # end `+00:00` and everything from 440 on ends `Z` -- and at the same instant
    # "Z" (0x5A) sorts after "." (0x2E), so a string compare picks by spelling
    # rather than by time. Narrow today because the spellings do not overlap
    # within a version, and wrong the moment they do.
    latest: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for (version, stamp), records in runs.items():
        key = _instant(stamp)
        if version not in latest or key > latest[version][0]:
            latest[version] = (key, records)
    return {
        version: (sum(1 for r in records if r.get("needed_agent")), len(records))
        for version, (_, records) in latest.items()
    }


def _selectivity(ledger: Path) -> dict[str, dict[str, str]]:
    """`version -> {hook: "candidates -> hits"}`, the margin that erodes first.

    The count reaching zero is the loud failure; a fingerprint narrowing toward
    `1 -> 1` is the quiet one that precedes it, which is the whole reason this is
    carried per version rather than compared pairwise.
    """

    if not ledger.is_file():
        return {}
    # LATEST RUN ONLY, matching `_cost_points`. Reading every row let a margin
    # measured on an earlier attempt survive into the version's column when the
    # latest run measured none -- so the number this module's own docstring calls
    # "the quiet one that precedes the loud failure" could silently be one build
    # old, which is the opposite of what it is for.
    runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _rows(ledger):
        record = row.get("record", row)
        runs.setdefault((record["version"], record["recorded_at"]), []).append(record)
    latest: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for (version, stamp), records in runs.items():
        key = _instant(stamp)
        if version not in latest or key > latest[version][0]:
            latest[version] = (key, records)

    out: dict[str, dict[str, str]] = {}
    for version, (_, records) in latest.items():
        for record in records:
            for item in record.get("selectivity") or ():
                if isinstance(item, dict) and "candidates" in item and "hits" in item:
                    out.setdefault(version, {})[record["hook_id"]] = (
                        f"{item['candidates']} -> {item['hits']}"
                    )
    return out


def series(root: Path | str = ".", *, baseline: str = BASELINE_VERSION) -> list[PortPoint]:
    """Every version from `baseline` forward, in release order."""

    root = Path(root)
    costs = _cost_points(root / "manifest" / "agent_cost.jsonl")
    margins = _selectivity(root / "manifest" / "agent_cost.jsonl")

    if not _NUMERIC.fullmatch(baseline):
        raise HistoryError(f"baseline {baseline!r} is not a version number")

    evidence_dir = root / "manifest" / "runtime_evidence"
    # Both sides filtered to numeric. The evidence side always was; `costs` was
    # not, so one non-numeric row in the ledger took the whole tool down with a
    # ValueError from the comparison below.
    versions = {
        path.stem for path in evidence_dir.glob("*.jsonl") if _NUMERIC.fullmatch(path.stem)
    } | {v for v in costs if _NUMERIC.fullmatch(v)}
    # `key=int`, because these are release numbers. Sorted as strings, a series
    # containing 1000 orders it FIRST -- the header would read "1000 -> 441" and
    # every point's differential would be looked up against the wrong
    # predecessor. Today's three-digit arc is ordered correctly by luck.
    ordered = sorted(
        (v for v in versions if int(v) >= int(baseline)), key=int
    )
    if not ordered:
        raise HistoryError(f"no version at or after {baseline}")

    points: list[PortPoint] = []
    for version in ordered:
        runtime: Counter | None = None
        shapes: dict[str, set[str]] = {}
        evidence = evidence_dir / f"{version}.jsonl"
        if evidence.is_file():
            rows = [row.get("record", row) for row in _rows(evidence)]
            # HOOKS, not claims. Counting rows made 440 read 13 passed against
            # 441's 6 and looked like a regression, when 440 simply holds 23
            # claims to 441's 9 -- the difference is re-measurement, and one of
            # those retry sequences is the thing the ledger's retry guard exists
            # to refuse. A version that measured twice must not outscore one that
            # got it right first time. Same error the cost metric made counting
            # runs instead of ports.
            pairs = [
                (_field(row, "hook_id", evidence, n), _field(row, "verdict", evidence, n))
                for n, row in enumerate(rows, 1)
            ]
            passed = {hook for hook, verdict in pairs if verdict == "passed"}
            measured = {hook for hook, _ in pairs}
            runtime = Counter({
                "passed": len(passed),
                "no_pass": len(measured - passed),
                "claims": len(rows),
            })
            for row in rows:
                shapes.setdefault(row["hook_id"], set()).add(_shape_of(row.get("detail", {})))

        differential: Counter | None = None
        previous = points[-1].version if points else None
        if previous is not None:
            pair = root / "manifest" / "differentials" / f"{previous}-{version}.jsonl"
            if pair.is_file():
                differential = Counter(
                    row.get("record", row)["verdict"] for row in _rows(pair)
                )

        invocations, hooks = costs.get(version, (None, None))
        points.append(
            PortPoint(
                version=version,
                agent_invocations=invocations,
                hooks_costed=hooks,
                runtime=runtime,
                shapes=shapes,
                differential=differential,
                selectivity=margins.get(version, {}),
            )
        )
    return points


def _cell(value: Any) -> str:
    return "—" if value is None else str(value)


def render(points: Iterable[PortPoint]) -> str:
    points = list(points)
    if not points:
        # Refused, not rendered as an empty table. `series` never returns empty,
        # so this can only be a caller that filtered its own list -- and a
        # returned sentence would be *printed*, reading as "the history is empty"
        # for a series that has three versions on disk. Surfacing the caller's bug
        # beats formatting around it.
        raise HistoryError("no versions to render")
    versions = [point.version for point in points]
    lines = [
        f"PORT HISTORY  {versions[0]} → {versions[-1]}   ({len(points)} points)",
        "=" * 60,
        "",
        f"  {'':<22}" + "".join(f"{v:>9}" for v in versions),
        f"  {'agent invocations':<22}"
        + "".join(f"{_cell(p.agent_invocations):>9}" for p in points),
        f"  {'hooks costed':<22}" + "".join(f"{_cell(p.hooks_costed):>9}" for p in points),
        f"  {'hooks runtime-passed':<22}"
        + "".join(
            f"{(_cell(None) if p.runtime is None else p.runtime['passed']):>9}"
            for p in points
        ),
        f"  {'hooks without a pass':<22}"
        + "".join(
            f"{(_cell(None) if p.runtime is None else p.runtime['no_pass']):>9}"
            for p in points
        ),
        f"  {'  (claims recorded)':<22}"
        + "".join(
            f"{(_cell(None) if p.runtime is None else p.runtime['claims']):>9}"
            for p in points
        ),
        f"  {'differential passed':<22}"
        + "".join(
            f"{(_cell(None) if p.differential is None else p.differential.get('passed', 0)):>9}"
            for p in points
        ),
    ]

    hooks = sorted({hook for point in points for hook in point.selectivity})
    if hooks:
        lines += ["", "  selectivity margins   candidates -> hits"]
        for hook in hooks:
            row = "".join(
                f"{point.selectivity.get(hook, '—'):>16}" for point in points
            )
            lines.append(f"    {hook[:38]:<38}{row}")

    lines += ["", "  probe shapes recorded, per hook"]
    hooks = sorted({hook for point in points for hook in point.shapes})
    for hook in hooks:
        cells = []
        for point in points:
            kinds = point.shapes.get(hook)
            cells.append(f"{(','.join(sorted(kinds)) if kinds else '—')[:15]:>16}")
        lines.append(f"    {hook[:38]:<38}{''.join(cells)}")

    lines += [""]
    if len(points) < POINTS_FOR_A_DIRECTION:
        # Said every time, not only when a number happens to move. The mistake
        # this module exists for was made while looking at exactly this many
        # points, and the guard is only worth anything before someone is tempted.
        lines.append(
            f"  {len(points)} points. A direction is not worth naming below "
            f"{POINTS_FOR_A_DIRECTION} — two adjacent minor releases moving the same way is a"
        )
        lines.append(
            "  change, not a trend. Read the columns; do not describe the slope."
        )
    else:
        lines.append(
            f"  {len(points)} points, enough to discuss direction. Still say which "
            "versions a claim rests on."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--baseline",
        default=BASELINE_VERSION,
        help=f"first version of the series (default {BASELINE_VERSION}; earlier "
        "versions are a different architecture and are not comparable)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        points = series(args.root, baseline=args.baseline)
    # `ValueError` and `OSError` alongside `HistoryError`: `--baseline nope` used
    # to reach `int()` outside the refusal channel and leave as a traceback, and
    # an unreadable manifest directory did the same. A typo on a flag is the most
    # ordinary way this tool is used wrongly, and it is the one that did not get a
    # `refused:`. Third module to ship that gap.
    except (HistoryError, ValueError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([point.to_dict() for point in points], indent=2))
    else:
        print(render(points))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
