"""Decisions that stopped matching the evidence — and the rule left out on purpose.

`reconsider` is the detection half of the reversal story: it reads committed
evidence and reports which recorded decisions no longer fit it. It proposes and
never decides, so its exit code is always 0 — a non-zero exit would fail a port
because a human has not yet answered a question, which is the "approve your way
past a red build" pressure the whole design avoids.

The property most worth defending is the one that is *absent*. Of the six
endpoints ruled on 2026-08-08, five gained guards the same day and are silent
because the hook that enforces them runs. The sixth,
`delivery/background_prefetch`, is declared-blocked and unenforced — and not
suspect: it is a no-op logger's marker name rather than a request path, so no
guard could ever test it. That is outstanding *withdrawal* work, which the
reversal gate owns, and `rulings --audit` already exits 1 for it. A rule firing
here as well would make the feature's first run a false alarm about a decision
taken the day before.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
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
    def test_the_committed_corpus_questions_four_blocks_and_only_by_measurement(self):
        """Today's real state. This test used to assert the corpus found NOTHING.

        It stopped being empty on 2026-08-08, when a measurement build was walked
        on the phone and four blocked endpoints turned out never to be requested.
        That is the feature working, not a regression — the reversal gate had no
        possible input until this corpus existed.

        What still has to hold is the property the module is proudest of: the
        deliberately omitted "declared and not enforced" rule must not have been
        re-created. So every finding must come from `block_never_observed`, and
        none of them may be an endpoint the app does not guard —
        `delivery/background_prefetch` is declared, unguarded and never observed,
        and its absence from this list is the omission still holding.
        """
        found, not_run = reconsiderations(REPOSITORY, version="441")

        self.assertEqual(
            {"block_never_observed"}, {item.trigger for item in found},
            "a finding from another rule means the corpus changed shape",
        )
        self.assertEqual(
            [
                "feed/injected_reels_media/",
                "feed/reels_media/",
                "feed/reels_media_stream/",
                "feed/timeline_stream/",
            ],
            sorted(item.subject for item in found),
        )
        # The omitted rule, still omitted, on the one endpoint that would expose it.
        self.assertNotIn(
            "delivery/background_prefetch", {item.subject for item in found},
            "an unguarded endpoint reached a reconsideration: the omitted rule is back",
        )
        # Every finding names the decision a withdrawal would be recorded against.
        for item in found:
            self.assertTrue(item.original_decision_id.startswith("decision-"))
        self.assertTrue(not_run, "the index rule should report itself skipped")


# ===========================================================================
#   Adversarial additions.
#
#   The property the module is proudest of — the rule left out on purpose — was
#   not observable in any test here. Every temporary corpus lacks
#   `dfinsta_source_439/…/hooks.smali`, so `unenforced_endpoints` raised, every
#   declared block was treated as enforced, and the `if endpoint in unenforced:
#   continue` line never executed once in the suite. The committed-corpus test
#   above cannot see it either: `tigon_url_block` runs on every version, so
#   `block_inert` is empty whether or not the skip exists. These fixtures ship an
#   app source, which is what makes the omission checkable.
# ===========================================================================

#: Enough of `hooks.smali` for `rulings.guarded_endpoints` to parse: it splits at
#: `throwIfBlocked`, stops at `.end method`, and reads `const-string` literals,
#: discarding the `disable_` preference keys.
GUARD_SOURCE = """.method public static throwIfBlocked(Ljava/net/URI;)V
    .locals 3
{literals}
    const-string v1, "disable_feed"
