"""The per-hook view, and the distinction it exists to preserve.

`roster` answers "what is this hook, and when did it last do anything" — the
question a human asks first and which no other module answered. It is a **view,
not an alarm**: it prints every hook every time, so it can afford to be
exhaustive, and so it can stay quiet about questions that are already settled.

The property most worth defending here is the three-way cell. A hook that ran, a
hook that was measured and stayed silent, and a hook nobody measured are three
different facts, and collapsing the last two is how "we never looked" comes to
read as "it is broken" — the mistake that made the first differential compare 2
of 7.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from dfinsta_pipeline.roster import (
    RAN,
    SILENT,
    UNMEASURED,
    HookLife,
    RosterError,
    main,
    render,
    roster,
)

REPOSITORY = Path(__file__).resolve().parent.parent
ALIVE = "set_app_context"
DORMANT = "replace_reels_discover_endpoint"


def claim(hook: str, kind: str, version: str, verdict: str, detail: dict | None = None) -> str:
    row = {
        "actor": "tests",
        "confidence": None,
        "decision_id": None,
        "detail": detail or {},
        "hook_id": hook,
        "kind": kind,
        "producer": "deterministic" if kind == "static_verified" else "device",
        "rationale": "",
        "recorded_at": f"2026-08-0{version[-1]}T00:00:00+00:00",
        "schema_version": 1,
        "summary": f"{hook} {kind}",
        "supersedes": None,
        "verdict": verdict,
        "version": version,
    }
    if kind == "static_verified":
        row["build_sha256"] = "b" * 64
    return json.dumps(row, sort_keys=True)


class RosterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.manifest = self.tmp / "manifest"
        for name in ("static_evidence", "runtime_evidence", "differentials"):
            (self.manifest / name).mkdir(parents=True)

    def write_manifest(self, hooks: list[dict]) -> None:
        (self.manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "policy_revision": "2026-08-01", "hooks": hooks}),
            encoding="utf-8",
        )

    def hook(self, hook_id: str, **extra) -> dict:
        return {"hook_id": hook_id, "intent": f"do {hook_id}", "tier": "robust",
                "status": "active", **extra}

    def runtime(self, version: str, rows: list[str]) -> None:
        (self.manifest / "runtime_evidence" / f"{version}.jsonl").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )


class CellTests(RosterTestCase):
    """Ran, silent, unmeasured — three facts, three glyphs."""

    def test_the_three_states_are_distinguished(self) -> None:
        """A hook nobody measured must not look like one that stayed silent.

        Collapsing them is how a gap in measurement reads as a broken hook. The
        one distinction this view exists to keep.
        """
        self.write_manifest([self.hook("a"), self.hook("b"), self.hook("c")])
        self.runtime("440", [
            claim("a", "runtime_probe", "440", "passed"),
            claim("b", "runtime_probe", "440", "inconclusive"),
            # "c" is absent: measured by nobody.
        ])

        lives, versions = roster(self.tmp)
        by_id = {life.hook_id: life for life in lives}

        self.assertEqual(("440",), by_id["a"].ran_on)
        self.assertEqual(("440",), by_id["a"].measured_on)
        self.assertEqual((), by_id["b"].ran_on)
        self.assertEqual(("440",), by_id["b"].measured_on)
        self.assertEqual((), by_id["c"].ran_on)
        self.assertEqual((), by_id["c"].measured_on)

        text = render(lives, versions)
        self.assertIn(RAN, text)
        self.assertIn(SILENT, text)
        self.assertIn(UNMEASURED, text)

    def test_a_hook_measured_twice_on_one_version_ran_if_either_passed(self) -> None:
        """Order of rows in a file must not decide whether a hook executed.

        440 really does carry `inconclusive` then `passed` for one hook. Taking
        the last row would make the answer depend on how the file was appended,
        and taking the first would call a hook that demonstrably ran silent.
        Whether re-measuring until green was legitimate is the ledger's retry
        guard's job, not this view's.
        """
        self.write_manifest([self.hook("a")])
        self.runtime("440", [
            claim("a", "runtime_probe", "440", "inconclusive"),
            claim("a", "runtime_probe", "440", "passed"),
        ])
        lives, _ = roster(self.tmp)
        self.assertEqual(("440",), lives[0].ran_on)

        # And the other order.
        self.runtime("440", [
            claim("a", "runtime_probe", "440", "passed"),
            claim("a", "runtime_probe", "440", "inconclusive"),
        ])
        lives, _ = roster(self.tmp)
        self.assertEqual(("440",), lives[0].ran_on)

    def test_last_ran_is_the_latest_version_not_the_last_file_read(self) -> None:
        self.write_manifest([self.hook("a")])
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        self.runtime("441", [claim("a", "runtime_probe", "441", "inconclusive")])
        lives, _ = roster(self.tmp)
        self.assertEqual("440", lives[0].last_ran)
        self.assertFalse(lives[0].never_ran)


class DecisionTests(RosterTestCase):
    def test_a_decision_note_is_found_wherever_it_was_written(self) -> None:
        """There is no schema for a recorded decision, which is the point.

        The one that exists lives in `probe.note`. Searching by pattern is a poor
        substitute for a field — and printing what it finds is what keeps the
        absence of a field visible rather than comfortable.
        """
        self.write_manifest([
            self.hook("a", probe={"note": "DECISION 2026-08-01 (human): KEEP. Because."}),
            self.hook("b", constraints=["DECISION 2026-08-02 (human): KEEP."]),
            self.hook("c"),
        ])
        self.runtime("440", [claim(h, "runtime_probe", "440", "inconclusive") for h in "abc"])

        by_id = {life.hook_id: life for life in roster(self.tmp)[0]}
        self.assertIn("KEEP", by_id["a"].note)
        self.assertIn("KEEP", by_id["b"].note)
        self.assertEqual("", by_id["c"].note)

    def test_dormant_and_undecided_is_the_only_opinion_this_view_holds(self) -> None:
        """Never ran AND nothing written down. Neither half alone is a finding.

        A hook that has never run can be dormant by server config — three are,
        deliberately. A hook with a recorded decision is settled whatever it does.
        Only the pair is a decision nobody can find.
        """
        self.write_manifest([
            self.hook("ran"),
            self.hook("dormant_noted", probe={"note": "DECISION: KEEP."}),
            self.hook("dormant_silent"),
        ])
        self.runtime("440", [
            claim("ran", "runtime_probe", "440", "passed"),
            claim("dormant_noted", "runtime_probe", "440", "inconclusive"),
            claim("dormant_silent", "runtime_probe", "440", "inconclusive"),
        ])

        by_id = {life.hook_id: life for life in roster(self.tmp)[0]}
        self.assertFalse(by_id["ran"].dormant_and_undecided)
        self.assertFalse(by_id["dormant_noted"].dormant_and_undecided)
        self.assertTrue(by_id["dormant_silent"].dormant_and_undecided)

    def test_a_hook_that_is_not_active_is_shown_as_such_and_still_listed(self) -> None:
        """`status` is the only remaining word for a hook we have stopped carrying.

        It replaced a recorded `retirement`, which used to answer this and was
        deleted with the rest of the decision-correction layer. The row is
        printed rather than filtered out — a hook nobody expects any more is
        exactly the one a reader needs to see beside its evidence — and it does
        NOT suppress `dormant_and_undecided`, because a status word is not a
        written reason.
        """
        self.write_manifest([self.hook("gone", status="removed")])
        self.runtime("440", [claim("gone", "runtime_probe", "440", "inconclusive")])

        life = roster(self.tmp)[0][0]
        text = render(*roster(self.tmp))

        self.assertEqual("removed", life.status)
        self.assertTrue(life.dormant_and_undecided)
        self.assertIn("not active", text)
        self.assertIn("gone — status removed", text)

    def test_an_active_hook_produces_no_not_active_section(self) -> None:
        """The control for the test above: the section must be able to be absent."""
        self.write_manifest([self.hook("here")])
        self.runtime("440", [claim("here", "runtime_probe", "440", "passed")])

        self.assertNotIn("not active", render(*roster(self.tmp)))


class RefusalTests(RosterTestCase):
    def test_a_corrupt_runtime_file_is_refused_by_line(self) -> None:
        """Not skipped. A row that cannot be read is not a hook that did not run."""
        self.write_manifest([self.hook("a")])
        (self.manifest / "runtime_evidence" / "440.jsonl").write_text(
            claim("a", "runtime_probe", "440", "passed") + "\nnot json\n", encoding="utf-8"
        )
        with self.assertRaises(RosterError) as caught:
            roster(self.tmp)
        self.assertIn("440.jsonl:2:", str(caught.exception))

    def test_a_claim_with_no_verdict_names_where_it_was_missing(self) -> None:
        self.write_manifest([self.hook("a")])
        (self.manifest / "runtime_evidence" / "440.jsonl").write_text(
            json.dumps({"hook_id": "a"}) + "\n", encoding="utf-8"
        )
        with self.assertRaises(RosterError) as caught:
            roster(self.tmp)
        self.assertIn("hook_id/verdict", str(caught.exception))

    def test_no_evidence_at_all_is_refused_rather_than_rendered_empty(self) -> None:
        self.write_manifest([self.hook("a")])
        with self.assertRaises(RosterError):
            roster(self.tmp)

    def test_render_refuses_an_empty_roster(self) -> None:
        """A printed "no hooks" for a manifest holding seven reads as a fact."""
        with self.assertRaises(RosterError):
            render([], ["440"])

    def test_main_refuses_cleanly_rather_than_tracebacking(self) -> None:
        self.write_manifest([self.hook("a")])
        self.assertEqual(2, main(["--root", str(self.tmp)]))
        self.assertEqual(2, main(["--root", str(self.tmp), "--baseline", "nope"]))


class CommittedCorpusTests(unittest.TestCase):
    """The roster over this repository, which is where it earns its keep."""

    def test_every_manifest_hook_appears_exactly_once(self) -> None:
        lives, _ = roster(REPOSITORY)
        declared = [
            hook["hook_id"]
            for hook in json.loads(
                (REPOSITORY / "manifest" / "hooks.json").read_text(encoding="utf-8")
            )["hooks"]
        ]
        self.assertEqual(declared, [life.hook_id for life in lives])

    def test_the_release_ready_column_agrees_with_the_expectation(self) -> None:
        """Not recomputed here. One answer, reached the same way the gate reaches it."""
        from dfinsta_pipeline import expectation

        lives, _ = roster(REPOSITORY)
        by_id = {life.hook_id: life for life in lives}
        report = expectation.port_report(REPOSITORY, "441", "440")
        for hook in report.ready:
            self.assertIn("441", by_id[hook].release_ready_on, hook)

    def test_the_dormant_three_are_visible_and_two_have_no_written_reason(self) -> None:
        """Pins today's real state, including the gap.

        Three hooks have never executed on any version and the owner decided on
        2026-08-01 to keep all three — but only one carries that decision in the
        manifest. The other two were settled in conversation, so the repository
        cannot answer "why are we carrying this?".

        This test changes when that changes, in either direction: writing the
        reasoning into the manifest, or one of them finally executing.
        """
        lives, _ = roster(REPOSITORY)
        never = sorted(life.hook_id for life in lives if life.never_ran)
        self.assertEqual(
            [
                "install_settings_long_click_actionbar",
                "replace_reels_discover_endpoint",
                "replace_reels_homecoming_endpoint",
            ],
            never,
        )
        undecided = sorted(life.hook_id for life in lives if life.dormant_and_undecided)
        self.assertEqual(
            ["install_settings_long_click_actionbar", "replace_reels_homecoming_endpoint"],
            undecided,
        )

    def test_it_renders(self) -> None:
        text = render(*roster(REPOSITORY))
        self.assertIn("HOOK ROSTER", text)
        self.assertIn("never executed, and nothing written down", text)
        self.assertEqual(0, main(["--root", str(REPOSITORY)]))
        self.assertEqual(0, main(["--root", str(REPOSITORY), "--json"]))


# ===========================================================================
#   Adversarial additions.
#
#   Written against the module rather than with it. Where an existing test
#   asserted a property the rendered text could not violate, the version here
#   reads the cell out of the table instead.
# ===========================================================================


def cells_of(text: str, hook_id: str, hook_ids: list[str], versions: list[str]) -> list[str]:
    """The per-version glyphs on one hook's row, read out of the rendered table.

    `assertIn(SILENT, text)` cannot fail: `render` prints the legend
    `✓ ran   · measured, did not run   — no evidence` on every run, so all three
    glyphs are in the output of every roster ever produced, including one that
    printed the same glyph in every cell. The distinction this module exists to
    keep therefore has to be asserted **positionally**, which is what this reads.
    """

    width = max(len(item) for item in hook_ids) + 2
    prefix = "  " + hook_id.ljust(width)
    for line in text.splitlines():
        if line.startswith(prefix):
            body = line[len(prefix) :]
            return [body[index * 6 : (index + 1) * 6].strip() for index in range(len(versions))]
    raise AssertionError(f"{hook_id} has no row in the table")


class ThreeWayCellTests(RosterTestCase):
    """`—` and `·` are different facts, per version and per hook."""

    def test_the_glyph_in_each_cell_is_read_back_positionally(self) -> None:
        """One hook, three versions, one of each state — in the table itself.

        Collapsing `·` into `—` leaves the legend untouched and every
        `assertIn(glyph, text)` still passing, so the only assertion that bites
        is the one that names the column.
        """
        self.write_manifest([self.hook("mixed")])
        self.runtime("439", [claim("mixed", "runtime_probe", "439", "passed")])
        self.runtime("440", [claim("mixed", "runtime_probe", "440", "inconclusive")])
        # 441 has evidence so the column exists, but says nothing about `mixed`.
        self.runtime("441", [claim("other", "runtime_probe", "441", "passed")])

        lives, versions = roster(self.tmp)
        self.assertEqual(["439", "440", "441"], list(versions))
        cells = cells_of(render(lives, versions), "mixed", ["mixed"], versions)
        self.assertEqual([RAN, SILENT, UNMEASURED], cells)

    def test_measured_and_silent_never_renders_as_unmeasured(self) -> None:
        """The two hooks that must not look alike, side by side.

        `quiet` was measured on every version and stayed silent. `absent` was
        measured on none. Rendering them identically is how "we never looked"
        comes to read as "it is broken" — the mistake that made the first
        differential compare 2 of 7.
        """
        self.write_manifest([self.hook("quiet"), self.hook("absent")])
        for version in ("439", "440"):
            self.runtime(version, [claim("quiet", "runtime_probe", version, "inconclusive")])

        lives, versions = roster(self.tmp)
        text = render(lives, versions)
        names = ["quiet", "absent"]
        self.assertEqual([SILENT, SILENT], cells_of(text, "quiet", names, versions))
        self.assertEqual([UNMEASURED, UNMEASURED], cells_of(text, "absent", names, versions))
        self.assertNotEqual(
            cells_of(text, "quiet", names, versions),
            cells_of(text, "absent", names, versions),
        )

        by_id = {life.hook_id: life for life in lives}
        self.assertEqual(("439", "440"), by_id["quiet"].measured_on)
        self.assertEqual((), by_id["absent"].measured_on)
        # Both never ran, so `never_ran` alone cannot tell them apart — which is
        # exactly why `measured_on` has to exist beside it.
        self.assertTrue(by_id["quiet"].never_ran)
        self.assertTrue(by_id["absent"].never_ran)

    def test_a_hook_measured_on_one_version_only_is_unmeasured_on_the_others(self) -> None:
        self.write_manifest([self.hook("late")])
        self.runtime("439", [claim("other", "runtime_probe", "439", "passed")])
        self.runtime("440", [claim("late", "runtime_probe", "440", "passed")])
        lives, versions = roster(self.tmp)
        self.assertEqual(
            [UNMEASURED, RAN], cells_of(render(lives, versions), "late", ["late"], versions)
        )


class RanOnTests(RosterTestCase):
    """Whether a hook executed must not depend on how its file was appended."""

    def test_three_rows_on_one_version_in_every_order(self) -> None:
        """440 really carries `inconclusive` then `passed` for one hook.

        Two rows in two orders leaves a mutation that reads, say, the middle
        row alive. Six permutations of three rows does not.
        """
        self.write_manifest([self.hook("a")])
        orders = [
            ("passed", "inconclusive", "inconclusive"),
            ("inconclusive", "passed", "inconclusive"),
            ("inconclusive", "inconclusive", "passed"),
            ("passed", "passed", "inconclusive"),
            ("inconclusive", "passed", "passed"),
            ("passed", "inconclusive", "passed"),
        ]
        for order in orders:
            with self.subTest(order=order):
                self.runtime(
                    "440", [claim("a", "runtime_probe", "440", verdict) for verdict in order]
                )
                lives, _ = roster(self.tmp)
                self.assertEqual(("440",), lives[0].ran_on)
                self.assertEqual(("440",), lives[0].measured_on)
                self.assertEqual("440", lives[0].last_ran)
                self.assertFalse(lives[0].never_ran)

    def test_a_hook_that_never_passes_stays_never_ran_however_many_rows(self) -> None:
        """The positive control for the test above.

        `any(passed)` and `True` are indistinguishable on a corpus where
        something always passes.
        """
        self.write_manifest([self.hook("a")])
        self.runtime("440", [
            claim("a", "runtime_probe", "440", "inconclusive"),
            claim("a", "runtime_probe", "440", "failed"),
            claim("a", "runtime_probe", "440", "inconclusive"),
        ])
        lives, _ = roster(self.tmp)
        self.assertEqual((), lives[0].ran_on)
        self.assertEqual(("440",), lives[0].measured_on)
        self.assertIsNone(lives[0].last_ran)
        self.assertTrue(lives[0].never_ran)

    def test_ran_on_is_in_release_order_with_a_gap_in_the_middle(self) -> None:
        self.write_manifest([self.hook("a")])
        self.runtime("439", [claim("a", "runtime_probe", "439", "passed")])
        self.runtime("440", [claim("a", "runtime_probe", "440", "inconclusive")])
        self.runtime("441", [claim("a", "runtime_probe", "441", "passed")])
        life = roster(self.tmp)[0][0]
        self.assertEqual(("439", "441"), life.ran_on)
        self.assertEqual(("439", "440", "441"), life.measured_on)
        self.assertEqual("441", life.last_ran)

    def test_versions_order_by_number_and_not_as_strings(self) -> None:
        """`last_ran` is the latest release, not the largest string.

        Sorted as text, `1000` orders before `439` and every hook's last run
        reads as the oldest version in the series.
        """
        self.write_manifest([self.hook("a")])
        self.runtime("439", [claim("a", "runtime_probe", "439", "passed")])
        self.runtime("1000", [claim("a", "runtime_probe", "1000", "passed")])
        lives, versions = roster(self.tmp)
        self.assertEqual(["439", "1000"], list(versions))
        self.assertEqual(("439", "1000"), lives[0].ran_on)
        self.assertEqual("1000", lives[0].last_ran)


class DormantAndUndecidedHalvesTests(unittest.TestCase):
    """Each conjunct on its own, without a corpus in the way.

    `dormant_and_undecided` is the only opinion this view holds, and it is two
    conditions ANDed — it was three until the recorded `retirement` that formed
    the third was deleted. Asserting it through `roster()` alone leaves a corpus
    able to satisfy one of them by accident, so the truth table is checked
    directly on the object and then confirmed end to end.
    """

    def life(self, **extra) -> HookLife:
        base = dict(
            hook_id="h", intent="", tier="", status="active",
            ran_on=(), measured_on=("440",), release_ready_on=(), assessed_on=(),
        )
        base.update(extra)
        return HookLife(**base)

    def test_both_conditions_are_required(self) -> None:
        self.assertTrue(self.life().dormant_and_undecided)
        # never_ran fails
        self.assertFalse(self.life(ran_on=("440",)).dormant_and_undecided)
        # note present
        self.assertFalse(self.life(note="DECISION: KEEP.").dormant_and_undecided)
        # and both together, so neither condition can carry the answer alone
        self.assertFalse(
            self.life(ran_on=("440",), note="DECISION: KEEP.").dormant_and_undecided
        )

    def test_a_status_word_does_not_stand_in_for_a_written_reason(self) -> None:
        """The exemption a recorded retirement used to give, deliberately not restored.

        `status` says the project has stopped carrying the hook; it does not say
        why, and `dormant_and_undecided` is about a decision nobody can find.
        """
        self.assertTrue(self.life(status="removed").dormant_and_undecided)

    def test_never_ran_is_not_inverted(self) -> None:
        self.assertTrue(self.life(ran_on=()).never_ran)
        self.assertFalse(self.life(ran_on=("440",)).never_ran)
        self.assertIsNone(self.life(ran_on=()).last_ran)
        self.assertEqual("440", self.life(ran_on=("439", "440")).last_ran)

    def test_to_dict_carries_the_verdict_and_both_version_sets(self) -> None:
        payload = self.life(ran_on=(), measured_on=("440",), note="").to_dict()
        self.assertTrue(payload["dormant_and_undecided"])
        self.assertEqual([], payload["ran_on"])
        self.assertEqual(["440"], payload["measured_on"])
        self.assertIsNone(payload["last_ran"])


class NoteDepthTests(RosterTestCase):
    def test_a_note_is_found_at_every_depth_including_the_top(self) -> None:
        """There is no field for this, and the search must not assume one.

        The only real note lives in `probe.note`. Narrowing the walk to
        top-level strings loses it; narrowing it to nested values loses a note
        somebody writes at the top. Both directions are checked.
        """
        self.write_manifest([
            self.hook("top", comment="DECISION 2026-08-01 (human): KEEP top."),
            self.hook("nested", probe={"note": "DECISION 2026-08-01 (human): KEEP nested."}),
            self.hook("in_list", constraints=["DECISION 2026-08-02 (human): KEEP list."]),
            self.hook("deep", probe={"history": [{"entries": [
                {"text": "DECISION 2026-08-03 (human): KEEP deep."}
            ]}]}),
            self.hook("none", probe={"note": "Measured by block counting."}),
        ])
        self.runtime("440", [
            claim(h, "runtime_probe", "440", "inconclusive")
            for h in ("top", "nested", "in_list", "deep", "none")
        ])
        by_id = {life.hook_id: life for life in roster(self.tmp)[0]}
        self.assertIn("KEEP top", by_id["top"].note)
        self.assertIn("KEEP nested", by_id["nested"].note)
        self.assertIn("KEEP list", by_id["in_list"].note)
        self.assertIn("KEEP deep", by_id["deep"].note)
        self.assertEqual("", by_id["none"].note)
        self.assertTrue(by_id["none"].dormant_and_undecided)
        self.assertFalse(by_id["deep"].dormant_and_undecided)

    def test_a_non_string_value_is_walked_past_rather_than_matched(self) -> None:
        self.write_manifest([self.hook("a", probe={"count": 3, "ok": True, "seen": None})])
        self.runtime("440", [claim("a", "runtime_probe", "440", "inconclusive")])
        self.assertEqual("", roster(self.tmp)[0][0].note)

    def test_the_excerpt_starts_at_the_word_decision(self) -> None:
        """Printing the note's first sentence summarised the decision as its
        measurement method, which is true and is not the decision."""
        self.write_manifest([self.hook("a", probe={"note":
            "Block-counting CANNOT measure this hook.\n"
            "DECISION 2026-08-01 (human): KEEP. Costs one instruction."})])
        self.runtime("440", [claim("a", "runtime_probe", "440", "inconclusive")])
        text = render(*roster(self.tmp))
        self.assertIn("written decisions in the manifest", text)
        excerpt = [line for line in text.splitlines() if "KEEP" in line]
        self.assertEqual(1, len(excerpt))
        self.assertTrue(excerpt[0].strip().startswith("DECISION 2026-08-01"), excerpt[0])
        self.assertNotIn("Block-counting", text)
        self.assertNotIn("REVISIT trigger", text)

    def test_a_revisit_trigger_is_called_out_only_when_present(self) -> None:
        self.write_manifest([self.hook("a", probe={"note":
            "DECISION 2026-08-01 (human): KEEP. REVISIT if the purge lands."})])
        self.runtime("440", [claim("a", "runtime_probe", "440", "inconclusive")])
        self.assertIn("REVISIT trigger", render(*roster(self.tmp)))


class ScopingTests(RosterTestCase):
    """`--root` must decide every read. A default resolved against the process
    CWD is half-scoped, which is worse than unscoped because it looks right."""

    def decoy(self) -> Path:
        """A second, contradictory repository to stand in the process CWD.

        Every answer here is the opposite of the corpus under test, so anything
        read relative to the CWD changes the result rather than hiding in it.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        decoy = Path(directory.name)
        manifest = decoy / "manifest"
        (manifest / "runtime_evidence").mkdir(parents=True)
        (manifest / "hooks.json").write_text(
            json.dumps({"schema_version": 1, "hooks": [
                {"hook_id": "DECOY", "intent": "wrong", "tier": "x", "status": "active"}
            ]}),
            encoding="utf-8",
        )
        (manifest / "runtime_evidence" / "998.jsonl").write_text(
            claim("DECOY", "runtime_probe", "998", "passed") + "\n", encoding="utf-8"
        )
        return decoy

    def test_the_answer_does_not_change_with_the_process_directory(self) -> None:
        self.write_manifest([self.hook("a")])
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        here = roster(self.tmp)

        decoy = self.decoy()
        previous = os.getcwd()
        os.chdir(decoy)
        self.addCleanup(os.chdir, previous)
        there = roster(self.tmp)

        self.assertEqual(
            ([life.to_dict() for life in here[0]], here[1]),
            ([life.to_dict() for life in there[0]], there[1]),
        )
        self.assertEqual(["a"], [life.hook_id for life in there[0]])
        self.assertEqual(["440"], list(there[1]))

    def test_the_decoy_is_a_real_control(self) -> None:
        """The test above proves nothing unless the decoy is readable as a root."""
        decoy = self.decoy()
        lives, versions = roster(decoy, baseline="998")
        self.assertEqual(["DECOY"], [life.hook_id for life in lives])
        self.assertEqual(["998"], list(versions))


