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
    # Joined when the single-shot verification grant got an exit: a re-driven run
    # must read the answer its gate already has rather than ask a question that
    # can no longer be answered.
    "resolve_replay_verification_grant_activity",
    "prepare_replay_verification_gate_activity",
    "admit_replay_verification_grant_activity",
    "prepare_feature_gate_activity",
    "admit_feature_dispositions_activity",
    # And the two retirement-gate Activities, when `HookRetirementRunWorkflow`
    # did. Same reason a second time: `dfinsta_pipeline.retirement` could build a
    # case and take a ruling at a command line, and nothing could *wait* — a
    # decision that takes days needs something that survives a worker restart.
    "prepare_retirement_gate_activity",
    "admit_retirement_rulings_activity",
    *STAGE_WRAPPERS,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


class _Stop(Exception):
    """Ends `run_worker` at a chosen point without starting a real worker."""


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
            {
                "PortRunWorkflow",
                "ReplayRunWorkflow",
                "FeatureAssessmentRunWorkflow",
                "HookRetirementRunWorkflow",
            },
        )
        for cls in worker.REGISTERED_WORKFLOWS:
            definition = WorkflowDefinition.from_class(cls)
            self.assertIsNotNone(
                definition.versioning_behavior,
                "a worker with use_worker_versioning refuses an unpinned workflow",
            )

    def test_the_graceful_shutdown_window_outlasts_every_stage_budget(self) -> None:
        """Greater than zero is not the property that matters; longer than the
        longest stage is.

        Cancellation quarantines a replay operation and quarantine is terminal.
        A replay stage can only act on a cancellation delivered through a
        heartbeat response or a local `WORKER_SHUTDOWN` after this window — and
        no replay stage heartbeats — so a window longer than any stage means the
        destructive cancellation cannot arrive mid-stage at all.

        Derived from the budgets rather than restated, so lengthening a stage
        cannot silently leave the window short. Deriving it is the whole test: a
        `> 0` assertion passed happily at 300 seconds against a 10,800-second
        stage.
        """
        from dfinsta_pipeline import activities

        # The plan timeouts the real harness pins: decode 600, build 600,
        # install_framework 300. Spelled out rather than imported, so a change
        # to either side has to be noticed here.
        plan_timeouts = {"install_framework": 300, "decode": 600, "build": 600}
        longest = max(
            plan_timeouts[activities._STAGE_BUDGET_ROLE[stage]] * multiplier
            for stage, multiplier in activities._STAGE_BUDGET_MULTIPLIER.items()
        )
        self.assertEqual(longest, 10_800)
        self.assertGreaterEqual(worker.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS, longest)
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


