"""`history.py` is the only view of the port record that is not pairwise.

Everything else in this pipeline compares N to N-1 — `differential` does, and so
does `agent_cost report`. This module reads the arc instead, and it exists
because of a specific mistake the owner corrected: three points were called a
trend. So its correctness is not "does it add up"; it is whether the four ways a
series view can quietly lie are each closed.

**Re-measuring must not raise a score.** :class:`HooksNotClaimsTests`. The
module was already fixed for this once — counting rows made 440 read 13 passed
against 441's 6, which is not 440 doing better, it is 440 holding 23 claims to
441's 9. The discriminating fixture is two versions with *identical* hook
outcomes and different claim counts: any counter that reads rows separates them,
and the right one does not. The real 440 retry sequence
(`inconclusive, passed, passed` on `install_settings_long_click`) is here by
name, because a hook that went green on the third attempt is the exact row a
row-counter scores three times.

**Absent must not read as zero.** :class:`AbsentIsNotZeroTests`. This is the
module docstring's own stated reason for existing: 439 recorded no identity
claims at all, and reading that as "no hooks ran" rather than "that shape was
never captured" is what made the first differential compare 2 of 7. Every test
here carries a positive control in the same assertion — a version whose evidence
file exists and holds nothing renders `0`, a version with no file at all renders
`—`. Without the control, "it printed a dash" is satisfied by a tool that prints
dashes.

**The floor is architectural.** :class:`BaselineFloorTests`. 340 and 430 are
really in `manifest/decisions.jsonl` — the exclusion is doing work, not
describing an empty set — and the tests check the ledgers hold them before
checking the series does not. `--baseline` moves the floor in both directions,
because a floor that only moved forward could be a coincidence of the fixture.

**A retry is not a port.** :class:`LatestRunNotAggregateTests`. The cost ledger
holds two runs for 439 and two for 441. Aggregating them makes 439 cost four
agent invocations and fourteen hooks, and there is no honest reading of "this
port needed four agents". The direction control matters as much as the count: a
*later and worse* run must be the one reported, or "take the best" passes too.

**The guard against naming a direction.** :class:`DirectionGuardTests`. The
threshold is never written as `5` in this file; every assertion is built from
`POINTS_FOR_A_DIRECTION`, and one test finds the point count at which the message
flips and requires it to *be* the constant. The guard is also required to fire on
flat data at every count below the threshold, because the mistake it exists for
was made while looking at exactly three points — nothing had to move for the
error to be available.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

:class:`CommittedDataTests` reads the repository itself rather than a fixture,
because the numbers it pins are historical facts and not arithmetic: `2, 4, 4`
runtime-passed, `2, 0, 0` agent invocations, `7, 23, 9` claims, and 439 having
recorded no `identity` claim for any hook. That last one is the bound on the
first differential and cannot be re-derived from anything smaller.

`series(".")` returning exactly `["439", "440", "441"]` is a deliberate
tripwire, not an oversight: the module's rule is *extend the series forward*, and
a port that adds 442 without anyone updating the recorded arc is the failure
`docs` calls a stale record. When 442 lands, these numbers grow by one column.

Everything else is a temp tree written row by row. The hook ids, verdicts,
timestamp formats and selectivity shapes are copied from
`manifest/agent_cost.jsonl` and `manifest/runtime_evidence/*.jsonl`, including
the ledger's two timestamp spellings (`…+00:00` on 439 and `…Z` from 440 on) —
that difference is not cosmetic, see :class:`KnownDefectTests`.

===============================================================================
  MUTATION RESULTS
===============================================================================

Seven mutations were run out of tree, one at a time against a fresh copy, with
the unmutated copy passing first as the control every time and again at the end.
The bracketed number is how many distinct tests here failed.

* `passed` counts claims rather than distinct hooks [6] →
  :class:`HooksNotClaimsTests` (4) and :class:`CommittedDataTests` (2)
* a missing evidence file yields an empty `Counter` instead of `None` [5] →
  :class:`AbsentIsNotZeroTests` (4) and :class:`CliTests` (1)
* the baseline filter is dropped [7] → :class:`BaselineFloorTests` (4),
  :class:`CliTests` (2) and the first-point differential rule
* every run of a version is aggregated instead of taking the latest [5] →
  :class:`LatestRunNotAggregateTests` (3) and :class:`CommittedDataTests` (2)
* the points-threshold comparison is inverted [7] →
  :class:`DirectionGuardTests` (6) and :class:`CommittedDataTests` (1)

The sort mutation is the odd one, because the defect is already present: writing
`sorted(...)` without a key is what the module does. It was run in both
directions instead.

* the floor compares version strings instead of integers (`v >= baseline`) [3] →
  :meth:`ReleaseOrderTests.
  test_a_version_past_999_is_in_the_series_because_the_floor_compares_numbers`
  fails, and two of the `int()` defects below stop being reachable and report as
  unexpected successes, which is the right signal for the wrong reason
* the sort is *fixed* to `key=int` [1] → :meth:`KnownDefectTests.
  test_versions_sort_numerically_rather_than_as_strings` reports an unexpected
  success, which `wasSuccessful()` treats as a failure. That is the detection
  channel for this one: the fix announces itself here.

===============================================================================
  KNOWN DEFECTS
===============================================================================

:class:`KnownDefectTests` are `expectedFailure` on purpose, the same way
`tests/test_probes.py` records its own. Each asserts what this module's
docstrings say must happen and what it does not yet do, so the suite stays green
today and reports an *unexpected success* the moment one is fixed. They are, in
order of how easily a human can trip them:

1. Versions are sorted as strings. `sorted(v for v in versions if …)` has no
   key, so 1000 sorts before 439 and the series is printed out of release order.
   The filter beside it *is* numeric, so the wrong version is not excluded —
   only mis-placed, which is the harder failure to notice.
2. `--baseline nope` is a `ValueError` traceback, not `refused: …` and exit 2.
   Reachable from the command line by a typo.
3. A version the cost ledger spells non-numerically is the same traceback.
   Evidence filenames are filtered by `\\d+`; ledger versions are not.
4. An evidence row missing `hook_id` is a bare `KeyError`, with no file and no
   line number, though `_rows` already refuses unparseable JSON with both.
5. A selectivity margin from an *earlier* run is carried into the version's
   column even when the latest run measured none — `_cost_points` takes the
   latest run and `_selectivity` takes the last row of the file, and the margin
   is the value the module says erodes first.
6. `recorded_at` is compared as a string across the ledger's two live timestamp
   spellings, so two runs inside one second are ordered by punctuation.
7. `render([])` is an `IndexError`. Unreachable through `main`, and `render` is
   in `__all__`.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Sequence

from dfinsta_pipeline import history
from dfinsta_pipeline.history import (
    BASELINE_VERSION,
    POINTS_FOR_A_DIRECTION,
    HistoryError,
    PortPoint,
    render,
    series,
)

#: The repository this test file lives in. Used instead of `"."` so the
#: committed-data tests do not depend on the working directory of the runner;
#: one test chdirs here on purpose to exercise the documented `series(".")`.
REPO = Path(__file__).resolve().parents[1]

#: The seven hooks of the manifest, in the order the ledgers record them.
HOOKS = (
    "set_app_context",
    "tigon_url_block",
    "replace_reels_discover_endpoint",
    "replace_reels_homecoming_endpoint",
    "replace_reels_stream_endpoint",
    "install_settings_long_click",
    "install_settings_long_click_actionbar",
)

DEVICE = "device:P3227J000775"

#: The two spellings the real cost ledger uses. 439 was recorded with
#: `datetime.isoformat()` and everything from 440 with a `Z` suffix.
STAMP_439_FIRST = "2026-08-02T16:12:40.616633+00:00"
STAMP_439_SECOND = "2026-08-02T16:42:05.463572+00:00"
STAMP_440 = "2026-08-06T18:52:42Z"


# ----------------------------------------------------------------- row builders


def evidence_row(hook_id: str, verdict: str = "passed", *, shape: str = "identity") -> dict:
    """One `runtime_probe` row, in the flat on-disk shape the real files use.

    `detail` decides the probe shape and nothing else does, which is why the
    three keys are spelled out here rather than passed in: `hooks_that_ran` is an
    identity probe, `signal` a delta probe, `control` an absence probe. That
    classification is duplicated inside `history` on purpose (see `_shape_of`),
    so a fixture that named the shape directly would test nothing.
    """
    detail = {
        "identity": {"hooks_that_ran": [hook_id], "proven": True},
        "delta": {"signal": "DFInsta", "enabled_observations": 3, "hits": 3},
        "absence": {"control": r"Start proc \d+:com\.instagram\.android", "hits": 0},
        "unknown": {"note": "a shape no reader recognises"},
    }[shape]
    return {
        "actor": DEVICE,
        "detail": detail,
        "hook_id": hook_id,
        "kind": "runtime_probe",
        "producer": "device",
        "schema_version": 1,
        "summary": f"{hook_id} probed as {shape}",
        "verdict": verdict,
    }


def margin(candidates: int, hits: int) -> dict:
    """A `selectivity` item in the ledger's own shape."""
    return {
        "candidates": candidates,
        "detail": {},
        "hits": hits,
        "margin": candidates - hits,
        "measure": "classes containing the least selective literal alone -> all of them",
        "subject": "by_literal",
    }


