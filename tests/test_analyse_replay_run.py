"""The reductions behind every loop-blocking number this project quotes.

`docs/WORKFLOW_REGISTRATION_DESIGN.md` §3f and §3g are tables of percentages —
"23% of query samples answered", "apply 92% blocked", "longest unbroken stretch
28 samples". Each is an aggregation of two arrays in a run root's `success.json`,
and until 2026-08-05 each was aggregated by hand. The design note records the
same regret twice already: a benchmark whose figures are "not reproducible from
the tree", and a heartbeat gap read off a CLI.

So the arithmetic gets tests. Not because summing a list is hard, but because the
numbers it produces are the evidence for changes that were made and are cited in
arguments for changes not yet made — and running it over the surviving records
immediately found that §3g cites a 340 run root that no longer exists, with the
apply and decode residuals swapped relative to the record that does.

`tests/test_verify_build.py` is the model for loading a bare `tools/` script.
"""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
ANALYSER_PATH = ROOT / "tools" / "analyse_replay_run.py"


def _load(name: str, path: Path):
    """Import a `tools/` script by path; the directory is not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyse = _load("analyse_replay_run", ANALYSER_PATH)


def sample(activity: str | None, answered: bool) -> dict:
    return {
        "elapsed_seconds": 0.0,
        "running_activity": activity,
        "query_answered": answered,
        "detail": None,
    }


def beat(stage: str, gap: float, beats: int) -> dict:
    return {
        "activity": f"replay_{stage}_stage_activity",
        "attempt": 1,
        "details": {
            "stage": stage,
            "beats": beats,
            "elapsed_seconds": 30.0 * beats,
            "worst_gap_seconds": gap,
        },
    }


class QueryAvailabilityTests(unittest.TestCase):
    def test_answered_and_total_are_counted_separately(self) -> None:
        """The share is derived, never stored, so a record cannot disagree with itself."""
        samples = [sample("decode", True), sample("decode", False), sample("decode", True)]
        self.assertEqual(analyse.query_availability(samples), (2, 3))

    def test_an_empty_run_is_zero_of_zero_and_not_a_division(self) -> None:
        """A run that never sampled must not be reported as 0% available."""
        self.assertEqual(analyse.query_availability([]), (0, 0))
        self.assertIn("0 of 0", analyse.describe({"target": 430}))


class BlockedAttributionTests(unittest.TestCase):
    def test_blocking_is_attributed_to_the_activity_that_was_running(self) -> None:
        """Per-stage attribution is what corrected the first reading of this measurement.

        Reported as one percentage, the blocking looked like the decode's tree
        capture. Split by stage it was every stage — including the one that
        already ran its subprocess in a thread, which is what ruled out
        "thread the subprocess" as the fix.
        """
        samples = [
            sample("replay_apply_tree_stage_activity", False),
            sample("replay_apply_tree_stage_activity", False),
            sample("replay_decode_stage_activity", True),
            sample("replay_decode_stage_activity", False),
        ]
        self.assertEqual(
            analyse.blocked_by_activity(samples),
            {
                "replay_apply_tree_stage_activity": (2, 2),
                "replay_decode_stage_activity": (2, 1),
            },
        )

    def test_samples_between_stages_are_named_rather_than_dropped(self) -> None:
        """`running_activity` is None between stages; those samples are still real."""
        self.assertEqual(
            analyse.blocked_by_activity([sample(None, True)]),
            {"(between stages)": (1, 0)},
        )


class LongestStretchTests(unittest.TestCase):
    def test_the_stretch_is_consecutive_and_not_a_total(self) -> None:
        """Six blocked samples in ones is a responsive worker; three in a row is not.

        This is the number that matters more than the percentage, because a
        contiguous slab is what expires a heartbeat.
        """
        alternating = [sample("decode", i % 2 == 0) for i in range(12)]
        self.assertEqual(analyse.longest_blocked_stretch(alternating), 1)

        run_of_three = [
            sample("decode", True),
            sample("decode", False),
            sample("decode", False),
            sample("decode", False),
            sample("decode", True),
            sample("decode", False),
        ]
        self.assertEqual(analyse.longest_blocked_stretch(run_of_three), 3)

    def test_a_run_blocked_to_the_last_sample_counts_the_tail(self) -> None:
        """A stretch that never ends is the worst case and must not be dropped."""
        self.assertEqual(
            analyse.longest_blocked_stretch([sample("verify", False)] * 5), 5
        )


class HeartbeatGapTests(unittest.TestCase):
    def test_the_maximum_wins_over_the_latest(self) -> None:
        """A stage that reported 30 s once needs a timeout clearing 30, not 12."""
        self.assertEqual(
            analyse.worst_heartbeat_gaps(
                [beat("decode", 30.9, 4), beat("decode", 12.0, 9), beat("build", 41.2, 6)]
            ),
            {"decode": 30.9, "build": 41.2},
        )

    def test_a_sample_with_no_decodable_details_contributes_nothing(self) -> None:
        """Positive control: a malformed sample must not read as a zero gap."""
        self.assertEqual(
            analyse.worst_heartbeat_gaps(
                [{"activity": "x", "details_error": "UnicodeDecodeError: …"}, {}]
            ),
            {},
        )


class ThreeStateReportingTests(unittest.TestCase):
    """Not asked, nothing found, and found — kept apart, as everywhere else here."""

    def test_a_record_predating_the_sampler_says_so_rather_than_showing_zeros(self) -> None:
        """"No gaps recorded" must not be readable as "no gaps occurred".

        The two 340 and 430 records taken before 2026-08-05 carry no heartbeat
        samples at all, and a table of zeros against them would be a claim that
        the stages never paused.
        """
        rendered = analyse.describe(
            {"target": 340, "worker_query_responsiveness": [sample("decode", True)]}
        )
        self.assertIn("no heartbeat samples in this record", rendered)
        self.assertNotIn("worst gap", rendered)

    def test_a_record_with_samples_shows_the_gap_and_the_beat_count(self) -> None:
        rendered = analyse.describe(
            {
                "target": 430,
                "worker_query_responsiveness": [sample("decode", True)],
                "stage_heartbeats": [beat("decode", 30.9, 4)],
                "worst_heartbeat_gap_seconds": {"decode": 30.9},
            }
        )
        self.assertIn("worst gap", rendered)
        self.assertIn("30.9", rendered)
        self.assertIn("decode", rendered)
        self.assertNotIn("no heartbeat samples", rendered)

    def test_gaps_are_derived_when_the_record_carries_only_the_samples(self) -> None:
        """A record written by a harness that sampled but did not reduce still reads."""
        rendered = analyse.describe(
            {
                "target": 430,
                "worker_query_responsiveness": [],
                "stage_heartbeats": [beat("build", 41.2, 6)],
            }
        )
        self.assertIn("41.2", rendered)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.directory = Path(holder.name)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = analyse.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_multi_target_record_reports_every_target(self) -> None:
        record = self.directory / "success.json"
        record.write_text(
            json.dumps(
                {
                    "target_evidence": [
                        {"target": 340, "worker_query_responsiveness": [sample("decode", True)]},
                        {"target": 430, "worker_query_responsiveness": [sample("apply", False)]},
                    ]
                }
            )
        )
        code, out, _ = self._run([str(record)])
        self.assertEqual(code, 0)
        self.assertIn("target 340", out)
        self.assertIn("target 430", out)

    def test_a_missing_record_is_a_refusal_naming_the_path(self) -> None:
        """Exit 2 and `refused: …`, not a traceback. The contract every CLI here keeps."""
        missing = self.directory / "nowhere.json"
        code, _, err = self._run([str(missing)])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn(str(missing), err)

    def test_a_file_that_is_not_json_is_a_refusal_too(self) -> None:
        record = self.directory / "success.json"
        record.write_bytes(b"not json at all")
        code, _, err = self._run([str(record)])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("refused: "), err)

    def test_a_json_list_is_refused_rather_than_iterated_as_targets(self) -> None:
        """Positive control on the shape check: valid JSON is not a run record."""
        record = self.directory / "success.json"
        record.write_text(json.dumps([{"target": 430}]))
        code, _, err = self._run([str(record)])
        self.assertEqual(code, 2)
        self.assertIn("not a run record", err)


if __name__ == "__main__":
    unittest.main()
