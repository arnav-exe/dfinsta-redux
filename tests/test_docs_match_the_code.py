"""Documentation claims about the code, checked against the code.

On 2026-08-08 a sweep of the twenty-odd documents in this repo found one fact —
"the replay chain is deliberately not registered" — stated as current in nine
places in `HANDOVER.md`, including its "exact continuation point", eight days
after registration landed. `pipeline_flowchart.md` said of the feature gate
**"Nothing raises it, and nothing can"** in the most recently refreshed table in
the repo, which is exactly where a reader trusts a line.

`tests/test_open_items.py` already solves the mirror image of this: it executes
the claims in the *open-item* lists so that closing a gap fails the suite. This
file does the same for claims that have already been corrected — each rule is
**derived from the code**, not from a list of banned strings, so it only fires
while the code makes the sentence false.

A rule passes if the phrase is absent, or struck through (`~~…~~`), or on a line
that marks itself stale. That is deliberate: these documents are historical
records and several are preserved verbatim on purpose. The requirement is that a
false sentence be *marked*, never that it be deleted.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dfinsta_pipeline import submission, worker

ROOT = Path(__file__).resolve().parents[1]

#: A line carrying one of these is owning up, not asserting. `>` is here because
#: every correction banner added on 2026-08-08 is a blockquote, and a banner has
#: to be able to quote the sentence it is correcting — the first version of this
#: test flagged its own corrections.
MARKED = (
    "~~",
    ">",
    "Stale",
    "stale",
    "Corrections",
    "RETRACTED",
    "Closed 2026",
    "Done 2026",
)


def offending_lines(relative: str, phrase: str) -> list[str]:
    """Lines containing `phrase` that do not mark themselves as superseded."""

    path = ROOT / relative
    if not path.is_file():
        return []
    return [
        f"{relative}:{number}: {line.strip()[:110]}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if phrase in line and not any(marker in line for marker in MARKED)
    ]


class DocumentationMatchesCodeTests(unittest.TestCase):
    def test_no_document_says_the_workflows_are_unregistered(self) -> None:
        """Only while four workflows are in fact registered.

        The rule is gated on the code so it cannot outlive its own premise: if
        registration were ever reverted, these sentences would become true again
        and this test would stop demanding they be struck.
        """
        registered = {cls.__name__ for cls in worker.REGISTERED_WORKFLOWS}
        self.assertEqual(
            {
                "PortRunWorkflow",
                "ReplayRunWorkflow",
                "FeatureAssessmentRunWorkflow",
                "HookRetirementRunWorkflow",
            },
            registered,
            "the premise of this test changed; re-read the rules below before editing",
        )

        found: list[str] = []
        for relative in ("HANDOVER.md", "docs/SESSION_HANDOFF.md", "docs/ADK_PIPELINE_PLAN.md"):
            for phrase in (
                "registration remains pending",
                "deliberately not registered",
                "intentionally unregistered",
                "No `workflow.py` or `worker.py` change has been made",
            ):
                found += offending_lines(relative, phrase)
        self.assertEqual([], found, "\n".join([""] + found))

    def test_no_document_says_the_feature_gate_cannot_be_raised(self) -> None:
        """Only while it has a gate kind, a producer and a starter."""

        self.assertIn(
            submission.FEATURE_ASSESSMENT_GATE,
            submission.GATE_KINDS,
            "the feature gate left GATE_KINDS; this rule's premise is gone",
        )
        starter = (ROOT / "src/dfinsta_pipeline/assessment_record.py").read_text(encoding="utf-8")
        self.assertIn("FeatureAssessmentRunWorkflow.run", starter)
        self.assertIn("start_workflow", starter)

        found: list[str] = []
        for relative in (
            "pipeline_flowchart.md",
            "docs/SUBMISSION_CLIENT.md",
            "docs/STAGE_4_DESIGN.md",
            "docs/STAGE_4_PRODUCER_DESIGN.md",
        ):
            for phrase in (
                "Nothing raises it",
                "The feature gate has no producer",
                "There is no trusted submission client",
                "Nothing computes a stage 4a assessment",
            ):
                found += offending_lines(relative, phrase)
        self.assertEqual([], found, "\n".join([""] + found))

    def test_no_document_says_heartbeats_are_missing(self) -> None:
        """Only while the replay stages actually heartbeat.

        This one was missed by the doc sweep *and* by me: I reported the roadmap's
        heartbeat item to the owner as genuinely open on the same day I could have
        grepped for `activity.heartbeat`.
        """
        activities = (ROOT / "src/dfinsta_pipeline/activities.py").read_text(encoding="utf-8")
        replay = (ROOT / "src/dfinsta_pipeline/replay_workflow.py").read_text(encoding="utf-8")
        self.assertIn("activity.heartbeat(", activities)
        self.assertIn("heartbeat_timeout=_STAGE_HEARTBEAT_TIMEOUT", replay)

        found = offending_lines("docs/ROADMAP.md", "**Heartbeats (F4)")
        self.assertEqual([], found, "\n".join([""] + found))

    def test_no_document_claims_a_tracked_file_is_untracked(self) -> None:
        """`docs/FINDINGS.md` and `docs/adk_pipeline_design.md` are both tracked."""

        import subprocess

        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "docs/"], capture_output=True, text=True
        )
        if listed.returncode != 0:
            self.skipTest("not a git work tree")
        tracked = set(listed.stdout.split())
        self.assertLessEqual({"docs/FINDINGS.md", "docs/adk_pipeline_design.md"}, tracked)

        found = offending_lines("HANDOVER.md", "currently untracked")
        self.assertEqual([], found, "\n".join([""] + found))

    def test_the_reconstruction_operation_count_agrees_with_the_files(self) -> None:
        """37 = 30 endpoint + 7 anchored. Four documents once gave four numbers."""

        import json

        patches = ROOT / "dfinsta_source_1.4.1" / "patches"
        counts = {
            path.name: len(json.loads(path.read_text(encoding="utf-8"))["operations"])
            for path in sorted(patches.glob("*.json"))
        }
        self.assertEqual({"anchored_patches.json": 7, "endpoint_replacements.json": 30}, counts)
        self.assertEqual([], offending_lines("AGENTS.md", "38 idempotent host operations"))


if __name__ == "__main__":
    unittest.main()
