"""`VERDICT_AT_FLOOR`: the case where "flat" was measuring the wrong thing.

The flowchart's central claim is *"agent invocations per port fall with every
port, and a flat count means the pipeline is not learning."* Instagram 441
produced the third point of the sequence — 439 -> 2, 440 -> 0, 441 -> 0 — and the
report said `FLAT` and "not learning" of a port that needed no agent at all. **A
count that has reached zero cannot fall.** At 2 -> 2 the wording was right; at
0 -> 0 it was not, and one verdict could not tell them apart.

`at_floor` is the fifth verdict, and the interesting thing about it is the clause
that *withholds* it. It is refused when the hook count fell, because zero
invocations over three hooks is not the achievement zero over seven is — the same
shape as a test count that grows while one module has no tests at all. So this
file is not mostly about the new verdict; it is mostly about the case that must
NOT get it.

The properties pinned here, in the order they would be wrong in:

**The coverage guard.** :class:`CoverageGuardTests`, the reason the file exists.
0 -> 0 invocations over a manifest that shrank from 7 hooks to 3 must stay `flat`
and must say so in words that name both counts, because the number a reader
watches did not move and the thing that moved instead was what the number is
computed over. Its positive controls are the same scenario with the hook count
equal and with it risen, which must be `at_floor` — without them "always flat"
would satisfy the guard and delete the verdict. The boundary case is there too:
**one** hook fewer is enough to lose the floor, so `>=` cannot quietly become
`> 0` or a comparison of something else.

**Each verdict by its own condition.** :class:`VerdictTests`. Four scenarios that
differ in one input each, asserting the exact constant rather than the rendered
sentence — a test that only reads the prose passes a mutation that reports the
right words under the wrong verdict, and the verdict is what a caller branches
on. The discriminating one is 2 -> 0: it is *falling*, not the floor, because it
moved; a rule that keyed on "zero now" rather than on the delta would call the
best port this pipeline has ever done a flat one.

**The two sentences are not interchangeable.** :class:`AtFloorRenderTests` and
:class:`FlatWordingTests`. `at_floor` must say the count cannot fall further and
send the reader to the selectivity margins — the thing that CAN still move — and
must not say "not learning". `flat` above zero must still say "not learning",
which is the case 439 was and the wording was always right for. Softening that
sentence to make the 441 report read better would be weakening a check to get
past it, so it is pinned here by its exact phrase.

**Nothing was taken from `untestable`.** :class:`UntestableTests`. One version in
the ledger has no previous port to be flat or at the floor against, and a first
port with zero invocations is the shape most likely to be mis-promoted: it is
zero, it is not a regression, and it has satisfied nothing.

**The live data.** :class:`RealLedgerTests`, against the committed
`manifest/agent_cost.jsonl`. Three real ports are on file and they must not all
read the same.

**The vocabulary has one owner.** :class:`VerdictVocabularyTests`. No module
outside `agent_cost.py` may compare against these strings, so a sixth verdict
cannot silently break a consumer that tested `== "flat"`. An absence assertion
needs a positive control, so the scanner is also pointed at a planted comparison
and at `agent_cost.py` itself.

**Two ports that are not at the floor get told they are.**
:class:`KnownGapTests`, found by writing this file and NOT fixed here. The hook
count is the number of records in a run, not the number of hooks a port
*resolved*, so the coverage clause catches a manifest that shrank and does not
catch a manifest that stopped working. Each test records what the module does
today, so a fix fails loudly here rather than silently changing what a report
says.

===============================================================================
  FIXTURE PROVENANCE
===============================================================================

:func:`port` builds `HookCost` records directly rather than driving a
`ResolveReport` through `hook_costs`. The scenarios here are *shapes* — "seven
hooks, none of which cost an agent, then three hooks, none of which cost an
agent" — and expressing them through fixture resolutions would make the hook
count and the invocation count emergent properties of a resolve fixture rather
than the two numbers under test. :class:`FixtureControlTests` closes the gap that
opens: it checks the builder produces the counts it claims, so every scenario
below means what its arguments say.

The real-ledger class uses the committed file, copied to a temp directory before
reading, for the same reason `RealLedgerTests` in `tests/test_manifest_update.py`
does: it is recorded evidence of real ports and a test run may not be why it
changes.

===============================================================================
  WHAT THE LIVE 439 REPORT ACTUALLY SAYS
===============================================================================

`cost_report(ledger, "439")` on the committed ledger is **`untestable`**, not
`flat`. 439 is the first version the file holds, so `previous_version` returns
None and there is nothing for it to be flat against — the 2 -> 2 pair that
prompted the original wording is 439's two *runs*, not two versions. The live
flat-above-zero case is therefore reported as
`cost_report(ledger, "439", "439", run=1)`: run 1 of 439 against 439's latest,
two invocations both times, and it is the case that must keep saying "not
learning". This is a fact about the ledger's contents, not a defect; the property
the three live ports have to satisfy — that they do not all read the same — is
asserted directly.

===============================================================================
  MUTATION RESULTS
===============================================================================

Six mutations, one at a time against a fresh out-of-tree copy of the repo, with
the unmutated copy passing first as the control every time. The first count in
brackets is how many tests in THIS file failed; the second is how many failed in
`tests/test_manifest_update.py`, which is where `cost_report` was tested before
this file existed.

* drop the `now["hooks"] >= was["hooks"]` clause [3 | **0**] →
  :meth:`CoverageGuardTests.test_zero_over_a_shrunken_manifest_is_flat_not_the_floor`,
  :meth:`CoverageGuardTests.test_the_shrunken_report_names_both_hook_counts_and_denies_the_floor`,
  :meth:`CoverageGuardTests.test_losing_one_single_hook_is_enough_to_lose_the_floor`.
  The zero is the reason this file exists: the clause the whole change turns on
  can be deleted and the pre-existing suite still passes.
* `at_floor` whenever `delta == 0`, both counts ignored [8 | 3] → the three
  above, all three of :class:`FlatWordingTests`,
  :meth:`VerdictTests.test_a_flat_count_above_zero_is_flat` and
  :meth:`VerdictTests.test_the_two_zero_delta_cases_are_told_apart`
* the flat branch prints the at_floor text [5 | 2] → :class:`FlatWordingTests`,
  the two shrunken-manifest text assertions, and
  :meth:`RealLedgerTests.test_the_live_two_to_two_pair_is_flat_and_still_says_not_learning`
* the at_floor branch moved to the front, ahead of `delta < 0` [3 | 2] →
  :meth:`VerdictTests.test_a_count_that_fell_to_zero_is_falling_and_not_the_floor`
  and, on the live data, 440 stops being a fall
* `>=` weakened to `>` [11 | **0**] → every positive control: the whole of
  :class:`AtFloorRenderTests`, the equal-hook-count control, the real 441 report
  and both of :class:`KnownGapTests`, whose ports also have an equal hook count.
  Also invisible to the pre-existing suite.
* the at_floor branch moved above `delta > 0`, keeping `delta == 0` in its
  condition [0 | 0] — an **equivalent mutant** and reported as one: `at_floor`
  already requires `delta == 0` and `rising` requires `delta > 0`, so the two
  branches are disjoint and their order cannot be observed. Recorded rather than
  dropped, because "no test caught it" and "there is nothing to catch" are
  different findings.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.agent_cost import (
    NEED_HOST,
    ROUTE_AGENT_PROPOSAL,
    ROUTE_ALREADY_APPLIED,
    ROUTE_MECHANICAL,
    ROUTE_NOT_RESOLVED,
    VERDICT_AT_FLOOR,
    VERDICT_FALLING,
    VERDICT_FLAT,
    VERDICT_RISING,
    VERDICT_UNTESTABLE,
    CostLedger,
    HookCost,
    cost_report,
    open_ledger,
    render,
    stamped,
)
from dfinsta_pipeline.resolve import Outcome

#: Every verdict `cost_report` can return. Kept as a set here rather than
#: imported as one so that a new verdict added to the module without a thought
#: for its consumers shows up as a scan failure below rather than as nothing.
VOCABULARY = frozenset(
    {
        VERDICT_UNTESTABLE,
        VERDICT_FALLING,
        VERDICT_FLAT,
        VERDICT_RISING,
        VERDICT_AT_FLOOR,
    }
)

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "src" / "dfinsta_pipeline"
OWNER = SOURCE / "agent_cost.py"

#: The committed ledger. Real recorded evidence of three real ports.
REAL_LEDGER = REPO / "manifest" / "agent_cost.jsonl"


# ------------------------------------------------------------------- fixtures


def port(version: str, *, hooks: int, agents: int) -> tuple[HookCost, ...]:
    """One port: *hooks* hooks of which the first *agents* cost an agent invocation.

    The two numbers the verdict is computed from, set directly. Hook ids are
    positional, so a manifest that shrank from 7 to 3 keeps hooks 1-3 rather than
    inventing three unrelated ones — a shrunken manifest is hooks going away, and
    `retired` / `newly_costly` join on the id.
    """
    if not 0 <= agents <= hooks:
        raise AssertionError(f"{agents} agent invocation(s) over {hooks} hook(s) is not a port")
    stamp = f"stamp-{version}"
    out = []
    for number in range(1, hooks + 1):
        paid = number <= agents
        out.append(
            stamped(
                HookCost(
                    hook_id=f"hook_{number}",
                    version=version,
                    route=ROUTE_AGENT_PROPOSAL if paid else ROUTE_MECHANICAL,
                    outcome=Outcome.RESOLVED.value,
                    agent_for=(NEED_HOST,) if paid else (),
                ),
                stamp,
            )
        )
    return tuple(out)


def ledger_of(*ports: tuple[HookCost, ...]) -> CostLedger:
    """A ledger of whole ports in the order they happened."""
    ledger = CostLedger()
    for records in ports:
        for cost in records:
            ledger.record(cost)
    return ledger


def text_of(report) -> str:
    return "\n".join(render(report))


def literals_used(source: str) -> set[str]:
    """Every verdict string that appears as a string constant in *source*.

    Exact-value matching on `ast.Constant`, not a substring grep: prose that
    happens to contain the word "flat" is not a consumer of the vocabulary, and a
    grep that flagged it would be a check people learn to ignore.
    """
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in VOCABULARY
    }


# ------------------------------------------------------------ fixture control


class FixtureControlTests(unittest.TestCase):
    """The scenarios below are two numbers each. This is the check that they are those numbers.

    Every assertion in this file reads `port(..., hooks=7, agents=0)` and trusts
    it means seven hooks and no agent invocations. A builder that quietly
    disagreed would make the coverage guard look tested while testing something
    else.
    """

    def test_the_builder_produces_the_hook_and_invocation_counts_it_claims(self):
        report = cost_report(ledger_of(port("439", hooks=7, agents=2)), "439")
        self.assertEqual(report["now"]["hooks"], 7)
        self.assertEqual(report["now"]["agent_invocations"], 2)
        self.assertEqual(report["now"]["routes"][ROUTE_AGENT_PROPOSAL], 2)
        self.assertEqual(report["now"]["routes"][ROUTE_MECHANICAL], 5)

    def test_a_port_with_no_agent_invocations_really_has_none(self):
        """The zero the whole verdict turns on, taken from the report rather than assumed."""
        report = cost_report(ledger_of(port("441", hooks=7, agents=0)), "441")
        self.assertEqual(report["now"]["agent_invocations"], 0)
        self.assertEqual(report["agent_hooks"], [])
        self.assertIn("(none — every hook resolved without one)", text_of(report))

    def test_two_ports_are_two_versions_and_the_comparison_is_between_them(self):
        ledger = ledger_of(port("440", hooks=7, agents=1), port("441", hooks=7, agents=0))
        report = cost_report(ledger, "441")
        self.assertEqual(report["previous_version"], "440")
        self.assertEqual(report["previous"]["agent_invocations"], 1)
        self.assertEqual(report["run"]["of"], 1)


# --------------------------------------------------------------- the verdicts


class VerdictTests(unittest.TestCase):
    """Each verdict reached by its own condition, asserted as the constant.

    The rendered sentence is checked elsewhere; what a caller branches on is
    `report["verdict"]`, and a mutation that prints the right words under the
    wrong verdict is invisible to a test that only reads prose.
    """

    def test_a_count_that_fell_is_falling(self):
        ledger = ledger_of(port("440", hooks=7, agents=2), port("441", hooks=7, agents=1))
        report = cost_report(ledger, "441")
        self.assertEqual(report["verdict"], VERDICT_FALLING)
        self.assertEqual(report["delta_agent_invocations"], -1)

    def test_a_count_that_fell_to_zero_is_falling_and_not_the_floor(self):
        """The best port this pipeline can do is *falling*, because the number moved.

        `at_floor` is for a count that has already arrived and cannot move again.
        A rule that keyed on "zero invocations now" instead of on the delta would
        report the port that retired the last agent as flat-at-the-floor and lose
        the one improvement the claim is actually about.
        """
        ledger = ledger_of(port("439", hooks=7, agents=2), port("440", hooks=7, agents=0))
        report = cost_report(ledger, "440")
        self.assertEqual(report["verdict"], VERDICT_FALLING)
        self.assertEqual(report["delta_agent_invocations"], -2)
        self.assertIn("falling — 2 fewer than 439", text_of(report))

    def test_a_count_that_rose_is_rising(self):
        ledger = ledger_of(port("440", hooks=7, agents=1), port("441", hooks=7, agents=3))
        report = cost_report(ledger, "441")
        self.assertEqual(report["verdict"], VERDICT_RISING)
        self.assertEqual(report["delta_agent_invocations"], 2)

    def test_a_flat_count_above_zero_is_flat(self):
        """The case the original wording was written for: 439's 2 -> 2."""
        ledger = ledger_of(port("430", hooks=7, agents=2), port("439", hooks=7, agents=2))
        report = cost_report(ledger, "439")
        self.assertEqual(report["verdict"], VERDICT_FLAT)
        self.assertEqual(report["delta_agent_invocations"], 0)
        self.assertEqual(report["now"]["agent_invocations"], 2)

    def test_zero_after_zero_over_the_same_hooks_is_at_floor(self):
        """441's case. Same delta as the test above and a different verdict."""
        ledger = ledger_of(port("440", hooks=7, agents=0), port("441", hooks=7, agents=0))
        report = cost_report(ledger, "441")
        self.assertEqual(report["verdict"], VERDICT_AT_FLOOR)
        self.assertEqual(report["delta_agent_invocations"], 0)
        self.assertEqual(report["now"]["agent_invocations"], 0)

    def test_the_two_zero_delta_cases_are_told_apart(self):
        """Stated as one assertion because it is the whole change.

        Both ports below have a delta of 0 over 7 hooks. Before `at_floor` existed
        they produced the same verdict and the same sentence.
        """
        costly = cost_report(
            ledger_of(port("430", hooks=7, agents=2), port("439", hooks=7, agents=2)), "439"
        )
        free = cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=7, agents=0)), "441"
        )
        self.assertEqual(costly["delta_agent_invocations"], free["delta_agent_invocations"])
        self.assertEqual(costly["now"]["hooks"], free["now"]["hooks"])
        self.assertNotEqual(costly["verdict"], free["verdict"])
        self.assertEqual({costly["verdict"], free["verdict"]}, {VERDICT_FLAT, VERDICT_AT_FLOOR})