def cost_row(
    version: str,
    stamp: str,
    hook_id: str,
    *,
    needed_agent: bool = False,
    selectivity: Sequence[dict] = (),
) -> dict:
    """One `hook_cost` row, wrapped in the `record` envelope the real ledger uses.

    Only five fields are read by `history` — version, recorded_at, hook_id,
    needed_agent, selectivity — but the envelope is real because `_cost_points`
    unwraps it and a flat fixture would never exercise that.
    """
    return {
        "kind": "hook_cost",
        "record": {
            "agent_for": ["host"] if needed_agent else [],
            "attempts": [],
            "hook_id": hook_id,
            "needed_agent": needed_agent,
            "note": "",
            "outcome": "resolved",
            "recorded_at": stamp,
            "route": "agent_proposal" if needed_agent else "mechanical",
            "selectivity": list(selectivity),
            "version": version,
        },
        "schema_version": 1,
    }


def differential_row(hook_id: str, verdict: str = "passed") -> dict:
    return {
        "actor": DEVICE,
        "detail": {"comparison": "same_shape"},
        "hook_id": hook_id,
        "kind": "differential",
        "producer": "device",
        "schema_version": 1,
        "summary": f"{hook_id} compared",
        "verdict": verdict,
    }


# ------------------------------------------------------------- reading the table


#: `render` lays every numeric row out as a 22-column label after two spaces,
#: then one nine-column right-aligned cell per version.
LABEL_WIDTH = 22
CELL_WIDTH = 9


def row_cells(text: str, label: str) -> list[str]:
    """The per-version cells of one labelled row of `render`'s table.

    Split rather than substring-matched: `assertIn("—", text)` would pass on the
    dash belonging to a different row, and the whole point of the absent-vs-zero
    tests is *which* cell says which.
    """
    prefix = f"  {label:<{LABEL_WIDTH}}"
    for line in text.splitlines():
        if line.startswith(prefix):
            body = line[len(prefix) :]
            return [
                body[start : start + CELL_WIDTH].strip()
                for start in range(0, len(body), CELL_WIDTH)
            ]
    raise AssertionError(f"no row labelled {label!r} in:\n{text}")


def shape_cells(text: str, hook_id: str) -> list[str]:
    """The per-version cells of one hook's row in the probe-shapes block."""
    marker = f"    {hook_id[:38]:<38}"
    for line in text.splitlines():
        if line.startswith(marker):
            body = line[len(marker) :]
            return [body[start : start + 16].strip() for start in range(0, len(body), 16)]
    raise AssertionError(f"no shape row for {hook_id!r} in:\n{text}")