class MoreRefusalTests(RosterTestCase):
    def main(self, argv: list[str]) -> int:
        """`main`, with its output swallowed. Only the exit code is under test."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def positive_control(self) -> None:
        """The same corpus, uncorrupted, must succeed — or the refusal proves nothing."""
        self.assertEqual(1, len(roster(self.tmp)[0]))

    def test_an_absent_manifest_names_the_path_it_looked_for(self) -> None:
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        with self.assertRaises(RosterError) as caught:
            roster(self.tmp)
        self.assertIn("hooks.json", str(caught.exception))
        self.write_manifest([self.hook("a")])
        self.positive_control()

    def test_a_manifest_that_is_not_json_names_the_path(self) -> None:
        (self.manifest / "hooks.json").write_text("{oops", encoding="utf-8")
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        with self.assertRaises(RosterError) as caught:
            roster(self.tmp)
        self.assertIn("hooks.json", str(caught.exception))
        self.assertEqual(2, self.main(["--root", str(self.tmp)]))
        self.write_manifest([self.hook("a")])
        self.positive_control()

    def test_a_corrupt_row_names_the_line_and_a_clean_file_does_not_refuse(self) -> None:
        self.write_manifest([self.hook("a")])
        (self.manifest / "runtime_evidence" / "440.jsonl").write_text(
            claim("a", "runtime_probe", "440", "passed") + "\n"
            + claim("a", "runtime_probe", "440", "passed") + "\n"
            + "{ not json\n",
            encoding="utf-8",
        )
        with self.assertRaises(RosterError) as caught:
            roster(self.tmp)
        self.assertIn("440.jsonl:3:", str(caught.exception))
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        self.positive_control()

    def test_a_claim_missing_either_field_is_refused_by_line(self) -> None:
        for row in (
            json.dumps({"hook_id": "a"}),
            json.dumps({"verdict": "passed"}),
            json.dumps({"record": {"hook_id": "a"}}),
        ):
            with self.subTest(row=row):
                self.write_manifest([self.hook("a")])
                (self.manifest / "runtime_evidence" / "440.jsonl").write_text(
                    "\n" + row + "\n", encoding="utf-8"
                )
                with self.assertRaises(RosterError) as caught:
                    roster(self.tmp)
                message = str(caught.exception)
                self.assertIn("440.jsonl:2:", message)
                self.assertIn("hook_id/verdict", message)

    def test_the_record_wrapper_is_honoured(self) -> None:
        """The positive control for the wrapper: both shapes must be readable."""
        self.write_manifest([self.hook("a")])
        (self.manifest / "runtime_evidence" / "440.jsonl").write_text(
            json.dumps({"schema_version": 1, "record": json.loads(
                claim("a", "runtime_probe", "440", "passed"))}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(("440",), roster(self.tmp)[0][0].ran_on)

    def test_every_cli_exit_code(self) -> None:
        self.write_manifest([self.hook("a")])
        # No evidence at all: refusal, not an empty table.
        self.assertEqual(2, self.main(["--root", str(self.tmp)]))
        self.assertEqual(2, self.main(["--root", str(self.tmp), "--json"]))
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        self.assertEqual(0, self.main(["--root", str(self.tmp)]))
        self.assertEqual(0, self.main(["--root", str(self.tmp), "--json"]))
        self.assertEqual(0, self.main(["--root", str(self.tmp), "--baseline", "440"]))
        # A baseline that is not a version number, and one that excludes the
        # whole series: both refusals, neither a traceback.
        self.assertEqual(2, self.main(["--root", str(self.tmp), "--baseline", "nope"]))
        self.assertEqual(2, self.main(["--root", str(self.tmp), "--baseline", "999"]))
        self.assertEqual(2, self.main(["--root", str(self.tmp / "nowhere")]))

    def test_a_missing_root_refuses_rather_than_tracebacking(self) -> None:
        with self.assertRaises(RosterError):
            roster(self.tmp / "nowhere")


class JsonFormTests(RosterTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(argv)
        return code, stream.getvalue()

    def test_the_json_form_carries_every_field_the_table_shows(self) -> None:
        """The machine-readable view is the one a script gates on.

        This project has shipped a `--json` that dropped the half the human form
        printed a banner about, so every field the table displays is asserted
        present here — including the one opinion, `dormant_and_undecided`.
        """
        self.write_manifest([
            self.hook("ran"),
            self.hook("quiet", probe={"note": "DECISION: KEEP."}),
            self.hook("undecided"),
        ])
        self.runtime("440", [
            claim("ran", "runtime_probe", "440", "passed"),
            claim("quiet", "runtime_probe", "440", "inconclusive"),
        ])
        code, out = self.run_main(["--root", str(self.tmp), "--json"])
        self.assertEqual(0, code)
        payload = json.loads(out)
        # An OBJECT with `versions` and `hooks`, not a bare list. `versions` is
        # half of `roster()`'s result and without it a JSON consumer cannot tell
        # `—` (never measured) from `·` (measured, silent) — it can only compare
        # `ran_on` against `measured_on`, and both are empty for a hook nobody
        # looked at AND for a hook nobody looked at on one particular version.
        self.assertEqual({"schema_version", "versions", "hooks"}, set(payload))
        hooks = payload["hooks"]
        self.assertEqual(["ran", "quiet", "undecided"], [item["hook_id"] for item in hooks])
        by_id = {item["hook_id"]: item for item in hooks}
        self.assertEqual(["440"], by_id["ran"]["ran_on"])
        self.assertEqual(["440"], by_id["ran"]["measured_on"])
        self.assertEqual([], by_id["quiet"]["ran_on"])
        self.assertEqual(["440"], by_id["quiet"]["measured_on"])
        self.assertEqual([], by_id["undecided"]["measured_on"])
        self.assertFalse(by_id["quiet"]["dormant_and_undecided"])
        self.assertTrue(by_id["undecided"]["dormant_and_undecided"])
        self.assertIn("KEEP", by_id["quiet"]["note"])
        for item in hooks:
            self.assertEqual(
                {"hook_id", "intent", "tier", "status", "ran_on", "measured_on",
                 "release_ready_on", "assessed_on", "last_ran", "note",
                 "dormant_and_undecided"},
                set(item),
            )

    def test_the_json_and_the_table_agree_about_which_hooks_are_undecided(self) -> None:
        """Two renderings of one answer. They may not disagree."""
        self.write_manifest([self.hook("a"), self.hook("b", probe={"note": "DECISION: KEEP."})])
        self.runtime("440", [
            claim("a", "runtime_probe", "440", "inconclusive"),
            claim("b", "runtime_probe", "440", "inconclusive"),
        ])
        _, out = self.run_main(["--root", str(self.tmp), "--json"])
        undecided = [
            i["hook_id"] for i in json.loads(out)["hooks"] if i["dormant_and_undecided"]
        ]
        text = render(*roster(self.tmp))
        self.assertEqual(["a"], undecided)
        self.assertIn("never executed, and nothing written down (1)", text)


class RenderRefusalTests(RosterTestCase):
    def test_render_refuses_an_empty_roster_even_with_versions(self) -> None:
        with self.assertRaises(RosterError):
            render([], ["439", "440"])

    def test_render_names_the_span_it_covers(self) -> None:
        self.write_manifest([self.hook("a")])
        self.runtime("439", [claim("a", "runtime_probe", "439", "passed")])
        self.runtime("441", [claim("a", "runtime_probe", "441", "passed")])
        text = render(*roster(self.tmp))
        self.assertIn("439 → 441", text.splitlines()[0])

    def test_the_legend_states_the_distinction_on_every_run(self) -> None:
        self.write_manifest([self.hook("a")])
        self.runtime("440", [claim("a", "runtime_probe", "440", "passed")])
        text = render(*roster(self.tmp))
        self.assertIn(f"{RAN} ran", text)
        self.assertIn(f"{SILENT} measured, did not run", text)
        self.assertIn(f"{UNMEASURED} no evidence", text)
        self.assertEqual(3, len({RAN, SILENT, UNMEASURED}))


class CommittedCorpusInvariantTests(unittest.TestCase):
    """Properties of today's real corpus that a port is expected to preserve."""

    def test_no_hook_is_release_ready_on_a_version_it_was_not_assessed_on(self) -> None:
        for life in roster(REPOSITORY)[0]:
            self.assertTrue(
                set(life.release_ready_on) <= set(life.assessed_on),
                f"{life.hook_id}: {life.release_ready_on} ⊄ {life.assessed_on}",
            )

    def test_every_hook_that_ran_was_measured(self) -> None:
        """`ran_on ⊆ measured_on` is what makes the three cells exhaustive.

        A version in `ran_on` and not in `measured_on` would render `✓` from a
        row the module also claims not to have seen.
        """
        lives, versions = roster(REPOSITORY)
        for life in lives:
            self.assertTrue(set(life.ran_on) <= set(life.measured_on), life.hook_id)
            self.assertTrue(set(life.measured_on) <= set(versions), life.hook_id)

    def test_the_three_dormant_hooks_are_measured_and_silent_not_unmeasured(self) -> None:
        """The distinction, on the real corpus and not only on a fixture.

        These three have never executed. If they were also never *measured* the
        honest reading would be "we have not looked", and the owner's decision to
        keep them would rest on nothing. They were measured on every version.
        """
        lives, versions = roster(REPOSITORY)
        names = [life.hook_id for life in lives]
        text = render(lives, versions)
        for hook_id in (
            "install_settings_long_click_actionbar",
            "replace_reels_discover_endpoint",
            "replace_reels_homecoming_endpoint",
        ):
            life = next(item for item in lives if item.hook_id == hook_id)
            self.assertEqual((), life.ran_on, hook_id)
            self.assertEqual(tuple(versions), life.measured_on, hook_id)
            self.assertEqual(
                [SILENT] * len(versions), cells_of(text, hook_id, names, versions), hook_id
            )

    def test_a_hook_that_runs_renders_a_tick_in_that_column(self) -> None:
        """The positive control for the test above."""
        lives, versions = roster(REPOSITORY)
        names = [life.hook_id for life in lives]
        cells = cells_of(render(lives, versions), "set_app_context", names, versions)
        self.assertEqual([RAN] * len(versions), cells)


if __name__ == "__main__":
    unittest.main()