# ---------------------------------------------------------- the coverage guard


class CoverageGuardTests(unittest.TestCase):
    """0 -> 0 over FEWER hooks is not the floor, and the report has to say so.

    The clause that makes this verdict honest rather than a fourth way of saying
    "good". Zero invocations across a manifest that shrank is a smaller problem
    being solved, not the same problem solved for free — and it is exactly the
    reading a pipeline under pressure would drift into, because dropping a hook
    that keeps needing an agent makes the headline number better.

    The three positive controls are load-bearing: without them a rule that never
    returned `at_floor` at all would pass the guard and quietly delete the
    verdict.
    """

    #: Seven hooks, then three. Same zero invocations at both ends, so the only
    #: thing that changed is what the zero is a zero *of*.
    def shrunken(self):
        return cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=3, agents=0)), "441"
        )

    def test_zero_over_a_shrunken_manifest_is_flat_not_the_floor(self):
        report = self.shrunken()
        self.assertEqual(report["delta_agent_invocations"], 0)
        self.assertEqual(report["now"]["agent_invocations"], 0)
        self.assertEqual(report["now"]["hooks"], 3)
        self.assertEqual(report["previous"]["hooks"], 7)
        self.assertEqual(report["verdict"], VERDICT_FLAT)
        self.assertNotEqual(report["verdict"], VERDICT_AT_FLOOR)

    def test_the_shrunken_report_names_both_hook_counts_and_denies_the_floor(self):
        """A verdict a reader cannot check is a verdict they will take on trust.

        "flat" alone, on a report whose headline is 0 agent invocations, reads as
        a formatting quirk. The line has to name the number that fell and the
        number it fell from, and say outright that this is not the floor.
        """
        text = text_of(self.shrunken())
        self.assertIn("NOT the floor", text)
        self.assertIn("0 invocations, but over 3 hook(s) against 7 last time", text)
        self.assertIn("Fewer hooks is a smaller problem, not a cheaper solution", text)
        self.assertNotIn("at the floor", text)
        self.assertNotIn("cannot fall further", text)

    def test_losing_one_single_hook_is_enough_to_lose_the_floor(self):
        """The boundary, so `>=` cannot become `>` or a comparison of something else.

        Six hooks out of seven is not obviously a shrunken manifest to a reader,
        which is precisely why the rule may not be a judgement about how much
        coverage was lost.
        """
        report = cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=6, agents=0)), "441"
        )
        self.assertEqual(report["verdict"], VERDICT_FLAT)
        self.assertIn("over 6 hook(s) against 7 last time", text_of(report))

    def test_the_same_scenario_with_an_equal_hook_count_is_at_floor(self):
        """POSITIVE CONTROL. Change one number back and the verdict must come back."""
        report = cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=7, agents=0)), "441"
        )
        self.assertEqual(report["verdict"], VERDICT_AT_FLOOR)
        self.assertNotIn("NOT the floor", text_of(report))

    def test_the_same_scenario_with_a_risen_hook_count_is_at_floor(self):
        """POSITIVE CONTROL, and the better port: more hooks, still no agent.

        A manifest that grew and still cost nothing is strictly more than 441 did.
        Refusing it the floor would make the guard a rule against change rather
        than a rule about coverage.
        """
        report = cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=9, agents=0)), "441"
        )
        self.assertEqual(report["verdict"], VERDICT_AT_FLOOR)
        self.assertIn("over 9 hook(s) against 7", text_of(report))

    def test_a_shrunken_manifest_that_also_got_cheaper_is_still_falling(self):
        """The guard is scoped to the flat case and must not annex the falling one.

        1 -> 0 over fewer hooks did move the number. It is `falling` with a
        caveat a reader can compute from the hook counts printed above it, not a
        third thing.
        """
        report = cost_report(
            ledger_of(port("440", hooks=7, agents=1), port("441", hooks=3, agents=0)), "441"
        )
        self.assertEqual(report["verdict"], VERDICT_FALLING)


