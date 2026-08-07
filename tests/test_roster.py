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

import json
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

    def test_a_retirement_settles_a_hook_even_with_no_note(self) -> None:
        self.write_manifest([self.hook("gone")])
        self.runtime("440", [claim("gone", "runtime_probe", "440", "inconclusive")])
        (self.manifest / "retirements.jsonl").write_text(
            json.dumps({
                "schema_version": 1, "hook_id": "gone", "effective_from": "441",
                "decision_id": "retire-1", "ruled_by": "arnav",
                "rationale": "Surface removed.", "recorded_at": "2026-08-08T00:00:00Z",
            }) + "\n",
            encoding="utf-8",
        )
        life = roster(self.tmp)[0][0]
        self.assertIsNotNone(life.retirement)
        self.assertFalse(life.dormant_and_undecided)
        self.assertIn("retired", render(*roster(self.tmp)))


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


if __name__ == "__main__":
    unittest.main()
