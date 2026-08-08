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

import json
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
        full = {
            version
            for version in series
            if (REPOSITORY / "manifest" / "static_evidence" / f"{version}.jsonl").is_file()
            and (REPOSITORY / "manifest" / "runtime_evidence" / f"{version}.jsonl").is_file()
        }
        # BOTH sides of the pair must be full, not just the later one. 439 has
        # runtime evidence and no static evidence -- `static_verified` had no
        # producer until 440 -- so its release-ready set is not computable and
        # 439 -> 440 cannot be compared by anything. Requiring only the later
        # version made this test demand a comparison the corpus cannot support.
        # A version part-way through a port has half a corpus and is skipped the
        # same way; that is what `skipped` is for, and it is reported so the two
        # never blur.
        owed = {
            version
            for previous, version in zip(series, series[1:])
            if previous in full and version in full
        }
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
                    "device session is what to fix. The bar comes down only through a "
                    "recorded retirement (`python -m dfinsta_pipeline.retirement retire`), "
                    "which takes effect at the NEXT version and so cannot clear this port; "
                    "otherwise the expectation is derived from the previous port's own "
                    "evidence and only the hook passing again clears it.",
                ]
            ),
        )

    def test_no_evidence_file_names_a_hook_that_does_not_exist(self) -> None:
        """The guard for the failure that made this test file necessary.

        On 2026-08-08 `manifest/static_evidence/439.jsonl` was found to be 36 rows
        of pure fixture data — `install_probe_long_click`, `replace_probe_endpoint`
        and `set_probe_context`, none of them hooks — written by
        `tests/test_claim_attribution`, which runs labelled ports through the
        driver. `driver.publish_static_evidence` defaulted to the repository's own
        `manifest/static_evidence/`, so a test that passed `version="439"` appended
        to a tracked file. Those rows had been committed the day before, and the
        readiness numbers derived from them were quoted in four documents.

        It was caught by `git status` showing a file dirty that nobody had edited,
        which is luck rather than a check. This is the check: every claim in the
        durable corpus must name a hook the manifest actually declares. A fixture
        hook cannot survive it, and neither can a hook deleted from the manifest
        without its evidence being dealt with.
        """

        declared = {
            hook["hook_id"]
            for hook in json.loads(
                (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8")
            )["hooks"]
        }
        self.assertTrue(declared, "no hooks in the manifest; the check cannot bite")

        strangers: dict[str, set[str]] = {}
        checked = 0
        for directory in ("static_evidence", "runtime_evidence", "differentials"):
            for path in sorted((REPOSITORY / "manifest" / directory).glob("*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    checked += 1
                    hook = row.get("record", row)["hook_id"]
                    if hook not in declared:
                        strangers.setdefault(str(path.relative_to(REPOSITORY)), set()).add(hook)

        # The positive control. A glob that matched nothing would report no
        # strangers, which reads identically to a clean corpus.
        self.assertGreater(checked, 20, "too few claims read; the sweep found nothing")
        self.assertEqual(
            {},
            {path: sorted(hooks) for path, hooks in strangers.items()},
            "evidence naming hooks the manifest does not declare — either a test "
            "wrote into the committed corpus, or a hook was removed and its "
            "evidence left behind",
        )


if __name__ == "__main__":
    unittest.main()