# ----------------------------------------------------------------- the wording


class AtFloorRenderTests(unittest.TestCase):
    """What the reader is told at the floor: the count is done, watch the margins.

    The verdict exists because a true sentence was being printed about the wrong
    quantity. The replacement has to point somewhere real, or it is just a nicer
    way of saying nothing moved.
    """

    def setUp(self):
        self.report = cost_report(
            ledger_of(port("440", hooks=7, agents=0), port("441", hooks=7, agents=0)), "441"
        )
        self.text = text_of(self.report)

    def test_it_says_the_count_cannot_fall_further(self):
        self.assertEqual(self.report["verdict"], VERDICT_AT_FLOOR)
        self.assertIn("VERDICT: at the floor", self.text)
        self.assertIn("cannot fall further", self.text)
        self.assertIn("the claim holding rather than the pipeline stalling", self.text)

    def test_it_names_both_hook_counts_so_the_withheld_case_is_checkable(self):
        """The floor is granted on a comparison, so the report shows the comparison."""
        self.assertIn("0 agent invocations again, over 7 hook(s) against 7", self.text)

    def test_it_sends_the_reader_to_selectivity_rather_than_to_the_count(self):
        """The number that can still move, named, with what it looks like when it does.

        Without this the report says "everything is fine" and offers nothing to
        check — and the thing that will actually break next is a fingerprint
        narrowing to 1 -> 1, which the count cannot show.
        """
        self.assertIn("What would move next is SELECTIVITY, not the count", self.text)
        self.assertIn("1 -> 1", self.text)

    def test_it_does_not_say_not_learning(self):
        """The sentence that was wrong about 441. It may not survive anywhere here."""
        self.assertNotIn("not learning", self.text)
        self.assertNotIn("FLAT", self.text)


