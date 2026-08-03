import inspect
import unittest
from pathlib import Path

from temporalio import activity
from temporalio.workflow import _Definition as WorkflowDefinition

from dfinsta_pipeline import worker
from dfinsta_pipeline.replay_contracts import (
    AdmittedReplayHandleV1,
    ReplayVerificationAdmissionV1,
    ReplayVerificationGrantHandleV1,
)

# The proven checkpoint Activities. These must never be registered directly:
# each takes a full AdmittedReplayV3, which embeds the intent, resolution and
# source manifest by value. Registering one would put the whole port recipe and
# every source path into Temporal History on every stage.
CHECKPOINT_ACTIVITIES = (
    "replay_install_frameworks_checkpoint_activity",
    "replay_decode_checkpoint_activity",
    "replay_apply_tree_checkpoint_activity",
    "replay_build_patched_apk_checkpoint_activity",
    "replay_verify_final_apk_checkpoint_activity",
)

# The registered wrappers, in the order a run executes them.
STAGE_WRAPPERS = (
    "replay_install_frameworks_stage_activity",
    "replay_decode_stage_activity",
    "replay_apply_tree_stage_activity",
    "replay_build_patched_apk_stage_activity",
    "replay_verify_final_apk_stage_activity",
)

# Registering an Activity is a decision, not an import side effect: this set is
# the record of which decisions were made. The two feature-gate Activities joined
# when `FeatureAssessmentRunWorkflow` did — the gate had been complete and
# unraisable until something could raise it.
EXPECTED_REGISTERED = {
    "admit_activity",
    "prepare_activity",
    "record_decision_activity",
    "apply_activity",
    "prepare_replay_plan_activity",
    "prepare_replay_verification_gate_activity",
    "admit_replay_verification_grant_activity",
    "prepare_feature_gate_activity",
    "admit_feature_dispositions_activity",
    *STAGE_WRAPPERS,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class WorkerRegistrationTests(unittest.TestCase):
    def test_worker_registers_exactly_the_reviewed_activities(self) -> None:
        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined]
            for fn in worker.REGISTERED_ACTIVITIES
        }
        self.assertEqual(registered, EXPECTED_REGISTERED)
        self.assertEqual(len(worker.REGISTERED_ACTIVITIES), len(EXPECTED_REGISTERED))

    def test_no_proven_checkpoint_activity_is_registered(self) -> None:
        registered = {
            activity._Definition.from_callable(fn).name  # type: ignore[attr-defined]
            for fn in worker.REGISTERED_ACTIVITIES
        }
        for name in CHECKPOINT_ACTIVITIES:
            self.assertNotIn(name, registered)

    def test_registered_workflows_are_exact_and_pinned(self) -> None:
        names = {WorkflowDefinition.from_class(cls).name for cls in worker.REGISTERED_WORKFLOWS}
        self.assertEqual(
            names,
            {"PortRunWorkflow", "ReplayRunWorkflow", "FeatureAssessmentRunWorkflow"},
        )
        for cls in worker.REGISTERED_WORKFLOWS:
            definition = WorkflowDefinition.from_class(cls)
            self.assertIsNotNone(
                definition.versioning_behavior,
                "a worker with use_worker_versioning refuses an unpinned workflow",
            )

    def test_worker_sets_a_non_zero_graceful_shutdown_timeout(self) -> None:
        """Temporal defaults this to zero, which cancels Activities immediately.

        Cancellation quarantines a replay operation and quarantine is terminal,
        so a zero default turns an ordinary worker stop into a destroyed run.
        """
        self.assertGreater(worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS, 0)
        signature = inspect.signature(worker.run_worker)
        self.assertEqual(
            signature.parameters["graceful_shutdown_seconds"].default,
            worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
        )
        source = inspect.getsource(worker.run_worker)
        self.assertIn("graceful_shutdown_timeout", source)

    def test_stage_wrappers_take_handles_not_admitted_authority(self) -> None:
        """The whole point of the wrappers: small, hash-pinned arguments."""
        from dfinsta_pipeline import activities

        expected = {
            "replay_install_frameworks_stage_activity": AdmittedReplayHandleV1,
            "replay_decode_stage_activity": AdmittedReplayHandleV1,
            "replay_apply_tree_stage_activity": AdmittedReplayHandleV1,
            "replay_build_patched_apk_stage_activity": AdmittedReplayHandleV1,
            "replay_verify_final_apk_stage_activity": ReplayVerificationGrantHandleV1,
            "prepare_replay_plan_activity": AdmittedReplayHandleV1,
            "prepare_replay_verification_gate_activity": AdmittedReplayHandleV1,
            "admit_replay_verification_grant_activity": ReplayVerificationAdmissionV1,
        }
        for name, annotation in expected.items():
            hints = inspect.get_annotations(getattr(activities, name), eval_str=True)
            parameters = [key for key in hints if key != "return"]
            self.assertEqual(len(parameters), 1, name)
            self.assertIs(hints[parameters[0]], annotation, name)

    def test_replay_workflow_module_has_no_target_conditional(self) -> None:
        """Workflow code must not branch on a port target.

        Stage membership comes from ReplayExecutionPlanV1, which an Activity
        derives from admitted authority.
        """
        source = (_repo_root() / "src/dfinsta_pipeline/replay_workflow.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("340", "430", ".frameworks", ".apk"):
            self.assertNotIn(forbidden, source)

    def test_phase_a_workflow_is_not_referenced_by_the_replay_chain(self) -> None:
        """Registration had to stay additive; PortRunWorkflow keeps its own file."""
        source = (_repo_root() / "src/dfinsta_pipeline/workflow.py").read_text(encoding="utf-8")
        for name in (*CHECKPOINT_ACTIVITIES, *STAGE_WRAPPERS, "ReplayRunWorkflow"):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
