"""The only way the expectation's bar comes down, and the only way it goes back up.

`expectation` is a ratchet, and a ratchet with no release is a trap: when
Instagram genuinely removes a surface, the hook that patched it can never pass
again. Shopping already did this once. So a retirement subtracts a hook — and
every test here is about the subtraction being *hard to abuse*, because a bar
that can be lowered easily is not a bar.

The two rules kept from the much larger machinery deleted on 2026-08-08:
`effective_from` is derived so a retirement cannot be backdated onto the port that
exposed the drop, and an agent may assemble every fact and still not rule.

And un-retirement is a **row**, never an edit — so the record still says a hook
was once doubted after Instagram brings its surface back.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_expectation import ExpectationTestCase, triple

from dfinsta_pipeline.expectation import compare
from dfinsta_pipeline.retirement import (
    KINDS,
    latest_ported,
    Retirement,
    RetirementError,
    append,
    history,
    main,
    read,
    render,
    retired_at,
    returned,
)


def record(hook="settings_hook", *, kind="retire", at="441", by="arnav", why="surface removed"):
    """One decision, with `effective_from` derived the way the module derives it."""
    return Retirement(
        kind=kind, hook_id=hook, effective_from=str(int(at) + 1), decided_at=at,
        ruled_by=by, rationale=why, recorded_at="2026-08-09T10:00:00+00:00",
    )


class RetirementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)


class TheRulesWorthKeepingTests(RetirementTestCase):
    def test_effective_from_must_be_the_version_after_the_decision(self):
        """Backdating is 'approve your way out of a red build' wearing a date."""
        for effective in ("441", "440", "443"):
            with self.subTest(effective=effective):
                with self.assertRaises(RetirementError) as caught:
                    Retirement(
                        kind="retire", hook_id="h", effective_from=effective, decided_at="441",
                        ruled_by="arnav", rationale="x", recorded_at="t",
                    )
                self.assertIn("derived, never supplied", str(caught.exception))
        # The control: the one value that is allowed.
        self.assertEqual("442", record(at="441").effective_from)

    def test_an_agent_may_not_rule(self):
        """The thing being measured must not rule that the measurement no longer applies."""
        for name in ("agent", "AGENT", " Agent "):
            with self.subTest(ruled_by=name):
                with self.assertRaises(RetirementError) as caught:
                    record(by=name)
                self.assertIn("may assemble every fact", str(caught.exception))
        self.assertEqual("arnav", record(by="arnav").ruled_by)

    def test_a_decision_must_carry_who_why_and_when(self):
        for field, value in (("by", "   "), ("why", ""), ):
            with self.subTest(field=field):
                with self.assertRaises(RetirementError):
                    record(**{field: value})
        with self.assertRaises(RetirementError):
            Retirement(kind="retire", hook_id="h", effective_from="442", decided_at="441",
                       ruled_by="arnav", rationale="x", recorded_at="  ")
        with self.assertRaises(RetirementError):
            record(hook="  ")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(RetirementError) as caught:
            record(kind="delete")
        self.assertIn("unknown kind", str(caught.exception))
        self.assertEqual(("retire", "unretire"), KINDS)


class TheFoldTests(RetirementTestCase):
    """`retired_at` is a fold over rows, which is what makes history survivable."""

    def test_a_hook_is_retired_from_its_effective_version_and_not_before(self):
        append(record(at="441"), self.tmp)
        self.assertEqual(frozenset(), retired_at("441", self.tmp), "not yet in force at 441")
        self.assertEqual(frozenset({"settings_hook"}), retired_at("442", self.tmp))
        self.assertEqual(frozenset({"settings_hook"}), retired_at("450", self.tmp))

    def test_un_retiring_puts_it_back_and_keeps_both_rows(self):
        """The whole reason un-retirement is a row: the doubt stays on the record."""
        append(record(at="441"), self.tmp)
        append(record(at="444", kind="unretire", why="the surface came back on 444"), self.tmp)

        self.assertEqual(frozenset({"settings_hook"}), retired_at("443", self.tmp))
        self.assertEqual(frozenset(), retired_at("445", self.tmp), "expected again from 445")

        told = history("settings_hook", self.tmp)
        self.assertEqual(["retire", "unretire"], [item.kind for item in told])
        self.assertEqual(["442", "445"], [item.effective_from for item in told])
        self.assertIn("surface removed", told[0].rationale)

    def test_a_hook_can_be_retired_again_after_coming_back(self):
        append(record(at="441"), self.tmp)
        append(record(at="444", kind="unretire", why="back"), self.tmp)
        append(record(at="450", why="gone for good"), self.tmp)
        self.assertEqual(frozenset({"settings_hook"}), retired_at("451", self.tmp))
        self.assertEqual(3, len(history("settings_hook", self.tmp)))

    def test_hooks_are_independent(self):
        append(record("a", at="441"), self.tmp)
        append(record("b", at="441"), self.tmp)
        append(record("a", at="444", kind="unretire", why="back"), self.tmp)
        self.assertEqual(frozenset({"b"}), retired_at("445", self.tmp))
        self.assertEqual((), history("never_mentioned", self.tmp))


class NoOpTests(RetirementTestCase):
    """A row that changes nothing would read as somebody changing their mind twice."""

    def test_retiring_an_already_retired_hook_is_refused(self):
        append(record(at="441"), self.tmp)
        with self.assertRaises(RetirementError) as caught:
            append(record(at="442"), self.tmp)
        self.assertIn("already retired", str(caught.exception))

    def test_un_retiring_something_that_is_not_retired_is_refused(self):
        with self.assertRaises(RetirementError) as caught:
            append(record(at="441", kind="unretire", why="never retired"), self.tmp)
        self.assertIn("nothing to un-retire", str(caught.exception))
        # And the control: legitimate after a retirement.
        append(record(at="441"), self.tmp)
        append(record(at="444", kind="unretire", why="back"), self.tmp)
        self.assertEqual(2, len(read(self.tmp)))

    def test_appending_never_rewrites_what_is_there(self):
        append(record("a", at="441"), self.tmp)
        first = (self.tmp / "manifest" / "retirements.jsonl").read_text(encoding="utf-8")
        append(record("b", at="441"), self.tmp)
        after = (self.tmp / "manifest" / "retirements.jsonl").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(first), "an earlier row was rewritten")


class ReadingTests(RetirementTestCase):
    def test_an_absent_store_is_empty_but_an_unreadable_one_is_refused(self):
        """Absent and unreadable have been conflated in four modules here.

        Absent genuinely means nothing has been retired — the state this project
        was in for its whole life. Unreadable reported as empty would silently
        restore the trap and every hook would look expected again.
        """
        self.assertEqual((), read(self.tmp))

        path = self.tmp / "manifest" / "retirements.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(RetirementError) as caught:
            read(self.tmp)
        self.assertIn("line 1", str(caught.exception))

    def test_a_row_missing_a_field_is_refused_by_name(self):
        path = self.tmp / "manifest" / "retirements.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": "retire", "hook_id": "h"}) + "\n", encoding="utf-8")
        with self.assertRaises(RetirementError) as caught:
            read(self.tmp)
        self.assertIn("missing", str(caught.exception))

    def test_a_hand_edited_effective_version_is_refused_on_READ(self):
        """The derivation is re-checked on read, not only on construction.

        Otherwise the rule would hold for anything this module wrote and not for
        the file, which is the thing a human can open in an editor.
        """
        path = self.tmp / "manifest" / "retirements.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = record(at="441").to_dict()
        row["effective_from"] = "441"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(RetirementError) as caught:
            read(self.tmp)
        self.assertIn("derived, never supplied", str(caught.exception))


class ReturnedTests(RetirementTestCase):
    """Instagram brings surfaces back. This reports it; it never rules."""

    def evidence(self, version, rows):
        directory = self.tmp / "manifest" / "runtime_evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{version}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    def probe(self, hook, verdict="passed"):
        return {"hook_id": hook, "kind": "runtime_probe", "verdict": verdict, "version": "445"}

    def test_a_retired_hook_that_passes_again_is_reported(self):
        append(record("settings_hook", at="441"), self.tmp)
        self.evidence("445", [self.probe("settings_hook"), self.probe("other")])
        self.assertEqual(("settings_hook",), returned("445", self.tmp))

    def test_a_hook_that_is_not_retired_is_not_reported(self):
        append(record("settings_hook", at="441"), self.tmp)
        self.evidence("445", [self.probe("other")])
        self.assertEqual((), returned("445", self.tmp))

    def test_an_inconclusive_probe_is_not_a_return(self):
        append(record("settings_hook", at="441"), self.tmp)
        self.evidence("445", [self.probe("settings_hook", verdict="inconclusive")])
        self.assertEqual((), returned("445", self.tmp))

    def test_an_un_retired_hook_stops_being_reported(self):
        append(record("settings_hook", at="441"), self.tmp)
        append(record("settings_hook", at="444", kind="unretire", why="back"), self.tmp)
        self.evidence("445", [self.probe("settings_hook")])
        self.assertEqual((), returned("445", self.tmp), "it is expected again; nothing to report")

    def test_absent_evidence_is_refused_rather_than_read_as_none_came_back(self):
        append(record("settings_hook", at="441"), self.tmp)
        with self.assertRaises(RetirementError) as caught:
            returned("445", self.tmp)
        self.assertIn("never measured", str(caught.exception))

    def test_nothing_retired_needs_no_evidence(self):
        """The one case where absent evidence is not a refusal: there is no question."""
        self.assertEqual((), returned("445", self.tmp))


class RenderAndCliTests(RetirementTestCase):
    def test_the_record_shows_both_decisions_and_what_is_retired_now(self):
        append(record("settings_hook", at="441"), self.tmp)
        append(record("settings_hook", at="444", kind="unretire", why="back on 444"), self.tmp)
        out = render("450", self.tmp)
        self.assertIn("RETIRED", out)
        self.assertIn("UN-RETIRED", out)
        self.assertIn("back on 444", out)
        self.assertIn("retired now (0)", out)

    def test_an_empty_record_says_so(self):
        self.assertIn("nothing has ever been retired", render("441", self.tmp))

    def test_the_cli_refuses_when_there_is_no_tree_to_read_the_version_from(self):
        """`decided_at` comes from the tree, so an empty tree has no answer.

        It must refuse rather than guess. Guessing here would reintroduce exactly
        the hole this module had on its first day.
        """
        code = main([
            "--root", str(self.tmp), "retire", "--hook", "h",
            "--ruled-by", "arnav", "--rationale", "x", "--recorded-at", "t",
        ])
        self.assertEqual(2, code)
        self.assertEqual((), read(self.tmp))

    def test_the_cli_offers_no_way_to_name_the_version(self):
        """The hole that made the backdating rule a formality.

        `--version` and `--effective-from` must both be rejected by argparse, not
        merely ignored: an accepted-and-ignored flag reads to the operator as
        having worked.
        """
        for flag in ("--version", "--effective-from", "--decided-at"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit):
                    main(["--root", str(self.tmp), "retire", "--hook", "h", flag, "440",
                          "--ruled-by", "arnav", "--rationale", "x", "--recorded-at", "t"])

    def test_the_cli_refuses_an_agent_with_exit_two(self):
        code = main([
            "--root", str(self.tmp), "retire", "--hook", "h",
            "--ruled-by", "agent", "--rationale", "x", "--recorded-at", "t",
        ])
        self.assertEqual(2, code)
        self.assertEqual((), read(self.tmp))


class SurvivorTests(RetirementTestCase):
    """Gaps an adversarial mutation pass found. Each names the mutation it kills."""

    def test_an_unreadable_file_is_refused_and_not_read_as_empty(self):
        """M17: `read` returning () on OSError survived the whole suite.

        The parse-error half was covered and the OS-error half was not, so
        "absent" and "unreadable" were still conflated for exactly the case a
        permissions or directory mistake produces.
        """
        path = self.tmp / "manifest" / "retirements.jsonl"
        path.mkdir(parents=True)  # a directory where a file belongs: read() raises OSError
        with self.assertRaises(RetirementError):
            read(self.tmp)

    def test_a_non_numeric_version_is_refused(self):
        """M12/M33: the "is not a version number" branch had zero tests."""
        for bad in ("banana", "44.1", "", "4a1", " "):
            with self.subTest(version=bad):
                with self.assertRaises(RetirementError) as caught:
                    retired_at(bad, self.tmp)
                self.assertIn("not a version number", str(caught.exception))
        self.assertEqual(frozenset(), retired_at("441", self.tmp))

    def test_the_fold_follows_effective_order_not_file_order(self):
        """F2: two individually valid rows written out of order folded wrongly.

        `read` re-checks the derivation because hand-editing is the threat model.
        It does not check ordering, so the fold has to.
        """
        path = self.tmp / "manifest" / "retirements.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        later = record("h", at="442", kind="unretire", why="back").to_dict()
        earlier = record("h", at="440", why="gone").to_dict()
        path.write_text(json.dumps(later) + "\n" + json.dumps(earlier) + "\n", encoding="utf-8")

        self.assertEqual(frozenset({"h"}), retired_at("442", self.tmp), "retired from 441")
        self.assertEqual(frozenset(), retired_at("443", self.tmp), "expected again from 443")

    def test_only_a_runtime_probe_counts_as_working_again(self):
        """M27: dropping the kind check made a static pass read as a device return.

        `static_verified` says the literal is in the DEX. It says nothing about the
        hook executing, which is the entire distinction this project exists on.
        """
        append(record("h", at="441"), self.tmp)
        directory = self.tmp / "manifest" / "runtime_evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "445.jsonl").write_text(
            json.dumps({"hook_id": "h", "kind": "static_verified", "verdict": "passed"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual((), returned("445", self.tmp))

    def test_the_record_survives_a_refusal_from_the_return_check(self):
        """F5: `render` called `returned` last, and its refusal discarded everything.

        From the first retirement onward, `show` printed only an error unless a
        runtime evidence file happened to exist — so the command that displays the
        record became useless exactly when there was a record to display.
        """
        append(record("settings_hook", at="441"), self.tmp)
        out = render("445", self.tmp)
        self.assertIn("settings_hook", out)
        self.assertIn("RETIRED", out)
        self.assertIn("could not check for returns", out)

    def test_the_record_shows_when_each_decision_was_taken(self):
        """The audit trail. Without `decided_at` the record cannot be checked.

        The most likely abuse of this module is a decision taken at the wrong
        version; a view that omits when it was taken cannot show it.
        """
        append(record("h", at="441"), self.tmp)
        out = render("441", self.tmp)
        self.assertIn("decided at 441", out)
        self.assertIn("2026-08-09T10:00:00+00:00", out)

    def test_a_returning_hook_is_announced(self):
        """M39: dropping the "working again" block from the render survived."""
        append(record("h", at="441"), self.tmp)
        directory = self.tmp / "manifest" / "runtime_evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "445.jsonl").write_text(
            json.dumps({"hook_id": "h", "kind": "runtime_probe", "verdict": "passed"}) + "\n",
            encoding="utf-8",
        )
        out = render("445", self.tmp)
        self.assertIn("RETIRED AND WORKING AGAIN", out)
        self.assertIn("human decision", out)

    def test_show_prints_the_record(self):
        """M41: `show` rendering and printing nothing survived; it had no test."""
        import contextlib, io

        append(record("settings_hook", at="441"), self.tmp)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--root", str(self.tmp), "show", "--version", "445"])
        self.assertEqual(0, code)
        self.assertIn("settings_hook", buffer.getvalue())


class TheBarActuallyComesDownTests(ExpectationTestCase):
    """The integration that is the whole point: does a retirement lower the bar?

    **The first version of this test proved nothing.** An adversarial pass deleted
    the subtraction from `expectation.compare` entirely and all 3345 tests passed,
    this one included. Its fixture made the retired hook unready on the version
    being compared *against*, so the hook was never in `expected` and there was
    nothing for the subtraction to remove. It asserted a thing that was already
    true without the module — the "assertion that cannot fail" shape, in the one
    test written specifically to avoid it.

    So the fixture below is built the only way that can catch it: the retired hook
    is release-ready on the PREVIOUS version, so it is in `expected` until
    something takes it out.
    """

    def test_a_retirement_is_what_stops_a_hook_being_expected(self):
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        self.port("441", {"kept": triple(), "doomed": triple()}, previous="440")

        # Decided at 441 — the newest port — so it takes effect at 442.
        append(record("doomed", at=latest_ported(self.tmp), why="surface removed"), self.tmp)
        self.assertEqual("441", read(self.tmp)[0].decided_at)

        # 442 loses `doomed`. It was ready on 441, so it IS in `expected` unless
        # the retirement removes it. Delete the subtraction and this goes red.
        self.port("442", {"kept": triple(), "doomed": triple(runtime_probe="inconclusive")},
                  previous="441")
        at442 = compare(self.tmp, version="442")
        self.assertNotIn("doomed", at442.expected, "the retirement did not lower the bar")
        self.assertEqual((), at442.dropped)
        self.assertTrue(all(v.met for v in [at442]) if hasattr(at442, "met") else True)

    def test_without_the_retirement_the_same_corpus_drops_the_hook(self):
        """The control. Identical fixture, no retirement recorded."""
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        self.port("441", {"kept": triple(), "doomed": triple()}, previous="440")
        self.port("442", {"kept": triple(), "doomed": triple(runtime_probe="inconclusive")},
                  previous="441")

        at442 = compare(self.tmp, version="442")
        self.assertIn("doomed", at442.expected)
        self.assertEqual(("doomed",), at442.dropped)

    def test_a_retirement_cannot_rescue_the_port_that_exposed_the_drop(self):
        """It takes effect at 442, so 441 is still red. This is the whole rule."""
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        self.port("441", {"kept": triple(), "doomed": triple(runtime_probe="inconclusive")},
                  previous="440")
        append(record("doomed", at=latest_ported(self.tmp), why="surface removed"), self.tmp)
        self.assertEqual(("doomed",), compare(self.tmp, version="441").dropped)

    def test_a_retired_hook_that_still_passes_is_reported_retired_not_gained(self):
        """Otherwise the report congratulates the port on a hook it gave up on."""
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        self.port("441", {"kept": triple(), "doomed": triple()}, previous="440")
        append(record("doomed", at=latest_ported(self.tmp), why="surface removed"), self.tmp)
        self.port("442", {"kept": triple(), "doomed": triple()}, previous="441")

        states = {v.hook_id: v.state for v in compare(self.tmp, version="442").verdicts}
        self.assertEqual("retired", states["doomed"])
        self.assertEqual("held", states["kept"])

    def test_un_retiring_makes_the_hook_expected_again(self):
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        self.port("441", {"kept": triple(), "doomed": triple()}, previous="440")
        append(record("doomed", at="441", why="surface removed"), self.tmp)
        # The surface comes back on 442 and a human un-retires at 442 -> in force 443.
        self.port("442", {"kept": triple(), "doomed": triple()}, previous="441")
        append(record("doomed", at=latest_ported(self.tmp), kind="unretire", why="it is back"),
               self.tmp)
        self.port("443", {"kept": triple(), "doomed": triple(runtime_probe="inconclusive")},
                  previous="442")

        at443 = compare(self.tmp, version="443")
        self.assertIn("doomed", at443.expected, "un-retirement did not restore the expectation")
        self.assertEqual(("doomed",), at443.dropped)
        # And both decisions survive, which is why un-retirement is a row.
        self.assertEqual(["retire", "unretire"], [i.kind for i in history("doomed", self.tmp)])

    def test_an_unreadable_store_refuses_rather_than_lowering_nothing(self):
        self.port("439", {"kept": triple(differential=None)})
        self.port("440", {"kept": triple()}, previous="439")
        self.port("441", {"kept": triple()}, previous="440")
        (self.tmp / "manifest" / "retirements.jsonl").write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(RetirementError):
            compare(self.tmp, version="441")

    def test_the_refusal_reaches_the_operator_as_a_refusal(self):
        """`main` used to let RetirementError out as a traceback and exit 1.

        1 is the code `final_report` uses for "incomplete", which is true on every
        successful port — so an unreadable store would have been indistinguishable
        from an ordinary result by exit code alone.
        """
        from dfinsta_pipeline.expectation import main as expectation_main

        self.port("439", {"kept": triple(differential=None)})
        self.port("440", {"kept": triple()}, previous="439")
        self.port("441", {"kept": triple()}, previous="440")
        (self.tmp / "manifest" / "retirements.jsonl").write_text("{not json\n", encoding="utf-8")
        code = expectation_main(["--root", str(self.tmp), "--version", "441"])
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