class FlatWordingTests(unittest.TestCase):
    """Flat above zero still says "not learning" — the case the wording was right for.

    439 cost two agent invocations twice. Nothing about `at_floor` makes that
    port better, and the temptation the change creates is to soften the flat
    sentence so no report reads harshly. That would be weakening a check to get
    past it: this is the one state where "the same hooks cost the same agent"
    is exactly what happened.
    """

    def setUp(self):
        self.report = cost_report(
            ledger_of(port("430", hooks=7, agents=2), port("439", hooks=7, agents=2)), "439"
        )
        self.text = text_of(self.report)

    def test_it_still_says_flat_and_not_learning(self):
        self.assertEqual(self.report["verdict"], VERDICT_FLAT)
        self.assertIn("VERDICT: FLAT against 430", self.text)
        self.assertIn("A pipeline whose agent count is flat is not learning", self.text)
        self.assertIn("the same hooks cost the same agent this port", self.text)

    def test_it_is_not_given_the_floor_language(self):
        self.assertNotIn("at the floor", self.text)
        self.assertNotIn("cannot fall further", self.text)
        self.assertNotIn("SELECTIVITY, not the count", self.text)

    def test_it_does_not_carry_the_shrunken_manifest_caveat(self):
        """That caveat explains a zero. Printed here it would deny a floor nobody claimed.

        The equal hook counts would make it read "0 invocations, but over 7
        hook(s) against 7" on a report whose headline is 2 invocations — three
        wrong statements in one line.
        """
        self.assertNotIn("NOT the floor", self.text)
        self.assertIn("agent invocations: 2", self.text)


