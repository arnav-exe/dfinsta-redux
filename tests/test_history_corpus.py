"""Replay every committed History against the Workflow that recorded it.

`tests/test_phase_a_history_corpus.py` did this for `PortRunWorkflow` and stated
the reason: a test that generates the History it replays in the same process can
only confirm that a Workflow agrees with itself. Three of the four registered
Workflows had no committed History at all, and every registered Workflow is
`versioning_behavior=PINNED` — so a change to `ReplayRunWorkflow`,
`FeatureAssessmentRunWorkflow`, `HookRetirementRunWorkflow` or
`ReversalRunWorkflow` could break replay of runs that were already durably
recorded and nothing in this repository would notice. A worker restarted after
such a deploy fails the *resumed* run, days into a human wait.

Everything here is derived rather than listed. The Workflow a fixture covers is
read out of the fixture's own `WorkflowExecutionStarted` event; the set of
Workflows that must be covered comes from `worker.REGISTERED_WORKFLOWS`; and
which of them need an *open* History as well as a closed one comes from whether
they declare an update handler. Adding a fifth Workflow therefore fails this file
until it has a fixture and a control, without anybody remembering to edit a list.

===============================================================================
  THE NEGATIVE CONTROLS ARE THE POINT
===============================================================================

A replay test that only asserts "the fixture replayed" passes just as happily
against a replayer that accepts anything, and that failure mode is invisible: a
green suite. So every fixture is also replayed against a Workflow registered
under the same type name with a different command stream, and that MUST raise
`NondeterminismError`.

A trivial control (complete immediately) is what is committed, because it stays
correct as the real Workflows evolve. The subtler question — does a *realistic*
edit to a real Workflow break these fixtures? — was answered by mutation instead:
dropping the `wait_condition` timeout, reordering two stage Activities and
retyping an Activity were each applied to an out-of-tree copy, and each one made
the corresponding fixture fail replay.

===============================================================================
  THIS MODULE IS RE-IMPORTED INSIDE THE WORKFLOW SANDBOX
===============================================================================

When the Replayer validates a Workflow class, Temporal's sandbox **re-imports the
module that defines it** — this one. So nothing at module scope here may do
anything the sandbox restricts. That is not a theoretical constraint:
`tests/test_phase_a_history_corpus.py` has a module-level
`Path(__file__).resolve()`, and its own negative control therefore fails with
`RuntimeError: Failed validating workflow PortRunWorkflow` whenever that file is
run alone (recorded in `docs/IMPLEMENTATION_STATE.md` as a test-isolation bug of
unknown cause; the cause is that `pathlib.Path.resolve` is restricted in the
sandbox, and the fallback control defined in the `except ImportError` branch is
the one that gets used when `tests/` is not on `sys.path`).

Here the controls are defined unconditionally, so the sandbox re-imports this
module on **every** run rather than only in the isolated one. A restricted call
added at module scope fails the suite immediately instead of hiding until someone
runs one file. `dfinsta_pipeline.worker` is imported inside the tests rather than
at module scope for the same reason: it pulls in `activities`, and there is no
reason to make the sandbox re-execute that.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from temporalio import workflow as temporal_workflow
from temporalio.client import WorkflowHistory
from temporalio.common import VersioningBehavior
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError
from temporalio.workflow import _Definition as WorkflowDefinition

# Only the request contracts, and only for the control annotations below. The
# real Workflow classes come from `REGISTERED_WORKFLOWS` at call time, which is
# what makes the coverage rules derived rather than restated.
from dfinsta_pipeline.contracts import RunSpec
from dfinsta_pipeline.feature_gate import FeatureRunRequestV1
from dfinsta_pipeline.replay_contracts import ReplayRunRequestV1
from dfinsta_pipeline.retirement_gate import RetirementRunRequestV1
from dfinsta_pipeline.reversal_gate import ReversalRunRequestV1
from tests.history_corpus import (
    FIXTURES,
    Fixture,
    histories_directory,
    identities,
    is_closed,
    leaks,
    payload_encodings,
    searchable_payload_count,
    workflow_type_name,
)
from tests.history_search import history_search_surface


# ---------------------------------------------------------------- the controls
#
# Each is registered under a real Workflow's type name and completes immediately,
# so replaying any History recorded from the real Workflow must fail: the first
# recorded command is a scheduled Activity and the control's first command is
# `CompleteWorkflowExecution`.
#
# The argument annotations are the real request types, and they are load-bearing —
# though not for the reason this comment gave until 2026-08-08, which was wrong on
# both halves. The Replayer decodes the recorded input before the run method is
# entered, and a control annotated `object` does **not** "still work": it fails
# with `RuntimeError: … "Failed decoding arguments" … Unserializable type during
# conversion: <class 'object'>`, so `test_every_fixture_rejects_an_incompatible_workflow`
# goes red. Nor would such a failure be "indistinguishable from a nondeterminism"
# — `assertRaises(NondeterminismError)` rejects a `RuntimeError` loudly, which is
# the good outcome. The real reason to annotate precisely is simply that the
# control cannot be constructed at all otherwise. Verified by mutation.


@temporal_workflow.defn(name="PortRunWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class IncompatiblePortRunWorkflow:
    @temporal_workflow.run
    async def run(self, spec: RunSpec) -> str:
        return spec.run_id


@temporal_workflow.defn(name="ReplayRunWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class IncompatibleReplayRunWorkflow:
    @temporal_workflow.run
    async def run(self, request: ReplayRunRequestV1) -> str:
        return request.handle.run_id


@temporal_workflow.defn(
    name="FeatureAssessmentRunWorkflow", versioning_behavior=VersioningBehavior.PINNED
)
class IncompatibleFeatureAssessmentRunWorkflow:
    @temporal_workflow.run
    async def run(self, request: FeatureRunRequestV1) -> str:
        return request.run_id


@temporal_workflow.defn(
    name="HookRetirementRunWorkflow", versioning_behavior=VersioningBehavior.PINNED
)
class IncompatibleHookRetirementRunWorkflow:
    @temporal_workflow.run
    async def run(self, request: RetirementRunRequestV1) -> str:
        return request.run_id


@temporal_workflow.defn(
    name="ReversalRunWorkflow", versioning_behavior=VersioningBehavior.PINNED
)
class IncompatibleReversalRunWorkflow:
    @temporal_workflow.run
    async def run(self, request: ReversalRunRequestV1) -> str:
        return request.run_id


#: Keyed by Workflow *type name*, which is what a History records and what a
#: Replayer matches on. `test_a_control_exists_for_every_registered_workflow`
#: asserts these keys are exactly the registered names, so a sixth Workflow
#: cannot arrive with a fixture and no control.
CONTROLS = {
    "PortRunWorkflow": IncompatiblePortRunWorkflow,
    "ReplayRunWorkflow": IncompatibleReplayRunWorkflow,
    "FeatureAssessmentRunWorkflow": IncompatibleFeatureAssessmentRunWorkflow,
    "HookRetirementRunWorkflow": IncompatibleHookRetirementRunWorkflow,
    "ReversalRunWorkflow": IncompatibleReversalRunWorkflow,
}


def fixture_text(fixture: Fixture) -> str:
    return (histories_directory() / fixture.filename).read_text(encoding="utf-8")


def registered_workflows() -> dict[str, type]:
    """`{workflow type name: class}` for everything the worker hosts.

    Imported here rather than at module scope: see the module docstring on the
    sandbox re-import.
    """

    from dfinsta_pipeline import worker

    return {
        WorkflowDefinition.must_from_class(cls).name: cls for cls in worker.REGISTERED_WORKFLOWS
    }


class HistoryCorpusInventoryTests(unittest.TestCase):
    """What is on disk, what covers what, and what is missing."""

    def test_the_registry_and_the_directory_agree(self) -> None:
        """Both directions. A file with no row would never be replayed, and a row
        with no file would fail everything else with a confusing error."""

        on_disk = {path.name for path in histories_directory().glob("*.json")}
        registered = {fixture.filename for fixture in FIXTURES}
        self.assertEqual(registered, on_disk)
        # A registry that had silently emptied would satisfy the equality above.
        self.assertGreaterEqual(len(FIXTURES), len(CONTROLS))

    def test_every_fixture_is_pinned_by_its_digest(self) -> None:
        """Why a digest and not just the replay test.

        Every replay test in this file passes against a fixture regenerated from
        current code — that is exactly the self-consistency the corpus exists to
        escape. The cheap wrong fix for a compatibility failure is therefore to
        re-run `tools/capture_history_corpus.py`, which would leave the suite
        green and the guarantee gone. Moving a digest is a deliberate edit that
        shows up in review next to the Workflow change that forced it.
        """

        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.filename):
                digest = hashlib.sha256(
                    (histories_directory() / fixture.filename).read_bytes()
                ).hexdigest()
                self.assertEqual(fixture.sha256, digest)

    def test_every_registered_workflow_has_a_committed_history(self) -> None:
        """Derived from `REGISTERED_WORKFLOWS`, so a fifth Workflow fails here.

        The join is the type name recorded inside each fixture, not a table: a
        fixture cannot be filed under a Workflow it was not captured from.
        """

        covered = {workflow_type_name(fixture_text(fixture)) for fixture in FIXTURES}
        missing = sorted(set(registered_workflows()) - covered)
        self.assertEqual([], missing, f"registered Workflows with no committed History: {missing}")

    def test_no_fixture_covers_an_unregistered_workflow(self) -> None:
        """The other half: a fixture for a Workflow the worker no longer hosts is
        a replay guarantee about something nobody runs."""

        orphans = sorted(
            {workflow_type_name(fixture_text(fixture)) for fixture in FIXTURES}
            - set(registered_workflows())
        )
        self.assertEqual([], orphans)

    def test_a_control_exists_for_every_registered_workflow(self) -> None:
        self.assertEqual(set(registered_workflows()), set(CONTROLS))

    def test_every_workflow_with_a_human_gate_has_an_open_and_a_closed_history(self) -> None:
        """An update handler is how every gate in this pipeline is answered.

        So a Workflow that declares one has a state in which it is parked waiting
        for a person — the state that must survive a worker restart, and the one
        a corpus of completed runs alone says nothing about. Derived from the
        Workflow definition rather than listed, so it keeps applying to whatever
        is registered next.
        """

        states: dict[str, set[bool]] = {}
        for fixture in FIXTURES:
            text = fixture_text(fixture)
            states.setdefault(workflow_type_name(text), set()).add(is_closed(text))

        for name, cls in registered_workflows().items():
            if not WorkflowDefinition.must_from_class(cls).updates:
                continue
            with self.subTest(workflow=name):
                self.assertIn(True, states.get(name, set()), f"{name} has no closed History")
                self.assertIn(False, states.get(name, set()), f"{name} has no open History")

    def test_the_corpus_really_contains_both_kinds(self) -> None:
        """A positive control for the rule above.

        If `is_closed` ever stopped recognising a terminal event, every fixture
        would read as open and the per-Workflow rule would fail loudly — but if it
        started reading *everything* as closed it would fail quietly, because
        `updates` could plausibly be empty. This asserts the two kinds are really
        distinguished on the committed bytes.
        """

        closed = [f.filename for f in FIXTURES if is_closed(fixture_text(f))]
        opened = [f.filename for f in FIXTURES if not is_closed(fixture_text(f))]
        self.assertTrue(closed)
        self.assertTrue(opened)
        self.assertEqual(len(FIXTURES), len(closed) + len(opened))


class HistoryCorpusSanitationTests(unittest.TestCase):
    """A fixture that names one machine cannot be replayed on another."""

    def test_no_fixture_carries_an_environment_specific_value(self) -> None:
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.filename):
                self.assertEqual([], leaks(fixture_text(fixture)))

    def test_the_search_surface_is_not_empty(self) -> None:
        """The positive control the absence assertion above needs.

        `to_json()` base64-encodes payload bodies, so a scan that silently stopped
        decoding them would report a clean corpus for ever. Each fixture is
        checked to contain payload bodies at all, and to contain its own run id
        *inside* one of them while not containing it in the raw JSON text.
        """

        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.filename):
                text = fixture_text(fixture)
                self.assertGreater(searchable_payload_count(text), 0)
                self.assertIn(fixture.workflow_id, history_search_surface(text))
                self.assertNotIn(fixture.workflow_id, text)

    def test_the_leak_scan_finds_a_planted_leak(self) -> None:
        """Proof that the scan reads inside payloads, not just the JSON text.

        A search that cannot succeed always passes. So a real fixture is poisoned
        two ways — a path pushed into a payload body, where it would really
        travel, and a `pid@hostname` identity written into the JSON — and both
        must be found. Without this, `leaks() == []` is an untested absence.
        """

        clean = fixture_text(FIXTURES[0])
        self.assertEqual([], leaks(clean))

        started = json.loads(clean)["events"][0]["workflowExecutionStartedEventAttributes"]
        payloads = started["input"]["payloads"]
        body = base64.b64decode(payloads[0]["data"]).decode("utf-8")
        payloads[0]["data"] = base64.b64encode(
            (body + '{"workspace": "/tmp/tmpab12cd/work"}').encode("utf-8")
        ).decode("ascii")
        document = json.loads(clean)
        document["events"][0]["workflowExecutionStartedEventAttributes"]["input"][
            "payloads"
        ] = payloads
        poisoned = json.dumps(document)
        self.assertNotIn("/tmp/", poisoned)  # the leak is only visible once decoded
        self.assertEqual(["/tmp/", "tmpab12cd"], sorted(set(leaks(poisoned))))

        # And the other shape: an identity the capture tool failed to rewrite.
        machine = json.loads(clean)
        machine["events"][0]["workflowExecutionStartedEventAttributes"]["identity"] = (
            "1292223@thinkpad"
        )
        self.assertEqual(["1292223@thinkpad"], leaks(json.dumps(machine)))

    def test_every_payload_is_readable_text(self) -> None:
        """Otherwise the scan above is searching only part of the fixture.

        `binary/null` carries no body at all; anything else that is not
        `json/plain` is a payload the decoder cannot see into, and the absence
        assertions would be quietly narrower than they claim.
        """

        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.filename):
                self.assertLessEqual(
                    payload_encodings(fixture_text(fixture)), {"json/plain", "binary/null"}
                )

    def test_every_fixture_records_a_machine_neutral_identity(self) -> None:
        """The positive half of "no `pid@hostname` anywhere".

        A History that recorded no identity at all would satisfy the absence
        assertion just as well, so this asserts identities are present and that
        each is a plain token. Not asserted against `CAPTURE_IDENTITY`: the Phase
        A fixture predates this tool and carries `phase-a-history-capture`, and
        pinning one spelling would say nothing extra about the machine.
        """

        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.filename):
                recorded = identities(fixture_text(fixture))
                self.assertTrue(recorded, "no identity was recorded at all")
                for identity in recorded:
                    self.assertNotIn("@", identity)


class HistoryCorpusReplayTests(unittest.IsolatedAsyncioTestCase):
    """The guarantee itself, and the control that proves it can fail."""

    async def test_every_fixture_replays_against_its_current_workflow(self) -> None:
        workflows = registered_workflows()
        replayed = 0
        for fixture in FIXTURES:
            text = fixture_text(fixture)
            with self.subTest(fixture=fixture.filename):
                await Replayer(
                    workflows=[workflows[workflow_type_name(text)]]
                ).replay_workflow(WorkflowHistory.from_json(fixture.workflow_id, text))
                replayed += 1
        # `subTest` failures do not stop the loop, and a loop over an empty
        # registry would pass silently.
        self.assertEqual(len(FIXTURES), replayed)

    async def test_every_fixture_rejects_an_incompatible_workflow(self) -> None:
        rejected = 0
        for fixture in FIXTURES:
            text = fixture_text(fixture)
            with self.subTest(fixture=fixture.filename):
                with self.assertRaises(NondeterminismError):
                    await Replayer(
                        workflows=[CONTROLS[workflow_type_name(text)]]
                    ).replay_workflow(WorkflowHistory.from_json(fixture.workflow_id, text))
                rejected += 1
        self.assertEqual(len(FIXTURES), rejected)


if __name__ == "__main__":
    unittest.main()
