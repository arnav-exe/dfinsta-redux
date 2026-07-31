"""Replay the STORED Phase A History corpus against the current Workflow.

``test_phase_a_temporal.test_history_is_compact_private_and_replay_safe``
generates the History it replays in the same process, so it can only ever
confirm that ``PortRunWorkflow`` agrees with itself. The fixture replayed here
was captured once from a completed Phase A run and committed, so it keeps
failing until a Workflow change is made replay compatible with the shape that
was already durably recorded.
"""

import hashlib
import unittest
from pathlib import Path

from temporalio import workflow as temporal_workflow
from temporalio.client import WorkflowHistory
from temporalio.common import VersioningBehavior
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from dfinsta_pipeline.contracts import RunSpec
from dfinsta_pipeline.workflow import PortRunWorkflow
from tests.history_search import decoded_payload_count, history_search_surface


try:
    from test_phase_a_temporal import IncompatiblePortRunWorkflow
except ImportError:  # pragma: no cover - only when tests/ is not importable

    @temporal_workflow.defn(name="PortRunWorkflow", versioning_behavior=VersioningBehavior.PINNED)
    class IncompatiblePortRunWorkflow:  # type: ignore[no-redef]
        @temporal_workflow.run
        async def run(self, spec: RunSpec) -> str:
            return spec.run_id


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "histories" / "phase_a_completed_v1.json"
WORKFLOW_MODULE_PATH = REPOSITORY_ROOT / "src" / "dfinsta_pipeline" / "workflow.py"

# Workflow id of the captured run; WorkflowHistory.from_json needs it because
# the exported History payload does not carry the execution identifiers.
FIXTURE_WORKFLOW_ID = "run-history"

# Identity of the stored corpus. A change here means the baseline itself moved,
# which is only legitimate when a new fixture is deliberately captured.
FIXTURE_SHA256 = "aab03cb8104e5ef5351d26afb2ab03d4583650ce6088b08ca89fd3f2f8adcb40"

# Identity of the Phase A Workflow definition the stored History was captured
# from. This pin exists to enforce that replay-chain registration stays
# ADDITIVE: new stages, activities or Workflow classes may be registered
# alongside PortRunWorkflow, but the recorded Phase A command sequence must not
# be reordered, removed or retyped. Changing this constant requires
# re-reviewing Phase A History compatibility -- re-run the replay tests above
# against the UNCHANGED fixture first, and only then update this hash.
WORKFLOW_MODULE_SHA256 = "d93157150fe8ba4bfeea284fccf9cfdc5627488f82fdf99976ad2068b3e6f0ce"


def saved_history() -> WorkflowHistory:
    return WorkflowHistory.from_json(
        FIXTURE_WORKFLOW_ID, FIXTURE_PATH.read_text(encoding="utf-8")
    )


class PhaseAHistoryCorpusTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_phase_a_history_replays_against_current_workflow(self) -> None:
        history = saved_history()
        await Replayer(workflows=[PortRunWorkflow]).replay_workflow(history)

    async def test_saved_phase_a_history_rejects_incompatible_workflow(self) -> None:
        history = saved_history()
        with self.assertRaises(NondeterminismError):
            await Replayer(workflows=[IncompatiblePortRunWorkflow]).replay_workflow(history)

    def test_saved_phase_a_history_contains_no_private_paths(self) -> None:
        """Searches decoded payload bodies, not just the JSON text.

        to_json() base64-encodes payloads. Asserting against the raw text alone
        cannot fail for a secret carried inside a payload, which is where one
        would be. The positive control below proves the payloads really were
        decoded, so absence is evidence rather than an empty search.
        """
        text = FIXTURE_PATH.read_text(encoding="utf-8")
        surface = history_search_surface(text)
        self.assertGreater(decoded_payload_count(text), 0)
        self.assertIn("run-history", surface)  # positive control: the run id
        self.assertNotIn("run-history", text)  # and it is base64-hidden in the text
        for forbidden in ("/home/", "/tmp/", "PRIVATE_KEY", "PASSWORD", "SECRET", "arnav"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, surface)

    def test_saved_phase_a_history_fixture_identity_is_pinned(self) -> None:
        self.assertEqual(
            hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(), FIXTURE_SHA256
        )

    def test_phase_a_workflow_module_is_unchanged(self) -> None:
        # See WORKFLOW_MODULE_SHA256: replay-chain registration must stay
        # additive, and moving this pin requires re-reviewing Phase A History
        # compatibility against the stored corpus.
        self.assertEqual(
            hashlib.sha256(WORKFLOW_MODULE_PATH.read_bytes()).hexdigest(),
            WORKFLOW_MODULE_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