class WorkerRuntimeBindingTests(unittest.TestCase):
    """What the worker must give the stages, beyond a state root.

    Found by running the documented CLI rather than reading it: the worker hosted
    every registered Activity and could not execute a single real replay stage,
    because `configure_runtime(state_root)` left `source_root` unset and
    `executor_paths` empty. `replay_apply_tree` and `replay_verify_final_apk`
    refuse without the first; every stage that launches a subprocess resolves its
    executable through the second. Registration was complete and disconnected
    from the inputs the thing it registered needs.
    """

    def test_the_stages_that_need_a_runtime_binding_can_get_one_from_the_cli(self) -> None:
        """Bind the requirement to the refusal, not to a list I typed.

        The two requirements are read out of `activities.py` itself, so a stage
        that starts needing a source root, or one that stops, changes this test's
        input rather than silently passing.
        """
        source = (_repo_root() / "src/dfinsta_pipeline/activities.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("configured.source_root is None", source)
        self.assertIn("configured.executor_paths[capability.executable_sha256]", source)

        signature = inspect.signature(worker.run_worker)
        for name in ("source_root", "executor_paths", "attempts_root"):
            self.assertIn(name, signature.parameters, name)

    def test_run_worker_configures_the_runtime_exactly_once_before_connecting(self) -> None:
        """Order and arguments, both recorded rather than inferred.

        An earlier version proved "before" by making `Client.connect` raise, which
        cannot see anything that happens *after* connect -- so a correct bind
        followed by a bare `configure_runtime(state_root)` one line later passed
        every test in the suite while reproducing the original bug exactly. The
        call log fixes that: a second bind shows up as a second entry.

        `assert_called_once_with` rather than a substring search of the source,
        because `assertIn("executor_paths=executor_paths", body)` is satisfied by
        any superstring -- including `executor_paths if source_root else None`.
        """
        import asyncio
        from unittest import mock

        calls: list[tuple[str, tuple, dict]] = []

        def record_configure(*args, **kwargs):
            calls.append(("configure", args, kwargs))

        async def record_connect(*args, **kwargs):
            calls.append(("connect", args, kwargs))
            return object()

        def stop(*args, **kwargs):
            calls.append(("worker", args, kwargs))
            raise _Stop

        root = Path("/srv")
        with mock.patch.object(worker, "configure_runtime", record_configure):
            with mock.patch("dfinsta_pipeline.worker.Client.connect", record_connect):
                with mock.patch.object(worker, "Worker", stop):
                    with self.assertRaises(_Stop):
                        asyncio.run(
                            worker.run_worker(
                                "localhost:7233",
                                "queue",
                                root / "state",
                                "deployment",
                                "build",
                                source_root=root / "source",
                                executor_paths={"b" * 64: root / "java"},
                                attempts_root=root / "attempts",
                            )
                        )
        self.assertEqual([name for name, *_ in calls], ["configure", "connect", "worker"])
        _, args, kwargs = calls[0]
        self.assertEqual(args, (root / "state",))
        self.assertEqual(
            kwargs,
            {
                "attempts_root": root / "attempts",
                "source_root": root / "source",
                "executor_paths": {"b" * 64: root / "java"},
            },
        )

        # Each argument independent of the others. Supplying all three at once
        # cannot see a forwarding guarded on one of them -- and
        # `executor_paths if source_root else None` was exactly that mutation,
        # which survived the assertion above.
        calls.clear()
        with mock.patch.object(worker, "configure_runtime", record_configure):
            with mock.patch("dfinsta_pipeline.worker.Client.connect", record_connect):
                with mock.patch.object(worker, "Worker", stop):
                    with self.assertRaises(_Stop):
                        asyncio.run(
                            worker.run_worker(
                                "localhost:7233",
                                "queue",
                                root / "state",
                                "deployment",
                                "build",
                                executor_paths={"b" * 64: root / "java"},
                            )
                        )
        self.assertEqual(
            calls[0][2],
            {
                "attempts_root": None,
                "source_root": None,
                "executor_paths": {"b" * 64: root / "java"},
            },
        )

    def test_the_worker_ledger_is_writable(self) -> None:
        """A read-only bind would host every Activity and refuse every write.

        Unasserted until a mutation exposed it: `configure_runtime(..., read_only=True)`
        passed every other check here, and only failed at all because the fixture's
        state root had no ledger yet. Against a real deployment it would not fail
        until the first stage tried to claim an operation.
        """
        import asyncio
        import tempfile
        from unittest import mock

        from dfinsta_pipeline import activities

        async def refuse(*_args, **_kwargs):
            raise _Stop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "java"
            executable.write_bytes(b"")
            digest = "b" * 64
            with mock.patch("dfinsta_pipeline.worker.Client.connect", refuse):
                with self.assertRaises(_Stop):
                    asyncio.run(
                        worker.run_worker(
                            "localhost:7233",
                            "queue",
                            root / "state",
                            "deployment",
                            "build",
                            source_root=root / "source",
                            executor_paths={digest: executable},
                            attempts_root=root / "attempts",
                        )
                    )
            configured = activities.runtime()
            self.assertFalse(configured.ledger.read_only)
            self.assertEqual(configured.source_root, (root / "source").resolve())
            self.assertEqual(configured.executor_paths[digest], executable.resolve())
            self.assertEqual(configured.attempts_root, (root / "attempts").resolve())

    def test_executor_path_argument_parsing(self) -> None:
        import argparse

        digest = "a" * 64
        self.assertEqual(
            worker.parse_executor_path(f"{digest}=/usr/bin/java"),
            (digest, Path("/usr/bin/java")),
        )
        # A path may contain '=', a SHA-256 may not, so the split is on the first.
        self.assertEqual(
            worker.parse_executor_path(f"{digest}=/opt/a=b/java"),
            (digest, Path("/opt/a=b/java")),
        )
        self.assertEqual(
            worker.parse_executor_path(f"{digest.upper()}=/usr/bin/java")[0], digest
        )
        for bad in ("", "=/usr/bin/java", f"{digest}=", "notahash=/usr/bin/java", digest):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                worker.parse_executor_path(bad)
        with self.assertRaises(argparse.ArgumentTypeError):
            worker.parse_executor_path(None)  # type: ignore[arg-type]

    def test_main_refuses_one_digest_named_twice_with_different_paths(self) -> None:
        """Last-one-wins would run a binary the operator did not name."""
        import asyncio
        from unittest import mock

        digest = "c" * 64

        def close(coroutine):
            coroutine.close()

        base = [
            "--build-id",
            "b",
            "--executor-path",
            f"{digest}=/one",
            "--executor-path",
            f"{digest}=/two",
        ]
        # argparse writes usage to stderr; captured so a passing test is silent
        # and so the refusal itself can be asserted rather than merely observed.
        import contextlib
        import io

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with mock.patch.object(asyncio, "run", close):
                with mock.patch("sys.argv", ["worker", *base]):
                    with self.assertRaises(SystemExit) as raised:
                        worker.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("twice with different paths", stderr.getvalue())
        self.assertIn(digest, stderr.getvalue())

    def test_main_forwards_every_argument_it_parses(self) -> None:
        """Positional arguments too, which nothing checked.

        The first version of this recorded `**kwargs` alone, so `main` could send
        the wrong state root, swap the endpoint and the task queue, or drop
        `--attempts-root`, and every test still passed. All three were reachable
        by a one-line edit.
        """
        import asyncio
        from unittest import mock

        digest = "c" * 64
        recorded: list[tuple[tuple, dict]] = []

        def capture(*args, **kwargs):
            recorded.append((args, kwargs))
            return None

        argv = [
            "worker",
            "--endpoint",
            "temporal.internal:7233",
            "--task-queue",
            "dfinsta-real",
            "--state-root",
            "/srv/pipeline-state",
            "--deployment-name",
            "dfinsta-pipeline",
            "--build-id",
            "b",
            "--graceful-shutdown-seconds",
            "12345",
            "--source-root",
            "/srv/source",
            "--attempts-root",
            "/srv/attempts",
            # The same path twice is not a conflict, unlike the test above.
            "--executor-path",
            f"{digest}=/one",
            "--executor-path",
            f"{digest}=/one",
        ]
        with mock.patch.object(worker, "run_worker", capture):
            with mock.patch.object(asyncio, "run", lambda value: value):
                with mock.patch("sys.argv", argv):
                    worker.main()
        args, kwargs = recorded[-1]
        self.assertEqual(
            args,
            (
                "temporal.internal:7233",
                "dfinsta-real",
                Path("/srv/pipeline-state"),
                "dfinsta-pipeline",
                "b",
                12345,
            ),
        )
        self.assertEqual(
            kwargs,
            {
                "source_root": Path("/srv/source"),
                "executor_paths": {digest: Path("/one")},
                "attempts_root": Path("/srv/attempts"),
            },
        )


if __name__ == "__main__":
    unittest.main()