class HistoryTestCase(unittest.TestCase):
    """A temp manifest tree, JSONL writers, and a way to run `main` in-process."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.manifest = self.tmp / "manifest"
        (self.manifest / "runtime_evidence").mkdir(parents=True)
        (self.manifest / "differentials").mkdir(parents=True)

    def write_evidence(self, version: str, rows: Iterable[dict]) -> Path:
        path = self.manifest / "runtime_evidence" / f"{version}.jsonl"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def write_costs(self, rows: Iterable[dict]) -> Path:
        path = self.manifest / "agent_cost.jsonl"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def write_differential(self, previous: str, current: str, rows: Iterable[dict]) -> Path:
        path = self.manifest / "differentials" / f"{previous}-{current}.jsonl"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def series(self, **kwargs: Any) -> list[PortPoint]:
        return series(self.tmp, **kwargs)

    def render(self, **kwargs: Any) -> str:
        return render(self.series(**kwargs))

    def by_version(self, **kwargs: Any) -> dict[str, PortPoint]:
        return {point.version: point for point in self.series(**kwargs)}

    def run_main(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = history.main(["--root", str(self.tmp), *args])
        return code, stdout.getvalue(), stderr.getvalue()


# ========================================================== hooks, not claims


class HooksNotClaimsTests(HistoryTestCase):
    """A version that measured twice must not outscore one that got it right once.

    This is the defect the module was already fixed for, and the fix is one
    `set()` in `series` that any later edit can drop without breaking anything
    else — the table still renders, the numbers still look plausible, and 440
    reads as better than 441 because it was probed three times. So the fixtures
    here are built to make the two readings disagree loudly: identical outcomes,
    different claim counts.
    """

    def test_a_hook_measured_three_times_counts_once_under_passed(self):
        """The real 440 retry sequence: inconclusive, then passed, then passed.

        `install_settings_long_click` on 440 was probed three times across three
        walkthroughs and the first was inconclusive. Counting rows scores that
        hook twice under `passed`; counting hooks scores it once, which is the
        only number that answers "does this hook work". The other two hooks are
        here so the total cannot be right by accident.
        """
        self.write_evidence(
            "440",
            [
                evidence_row("install_settings_long_click", "inconclusive"),
                evidence_row("install_settings_long_click", "passed"),
                evidence_row("install_settings_long_click", "passed"),
                evidence_row("set_app_context", "passed"),
                evidence_row("replace_reels_discover_endpoint", "inconclusive"),
            ],
        )

        runtime = self.by_version()["440"].runtime

        self.assertEqual(runtime["passed"], 2)
        self.assertEqual(runtime["no_pass"], 1)
        # The claim count is reported too, so the difference is visible rather
        # than hidden: five rows, three hooks.
        self.assertEqual(runtime["claims"], 5)

    def test_re_measuring_cannot_raise_a_versions_score(self):
        """Three hooks measured once, against the same three measured three times.

        The two versions are the same port by every outcome that matters. If
        `passed` differed between them the metric would be rewarding the number
        of walkthroughs somebody ran, and 440 versus 441 is exactly that
        comparison on the real tree.
        """
        outcomes = {
            "set_app_context": "passed",
            "tigon_url_block": "passed",
            "install_settings_long_click_actionbar": "inconclusive",
        }
        self.write_evidence(
            "439", [evidence_row(hook, verdict) for hook, verdict in outcomes.items()]
        )
        self.write_evidence(
            "440",
            [
                evidence_row(hook, verdict)
                for hook, verdict in outcomes.items()
                for _ in range(3)
            ],
        )

        points = self.by_version()

        self.assertEqual(points["439"].runtime["passed"], points["440"].runtime["passed"])
        self.assertEqual(points["439"].runtime["no_pass"], points["440"].runtime["no_pass"])
        self.assertEqual(points["439"].runtime["passed"], 2)
        # The positive control: the fixture really does re-measure. Without this
        # the equality above would also hold for two identical files.
        self.assertEqual(points["439"].runtime["claims"], 3)
        self.assertEqual(points["440"].runtime["claims"], 9)

    def test_a_hook_that_never_passed_is_counted_once_under_no_pass(self):
        """The same rule on the other side of the partition.

        A fix that de-duplicated only the passing set would leave a hook probed
        three times contributing three to `no_pass`, and "hooks without a pass"
        would exceed the number of hooks.
        """
        self.write_evidence(
            "439",
            [evidence_row("install_settings_long_click_actionbar", "inconclusive")] * 3,
        )

        runtime = self.by_version()["439"].runtime

        self.assertEqual(runtime["no_pass"], 1)
        self.assertEqual(runtime["passed"], 0)
        self.assertEqual(runtime["claims"], 3)

    def test_passed_and_no_pass_partition_the_hooks_and_never_the_claims(self):
        """The invariant that makes the table readable as "of seven hooks, four".

        Stated as an identity against the distinct hook ids in the fixture rather
        than against a literal, so it holds whatever the fixture becomes. A reader
        who sees `4` above `3` and knows the port has seven hooks has read the
        table correctly only while this is true.
        """
        rows = [
            evidence_row("set_app_context", "passed"),
            evidence_row("set_app_context", "passed"),
            evidence_row("tigon_url_block", "inconclusive"),
            evidence_row("tigon_url_block", "passed"),
            evidence_row("replace_reels_stream_endpoint", "inconclusive"),
            evidence_row("replace_reels_stream_endpoint", "failed"),
        ]
        self.write_evidence("439", rows)

        runtime = self.by_version()["439"].runtime

        self.assertEqual(
            runtime["passed"] + runtime["no_pass"], len({row["hook_id"] for row in rows})
        )
        self.assertEqual(runtime["passed"] + runtime["no_pass"], 3)
        self.assertNotEqual(runtime["passed"] + runtime["no_pass"], runtime["claims"])

    def test_a_hook_is_no_pass_only_if_no_claim_of_its_passed(self):
        """One pass anywhere in the sequence is a pass; the order does not matter.

        Both orderings are asserted in one test because the two are the same fact
        and a reader should not have to check whether the module happened to take
        the first row or the last. `final_report` deliberately reads the same
        sequence the *other* way for release readiness — a probe that went green
        on a retry is not release-ready there — and the two are not in conflict:
        this row says "the hook has been shown to work", that one says "the last
        thing we saw was not a clean run".
        """
        self.write_evidence(
            "439",
            [
                evidence_row("tigon_url_block", "inconclusive"),
                evidence_row("tigon_url_block", "passed"),
            ],
        )
        self.write_evidence(
            "440",
            [
                evidence_row("tigon_url_block", "passed"),
                evidence_row("tigon_url_block", "inconclusive"),
            ],
        )

        points = self.by_version()

        self.assertEqual(points["439"].runtime["passed"], 1)
        self.assertEqual(points["440"].runtime["passed"], 1)
        self.assertEqual(points["439"].runtime["no_pass"], 0)
        self.assertEqual(points["440"].runtime["no_pass"], 0)

    def test_the_rendered_rows_carry_the_hook_counts_and_the_claim_count_apart(self):
        """Through `render`, because the table is what a human actually reads.

        The claim count is printed on its own indented line rather than folded
        into the hook counts, which is the whole reason the earlier confusion was
        possible to detect at all.
        """
        self.write_evidence(
            "440",
            [evidence_row("set_app_context", "passed")] * 4
            + [evidence_row("tigon_url_block", "inconclusive")] * 2,
        )

        text = self.render()

        self.assertEqual(row_cells(text, "hooks runtime-passed"), ["1"])
        self.assertEqual(row_cells(text, "hooks without a pass"), ["1"])
        self.assertEqual(row_cells(text, "  (claims recorded)"), ["6"])


# ========================================================== absent is not zero


class AbsentIsNotZeroTests(HistoryTestCase):
    """A gap must print as a gap. The module's stated reason for existing.

    439 recorded no identity claims, and reading that as "no hooks ran" rather
    than "that shape was never captured" is what made the first differential
    compare 2 of 7. Every test below pairs the absent case with a genuinely-zero
    case in the same series, because a tool that printed `—` unconditionally
    would satisfy half of each of these on its own.
    """

    def test_a_version_with_no_evidence_file_has_runtime_none_not_an_empty_counter(self):
        """`None` and `Counter()` are both falsy, which is how this gets lost.

        440 is in the cost ledger and has no evidence file; 441 has a file
        holding nothing. Only the first is absent. Anyone writing
        `if point.runtime:` gets the same answer for both, so the type is
        asserted rather than the truthiness.
        """
        self.write_costs(
            [cost_row("440", STAMP_440, "set_app_context")]
            + [cost_row("441", STAMP_440, "set_app_context")]
        )
        self.write_evidence("441", [])

        points = self.by_version()

        self.assertIsNone(points["440"].runtime)
        self.assertIsNotNone(points["441"].runtime)
        self.assertEqual(points["441"].runtime["passed"], 0)
        self.assertEqual(points["441"].runtime["claims"], 0)

    def test_the_absent_runtime_cells_are_dashes_and_the_empty_ones_are_zeros(self):
        """One rendered table, three rows, both readings side by side.

        `0` in this column is a claim — "we measured and nothing passed". `—` is
        the refusal to make it. Printing `0` for 440 here would be the reporting
        equivalent of the first differential's mistake.
        """
        self.write_costs(
            [
                cost_row("439", STAMP_439_SECOND, "set_app_context"),
                cost_row("440", STAMP_440, "set_app_context"),
                cost_row("441", STAMP_440, "set_app_context"),
            ]
        )
        self.write_evidence("439", [evidence_row("set_app_context", "passed")])
        self.write_evidence("441", [])

        text = self.render()

        self.assertEqual(row_cells(text, "hooks runtime-passed"), ["1", "—", "0"])
        self.assertEqual(row_cells(text, "hooks without a pass"), ["0", "—", "0"])
        self.assertEqual(row_cells(text, "  (claims recorded)"), ["1", "—", "0"])

    def test_a_missing_differential_is_none_and_a_differential_of_no_passes_is_zero(self):
        """The same distinction on the column the first differential got wrong.

        440 has no differential file against 439. 441 has one whose every row is
        inconclusive, so `Counter.get("passed", 0)` is a real zero. The two cells
        must not agree.
        """
        for version in ("439", "440", "441"):
            self.write_evidence(version, [evidence_row("set_app_context", "passed")])
        self.write_differential(
            "440", "441", [differential_row(hook, "inconclusive") for hook in HOOKS]
        )

        points = self.by_version()
        text = self.render()

        self.assertIsNone(points["440"].differential)
        self.assertEqual(points["441"].differential["inconclusive"], 7)
        self.assertEqual(row_cells(text, "differential passed"), ["—", "—", "0"])

    def test_the_first_point_of_the_series_has_no_differential_by_construction(self):
        """Not a gap in the record — there is no predecessor inside the series.

        The file `439-440.jsonl` exists in this fixture, and with `--baseline 440`
        the series starts at 440 and must still print `—` rather than reaching
        outside its own span for a comparison against a version it has excluded
        as architecturally incomparable.
        """
        for version in ("439", "440", "441"):
            self.write_evidence(version, [evidence_row("set_app_context", "passed")])
        self.write_differential("439", "440", [differential_row("set_app_context")])
        self.write_differential("440", "441", [differential_row("set_app_context")])

        full = self.by_version()
        clipped = self.by_version(baseline="440")

        self.assertEqual(full["440"].differential["passed"], 1)
        self.assertIsNone(clipped["440"].differential)
        self.assertEqual(clipped["441"].differential["passed"], 1)

    def test_a_version_with_no_cost_row_reports_dashes_not_zero_invocations(self):
        """The cost columns take the same rule, and zero is the flattering reading.

        "This port needed no agents" is the project's headline claim. A version
        that was never costed must not be able to make it by omission.
        """
        self.write_costs([cost_row("439", STAMP_439_SECOND, "set_app_context")])
        self.write_evidence("440", [evidence_row("set_app_context", "passed")])

        points = self.by_version()
        text = self.render()

        self.assertIsNone(points["440"].agent_invocations)
        self.assertIsNone(points["440"].hooks_costed)
        self.assertEqual(row_cells(text, "agent invocations"), ["0", "—"])
        self.assertEqual(row_cells(text, "hooks costed"), ["1", "—"])

    def test_a_hook_never_probed_on_one_version_shows_a_dash_in_the_shapes_block(self):
        """Absent shapes too, since that block is where 439's gap is legible.

        A hook probed on 440 and not on 439 must leave 439's cell empty rather
        than borrowing its neighbour's shape, which is how a reader would come to
        believe 439 had identity evidence.
        """
        self.write_evidence("439", [evidence_row("set_app_context", shape="absence")])
        self.write_evidence(
            "440",
            [
                evidence_row("set_app_context", shape="identity"),
                evidence_row("tigon_url_block", shape="identity"),
            ],
        )

        text = self.render()

        self.assertEqual(shape_cells(text, "tigon_url_block"), ["—", "identity"])
        self.assertEqual(shape_cells(text, "set_app_context"), ["absence", "identity"])

    def test_absent_is_null_in_the_json_and_an_empty_measurement_is_an_object(self):
        """`to_dict` must carry the distinction out of the process intact.

        A consumer reading the JSON gets `null` for "never captured" and `{}` or
        a populated object for "captured". Collapsing them to `{}` in
        serialisation would undo the whole thing at the last step.
        """
        self.write_costs([cost_row("440", STAMP_440, "set_app_context")])
        self.write_evidence("441", [])

        payload = {point["version"]: point for point in
                   (p.to_dict() for p in self.series())}

        self.assertIsNone(payload["440"]["runtime"])
        self.assertEqual(payload["441"]["runtime"], {"passed": 0, "no_pass": 0, "claims": 0})
        self.assertIsNone(payload["440"]["differential"])
        self.assertIsNone(payload["441"]["differential"])

    def test_no_row_of_the_table_prints_zero_for_a_thing_never_captured(self):
        """The blunt form of the rule, over every numeric row at once.

        A version present only in the cost ledger has no runtime and no
        differential, so every cell of those rows must be a dash. Written as a
        sweep because the rule is about the table, not about one column, and a
        new row added later should have to answer it too.
        """
        self.write_costs([cost_row("440", STAMP_440, "set_app_context")])

        text = self.render()

        for label in (
            "hooks runtime-passed",
            "hooks without a pass",
            "  (claims recorded)",
            "differential passed",
        ):
            self.assertEqual(row_cells(text, label), ["—"], label)
        # The positive control: the rows that *were* captured print numbers, so
        # this is not a table of dashes.
        self.assertEqual(row_cells(text, "agent invocations"), ["0"])
        self.assertEqual(row_cells(text, "hooks costed"), ["1"])


# ============================================================== the 439 floor


class BaselineFloorTests(HistoryTestCase):
    """340 and 430 are a different architecture, and the series must not span them.

    The module's reason is written down: both keys of the self-profile rule fail
    together on 340 as consequences of one rewrite, so 430 and 439 are closer to
    one data point than two. A series that reached back would be four points that
    look like evidence and are not.
    """

    def setUp(self) -> None:
        super().setUp()
        # Every version the project has ever ported, all present in both ledgers,
        # so the filter is the only thing that can exclude the old ones.
        for version in ("340", "430", "439", "440"):
            self.write_evidence(version, [evidence_row("set_app_context", "passed")])
        self.write_costs(
            [cost_row(v, STAMP_440, "set_app_context") for v in ("340", "430", "439", "440")]
        )

    def test_versions_below_the_floor_are_excluded_though_both_ledgers_hold_them(self):
        """The exclusion is the filter's, not the fixture's.

        The assertion on the evidence directory is the positive control: without
        it, "the series is 439 and 440" is also what a broken reader that found
        no files at all would say.
        """
        found = {path.stem for path in (self.manifest / "runtime_evidence").glob("*.jsonl")}

        self.assertEqual(found, {"340", "430", "439", "440"})
        self.assertEqual([point.version for point in self.series()], ["439", "440"])

    def test_the_real_tree_records_340_and_430_and_the_series_still_starts_at_439(self):
        """Against the committed `manifest/decisions.jsonl`, which really holds both.

        This is the case the floor exists for, and it is not hypothetical: the
        340 Shopping-identifier miss and the 430 MobileConfig miss are both on
        record. A reader of the arc must not be shown them as earlier points of
        the same series.
        """
        versions = set()
        for line in (REPO / "manifest" / "decisions.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.strip():
                row = json.loads(line)
                versions.add(row.get("record", row).get("version"))

        self.assertIn("340", versions)
        self.assertIn("430", versions)
        self.assertEqual(
            [point.version for point in series(REPO)], ["439", "440", "441"]
        )

    def test_the_default_is_the_constant_and_the_constant_is_439(self):
        """Pinned as a value because it is an architectural boundary, not a default.

        Both halves matter: that the constant is 439, and that `series` with no
        argument uses it rather than some independently written literal.
        """
        self.assertEqual(BASELINE_VERSION, "439")
        self.assertEqual(
            [p.version for p in self.series()],
            [p.version for p in self.series(baseline=BASELINE_VERSION)],
        )

    def test_the_floor_is_inclusive_of_the_baseline_itself(self):
        """Off-by-one on a floor drops the version the whole series is pinned at."""
        self.assertIn("440", [p.version for p in self.series(baseline="440")])
        self.assertEqual([p.version for p in self.series(baseline="440")], ["440"])

    def test_the_baseline_moves_the_floor_backward_as_well_as_forward(self):
        """Both directions, so the forward case is not passing by coincidence.

        Moving it back to 340 must produce the four-point series the default
        refuses — which proves the default's exclusion is the baseline's doing
        and not some other property of the old versions' records.
        """
        self.assertEqual(
            [p.version for p in self.series(baseline="340")], ["340", "430", "439", "440"]
        )
        self.assertEqual([p.version for p in self.series(baseline="430")], ["430", "439", "440"])

    def test_a_baseline_after_every_version_is_refused_rather_than_left_empty(self):
        """An empty table is a statement about the data; this is a statement about the ask.

        A history printed over nothing would render a header, no columns and the
        do-not-name-a-direction guard, and read as "the record is empty" when the
        record is fine and the baseline was wrong.
        """
        with self.assertRaises(HistoryError) as caught:
            self.series(baseline="999")

        self.assertIn("999", str(caught.exception))
        self.assertIn("no version at or after", str(caught.exception))


class ReleaseOrderTests(HistoryTestCase):
    """The columns are read left to right as time. That has to be true.

    See :class:`KnownDefectTests` — it is not, past 999. What is pinned here is
    the part that holds today and the part that a string comparison in the
    *filter* rather than the sort would break, which are different bugs with the
    same cause.
    """

    def test_the_committed_three_digit_series_is_in_release_order(self):
        """439, 440, 441 — where lexicographic and numeric order agree.

        Recorded so the reach of the sort defect is unambiguous: today's data is
        correct, and it is correct by luck.
        """
        self.assertEqual([p.version for p in series(REPO)], ["439", "440", "441"])

    def test_a_version_past_999_is_in_the_series_because_the_floor_compares_numbers(self):
        """`int(v) >= int(baseline)`, not `v >= baseline`.

        As strings, `"1000" < "439"`, so a string comparison in the filter would
        silently drop every version past 999 out of the history entirely — a
        worse failure than the ordering one, because a missing column cannot be
        misread, it simply is not there to read.
        """
        for version in ("439", "441", "1000"):
            self.write_evidence(version, [evidence_row("set_app_context", "passed")])

        self.assertEqual(
            sorted(int(p.version) for p in self.series()), [439, 441, 1000]
        )


# ================================================ latest run, not the aggregate


class LatestRunNotAggregateTests(HistoryTestCase):
    """Two attempts at one version are one port. The cost metric says so itself.

    This is the same error as counting claims instead of hooks, one file over: a
    version that was run twice would report twice the hooks and, on 439, twice
    the agent invocations. "Four agent invocations to port 439" is not a sentence
    anyone should be able to produce from this ledger.
    """

    def test_the_latest_run_supplies_the_numbers_and_the_earlier_one_is_not_added(self):
        """Two runs, three hooks each, and the answer is three.

        The invocation count is the discriminator that matters — the earlier run
        needed two agents and the later needed none, so aggregation reads `2`,
        the maximum reads `2`, and only "the latest run" reads `0`. That is the
        claim the project makes about a port going mechanical.
        """
        self.write_costs(
            [
                cost_row("439", STAMP_439_FIRST, "set_app_context"),
                cost_row("439", STAMP_439_FIRST, "install_settings_long_click",
                         needed_agent=True),
                cost_row("439", STAMP_439_FIRST, "install_settings_long_click_actionbar",
                         needed_agent=True),
                cost_row("439", STAMP_439_SECOND, "set_app_context"),
                cost_row("439", STAMP_439_SECOND, "install_settings_long_click"),
                cost_row("439", STAMP_439_SECOND, "install_settings_long_click_actionbar"),
            ]
        )

        point = self.by_version()["439"]

        self.assertEqual(point.hooks_costed, 3)
        self.assertEqual(point.agent_invocations, 0)

    def test_a_later_and_worse_run_is_still_the_one_reported(self):
        """The direction control. "Latest" is not "best".

        Without this, a rule that took the minimum invocation count across runs
        passes the test above, and a port that regressed on its second attempt
        would keep reporting its first attempt's number forever.
        """
        self.write_costs(
            [
                cost_row("440", "2026-08-03T17:23:38.600070+00:00", "set_app_context"),
                cost_row("440", STAMP_440, "set_app_context", needed_agent=True),
                cost_row("440", STAMP_440, "tigon_url_block", needed_agent=True),
            ]
        )

        point = self.by_version()["440"]

        self.assertEqual(point.agent_invocations, 2)
        self.assertEqual(point.hooks_costed, 2)

    def test_the_real_ledger_holds_two_runs_for_439_and_reports_two_not_four(self):
        """Against the committed ledger, where the aggregate is exactly double.

        The count of distinct `recorded_at` stamps is the positive control: if the
        ledger were ever rewritten to one run per version, `2` would be the
        aggregate answer too and this test would stop discriminating. It would
        then fail here rather than quietly weaken.
        """
        stamps: dict[str, set[str]] = {}
        rows = []
        for line in (REPO / "manifest" / "agent_cost.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.strip():
                record = json.loads(line)["record"]
                rows.append(record)
                stamps.setdefault(record["version"], set()).add(record["recorded_at"])

        self.assertEqual(len(stamps["439"]), 2)
        self.assertEqual(len(stamps["441"]), 2)
        # The aggregate readings, computed here so the contrast is explicit.
        self.assertEqual(sum(1 for r in rows if r["version"] == "439"), 14)
        self.assertEqual(
            sum(1 for r in rows if r["version"] == "439" and r["needed_agent"]), 4
        )

        point = {p.version: p for p in series(REPO)}["439"]

        self.assertEqual(point.hooks_costed, 7)
        self.assertEqual(point.agent_invocations, 2)

    def test_a_single_run_version_is_unaffected(self):
        """The control for the whole class: one run, and the two readings agree.

        Present so a "take the latest" rule that accidentally dropped rows would
        be caught here rather than looking like the same correct answer.
        """
        self.write_costs(
            [cost_row("440", STAMP_440, hook) for hook in HOOKS]
        )

        point = self.by_version()["440"]

        self.assertEqual(point.hooks_costed, len(HOOKS))
        self.assertEqual(point.agent_invocations, 0)


# ======================================================= do not name a direction


class DirectionGuardTests(HistoryTestCase):
    """The refusal the module was written to institutionalise.

    The owner's correction was made about three points. Nothing in the data had
    to move for the error to be available — three numbers in a row were enough —
    so the guard cannot be conditional on movement, and every assertion here is
    built from `POINTS_FOR_A_DIRECTION` rather than from the number 5.
    """

    BELOW = "A direction is not worth naming below"
    ABOVE = "enough to discuss direction"

    def build(self, count: int, *, moving: bool = False) -> str:
        """A series of `count` versions from 439 up, flat unless asked otherwise."""
        for offset in range(count):
            version = str(439 + offset)
            passes = offset + 1 if moving else 1
            self.write_evidence(
                version,
                [evidence_row(hook, "passed") for hook in HOOKS[:passes]],
            )
        return self.render()

    def test_below_the_threshold_the_output_refuses_to_name_a_direction(self):
        """One point short of the constant, whatever the constant becomes."""
        text = self.build(POINTS_FOR_A_DIRECTION - 1)

        self.assertIn(self.BELOW, text)
        self.assertNotIn(self.ABOVE, text)
        self.assertIn(f"{POINTS_FOR_A_DIRECTION - 1} points.", text)

    def test_at_the_threshold_the_output_says_the_other_thing(self):
        """Exactly the constant. The boundary is inclusive on the permissive side.

        Asserted separately from "above" because an inverted or off-by-one
        comparison changes behaviour only at this one count.
        """
        text = self.build(POINTS_FOR_A_DIRECTION)

        self.assertIn(self.ABOVE, text)
        self.assertNotIn(self.BELOW, text)

    def test_the_count_at_which_the_message_flips_is_the_constant_itself(self):
        """The strongest available form: find the boundary, then name it.

        A test that hard-coded 5 would keep passing if someone lowered
        `POINTS_FOR_A_DIRECTION` to 3 and the module started blessing three
        points — the precise mistake. This one searches for where the wording
        changes and requires that place to be the constant, so the constant and
        the behaviour cannot drift apart.
        """
        flip = None
        for count in range(1, POINTS_FOR_A_DIRECTION + 3):
            with self.subTest(points=count):
                self.setUp()
                text = self.build(count)
                names_direction = self.ABOVE in text
                self.assertNotEqual(names_direction, self.BELOW in text)
                if names_direction and flip is None:
                    flip = count

        self.assertEqual(flip, POINTS_FOR_A_DIRECTION)

    def test_the_guard_fires_on_every_run_below_the_threshold_not_only_on_movement(self):
        """Flat data gets the same refusal as moving data, at every count below.

        The mistake was `5->1`, `7->1`, `4->1` — a reader inventing a direction
        from three numbers. But a guard that only appeared when a number moved
        would be absent exactly when the reader most easily supplies the movement
        themselves, and it would also be a verdict, which this module refuses to
        compute. So: identical numbers, every count, guard present.
        """
        for count in range(1, POINTS_FOR_A_DIRECTION):
            with self.subTest(points=count, data="flat"):
                self.setUp()
                text = self.build(count)
                self.assertIn(self.BELOW, text)
                self.assertEqual(
                    row_cells(text, "hooks runtime-passed"), ["1"] * count
                )
        for count in range(1, POINTS_FOR_A_DIRECTION):
            with self.subTest(points=count, data="moving"):
                self.setUp()
                self.assertIn(self.BELOW, self.build(count, moving=True))

    def test_the_threshold_is_named_in_the_refusal_so_a_reader_can_check_it(self):
        """The number is printed, not just implied.

        "Not worth naming" without saying below what is an instruction; with the
        number it is an argument the reader can disagree with.
        """
        text = self.build(2)

        self.assertIn(f"below {POINTS_FOR_A_DIRECTION}", text)

    def test_the_point_count_is_printed_in_the_header_and_in_the_guard(self):
        """Twice, deliberately: the header states the sample size before the table.

        Someone reading only the top of the output should already know how many
        points the columns are.
        """
        text = self.build(3)

        self.assertIn("(3 points)", text)
        self.assertIn("3 points.", text)
        self.assertIn("439 → 441", text)


# ============================================================ the committed data


class CommittedDataTests(unittest.TestCase):
    """The real tree, whose numbers are historical facts rather than arithmetic.

    A new port makes several of these fail. That is the point: the module's rule
    is *extend the series forward*, and the recorded arc going stale silently is
    the failure mode this project has already been bitten by once with its
    open-item record.
    """

    def setUp(self) -> None:
        self.points = series(REPO)
        self.by_version = {point.version: point for point in self.points}

    def test_the_series_is_exactly_439_440_441(self):
        """Three points, in release order, floor included, nothing before it."""
        self.assertEqual([point.version for point in self.points], ["439", "440", "441"])

    def test_series_of_a_dot_is_the_same_as_series_of_the_repository(self):
        """The documented invocation. `python -m dfinsta_pipeline.history` uses `"."`.

        Run under an explicit `chdir` so the assertion does not depend on where
        the test runner was started from.
        """
        with contextlib.chdir(REPO):
            from_cwd = series(".")

        self.assertEqual(
            [point.version for point in from_cwd], ["439", "440", "441"]
        )

    def test_hooks_runtime_passed_is_two_four_four(self):
        """The measured arc, and the number the claim-counting defect distorted.

        Counting claims instead of hooks reads `2, 13, 6` from the same files and
        makes 440 look like the best port of the three.
        """
        self.assertEqual(
            [point.runtime["passed"] for point in self.points], [2, 4, 4]
        )
        self.assertEqual(
            [point.runtime["no_pass"] for point in self.points], [5, 3, 3]
        )

    def test_the_claim_counts_are_seven_twenty_three_and_nine(self):
        """The re-measurement the hook counts must survive, stated as a number.

        440 holds 23 claims for the same seven hooks 441 covers in 9. This is the
        positive control for the test above: if the three versions all held one
        claim per hook, `2, 4, 4` would be what a claim-counter printed too.
        """
        self.assertEqual([point.runtime["claims"] for point in self.points], [7, 23, 9])

    def test_agent_invocations_are_two_zero_zero(self):
        """The central falsifiable claim of the project, read off the arc.

        439 needed an agent for both settings hooks; 440 and 441 needed none. The
        aggregate reading of the same ledger is `4, 0, 0`, because 439 was run
        twice.
        """
        self.assertEqual(
            [point.agent_invocations for point in self.points], [2, 0, 0]
        )
        self.assertEqual([point.hooks_costed for point in self.points], [7, 7, 7])

    def test_439_recorded_no_identity_claim_for_any_hook(self):
        """The recorded fact that bounded the first differential to 2 of 7.

        439 was probed by delta and absence only; identity probing arrived with
        440. So a differential across that boundary can compare shapes for two
        hooks and must refuse the other five — and this is the source of that,
        pinned here so nobody later reads 439's `no_pass` as five broken hooks.
        The 440 and 441 assertions are the positive control: the shape exists in
        the corpus, it is 439 that lacks it.
        """
        shapes_439 = self.by_version["439"].shapes

        self.assertEqual(set(shapes_439), set(HOOKS))
        for hook, kinds in shapes_439.items():
            self.assertNotIn("identity", kinds, hook)
            self.assertTrue(kinds <= {"absence", "delta"}, (hook, kinds))
        for version in ("440", "441"):
            recorded = self.by_version[version].shapes
            self.assertTrue(
                all("identity" in kinds for kinds in recorded.values()), version
            )

    def test_the_differential_column_is_absent_then_two_then_four(self):
        """439 has no predecessor inside the series; the other two are on disk.

        The `—` for 439 is the "first point" rule, not a missing file, and the two
        real differentials disagree with each other — 2 of 7 across the shape
        boundary, 4 of 7 after it — which is the comparison the module was built
        to make legible.
        """
        self.assertIsNone(self.by_version["439"].differential)
        self.assertEqual(self.by_version["440"].differential["passed"], 2)
        self.assertEqual(self.by_version["441"].differential["passed"], 4)

    def test_the_reels_margins_are_the_five_seven_four_that_started_this(self):
        """The exact numbers of the mistake, carried per version rather than pairwise.

        `5 -> 1`, `7 -> 1`, `4 -> 1` across 439, 440, 441. Read as a slope this is
        "selectivity is collapsing"; read as three measurements it is a literal
        count of classes in three different builds. The module prints them in a
        row and refuses to say which.
        """
        margins = [
            point.selectivity["replace_reels_discover_endpoint"] for point in self.points
        ]

        self.assertEqual(margins, ["5 -> 1", "7 -> 1", "4 -> 1"])
        self.assertEqual(
            [point.selectivity["install_settings_long_click"] for point in self.points],
            ["10 -> 1", "10 -> 1", "10 -> 1"],
        )

    def test_the_rendered_table_carries_the_same_numbers_as_the_objects(self):
        """`render` reads the points and computes nothing of its own.

        Two derivations of one fact, held together in one test — the table is
        what a human reads, and a formatting layer that recomputed anything would
        be a second answer to every question above.
        """
        text = render(self.points)

        self.assertEqual(row_cells(text, "agent invocations"), ["2", "0", "0"])
        self.assertEqual(row_cells(text, "hooks costed"), ["7", "7", "7"])
        self.assertEqual(row_cells(text, "hooks runtime-passed"), ["2", "4", "4"])
        self.assertEqual(row_cells(text, "hooks without a pass"), ["5", "3", "3"])
        self.assertEqual(row_cells(text, "  (claims recorded)"), ["7", "23", "9"])
        self.assertEqual(row_cells(text, "differential passed"), ["—", "2", "4"])

    def test_the_real_history_is_three_points_and_says_so(self):
        """Three, which is below the threshold, which is why the guard is visible."""
        text = render(self.points)

        self.assertLess(len(self.points), POINTS_FOR_A_DIRECTION)
        self.assertIn("A direction is not worth naming below", text)
        self.assertNotIn("enough to discuss direction", text)


# ==================================================================== the CLI


class CliTests(HistoryTestCase):
    """The process boundary: exit codes, stderr, and JSON a script can consume."""

    def setUp(self) -> None:
        super().setUp()
        self.write_costs(
            [cost_row("439", STAMP_439_SECOND, "set_app_context", needed_agent=True)]
            + [cost_row("440", STAMP_440, hook) for hook in HOOKS[:3]]
        )
        self.write_evidence(
            "439",
            [
                evidence_row("set_app_context", "passed", shape="absence"),
                evidence_row("tigon_url_block", "inconclusive", shape="delta"),
            ],
        )

    def test_a_readable_history_prints_the_table_and_exits_zero(self):
        code, stdout, stderr = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("PORT HISTORY", stdout)
        self.assertEqual(row_cells(stdout, "agent invocations"), ["1", "0"])

    def test_the_json_form_parses_and_matches_the_rendered_numbers(self):
        """Same run, two renderings, compared cell by cell.

        A JSON view that drifted from the table would be worse than no JSON view:
        the table is reviewed by a human and the JSON is not.
        """
        _, table, _ = self.run_main()
        code, raw, stderr = self.run_main("--json")
        payload = json.loads(raw)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual([point["version"] for point in payload], ["439", "440"])
        self.assertEqual(
            [str(point["agent_invocations"]) for point in payload],
            row_cells(table, "agent invocations"),
        )
        self.assertEqual(
            [str(point["hooks_costed"]) for point in payload],
            row_cells(table, "hooks costed"),
        )
        self.assertEqual(
            [str(point["runtime"]["passed"]) for point in payload[:1]],
            row_cells(table, "hooks runtime-passed")[:1],
        )

    def test_the_json_absences_are_null_where_the_table_prints_a_dash(self):
        """The same cell, both ways, in one assertion.

        440 has cost rows and no evidence file, so its runtime is the absence the
        module exists to report honestly. `null` in JSON, `—` in the table, and
        `0` in neither.
        """
        _, table, _ = self.run_main()
        _, raw, _ = self.run_main("--json")
        payload = json.loads(raw)

        self.assertIsNone(payload[1]["runtime"])
        self.assertEqual(row_cells(table, "hooks runtime-passed")[1], "—")

    def test_the_json_shapes_are_sorted_lists_because_a_set_does_not_serialise(self):
        """`to_dict` converts; a raw `PortPoint` holds sets.

        Worth its own test because `json.dumps` on a set raises `TypeError`, which
        is a crash rather than a wrong number — and it would only appear once a
        version recorded a shape, so a smoke test on empty data would miss it.
        """
        _, raw, _ = self.run_main("--json")
        payload = json.loads(raw)

        self.assertEqual(payload[0]["shapes"]["set_app_context"], ["absence"])
        self.assertEqual(payload[0]["shapes"]["tigon_url_block"], ["delta"])
        self.assertIsInstance(self.series()[0].shapes["set_app_context"], set)

    def test_a_baseline_with_no_versions_is_refused_on_stderr_with_exit_two(self):
        """Exit 2 and `refused: `, the project's refusal channel.

        Not 0-with-an-empty-table and not a traceback. A caller distinguishing
        "the history is short" from "your baseline was wrong" has only the exit
        code to do it with.
        """
        code, stdout, stderr = self.run_main("--baseline", "9999")

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertIn("9999", stderr)
        self.assertEqual(stdout, "")

    def test_a_root_with_no_manifest_at_all_is_refused_the_same_way(self):
        """A mistyped `--root` is the commonest bad input and must not print a table.

        Without a refusal this prints a header, no columns, and the
        do-not-name-a-direction guard — an empty success about a directory that
        was never looked at properly.
        """
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = history.main(["--root", str(self.tmp / "typo")])
        stdout, stderr = out.getvalue(), err.getvalue()

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)
        self.assertEqual(stdout, "")

    def test_the_baseline_flag_reaches_the_series(self):
        """The positive control for the refusal above: a valid baseline works.

        Otherwise "exit 2" would also be produced by a flag that was ignored and
        a series that happened to be empty.
        """
        code, stdout, _ = self.run_main("--baseline", "440")

        self.assertEqual(code, 0)
        self.assertIn("PORT HISTORY  440 → 440", stdout)


# ================================================================= known defects


class ClosedDefectTests(HistoryTestCase):
    """Seven defects this file found in `history.py`, all fixed the same hour.

    Each was written first as an `expectedFailure` pinning what the module then
    did, so the fix announced itself as an unexpected success rather than as a
    test somebody remembered to add. They now pin the corrected behaviour.

    Worth noting what they have in common: five of the seven are the module
    mishandling its *own* inputs — a version that is not a number, a row missing
    a field, two timestamp spellings, an empty list — rather than anything about
    Instagram. A reporting tool reads whatever is on disk, and what is on disk
    accumulated over months from several writers.
    """

    def test_versions_sort_numerically_rather_than_as_strings(self):
        """CLOSED 2026-08-07. Was: `sorted(v for v in versions if int(v) >= int(baseline))` has no key.

        The filter compares integers and the sort compares strings, so 1000 is
        admitted to the series and then printed first. The header would read
        `PORT HISTORY 1000 → 441`, the differential for each point would be
        looked up against the wrong predecessor, and the columns a reader takes
        for time would be alphabetical. Today's data hides it because every
        version is three digits.

        Fix: `sorted(..., key=int)`.
        """
        for version in ("439", "441", "1000"):
            self.write_evidence(version, [evidence_row("set_app_context", "passed")])

        self.assertEqual(
            [point.version for point in self.series()], ["439", "441", "1000"]
        )

    def test_a_non_numeric_baseline_is_refused_rather_than_raising_valueerror(self):
        """CLOSED 2026-08-07. Was: `--baseline nope` is a traceback, not `refused: ` and exit 2.

        `int(baseline)` runs outside anything that raises `HistoryError`, so a
        typo on the flag most likely to be typed by hand escapes the refusal
        channel entirely. This project has shipped that gap before — the
        feature-gate client — and the lesson recorded from it is that a refusal
        channel is only a channel if everything uses it.
        """
        self.write_evidence("440", [evidence_row("set_app_context", "passed")])

        code, _, stderr = self.run_main("--baseline", "nope")

        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("refused: "), stderr)

    def test_a_non_numeric_version_in_the_cost_ledger_is_refused(self):
        """CLOSED 2026-08-07. Was: `ValueError: invalid literal for int()` from a ledger row.

        Evidence filenames are filtered by `re.fullmatch(r"\\d+", …)`; versions
        arriving from the cost ledger are not filtered at all. A ledger written
        with `1.4.1`, or with an empty version, crashes the reporting tool rather
        than being named as an unreadable row — and the tool cannot then report
        anything, including the versions it could read.
        """
        self.write_costs([cost_row("1.4.1", STAMP_440, "set_app_context")])

        with self.assertRaises(HistoryError):
            self.series()

    def test_an_evidence_row_missing_a_field_is_refused_rather_than_a_keyerror(self):
        """CLOSED 2026-08-07. Was: `row["hook_id"]` on a malformed row is a bare `KeyError`.

        `_rows` already raises `HistoryError` naming the file and line for
        unparseable JSON, so the shape of the refusal exists and is simply not
        applied to the fields. A row that parses and lacks `hook_id` gets none of
        that: no path, no line number, and an exception `main` does not catch.
        """
        self.write_evidence("440", [{"verdict": "passed", "detail": {}}])

        with self.assertRaises(HistoryError):
            self.series()

    def test_a_margin_from_an_earlier_run_is_not_carried_into_the_latest(self):
        """CLOSED 2026-08-07. Was: `_cost_points` takes the latest run; `_selectivity` does not.

        `_selectivity` walks every row of the ledger and lets the last one in
        *file order* win, per hook. So a hook measured at `10 -> 1` on the first
        attempt and not measured at all on the second still shows `10 -> 1`,
        attributed to the version — a stale margin presented beside numbers that
        are all from the latest run.

        That matters more here than anywhere else in the module: the docstring on
        `_selectivity` calls a narrowing margin "the quiet one that precedes"
        the loud failure, and this is the value that can silently be a build old.
        """
        self.write_costs(
            [
                cost_row("440", "2026-08-03T17:23:38.600070+00:00",
                         "replace_reels_discover_endpoint", selectivity=[margin(7, 1)]),
                cost_row("440", STAMP_440, "replace_reels_discover_endpoint"),
            ]
        )

        self.assertEqual(self.by_version()["440"].selectivity, {})

    def test_two_runs_in_the_same_second_are_ordered_by_time_not_by_spelling(self):
        """CLOSED 2026-08-07. Was: `recorded_at` is compared as a string across two formats.

        The ledger holds both `2026-08-02T16:42:05.463572+00:00` and
        `2026-08-06T18:52:42Z`. Compared as strings at the same second, `"Z"`
        (0x5A) sorts after `"."` (0x2E), so the `Z`-spelled run wins whatever the
        actual instant. Narrow — it needs two runs inside one second — but the
        two spellings are both live in the committed ledger, so the collision is
        available rather than hypothetical.
        """
        self.write_costs(
            [
                cost_row("440", "2026-08-06T18:52:42Z", "set_app_context",
                         needed_agent=True),
                cost_row("440", "2026-08-06T18:52:42.900000+00:00", "set_app_context"),
                cost_row("440", "2026-08-06T18:52:42.900000+00:00", "tigon_url_block"),
            ]
        )

        point = self.by_version()["440"]

        self.assertEqual(point.hooks_costed, 2)
        self.assertEqual(point.agent_invocations, 0)

    def test_rendering_an_empty_series_refuses_rather_than_raising_indexerror(self):
        """CLOSED 2026-08-07. Was: `render([])` is `IndexError: list index out of range`.

        Unreachable through `main`, because `series` refuses first — but `render`
        is in `__all__` and is the function another reporting stage would import.
        `versions[0]` in the header is the first statement it executes.
        """
        with self.assertRaises(HistoryError):
            render([])


if __name__ == "__main__":
    unittest.main()
