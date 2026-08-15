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

import re
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
        """Only while every workflow is in fact registered.

        The rule is gated on the code so it cannot outlive its own premise: if
        registration were ever reverted, these sentences would become true again
        and this test would stop demanding they be struck.
        """
        registered = {cls.__name__ for cls in worker.REGISTERED_WORKFLOWS}
        self.assertEqual(
            # `PortRunWorkflow` was here until 2026-08-15. The sentences this rule
            # strikes were written when NOTHING was registered, so they are still
            # false while these two are — which is what the rule needs. Deleting a
            # workflow does not make "registration remains pending" true again.
            {
                "ReplayRunWorkflow",
                "FeatureAssessmentRunWorkflow",
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

    def test_no_document_says_nothing_asserts_a_heartbeat(self) -> None:
        """A design note's own test-gap list rotted, and an agent survey repeated it.

        `WORKFLOW_REGISTRATION_DESIGN.md` listed "nothing anywhere asserts a
        heartbeat" among the gaps a future slice would close. It was closed the
        same week by `tests/test_phase_b_heartbeat.py`, and the list was not
        updated — so a doc sweep citing that list reported a gap that had not
        existed for days. Citing a document is not measuring the code.
        """
        heartbeat_tests = ROOT / "tests" / "test_phase_b_heartbeat.py"
        self.assertTrue(heartbeat_tests.is_file())
        self.assertIn("def test_", heartbeat_tests.read_text(encoding="utf-8"))

        found = offending_lines(
            "docs/WORKFLOW_REGISTRATION_DESIGN.md", "nothing anywhere asserts a heartbeat"
        )
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



class DocsNameOnlyModulesThatExistTests(unittest.TestCase):
    """A document that names a deleted module is rot a reader can act on.

    Nine modules were deleted on 2026-08-08 and the sweep that found the stale
    references afterwards was done by hand. The harm is specific: `docs/` carried
    runnable command lines for modules that no longer import, and `README.md`
    linked a file that no longer existed. Both are the kind of thing a reader
    trusts precisely because it looks executable.

    Derived, not listed: every `dfinsta_pipeline.<name>` a document mentions must
    be a module that exists. No deny-list to keep in step with the source, and it
    keeps working for the next deletion without being edited.

    `docs/history/` is deliberately excluded — those documents describe a
    superseded design and naming its modules is what they are FOR. Their own
    README says nothing in them is authoritative.
    """

    MODULE = re.compile(r"dfinsta_pipeline[./]([a-z_][a-z0-9_]*)")

    def documents(self):
        return [
            path
            for path in sorted(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]
            if path.is_file()
        ]

    def test_every_module_a_current_document_names_still_exists(self):
        missing: dict[str, list[str]] = {}
        named = 0
        for document in self.documents():
            for match in self.MODULE.finditer(document.read_text(encoding="utf-8")):
                name = match.group(1)
                named += 1
                if not (ROOT / "src" / "dfinsta_pipeline" / f"{name}.py").is_file():
                    missing.setdefault(name, []).append(document.name)
        self.assertEqual(
            {}, missing,
            "a current document names a module that no longer exists; either the "
            "document is stale or it belongs in docs/history/",
        )
        # Not vacuous: a regex that matched nothing would also report no misses.
        self.assertGreater(named, 5, "the module reference pattern matched almost nothing")

    def test_the_check_would_catch_a_deleted_module(self):
        """The control. Without it the assertion above cannot be shown to work."""
        missing = [
            name
            for name in ("reconsider", "reversal_gate", "guards")
            if not (ROOT / "src" / "dfinsta_pipeline" / f"{name}.py").is_file()
        ]
        self.assertEqual(["reconsider", "reversal_gate"], missing)

    def test_history_is_excluded_and_does_name_deleted_modules(self):
        """Both halves: the exclusion is real, and it is load-bearing rather than tidy."""
        history = sorted((ROOT / "docs" / "history").glob("*.md"))
        self.assertTrue(history, "docs/history/ is empty; the exclusion protects nothing")
        self.assertNotIn(
            "history", {path.parent.name for path in self.documents()}
        )
        stale = {
            name
            for path in history
            for name in self.MODULE.findall(path.read_text(encoding="utf-8"))
            if not (ROOT / "src" / "dfinsta_pipeline" / f"{name}.py").is_file()
        }
        self.assertTrue(
            stale, "no archived document names a deleted module, so excluding them proves nothing"
        )

if __name__ == "__main__":
    unittest.main()
