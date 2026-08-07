"""The expectation, checked against the evidence actually committed here.

`tests/test_expectation.py` exercises the module against corpora it builds. This
file points it at `manifest/` and is the reason the module is worth having:
without it, `expectation` would be a third command nobody runs, alongside
`final_report` and `history`. A port that drops a release-ready hook and commits
its evidence turns the suite red here, in the run everyone does anyway.

The failure mode this guards against in *itself* is the one this project has
shipped repeatedly -- a check that cannot fail. So the corpus is pinned: it is not
enough that every pair the sweep managed to compare passed, because deleting an
evidence file makes a pair uncomparable and a sweep of nothing passes vacuously.
See `absence-assertions-need-positive-controls`.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dfinsta_pipeline import expectation

REPOSITORY = Path(__file__).resolve().parent.parent

#: Versions whose durable evidence is committed and must stay so. Only ever
#: added to. A version leaving this set means an evidence file was deleted, which
#: is the quiet way to make the sweep below stop checking anything.
PINNED = {"439", "440", "441"}


class CommittedCorpusTests(unittest.TestCase):
    def test_the_pinned_versions_still_have_evidence(self) -> None:
        series = set(expectation.versions_with_evidence(REPOSITORY))
        self.assertLessEqual(
            PINNED,
            series,
            "durable evidence for a pinned version has gone missing. The sweep "
            "below would then pass by not checking it.",
        )

    def test_every_full_corpus_version_is_actually_compared(self) -> None:
        """The positive control: assert the sweep did work, not just that it was calm."""

        comparisons, skipped = expectation.sweep(REPOSITORY)
        compared = {item.version for item in comparisons}
        series = expectation.versions_with_evidence(REPOSITORY)
        full = [
            version
            for version in series
            if (REPOSITORY / "manifest" / "static_evidence" / f"{version}.jsonl").is_file()
            and (REPOSITORY / "manifest" / "runtime_evidence" / f"{version}.jsonl").is_file()
        ]
        # Every full-corpus version except the first of the series owes a
        # comparison. A version part-way through a port has half a corpus and is
        # legitimately skipped -- that is what `skipped` is for, and it is
        # reported so the two never blur.
        owed = {version for version in full if version != series[0]}
        self.assertEqual(
            owed,
            compared,
            f"pairs not compared: {sorted(owed - compared)}; skipped said "
            f"{[pair for pair, _ in skipped]}",
        )
        self.assertTrue(comparisons, "no pair was compared; nothing was checked")

    def test_no_port_has_dropped_a_release_ready_hook(self) -> None:
        comparisons, _ = expectation.sweep(REPOSITORY)
        failures = [
            f"{item.previous} -> {item.version} lost {', '.join(item.dropped)}"
            for item in comparisons
            if not item.met
        ]
        self.assertEqual(
            [],
            failures,
            "\n".join(
                [""]
                + failures
                + [
                    "",
                    "Run `python -m dfinsta_pipeline.expectation` for the reasons. A "
                    "differential verdict of failed/regressed is a real regression; "
                    "inconclusive/no_current means the hook was not measured and the "
                    "device session is what to fix. To lower the bar legitimately, "
                    "record a retirement -- see manifest/RETIREMENTS.md.",
                ]
            ),
        )

    def test_no_retirement_is_recorded_yet(self) -> None:
        """Pins today's state so the first one is a deliberate, reviewed change.

        Not a rule against retiring hooks -- it is a rule against a retirement
        arriving unnoticed. Deleting this test is the intended way past it, in the
        same commit as the row and the reasoning.
        """

        self.assertEqual({}, expectation.read_retirements(REPOSITORY))


if __name__ == "__main__":
    unittest.main()
