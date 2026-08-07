"""Decisions that stopped matching the evidence — and the rule left out on purpose.

`reconsider` is the detection half of the reversal story: it reads committed
evidence and reports which recorded decisions no longer fit it. It proposes and
never decides, so its exit code is always 0 — a non-zero exit would fail a port
because a human has not yet answered a question, which is the "approve your way
past a red build" pressure the whole design avoids.

The property most worth defending is the one that is *absent*. Every one of the
six endpoints ruled on 2026-08-08 is declared-blocked and not yet enforced, and
none of them is suspect — they are outstanding implementation work, which
`rulings --audit` already owns. A rule firing on them would make the feature's
first run a false alarm about decisions taken the day before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.reconsider import (
    TRIGGERS,
    ReconsiderError,
    Reconsideration,
    main,
    reconsiderations,
    render,
)

REPOSITORY = Path(__file__).resolve().parent.parent


def claim(hook: str, kind: str, version: str, verdict: str) -> str:
    return json.dumps({
        "actor": "tests", "confidence": None, "decision_id": None, "detail": {},
        "hook_id": hook, "kind": kind,
        "producer": "deterministic" if kind == "static_verified" else "device",
        "rationale": "", "recorded_at": f"2026-08-0{version[-1]}T00:00:00+00:00",
        "schema_version": 1, "summary": "", "supersedes": None,
        "verdict": verdict, "version": version,
        **({"build_sha256": "b" * 64} if kind == "static_verified" else {}),
    }, sort_keys=True)


class ReconsiderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.manifest = self.tmp / "manifest"
        for name in ("static_evidence", "runtime_evidence", "differentials"):
            (self.manifest / name).mkdir(parents=True)

    def build(self, *, deps_on: dict[str, list[str]], ran: dict[str, bool]) -> None:
        hooks = [
            {"hook_id": hook, "intent": f"do {hook}", "tier": "robust", "status": "active",
             "strategy": "url_block" if deps_on.get(hook) else "insert",
             "semantic_deps": deps_on.get(hook, [])}
            for hook in ran
        ]
        (self.manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "policy_revision": "2026-08-01", "hooks": hooks}),
            encoding="utf-8",
        )
        (self.manifest / "runtime_evidence" / "441.jsonl").write_text(
            "\n".join(
                claim(hook, "runtime_probe", "441", "passed" if ok else "inconclusive")
                for hook, ok in ran.items()
            ) + "\n",
            encoding="utf-8",
        )

    def ruling(self, endpoint: str, decision_id: str = "decision-1") -> None:
        (self.manifest / "rulings.jsonl").write_text(
            json.dumps({"record": {
                "assessment_sha256": "a" * 64, "candidate_id": f"gap:{endpoint}",
                "decision_id": decision_id, "policy_revision": "2026-08-01",
                "rationale": "blocked", "recorded_at": "2026-08-08T00:00:00Z",
                "run_id": "feat-441", "verdict": "block",
            }, "schema_version": 1}) + "\n",
            encoding="utf-8",
        )


class BlockInertTests(ReconsiderTestCase):
    def test_a_block_whose_hook_never_ran_is_reported(self) -> None:
        """Four patches in this project were applied, verified and never reached.

        A block nothing runs is a rule that only looks like protection, and it is
        indistinguishable from a working one unless somebody checks execution.
        """
        self.build(deps_on={"dead": ["feed/x/"]}, ran={"dead": False, "alive": True})
        self.ruling("feed/x/")

        found, _ = reconsiderations(self.tmp, version="441")

        self.assertEqual(1, len(found))
        self.assertEqual("block_inert", found[0].trigger)
        self.assertEqual("feed/x/", found[0].subject)
        self.assertEqual("decision-1", found[0].original_decision_id)
        self.assertIn("never executed", found[0].summary)

    def test_a_block_whose_hook_runs_is_not_reported(self) -> None:
        self.build(deps_on={"alive": ["feed/x/"]}, ran={"alive": True})
        self.ruling("feed/x/")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)

    def test_an_endpoint_with_no_recorded_ruling_is_not_reported(self) -> None:
        """The report names the decision it questions. Without one there is nothing
        to withdraw, and inventing an id would make a reversal unrecordable."""
        self.build(deps_on={"dead": ["feed/x/"]}, ran={"dead": False})
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)


class RetirementReturnedTests(ReconsiderTestCase):
    def retire(self, hook: str, effective_from: str) -> None:
        (self.manifest / "retirements.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "hook_id": hook, "effective_from": effective_from,
                "decision_id": "retire-1", "ruled_by": "arnav",
                "rationale": "believed gone", "recorded_at": "2026-08-08T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )

    def test_a_retired_hook_that_runs_again_is_reported(self) -> None:
        self.build(deps_on={}, ran={"back": True})
        self.retire("back", "441")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual(1, len(found))
        self.assertEqual("retirement_returned", found[0].trigger)
        self.assertEqual("retire-1", found[0].original_decision_id)

    def test_a_run_before_the_retirement_took_effect_is_not_a_return(self) -> None:
        """Retired from 442; running on 441 is the past, not a resurrection.

        Without the version comparison every retirement of a once-working hook
        would report itself the moment it was recorded.
        """
        self.build(deps_on={}, ran={"back": True})
        self.retire("back", "442")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)

    def test_a_withdrawn_retirement_is_no_longer_questioned(self) -> None:
        """It reads through `retirements_on_record`, so a withdrawal removes it."""
        self.build(deps_on={}, ran={"back": True})
        self.retire("back", "441")
        (self.manifest / "reversals.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "withdraws": "retirement", "subject": "back",
                "original_decision_id": "retire-1", "decision_id": "withdraw-1",
                "ruled_by": "arnav", "rationale": "it came back",
                "recorded_at": "2026-08-09T00:00:00Z", "effective_from": "442",
            }) + "\n",
            encoding="utf-8",
        )
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)


class RulesNotRunTests(ReconsiderTestCase):
    def test_a_rule_that_could_not_run_is_reported_not_hidden(self) -> None:
        """A skipped rule found nothing for a reason unrelated to the evidence.

        Returning only the findings would make "no index supplied" read as
        "nothing wrong", which is the same shape as a sweep that skips a pair
        silently.
        """
        self.build(deps_on={"alive": ["feed/x/"]}, ran={"alive": True})
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        self.assertTrue(any("block_endpoint_absent" in line for line in not_run))
        self.assertIn("RULES NOT RUN", render(found, not_run, "441"))


class RefusalTests(ReconsiderTestCase):
    def test_a_non_numeric_version_is_refused(self) -> None:
        self.build(deps_on={}, ran={"a": True})
        with self.assertRaises(ReconsiderError):
            reconsiderations(self.tmp, version="v441")

    def test_an_unknown_trigger_cannot_be_constructed(self) -> None:
        with self.assertRaises(ReconsiderError):
            Reconsideration("block", "x", "d", "made_up", "", ())

    def test_a_reconsideration_must_name_the_decision_it_questions(self) -> None:
        with self.assertRaises(ReconsiderError):
            Reconsideration("block", "x", "", TRIGGERS[0], "", ())

    def test_main_exits_zero_even_when_it_finds_something(self) -> None:
        """A proposal must never fail a port. Exit 3 here would make the fastest
        way to a green build the approval of a withdrawal."""
        self.build(deps_on={"dead": ["feed/x/"]}, ran={"dead": False})
        self.ruling("feed/x/")
        self.assertEqual(0, main(["--root", str(self.tmp), "--version", "441"]))
        self.assertEqual(0, main(["--root", str(self.tmp), "--version", "441", "--json"]))

    def test_main_refuses_a_bad_version_with_exit_two(self) -> None:
        self.build(deps_on={}, ran={"a": True})
        self.assertEqual(2, main(["--root", str(self.tmp), "--version", "nope"]))


class CommittedCorpusTests(unittest.TestCase):
    def test_nothing_recorded_here_has_stopped_matching(self) -> None:
        """Today's real state, and the reason the six fresh blocks are silent.

        All six are declared-blocked and not yet enforced. That is outstanding
        work, not a suspect decision, and `rulings --audit` already exits 1 for
        it. This asserting empty is what proves the omitted rule stayed omitted.
        """
        found, not_run = reconsiderations(REPOSITORY, version="441")
        self.assertEqual([], [item.to_dict() for item in found])
        self.assertTrue(not_run, "the index rule should report itself skipped")


if __name__ == "__main__":
    unittest.main()