.end method
"""

APP_SOURCE = Path("dfinsta_source_439") / "newCode" / "com" / "dfinstagram" / "hooks.smali"


class EnforcementTestCase(ReconsiderTestCase):
    """A corpus that can answer "does the app actually block this?"."""

    def app_source(self, *guarded: str) -> Path:
        path = self.tmp / APP_SOURCE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            GUARD_SOURCE.format(
                literals="\n".join(f'    const-string v1, "{item}"' for item in guarded)
            ),
            encoding="utf-8",
        )
        return path

    def blocker(
        self,
        *,
        deps: list[str],
        ran: bool,
        hook_id: str = "blocker",
        others: list[tuple[str, list[str], bool]] = (),
    ) -> None:
        """The url-block hook, optionally preceded in file order by others.

        `others` is `(hook_id, semantic_deps, ran)`. It exists so that "which
        hook declares this endpoint" can be got wrong: with one hook in the
        manifest, picking the first and picking the matching one are the same
        answer, and a fixture that cannot tell them apart is not a test.
        """
        hooks = [
            {"hook_id": name, "intent": "other", "tier": "robust", "status": "active",
             "strategy": "insert", "semantic_deps": list(other_deps)}
            for name, other_deps, _ in others
        ]
        hooks.append(
            {"hook_id": hook_id, "intent": "block", "tier": "robust", "status": "active",
             "strategy": "url_block", "semantic_deps": deps}
        )
        (self.manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "policy_revision": "2026-08-01", "hooks": hooks}),
            encoding="utf-8",
        )
        rows = [
            claim(name, "runtime_probe", "441", "passed" if ok else "inconclusive")
            for name, _, ok in others
        ]
        rows.append(claim(hook_id, "runtime_probe", "441", "passed" if ran else "inconclusive"))
        (self.manifest / "runtime_evidence" / "441.jsonl").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )

    def rulings(self, *endpoints: str, decision_id: str = "decision-1") -> None:
        (self.manifest / "rulings.jsonl").write_text(
            "\n".join(
                json.dumps({"record": {
                    "assessment_sha256": "a" * 64, "candidate_id": f"gap:{endpoint}",
                    "decision_id": decision_id, "policy_revision": "2026-08-01",
                    "rationale": "blocked", "recorded_at": "2026-08-08T00:00:00Z",
                    "run_id": "feat-441", "verdict": "block",
                }, "schema_version": 1})
                for endpoint in endpoints
            ) + "\n",
            encoding="utf-8",
        )

    def observed(
        self,
        *sessions: tuple[str, str, list[str], dict[str, int]],
        version: str = "441",
    ) -> Path:
        """Observation sessions, as `(session_id, surface, watched, counts)`.

        Written raw rather than through `observation.append`, deliberately: a
        fixture built by the writer under test cannot catch a writer and a reader
        that agree with each other and with nothing else.

        **Always under `self.tmp`.** Never `manifest/observations/` in this
        repository — a test that wrote into a committed corpus once shipped 36
        fabricated rows, and the defence is that the root is a parameter with no
        way to reach the real one from here.
        """

        path = self.tmp / "manifest" / "observations" / f"{version}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps({
                    "schema_version": 1, "version": version, "build_sha256": "b" * 64,
                    "recorded_at": "2026-08-09T10:00:00Z", "session_id": session_id,
                    "surface": surface, "watched": watched,
                    "counts": counts, "total": sum(counts.values()),
                }, sort_keys=True) + "\n"
                for session_id, surface, watched, counts in sessions
            ),
            encoding="utf-8",
        )
        return path


class TheOmittedRuleTests(EnforcementTestCase):
    """"Declared blocked and not yet enforced" is not a trigger, on purpose."""

    def test_an_unenforced_block_is_silent_while_an_enforced_one_fires(self) -> None:
        """Both halves in one corpus, so neither can pass by accident.

        `guarded` is in `throwIfBlocked` and its hook never runs — a block that
        exists and cannot be doing anything, which is `block_inert`.
        `unguarded` is ruled and declared and the app does not test it — the
        state one of the six endpoints ruled on 2026-08-08 is in. It is outstanding
        implementation work, `rulings --audit` already exits 1 for it, and a
        second alarm here would make this feature's first run a false alarm
        about decisions taken the day before.
        """
        self.blocker(deps=["feed/guarded/", "feed/unguarded/"], ran=False)
        self.rulings("feed/guarded/", "feed/unguarded/")
        self.app_source("feed/guarded/")

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual([("block_inert", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in found])
        # The rule ran. Silence about `unguarded` is a judgement, not a skip.
        self.assertEqual([], [line for line in not_run if line.startswith("block_inert")])

    def test_the_fixture_source_really_is_being_read(self) -> None:
        """The positive control. Move the guard and the finding moves with it.

        Without this, a fixture whose smali the module never opened would look
        exactly like the test above passing.
        """
        self.blocker(deps=["feed/guarded/", "feed/unguarded/"], ran=False)
        self.rulings("feed/guarded/", "feed/unguarded/")
        self.app_source("feed/unguarded/")
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([("block_inert", "feed/unguarded/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertEqual([], [line for line in not_run if line.startswith("block_inert")])

    def test_an_unreadable_source_judges_everything_and_says_so(self) -> None:
        """The honest failure mode, and it is checked to be honest.

        With no app source nothing can be shown to be unenforced, so every
        declared block is treated as enforced — the direction that over-reports
        rather than the one that goes quiet. The line in `rules_not_run` is what
        keeps that from reading as a clean result, so it must name the path it
        could not read and say what it assumed instead.
        """
        self.blocker(deps=["feed/guarded/", "feed/unguarded/"], ran=False)
        self.rulings("feed/guarded/", "feed/unguarded/")
        # No app_source() call: nothing to read.

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual(
            [("block_inert", "feed/guarded/"), ("block_inert", "feed/unguarded/")],
            [(item.trigger, item.subject) for item in found],
        )
        excuses = [line for line in not_run if line.startswith("block_inert")]
        self.assertEqual(1, len(excuses), not_run)
        self.assertIn(str(self.tmp / APP_SOURCE), excuses[0])
        self.assertIn("treated as enforced", excuses[0])

    def test_a_source_that_is_the_wrong_file_is_reported_the_same_way(self) -> None:
        """Present and unreadable is the same fact as absent: nobody checked."""
        self.blocker(deps=["feed/guarded/"], ran=False)
        self.rulings("feed/guarded/")
        path = self.tmp / APP_SOURCE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("some other smali with no guard method\n", encoding="utf-8")

        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual(["feed/guarded/"], [item.subject for item in found])
        self.assertTrue([line for line in not_run if line.startswith("block_inert")], not_run)

    def test_an_enforced_block_whose_hook_runs_is_not_reported(self) -> None:
        self.blocker(deps=["feed/guarded/"], ran=True)
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        self.assertEqual([], [line for line in not_run if line.startswith("block_inert")])

    def test_the_manifest_spelling_and_the_candidate_spelling_may_differ(self) -> None:
        """A leading slash hid a whole grouping once. `normalise` is what joins them."""
        self.blocker(deps=["/api/v1/feed/guarded/"], ran=False)
        self.rulings("feed/guarded/")
        self.app_source("/api/v1/feed/guarded/")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([("block_inert", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in found])


class BlockNeverObservedTests(EnforcementTestCase):
    """The rule built on a measurement of traffic rather than the shape of code.

    Stage 4 judges an endpoint from its name in a class of names. That produced
    two rulings on 2026-08-08 it should not have — one path that fires zero times,
    and `delivery/background_prefetch`, which is a no-op logger's marker name and
    not a request path at all. Both looked exactly like the four good rulings
    beside them, because a name is all stage 4 has to look at.

    The tests that matter most here are the ones where it stays **silent**: on an
    unenforced endpoint, on a path only a vacuous session watched, and on a store
    that does not exist. Each of those is a way for this rule to become the one
    the module docstring leaves out on purpose.
    """

    def test_a_watched_endpoint_never_requested_is_reported(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}))

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual([("block_never_observed", "feed/gone/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertEqual("decision-1", found[0].original_decision_id)
        self.assertEqual("block", found[0].kind)
        self.assertIn("never requested once", found[0].summary)
        self.assertEqual([], [l for l in not_run if l.startswith("block_never_observed")])

    def test_an_endpoint_that_was_observed_is_not_reported(self) -> None:
        """The positive control. One request is the whole difference."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/"], {"feed/gone/": 1}))
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_an_endpoint_no_build_was_watching_is_not_reported(self) -> None:
        """Silence from a path nobody looked for is not evidence of anything.

        Without the watch list this rule would fire on every blocked endpoint the
        moment any session was recorded, which is a report of the manifest rather
        than of the phone.
        """
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/busy/"], {"feed/busy/": 40}))
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_an_unenforced_endpoint_cannot_reach_this_rule(self) -> None:
        """The omitted rule stays omitted, and structurally rather than by luck.

        `feed/unguarded/` is ruled, declared, and has no `throwIfBlocked` guard —
        the state `delivery/background_prefetch` is in. A watch list is a claim by
        a build about what it was watching, so a mis-generated one naming an
        unenforced path would otherwise put a day-old decision in front of a human
        as a false alarm, which is exactly what the omission exists to prevent.
        `feed/guarded/` in the same corpus is the positive control: the rule is
        running, and its silence about the other is a judgement.
        """
        self.blocker(deps=["feed/guarded/", "feed/unguarded/"], ran=True)
        self.rulings("feed/guarded/", "feed/unguarded/")
        self.app_source("feed/guarded/")
        self.observed((
            "s1", "feed_tab",
            ["feed/guarded/", "feed/unguarded/", "feed/busy/"],
            {"feed/busy/": 40},
        ))

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual([("block_never_observed", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertEqual([], [l for l in not_run if l.startswith("block_never_observed")])

    def test_an_unreadable_app_source_makes_this_rule_report_its_blind_spot(self) -> None:
        """With no source nothing can be shown to be unenforced.

        So the guarantee above does not hold on this run, and the rule says so
        under its own name rather than quietly judging endpoints whose enforcement
        nobody checked. It still runs — a caveat is not a skip.
        """
        self.blocker(deps=["feed/unguarded/"], ran=True)
        self.rulings("feed/unguarded/")
        self.observed((
            "s1", "feed_tab", ["feed/unguarded/", "feed/busy/"], {"feed/busy/": 40},
        ))

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual(["feed/unguarded/"],
                         [i.subject for i in found if i.trigger == "block_never_observed"])
        excuses = [l for l in not_run if l.startswith("block_never_observed")]
        self.assertEqual(1, len(excuses), not_run)
        self.assertIn(str(self.tmp / APP_SOURCE), excuses[0])
        self.assertIn("was not excluded", excuses[0])

    def test_a_readable_source_removes_that_blind_spot_line(self) -> None:
        """The positive control for the caveat above: it must be absent sometimes."""
        self.blocker(deps=["feed/guarded/"], ran=True)
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")
        self.observed((
            "s1", "feed_tab", ["feed/guarded/", "feed/busy/"], {"feed/busy/": 40},
        ))
        _, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], [l for l in not_run if l.startswith("block_never_observed")])

    def test_the_blind_spot_caveat_is_not_raised_about_a_rule_that_never_ran(self) -> None:
        """The other control, and the one that was missing.

        With no observation store the rule is skipped, and "an endpoint with no
        guard was not excluded from this rule" would be a caveat about a rule that
        did not run — text that travels inside the signed docket. Exactly one line
        about this rule, and it is the skip.
        """
        self.blocker(deps=["feed/unguarded/"], ran=True)
        self.rulings("feed/unguarded/")
        # No app_source() and no observed(): both unreadable at once.
        _, not_run = reconsiderations(self.tmp, version="441")
        mine = [l for l in not_run if l.startswith("block_never_observed")]
        self.assertEqual(1, len(mine), not_run)
        self.assertIn("no observation evidence", mine[0])
        self.assertNotIn("was not excluded", mine[0])

    def test_a_ruled_endpoint_no_hook_declares_cannot_reach_this_rule(self) -> None:
        """The hole in "an unenforced endpoint cannot reach this rule".

        `unenforced_endpoints` names what is *declared and unguarded*, so an
        endpoint no hook declares at all is in neither set. That is reachable the
        moment a dep leaves `hooks.json` while `rulings.jsonl` keeps its row —
        which is what `apply_unblock` does — and it would put the exact endpoint
        the omitted rule protects in front of a human with a readable source and
        no caveat at all. `feed/guarded/` is the positive control: the rule is
        running.
        """
        self.blocker(deps=["feed/guarded/"], ran=True)
        self.rulings("feed/guarded/", "delivery/background_prefetch")
        self.app_source("feed/guarded/")
        self.observed((
            "s1", "feed_tab",
            ["feed/guarded/", "delivery/background_prefetch", "feed/busy/"],
            {"feed/busy/": 40},
        ))

        found, not_run = reconsiderations(self.tmp, version="441")

        self.assertEqual([("block_never_observed", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertEqual([], [l for l in not_run if l.startswith("block_never_observed")])

    def test_a_path_watched_only_by_a_vacuous_session_is_not_reported(self) -> None:
        """A session that saw nothing at all proves nothing about any path.

        It is equally well explained by a build that was not observing, an empty
        capture, and an app that never ran — so the path it watched is not
        evidence, and the rule must stay silent about it while still reporting the
        one an evidential session watched.
        """
        self.blocker(deps=["feed/gone/", "feed/quiet/"], ran=True)
        self.rulings("feed/gone/", "feed/quiet/")
        self.app_source("feed/gone/", "feed/quiet/")
        self.observed(
            ("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}),
            ("v1", "reels_tab", ["feed/quiet/"], {}),
        )
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual(["feed/gone/"], [item.subject for item in found])

    def test_no_observation_store_makes_the_rule_report_itself_skipped(self) -> None:
        """A rule that quietly did not run is indistinguishable from one that
        found nothing, and this rule's finding is a negative claim — the exact
        shape where a missing measurement reads as a result."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        excuses = [l for l in not_run if l.startswith("block_never_observed")]
        self.assertEqual(1, len(excuses), not_run)
        self.assertIn("no observation evidence", excuses[0])

    def test_an_all_vacuous_store_makes_the_rule_report_itself_skipped(self) -> None:
        """The dangerous corpus: sessions exist and none of them is evidence."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("v1", "feed_tab", ["feed/gone/"], {}))
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        self.assertTrue(
            [l for l in not_run if l.startswith("block_never_observed") and "vacuous" in l],
            not_run,
        )

    def test_a_corrupt_observation_store_is_reported_and_never_read_as_clean(self) -> None:
        """Present and unreadable is the same fact as absent: nobody measured."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        path = self.observed(("s1", "feed_tab", ["feed/gone/"], {"feed/busy/": 1}))
        path.write_text("{ not json\n", encoding="utf-8")
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        self.assertTrue([l for l in not_run if l.startswith("block_never_observed")], not_run)

    def test_the_store_is_read_at_the_version_being_reported(self) -> None:
        """A 440 session must not answer a question about 441."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(
            ("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}),
            version="440",
        )
        _, not_run = reconsiderations(self.tmp, version="441")
        self.assertTrue([l for l in not_run if l.startswith("block_never_observed")], not_run)

    def test_the_manifest_spelling_and_the_watched_spelling_may_differ(self) -> None:
        """The watch list carries the literal as the smali does; a ruling carries
        the candidate's. A leading slash hid a whole grouping once."""
        self.blocker(deps=["/api/v1/feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("/api/v1/feed/gone/")
        self.observed((
            "s1", "feed_tab", ["/api/v1/feed/gone/", "feed/busy/"], {"feed/busy/": 40},
        ))
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([("block_never_observed", "feed/gone/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertIn("watched as /api/v1/feed/gone/", "\n".join(found[0].evidence))

    def test_a_withdrawn_block_is_no_longer_questioned(self) -> None:
        """A withdrawal removes the question for good. Asking again would make the
        gate propose reconsidering a decision a human already reconsidered."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}))
        # The positive control: it fires before the withdrawal is recorded.
        self.assertEqual(1, len(reconsiderations(self.tmp, version="441")[0]))
        (self.manifest / "reversals.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "withdraws": "block",
                # Spelled with the leading slash a human would have typed, which
                # `reversal` stores verbatim — the join has to normalise.
                "subject": "/feed/gone/",
                "original_decision_id": "decision-1", "decision_id": "withdraw-1",
                "ruled_by": "arnav", "rationale": "measured, never requested",
                "recorded_at": "2026-08-09T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_a_withdrawal_of_a_different_decision_does_not_silence_this_one(self) -> None:
        """Keyed on the pair, not on the subject and not on the id alone."""
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}))
        (self.manifest / "reversals.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "withdraws": "block", "subject": "feed/gone/",
                "original_decision_id": "some-other-decision",
                "decision_id": "withdraw-1", "ruled_by": "arnav",
                "rationale": "a different docket", "recorded_at": "2026-08-09T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(1, len(reconsiderations(self.tmp, version="441")[0]))

    def test_every_finding_states_the_bound_a_reader_would_otherwise_miss(self) -> None:
        """A path only one screen requests is not observed by a session that never
        went there, and server config can suppress a request the app would make.
        Neither is resolvable from the store, so both travel on the finding.
        """
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(
            ("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}),
            ("s2", "explore_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 2}),
            # A vacuous session on a third surface. Its `reels_tab` must NOT be
            # named: the surface list exists so a human can ask "would this
            # session have seen it?", and a session that saw nothing at all
            # answers that wrongly — in the direction of withdrawing a block.
            ("v1", "reels_tab", ["feed/gone/"], {}),
        )
        found, _ = reconsiderations(self.tmp, version="441")
        evidence = "\n".join(found[0].evidence)
        self.assertIn("BOUNDED BY THE SURFACES WALKED", evidence)
        self.assertEqual("measured on: explore_tab, feed_tab",
                         [l for l in found[0].evidence if l.startswith("measured on:")][0])
        self.assertNotIn("reels_tab", evidence)
        self.assertIn("feed/busy/ x42", evidence)
        self.assertIn("observed 0 times", evidence)
        # Two sessions, not three. The vacuous one is not counted as evidence
        # anywhere the finding speaks.
        self.assertIn("2 session(s)", found[0].summary)

    def test_it_groups_with_the_other_rules_in_both_output_forms(self) -> None:
        """Two rules on one decision is one question for a human; the report has
        to carry both without either form going quieter than the other."""
        self.blocker(deps=["feed/gone/"], ran=False)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 40}))

        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual({"block_inert", "block_never_observed"},
                         {item.trigger for item in found})

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, main(["--root", str(self.tmp), "--version", "441", "--json"]))
        payload = json.loads(out.getvalue())
        self.assertEqual([item.to_dict() for item in found], payload["reconsiderations"])
        self.assertEqual(not_run, payload["rules_not_run"])

        text = io.StringIO()
        with contextlib.redirect_stdout(text), contextlib.redirect_stderr(io.StringIO()):
            main(["--root", str(self.tmp), "--version", "441"])
        self.assertIn("[block_never_observed] block: feed/gone/", text.getvalue())
        for line in payload["rules_not_run"]:
            self.assertIn(line, text.getvalue())


class BlockingHookAttributionTests(EnforcementTestCase):
    """Which hook enforces the block decides whether the block is inert.

    The whole rule turns on it: `block_inert` asks whether *that* hook has ever
    executed, so naming the wrong one answers a question about a different hook
    and reports — or fails to report — the wrong thing. With one hook in the
    manifest the distinction is invisible, which is why these fixtures carry two.
    """

    def test_the_declaring_hook_is_found_by_its_dep_not_by_file_order(self) -> None:
        """A hook that ran stands first in the manifest; the blocker does not.

        Taking the first entry answers "has `noise` ever run?" — yes — and the
        inert block goes unreported.
        """
        self.blocker(
            deps=["feed/guarded/"], ran=False,
            others=[("noise", ["some/other/path/"], True)],
        )
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")

        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual([("block_inert", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertIn("blocker", found[0].summary)
        self.assertNotIn("noise", found[0].summary)
        self.assertIn("enforced by blocker, which ran on: none", found[0].evidence)

    def test_a_hook_that_declares_nothing_relevant_is_not_blamed(self) -> None:
        """The mirror image: the blocker runs and the other hook does not.

        Picking by file order here would report an inert block that is not one.
        """
        self.blocker(
            deps=["feed/guarded/"], ran=True,
            others=[("noise", ["some/other/path/"], False)],
        )
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_a_ruled_endpoint_no_hook_declares_produces_no_finding(self) -> None:
        """Today's behaviour, pinned so a change to it is visible.

        Nothing in the manifest carries this endpoint, so there is no hook whose
        execution could be checked. The ruling exists and the block does not.
        """
        self.blocker(
            deps=["feed/guarded/"], ran=False,
            others=[("noise", ["some/other/path/"], False)],
        )
        self.rulings("feed/never_declared/")
        self.app_source("feed/guarded/")
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertEqual([], found)
        self.assertEqual([], [line for line in not_run if line.startswith("block_inert")])


class CommittedCorpusAttributionTests(unittest.TestCase):
    """Why the real corpus is empty — stated so the emptiness is attributable.

    `test_nothing_recorded_here_has_stopped_matching` passes for two independent
    reasons, and would keep passing if either disappeared. These pin both, so a
    port that changes one of them fails here rather than quietly making that
    test vacuous.
    """

    def test_the_ruled_endpoints_split_into_unenforced_and_enforced(self) -> None:
        """Reason one, and the exact population it still covers.

        Until 2026-08-08 every ruled endpoint was unenforced and this reason
        covered all six on its own. Five then gained guards, so they are held
        silent by reason two alone — which is precisely the "would keep passing
        if either disappeared" this class exists to prevent. Asserting the split
        rather than the equality is what keeps it visible: an endpoint moves from
        one side to the other as its guard is written, and both sides are named
        here. The one that remains cannot move, because it is not a request path.
        """
        from dfinsta_pipeline.rulings import (
            DEFAULT_SOURCE_PATH,
            read_store,
            unenforced_endpoints,
        )

        ruled = sorted(
            item.candidate_id.split(":", 1)[-1]
            for item in read_store(REPOSITORY / "manifest" / "rulings.jsonl")
            if item.verdict == "block"
        )
        unenforced = sorted(unenforced_endpoints(
            REPOSITORY / "manifest" / "hooks.json", REPOSITORY / DEFAULT_SOURCE_PATH
        ))
        self.assertEqual(6, len(ruled))
        # Every unenforced endpoint is one that was ruled: the audit must never
        # report a gap this class has not accounted for.
        self.assertEqual([], sorted(set(unenforced) - set(ruled)))
        self.assertEqual(["delivery/background_prefetch"], unenforced)
        self.assertEqual(
            [
                "feed/injected_reels_media/",
                "feed/reels_media/",
                "feed/reels_media_stream/",
                "feed/text_post_app_timeline",
                "feed/timeline_stream/",
            ],
            sorted(set(ruled) - set(unenforced)),
        )

    def test_the_hook_that_would_be_judged_does_execute(self) -> None:
        """The second, independent reason the report is empty.

        `block_inert` only fires on a hook that has never run, and it fires only
        on an *enforced* block. Five endpoints became enforced on 2026-08-08, so
        this is now the only thing keeping them out of the report. If
        `tigon_url_block` ever stops executing they become the exact population
        the skip protects, which is when the skip earns its keep and when this
        assertion should fail and be looked at.
        """
        from dfinsta_pipeline.roster import roster as read_roster

        lives = {life.hook_id: life for life in read_roster(REPOSITORY)[0]}
        self.assertFalse(lives["tigon_url_block"].never_ran)

    def test_every_finding_carries_a_closed_trigger(self) -> None:
        found, _ = reconsiderations(REPOSITORY, version="441")
        for item in found:
            self.assertIn(item.trigger, TRIGGERS)


class RetirementReturnedBoundaryTests(EnforcementTestCase):
    def retire(self, hook: str, effective_from: str, decision_id: str = "retire-1") -> None:
        (self.manifest / "retirements.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "hook_id": hook, "effective_from": effective_from,
                "decision_id": decision_id, "ruled_by": "arnav",
                "rationale": "believed gone", "recorded_at": "2026-08-08T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )

    def series(self, hook: str, ran: dict[str, bool]) -> None:
        (self.manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "policy_revision": "2026-08-01", "hooks": [
                {"hook_id": hook, "intent": "x", "tier": "robust", "status": "active",
                 "strategy": "insert", "semantic_deps": []}
            ]}),
            encoding="utf-8",
        )
        for version, ok in ran.items():
            (self.manifest / "runtime_evidence" / f"{version}.jsonl").write_text(
                claim(hook, "runtime_probe", version, "passed" if ok else "inconclusive") + "\n",
                encoding="utf-8",
            )

    def test_running_on_the_effective_version_itself_is_a_return(self) -> None:
        """`>=`, not `>`. A retirement takes effect at N and the hook ran at N."""
        self.series("back", {"441": True})
        self.retire("back", "441")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual(["retirement_returned"], [item.trigger for item in found])

    def test_only_the_versions_at_or_after_the_retirement_are_named(self) -> None:
        """Dropping the comparison makes every retirement of a once-working hook
        report itself the moment it is recorded — and the summary would name
        versions from before the decision as evidence against it."""
        self.series("back", {"439": True, "440": False, "441": True})
        self.retire("back", "441")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual(1, len(found))
        item = found[0]
        self.assertIn("executed since, on 441", item.summary)
        self.assertNotIn("439", item.summary)
        # The evidence line is the full history and is allowed to name 439.
        self.assertIn("ran on: 439, 441", item.evidence)
        self.assertEqual("retirement", item.kind)
        self.assertEqual("back", item.subject)

    def test_a_hook_that_only_ran_before_the_retirement_is_not_a_return(self) -> None:
        """The positive control for the test above, one version apart."""
        self.series("back", {"439": True, "440": True, "441": False})
        self.retire("back", "441")
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_a_retired_hook_that_has_never_run_is_not_a_return(self) -> None:
        self.series("back", {"441": False})
        self.retire("back", "439")
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_a_retirement_for_a_hook_no_longer_in_the_manifest_is_skipped(self) -> None:
        self.series("still_here", {"441": True})
        self.retire("gone_from_the_manifest", "439")
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])

    def test_the_report_names_the_decision_a_withdrawal_would_be_recorded_against(self) -> None:
        self.series("back", {"441": True})
        self.retire("back", "441", decision_id="retire-abcdef123456")
        found, _ = reconsiderations(self.tmp, version="441")
        self.assertEqual("retire-abcdef123456", found[0].original_decision_id)
        self.assertIn("withdraws retire-abcdef123456", render(found, [], "441"))


class BothHalvesOfTheResultTests(EnforcementTestCase):
    """Findings and skipped rules are both the result, in every output form."""

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, out.getvalue()

    def corpus_with_both(self) -> None:
        """A finding and a skipped rule at the same time.

        The JSON form is the one a release script gates on. A `--json` that
        carried the findings and dropped the skips would let "the index rule
        never ran" read as "nothing wrong", which is the shape this project
        already shipped once — the human form printed a NOT CHECKED banner and
        the machine form had no such field.
        """
        self.blocker(deps=["feed/guarded/"], ran=False)
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")

    def test_the_json_form_carries_both_halves(self) -> None:
        self.corpus_with_both()
        found, not_run = reconsiderations(self.tmp, version="441")
        self.assertTrue(found)
        self.assertTrue(not_run)

        code, out = self.run_main(["--root", str(self.tmp), "--version", "441", "--json"])
        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual(
            {"schema_version", "version", "reconsiderations", "rules_not_run"}, set(payload)
        )
        self.assertEqual(not_run, payload["rules_not_run"])
        self.assertEqual([item.to_dict() for item in found], payload["reconsiderations"])
        self.assertEqual("441", payload["version"])

    def test_the_human_form_carries_both_halves(self) -> None:
        self.corpus_with_both()
        code, out = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(0, code)
        self.assertIn("RULES NOT RUN", out)
        self.assertIn("block_endpoint_absent", out)
        self.assertIn("feed/guarded/", out)
        self.assertIn("withdraws decision-1", out)

    def test_the_two_forms_agree_about_the_skipped_rules(self) -> None:
        """Not just present in both: the same list in both."""
        self.corpus_with_both()
        _, as_json = self.run_main(["--root", str(self.tmp), "--version", "441", "--json"])
        _, as_text = self.run_main(["--root", str(self.tmp), "--version", "441"])
        for line in json.loads(as_json)["rules_not_run"]:
            self.assertIn(line, as_text)

    def test_a_clean_result_still_reports_what_did_not_run(self) -> None:
        """The dangerous direction: nothing found AND a rule skipped."""
        self.blocker(deps=["feed/guarded/"], ran=True)
        self.rulings("feed/guarded/")
        self.app_source("feed/guarded/")
        code, out = self.run_main(["--root", str(self.tmp), "--version", "441", "--json"])
        payload = json.loads(out)
        self.assertEqual(0, code)
        self.assertEqual([], payload["reconsiderations"])
        self.assertTrue(payload["rules_not_run"])

        _, text = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertIn("Nothing recorded has stopped matching", text)
        self.assertIn("RULES NOT RUN", text)

    def test_render_omits_the_banner_only_when_nothing_was_skipped(self) -> None:
        """The banner has to be absent sometimes, or its presence proves nothing."""
        self.assertNotIn("RULES NOT RUN", render([], [], "441"))
        self.assertIn("RULES NOT RUN", render([], ["block_endpoint_absent: no index"], "441"))


class IndexRuleTests(EnforcementTestCase):
    def write_index(self, api_paths: dict[str, list[str]]) -> Path:
        index = self.tmp / "index"
        index.mkdir(exist_ok=True)
        (index / "header.json").write_text(
            json.dumps({"schema_version": 1, "decode_path": "/decode/441"}), encoding="utf-8"
        )
        (index / "api_surface.json").write_text(
            json.dumps({"api_paths": api_paths}), encoding="utf-8"
        )
        # `structural.jsonl` too: `reconsider` now loads through
        # `HookIndex.load`, which refuses an incomplete index rather than
        # constructing one past every shape check. An index missing a file is a
        # real problem, so the fixture provides a real index.
        (index / "structural.jsonl").write_text("", encoding="utf-8")
        return index

    def test_an_endpoint_absent_from_the_index_is_reported(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        # Observed on the phone, so `block_never_observed` runs and stays silent.
        # Without this the corpus cannot reach `not_run == []`, and an assertion
        # that no rule was skipped is the strongest thing this test says.
        self.observed(("s1", "feed_tab", ["feed/gone/"], {"feed/gone/": 3}))
        index = self.write_index({"feed/still_here/": ["LX/01ab;"]})
        found, not_run = reconsiderations(self.tmp, version="441", index_dir=index)
        self.assertEqual([("block_endpoint_absent", "feed/gone/")],
                         [(item.trigger, item.subject) for item in found])
        self.assertEqual([], not_run)

    def test_an_endpoint_present_under_any_spelling_is_not_reported(self) -> None:
        """The manifest normalises a leading slash the index keeps. Reading one
        spelling is how an entire grouping went invisible on 440."""
        for spelling in ("feed/gone/", "/feed/gone/", "feed/gone", "/feed/gone"):
            with self.subTest(spelling=spelling):
                self.blocker(deps=["feed/gone/"], ran=True)
                self.rulings("feed/gone/")
                self.app_source("feed/gone/")
                index = self.write_index({spelling: ["LX/01ab;"]})
                found, _ = reconsiderations(self.tmp, version="441", index_dir=index)
                self.assertEqual([], found, spelling)

    def test_a_withdrawn_block_is_not_questioned_again_when_its_endpoint_vanishes(self):
        """The last of the three rules to consult `withdrawn`, and it did not.

        `retirement_returned` reads through `retirements_on_record`, and
        `block_inert` is silenced only *indirectly* — `apply_unblock` removes the
        dep, so `_blocking_hook` returns "". Neither of those protects this rule,
        so a block that a human withdrew and whose endpoint later disappeared
        would be proposed for withdrawal a second time. A reversal is a decision
        taken for ever; re-asking is the false alarm that stops a gate being
        answered twice.

        Both halves, so neither can pass by accident: it fires before the
        withdrawal is recorded and is silent after.
        """
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/"], {"feed/gone/": 3}))
        index = self.write_index({"feed/still_here/": ["LX/01ab;"]})

        found, _ = reconsiderations(self.tmp, version="441", index_dir=index)
        self.assertEqual(
            [("block_endpoint_absent", "feed/gone/")],
            [(i.trigger, i.subject) for i in found],
            "the positive control: without a withdrawal this rule must fire",
        )

        (self.manifest / "reversals.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "withdraws": "block",
                "subject": "/feed/gone/",
                "original_decision_id": "decision-1", "decision_id": "withdraw-1",
                "ruled_by": "arnav", "rationale": "measured, never requested",
                "recorded_at": "2026-08-09T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        found, not_run = reconsiderations(self.tmp, version="441", index_dir=index)
        self.assertEqual([], found)
        self.assertEqual([], not_run, "and no rule may have been skipped to achieve it")

    def test_supplying_an_index_removes_the_skip_and_only_that_skip(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        self.observed(("s1", "feed_tab", ["feed/gone/"], {"feed/gone/": 3}))
        _, without = reconsiderations(self.tmp, version="441")
        _, with_index = reconsiderations(
            self.tmp, version="441", index_dir=self.write_index({"feed/gone/": ["LX/01ab;"]})
        )
        self.assertTrue(any("block_endpoint_absent" in line for line in without))
        self.assertEqual([], with_index)

    def test_an_absent_index_directory_is_refused_by_name(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        with self.assertRaises(ReconsiderError) as caught:
            reconsiderations(self.tmp, version="441", index_dir=self.tmp / "nowhere")
        self.assertIn("nowhere", str(caught.exception))

    def test_a_malformed_index_file_is_refused_by_name(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        index = self.write_index({})
        (index / "api_surface.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ReconsiderError) as caught:
            reconsiderations(self.tmp, version="441", index_dir=index)
        self.assertIn(str(index), str(caught.exception))

    def test_the_cli_refuses_a_bad_index_with_exit_two(self) -> None:
        self.blocker(deps=["feed/gone/"], ran=True)
        self.rulings("feed/gone/")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main([
                "--root", str(self.tmp), "--version", "441",
                "--index", str(self.tmp / "nowhere"),
            ])
        self.assertEqual(2, code)


class ScopingTests(EnforcementTestCase):
    """Every read must be decided by `--root`, including the app source.

    `unenforced_endpoints` defaults its source to a path relative to the process
    CWD. Passing only the manifest made a `--root /elsewhere` run read that
    root's manifest against *this repository's* app source — half-scoped, which
    is worse than unscoped because it looks right.
    """

    def decoy(self) -> Path:
        """A second repository, contradicting the corpus in every answer."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        decoy = Path(directory.name)
        manifest = decoy / "manifest"
        (manifest / "runtime_evidence").mkdir(parents=True)
        (manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "hooks": [
                {"hook_id": "DECOY", "intent": "wrong", "tier": "x", "status": "active",
                 "strategy": "url_block", "semantic_deps": ["feed/unguarded/"]}
            ]}),
            encoding="utf-8",
        )
        (manifest / "runtime_evidence" / "441.jsonl").write_text(
            claim("DECOY", "runtime_probe", "441", "passed") + "\n", encoding="utf-8"
        )
        # The decoy guards the OPPOSITE endpoint, so a CWD-relative read of the
        # app source flips which finding is produced rather than hiding.
        source = decoy / APP_SOURCE
        source.parent.mkdir(parents=True)
        source.write_text(
            GUARD_SOURCE.format(literals='    const-string v1, "feed/unguarded/"'),
            encoding="utf-8",
        )
        (manifest / "rulings.jsonl").write_text(
            json.dumps({"record": {
                "assessment_sha256": "a" * 64, "candidate_id": "gap:feed/decoy/",
                "decision_id": "decoy-decision", "policy_revision": "2026-08-01",
                "rationale": "wrong root", "recorded_at": "2026-01-01T00:00:00Z",
                "run_id": "decoy", "verdict": "block",
            }, "schema_version": 1}) + "\n",
            encoding="utf-8",
        )
        return decoy

    def test_the_answer_does_not_change_with_the_process_directory(self) -> None:
        self.blocker(deps=["feed/guarded/", "feed/unguarded/"], ran=False)
        self.rulings("feed/guarded/", "feed/unguarded/")
        self.app_source("feed/guarded/")
        here = reconsiderations(self.tmp, version="441")

        previous = os.getcwd()
        os.chdir(self.decoy())
        self.addCleanup(os.chdir, previous)
        there = reconsiderations(self.tmp, version="441")

        self.assertEqual(
            ([item.to_dict() for item in here[0]], here[1]),
            ([item.to_dict() for item in there[0]], there[1]),
        )
        self.assertEqual([("block_inert", "feed/guarded/")],
                         [(item.trigger, item.subject) for item in there[0]])
        self.assertEqual([], [line for line in there[1] if line.startswith("block_inert")])
        self.assertNotIn("decoy-decision", str(there))

    def test_the_decoy_is_a_real_control(self) -> None:
        """The decoy must be readable as a root, or standing in it proves nothing."""
        decoy = self.decoy()
        found, not_run = reconsiderations(decoy, version="441")
        self.assertEqual([], found)
        self.assertEqual([], [line for line in not_run if line.startswith("block_inert")])


class MoreRefusalTests(EnforcementTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, err.getvalue()

    def test_every_shape_of_bad_version_is_refused(self) -> None:
        self.blocker(deps=[], ran=True)
        for version in ("v441", "", "441 ", " 441", "441.0", "-441", "44_1", "四四一"):
            with self.subTest(version=version):
                with self.assertRaises(ReconsiderError) as caught:
                    reconsiderations(self.tmp, version=version)
                self.assertIn("not a version number", str(caught.exception))

    def test_a_good_version_is_accepted(self) -> None:
        """The positive control: the guard above must not reject everything.

        The `not_run` list is asserted exactly, not by prefix. A rule that stops
        reporting itself skipped is the failure both halves of the result exist
        to prevent, and only an equality catches one going quiet.
        """
        from dfinsta_pipeline.observation import store_path

        self.blocker(deps=[], ran=True)
        self.app_source()
        self.assertEqual(
            ([], ["block_endpoint_absent: no --index given, so whether these endpoints "
                  "still exist in the app was not checked",
                  "block_never_observed: there is no observation evidence for 441 "
                  f"({store_path('441', self.tmp)} holds no session). Nothing can be "
                  "said about what the app never requested until something recorded "
                  "what it did"]),
            reconsiderations(self.tmp, version="441"),
        )

    def test_a_corrupt_rulings_store_is_refused_by_line(self) -> None:
        self.blocker(deps=["feed/x/"], ran=True)
        self.rulings("feed/x/")
        self.app_source("feed/x/")
        # The positive control: this corpus reads cleanly before it is broken.
        self.assertEqual([], reconsiderations(self.tmp, version="441")[0])
        (self.manifest / "rulings.jsonl").write_text("\nnot json\n", encoding="utf-8")
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("rulings.jsonl:2:", err)
        self.assertTrue(err.startswith("refused:"), err)

    def test_a_ruling_record_missing_a_field_is_refused_by_line(self) -> None:
        self.blocker(deps=["feed/x/"], ran=True)
        (self.manifest / "rulings.jsonl").write_text(
            json.dumps({"schema_version": 1, "record": {"candidate_id": "gap:feed/x/",
                                                        "verdict": "block"}}) + "\n",
            encoding="utf-8",
        )
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("rulings.jsonl:1:", err)

    def test_a_corrupt_retirements_file_is_refused_by_line(self) -> None:
        self.blocker(deps=[], ran=True)
        (self.manifest / "retirements.jsonl").write_text("not json\n", encoding="utf-8")
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("retirements.jsonl:1:", err)

    def test_a_corrupt_runtime_claim_is_refused_by_line(self) -> None:
        self.blocker(deps=[], ran=True)
        (self.manifest / "runtime_evidence" / "441.jsonl").write_text(
            claim("blocker", "runtime_probe", "441", "passed") + "\nnot json\n", encoding="utf-8"
        )
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("441.jsonl:2:", err)

    def test_an_unreadable_manifest_is_refused_not_tracebacked(self) -> None:
        self.blocker(deps=[], ran=True)
        (self.manifest / "hooks.json").write_text("{ not json", encoding="utf-8")
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("hooks.json", err)

    def test_no_evidence_at_all_is_refused(self) -> None:
        self.blocker(deps=[], ran=True)
        (self.manifest / "runtime_evidence" / "441.jsonl").unlink()
        code, err = self.run_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)
        self.assertIn("no committed evidence", err)

    def test_a_missing_root_is_refused(self) -> None:
        code, _ = self.run_main([
            "--root", str(self.tmp / "nowhere"), "--version", "441",
        ])
        self.assertEqual(2, code)

    def test_a_bad_baseline_is_refused(self) -> None:
        self.blocker(deps=[], ran=True)
        code, err = self.run_main([
            "--root", str(self.tmp), "--version", "441", "--baseline", "nope",
        ])
        self.assertEqual(2, code)
        self.assertIn("not a version number", err)

    def test_a_blank_subject_or_decision_id_cannot_be_constructed(self) -> None:
        for subject, decision in ((" ", "d"), ("", "d"), ("s", " "), ("s", "")):
            with self.subTest(subject=subject, decision=decision):
                with self.assertRaises(ReconsiderError):
                    Reconsideration("block", subject, decision, TRIGGERS[0], "", ())
        # The positive control: a well-formed one constructs.
        self.assertEqual("s", Reconsideration("block", "s", "d", TRIGGERS[0], "", ()).subject)

    def test_the_trigger_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            ("block_inert", "block_endpoint_absent", "block_never_observed",
             "retirement_returned"),
            TRIGGERS,
        )
        for trigger in TRIGGERS:
            Reconsideration("block", "s", "d", trigger, "", ())


class ExitCodeTests(EnforcementTestCase):
    """A proposal must never fail a port, in any output form, with any finding."""

    def run_main(self, argv: list[str]) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_zero_with_findings_from_every_rule_at_once(self) -> None:
        """Exit 3 here would make the fastest route to a green build the
        approval of a withdrawal — the pressure the whole design avoids."""
        self.blocker(deps=["feed/gone/"], ran=False)
        self.rulings("feed/gone/")
        self.app_source("feed/gone/")
        # Watched on a session that saw plenty of other traffic and never this —
        # so the third block rule fires too, and `not_run` can still be empty.
        self.observed((
            "s1", "feed_tab", ["feed/gone/", "feed/busy/"], {"feed/busy/": 12},
        ))
        (self.manifest / "retirements.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "hook_id": "blocker", "effective_from": "441",
                "decision_id": "retire-1", "ruled_by": "arnav", "rationale": "gone",
                "recorded_at": "2026-08-08T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        index = self.tmp / "index"
        index.mkdir()
        (index / "header.json").write_text(
            json.dumps({"schema_version": 1, "decode_path": "/decode/441"}), encoding="utf-8"
        )
        (index / "api_surface.json").write_text(
            json.dumps({"api_paths": {}}), encoding="utf-8"
        )
        (index / "structural.jsonl").write_text("", encoding="utf-8")

        found, not_run = reconsiderations(self.tmp, version="441", index_dir=index)
        self.assertEqual(
            {"block_inert", "block_endpoint_absent", "block_never_observed"},
            {item.trigger for item in found},
        )
        self.assertEqual([], not_run)
        self.assertEqual(0, self.run_main([
            "--root", str(self.tmp), "--version", "441", "--index", str(index)]))
        self.assertEqual(0, self.run_main([
            "--root", str(self.tmp), "--version", "441", "--index", str(index), "--json"]))

    def test_zero_on_a_clean_corpus_in_both_forms(self) -> None:
        self.blocker(deps=[], ran=True)
        self.assertEqual(0, self.run_main(["--root", str(self.tmp), "--version", "441"]))
        self.assertEqual(0, self.run_main([
            "--root", str(self.tmp), "--version", "441", "--json"]))


if __name__ == "__main__":
    unittest.main()
