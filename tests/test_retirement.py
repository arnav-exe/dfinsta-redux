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

    def test_the_cli_derives_effective_from_and_offers_no_flag_for_it(self):
        code = main([
            "--root", str(self.tmp), "retire", "--hook", "settings_hook", "--version", "441",
            "--ruled-by", "arnav", "--rationale", "surface removed",
            "--recorded-at", "2026-08-09T10:00:00+00:00",
        ])
        self.assertEqual(0, code)
        self.assertEqual("442", read(self.tmp)[0].effective_from)
        with self.assertRaises(SystemExit):
            main(["--root", str(self.tmp), "retire", "--hook", "h", "--effective-from", "999"])

    def test_the_cli_refuses_an_agent_with_exit_two(self):
        code = main([
            "--root", str(self.tmp), "retire", "--hook", "h", "--version", "441",
            "--ruled-by", "agent", "--rationale", "x", "--recorded-at", "t",
        ])
        self.assertEqual(2, code)
        self.assertEqual((), read(self.tmp))


class TheBarActuallyComesDownTests(ExpectationTestCase):
    """The integration that is the whole point: does a retirement lower the bar?

    Every test above is about the record being hard to abuse. None of them shows
    the record *doing* anything — and a retirement store nothing consults is the
    "complete and disconnected" failure this project has shipped at both ends of
    three separate gates.
    """

    def test_a_retired_hook_is_no_longer_expected_and_un_retiring_expects_it_again(self):
        self.port("439", {"kept": triple(differential=None), "doomed": triple(differential=None)})
        self.port("440", {"kept": triple(), "doomed": triple()}, previous="439")
        # 441 loses `doomed`: its runtime probe stops passing.
        self.port("441", {"kept": triple(), "doomed": triple(runtime_probe="inconclusive")},
                  previous="440")

        before = compare(self.tmp, version="441")
        self.assertEqual(("doomed",), before.dropped, "the positive control: it drops")
        self.assertIn("doomed", before.expected)

        # A human rules at 441, so it takes effect at 442.
        append(record("doomed", at="441", why="Instagram removed the surface"), self.tmp)
        self.assertEqual(("doomed",), tuple(compare(self.tmp, version="441").dropped),
                         "a retirement decided AT 441 must not rescue 441 itself")

        self.port("442", {"kept": triple()}, previous="441")
        at442 = compare(self.tmp, version="442")
        self.assertNotIn("doomed", at442.expected, "the bar did not come down")
        self.assertEqual((), at442.dropped)

        # Instagram brings it back: ruled at 442, in force from 443.
        append(record("doomed", at="442", kind="unretire", why="the surface is back"), self.tmp)
        self.port("443", {"kept": triple(), "doomed": triple()}, previous="442")
        self.port("444", {"kept": triple()}, previous="443")
        at444 = compare(self.tmp, version="444")
        self.assertIn("doomed", at444.expected, "un-retirement did not restore the expectation")
        self.assertEqual(("doomed",), at444.dropped)

    def test_an_unreadable_store_fails_the_comparison_rather_than_lowering_nothing(self):
        """If the store cannot be read, the bar must not silently stay where it is.

        A corrupt file read as "no retirements" is the safe-looking direction, and
        it is still wrong: it would hide that a hook the project agreed to stop
        expecting is being demanded again, and the reader would go and fix a hook
        nobody wants.
        """
        self.port("439", {"kept": triple(differential=None)})
        self.port("440", {"kept": triple()}, previous="439")
        self.port("441", {"kept": triple()}, previous="440")
        path = self.tmp / "manifest" / "retirements.jsonl"
        path.write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(RetirementError):
            compare(self.tmp, version="441")


if __name__ == "__main__":
    unittest.main()