class UntestableTests(unittest.TestCase):
    """One version in the ledger is still untestable, zero invocations or not.

    The claim is about a sequence. A first port that needed no agent has the
    floor's numbers and none of its evidence, and it is the report most likely to
    be read as a success — so the branch order that answers `untestable` before
    anything looks at the count is pinned rather than assumed.
    """

    def test_a_single_version_with_no_previous_port_is_untestable(self):
        report = cost_report(ledger_of(port("441", hooks=7, agents=1)), "441")
        self.assertEqual(report["verdict"], VERDICT_UNTESTABLE)
        self.assertIsNone(report["delta_agent_invocations"])
        self.assertIsNone(report["previous"])

    def test_a_first_port_that_cost_nothing_is_untestable_and_not_at_floor(self):
        report = cost_report(ledger_of(port("441", hooks=7, agents=0)), "441")
        self.assertEqual(report["now"]["agent_invocations"], 0)
        self.assertEqual(report["verdict"], VERDICT_UNTESTABLE)
        self.assertNotEqual(report["verdict"], VERDICT_AT_FLOOR)
        text = text_of(report)
        self.assertIn("claim about a sequence", text)
        self.assertNotIn("at the floor", text)


# --------------------------------------------------------------- the live data


class RealLedgerTests(unittest.TestCase):
    """Against `manifest/agent_cost.jsonl`: three real ports, and they do not read alike.

    Copied to a temp directory before reading. It is recorded evidence of real
    ports and a test run may not be the reason it changes.

    The 439 line is worth reading twice. It is `untestable`, not `flat`, because
    439 is the first version the file holds and has no previous port to be flat
    against — the 2 -> 2 pair the original wording was right about is 439's two
    *runs*, and that is reported below by asking for run 1 against the version's
    latest.
    """

    def setUp(self):
        self.assertTrue(
            REAL_LEDGER.exists(),
            f"{REAL_LEDGER} is committed recorded data and this class is about it; a "
            "skip here would be a test that cannot fail",
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="at-floor-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.copy = self.tmp / "agent_cost.jsonl"
        shutil.copy2(REAL_LEDGER, self.copy)
        self.ledger = open_ledger(self.copy)

    def test_the_ledger_holds_the_three_ports_this_verdict_was_added_for(self):
        """439 -> 2, 440 -> 0, 441 -> 0, one run each for the two that matter."""
        self.assertEqual(self.ledger.versions, ("439", "440", "441"))
        latest = {
            version: self.ledger.latest_run(version) for version in self.ledger.versions
        }
        self.assertEqual(
            {version: run.agent_invocations for version, run in latest.items()},
            {"439": 2, "440": 0, "441": 0},
        )
        self.assertEqual({version: len(run.costs) for version, run in latest.items()},
                         {"439": 7, "440": 7, "441": 7})

    def test_441_is_at_floor(self):
        """The port that produced `FLAT` and "not learning" while needing no agent."""
        report = cost_report(self.ledger, "441")
        self.assertEqual(report["verdict"], VERDICT_AT_FLOOR)
        self.assertEqual(report["delta_agent_invocations"], 0)
        self.assertEqual(report["previous_version"], "440")
        text = text_of(report)
        self.assertIn("VERDICT: at the floor", text)
        self.assertIn("over 7 hook(s) against 7", text)
        self.assertNotIn("not learning", text)

    def test_439_is_untestable_and_is_certainly_not_at_floor(self):
        """It cost two agent invocations and it is the first port on file.

        Both halves are the point: a verdict that read `at_floor` here would be
        claiming the floor for the most expensive port in the ledger.
        """
        report = cost_report(self.ledger, "439")
        self.assertEqual(report["now"]["agent_invocations"], 2)
        self.assertIsNone(report["previous_version"])
        self.assertEqual(report["verdict"], VERDICT_UNTESTABLE)
        self.assertNotEqual(report["verdict"], VERDICT_AT_FLOOR)

    def test_the_live_two_to_two_pair_is_flat_and_still_says_not_learning(self):
        """439's two attempts, both costing 2 over 7 hooks. Real data, real flat."""
        report = cost_report(self.ledger, "439", "439", run=1)
        self.assertEqual(report["now"]["agent_invocations"], 2)
        self.assertEqual(report["previous"]["agent_invocations"], 2)
        self.assertEqual(report["verdict"], VERDICT_FLAT)
        self.assertIn("is not learning", text_of(report))

    def test_440_is_falling_because_it_retired_both_agents(self):
        report = cost_report(self.ledger, "440")
        self.assertEqual(report["verdict"], VERDICT_FALLING)
        self.assertEqual(report["delta_agent_invocations"], -2)
        self.assertEqual(
            report["retired"],
            ["install_settings_long_click", "install_settings_long_click_actionbar"],
        )

    def test_the_three_live_ports_do_not_all_read_the_same(self):
        """The property the change is for, stated over the file rather than a fixture.

        Before `at_floor`, 441 said what a stalled pipeline says. One verdict for
        three ports that cost 2, 0 and 0 is a metric that has stopped
        distinguishing.
        """
        verdicts = [cost_report(self.ledger, version)["verdict"] for version in ("439", "440", "441")]
        self.assertEqual(len(set(verdicts)), 3)
        self.assertEqual(verdicts.count(VERDICT_AT_FLOOR), 1)
        self.assertEqual(verdicts[-1], VERDICT_AT_FLOOR)


# ------------------------------------------------------------- the vocabulary


class VerdictVocabularyTests(unittest.TestCase):
    """`agent_cost.py` owns these five strings, and nothing else may compare against them.

    A verdict added later must be a change to one module. A consumer somewhere
    testing `report["verdict"] == "flat"` would keep working, keep type-checking,
    and be silently wrong the first time a port lands at the floor — the failure
    mode that has no symptom.

    An absence assertion needs a positive control, so the scanner is pointed at a
    planted comparison and at the owning module before it is believed.
    """

    def modules(self) -> list[Path]:
        return sorted(SOURCE.rglob("*.py"))

    def test_the_scanner_finds_a_planted_comparison(self):
        """POSITIVE CONTROL. Without it, a scan that matched nothing would pass."""
        planted = 'def stale(report):\n    return report["verdict"] == "at_floor"\n'
        self.assertEqual(literals_used(planted), {VERDICT_AT_FLOOR})
        self.assertEqual(literals_used('x = "flat"\ny = "rising"\n'), {VERDICT_FLAT, VERDICT_RISING})

    def test_the_scanner_ignores_the_same_words_in_prose(self):
        """A docstring saying a count is flat is not a consumer of the vocabulary."""
        prose = '"""A pipeline whose agent count is flat is not learning."""\n'
        self.assertEqual(literals_used(prose), set())

    def test_the_owning_module_defines_every_verdict_in_the_vocabulary(self):
        """POSITIVE CONTROL for the scan reaching a real file, and for the set being whole."""
        self.assertIn(OWNER, self.modules())
        self.assertEqual(literals_used(OWNER.read_text(encoding="utf-8")), set(VOCABULARY))
        self.assertEqual(len(VOCABULARY), 5)

    def test_no_other_module_in_the_tree_uses_a_verdict_as_a_string(self):
        found = self.modules()
        self.assertGreater(len(found), 1, "the scan found no modules; it cannot fail")
        offenders = {
            path.relative_to(REPO).as_posix(): sorted(literals_used(path.read_text(encoding="utf-8")))
            for path in found
            if path != OWNER and literals_used(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            offenders,
            {},
            "these modules compare against a verdict string instead of importing the "
            "constant; a sixth verdict would pass them by silently",
        )



# --------------------------------------------------------------- the known gaps


def unresolved_port(version: str, *, hooks: int, route: str, outcome: str) -> tuple[HookCost, ...]:
    """A port whose hooks all took *route*: no agent invocations, and no resolutions either."""
    return tuple(
        stamped(
            HookCost(
                hook_id=f"hook_{number}",
                version=version,
                route=route,
                outcome=outcome,
            ),
            f"stamp-{version}",
        )
        for number in range(1, hooks + 1)
    )


class SpendingNothingWithoutEarningItTests(unittest.TestCase):
    """Three ways to spend no agent invocations; only one is the claim holding.

    Found by this file within an hour of `at_floor` being written, and fixed the
    same hour. The first draft asked only whether the hook count fell — and
    `now["hooks"]` is `len(costs)`, how many records the run *wrote*, not how many
    hooks it *resolved*. That catches a manifest somebody shrank and misses a
    manifest that stopped working, and both misses landed on the congratulatory
    side.

    Neither case was a regression the verdict introduced — both previously read
    `flat` — but `flat` did not congratulate anyone, so the new verdict turned a
    quiet wrong answer into a loud one. `_genuinely_at_floor` now also requires
    nothing blocked and at least one hook resolved by a route that had to work for
    it.
    """

    def test_a_rerun_over_an_already_patched_decode_is_not_the_floor(self):
        """`already_applied` is documented as "not a cost at all", so it cannot buy the floor.

        `ROUTE_ALREADY_APPLIED` exists precisely so a re-run cannot be mistaken for
        mechanisation — the note on every such record says the port "paid nothing
        for this hook and learned nothing about what it would cost". Zero
        invocations there measures a no-op.
        """
        ledger = ledger_of(
            port("440", hooks=7, agents=0),
            unresolved_port(
                "441", hooks=7, route=ROUTE_ALREADY_APPLIED, outcome=Outcome.ALREADY_APPLIED.value
            ),
        )
        report = cost_report(ledger, "441")
        self.assertEqual(report["now"]["routes"][ROUTE_ALREADY_APPLIED], 7)
        self.assertEqual(report["verdict"], VERDICT_FLAT)
        self.assertNotIn("the claim holding rather than the pipeline stalling", text_of(report))

    def test_a_wholly_blocked_port_is_not_the_floor_and_says_so_once(self):
        """Every hook NOT_FOUND, nothing resolved — and the report must not contradict itself.

        This was the worse of the two, because the same report already contained
        the refutation: the ROUTES block prints "the port is blocked, not
        expensive" and the verdict said "the claim holding rather than the
        pipeline stalling" four lines below. A port that resolved nothing spends
        no agent invocations for exactly the reason that makes the count
        meaningless.
        """
        ledger = ledger_of(
            port("440", hooks=7, agents=0),
            unresolved_port(
                "441", hooks=7, route=ROUTE_NOT_RESOLVED, outcome=Outcome.NOT_FOUND.value
            ),
        )
        report = cost_report(ledger, "441")
        self.assertEqual(report["now"]["blocked"], 7)
        self.assertEqual(report["now"]["agent_invocations"], 0)
        self.assertEqual(report["verdict"], VERDICT_FLAT)

        text = text_of(report)
        self.assertIn("the port is blocked, not expensive", text)
        self.assertNotIn("the claim holding rather than the pipeline stalling", text)

    def test_one_earned_hook_among_blocked_ones_is_still_not_the_floor(self):
        """Blocked is disqualifying on its own, not merely outweighed.

        A port that mechanised six hooks and could not resolve the seventh has not
        reached any floor — it has an unported hook. Without this the guard would
        pass on "some hook earned it" and let a partial failure read as complete
        success.
        """
        blocked = stamped(
            HookCost(
                hook_id="hook_7",
                version="441",
                route=ROUTE_NOT_RESOLVED,
                outcome=Outcome.NOT_FOUND.value,
            ),
            "stamp-441",
        )
        ledger = ledger_of(
            port("440", hooks=7, agents=0),
            (*port("441", hooks=6, agents=0), blocked),
        )
        report = cost_report(ledger, "441")
        self.assertEqual(report["now"]["blocked"], 1)
        self.assertEqual(report["verdict"], VERDICT_FLAT)

    def test_the_control_is_that_the_same_shape_all_mechanical_is_the_floor(self):
        """POSITIVE CONTROL: without it the three above pass for any reason at all."""
        ledger = ledger_of(port("440", hooks=7, agents=0), port("441", hooks=7, agents=0))
        report = cost_report(ledger, "441")
        self.assertEqual(report["verdict"], VERDICT_AT_FLOOR)
        self.assertIn("the claim holding rather than the pipeline stalling", text_of(report))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
