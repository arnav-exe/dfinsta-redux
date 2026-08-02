"""The trusted submission client against a real Workflow and a real gate.

Everything here runs through a *separate* `Client.connect`, never the client the
worker was built from, because that is the deployment the plan requires: a
trusted client is its own authenticated OS process, and a test that reuses the
worker's connection proves nothing about it. The Workflow's Activities are
stubs registered under their real names, so what is under test is the client and
the gate, not apktool.

The three properties that matter, and why each is here rather than assumed:

* **A subject the client cannot reproduce is never signed.** `test_refuses_...`
  drives a real pending gate whose published hash disagrees with the derived one
  and asserts both that the client refuses and that the Workflow is still
  waiting afterwards -- a refusal that had already submitted something would be
  worse than no refusal at all.
* **A resubmission is the same decision, not a second one.** The journal exists
  so `issued_at` survives a process restart; the test passes a deliberately
  different `issued_at` on the retry and asserts it is ignored.
* **The decision the Workflow acts on is the one the client assembled**, down to
  the derived identifiers.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from temporalio import activity
from temporalio.client import Client
from temporalio.common import PinnedVersioningOverride, VersioningBehavior, WorkerDeploymentVersion
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, WorkerDeploymentConfig

from dfinsta_pipeline import submission
from dfinsta_pipeline.contracts import ArtifactRef, GateRequest
from dfinsta_pipeline.replay_contracts import (
    REPLAY_STAGES_WITHOUT_FRAMEWORK,
    AdmittedReplayHandleV1,
    ReplayExecutionPlanV1,
    ReplayRunRequestV1,
    ReplayVerificationAdmissionV1,
    ReplayVerificationGateV1,
    ReplayVerificationGrantHandleV1,
)
from dfinsta_pipeline.replay_workflow import ReplayRunWorkflow

TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-pipeline-tests", "submission-v1")

RUN_ID = "submission-run-1"
ADMITTED_REPLAY_SHA256 = "a" * 64
#: The real derived shape, not the placeholder the workflow tests use: this
#: client selects its resolver by gate id, so the id has to be the one
#: `replay_gate.derived_identifier` actually mints.
GATE_ID = f"{RUN_ID}-final-verification-gate"
SUBJECT_SHA256 = "b" * 64
OTHER_SUBJECT_SHA256 = "d" * 64
ACTOR = "operator"
POLICY_REVISION = "policy-1"
GATE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
STAGE_BUDGET_SECONDS = 3600


def artifact(kind: str, digest: str, operation_id: str) -> ArtifactRef:
    return ArtifactRef(1, kind, digest, 64, f"cas://sha256/{digest}", operation_id, ())


def derived(subject: str = SUBJECT_SHA256, *, actor: str = ACTOR) -> submission.DerivedSubject:
    return submission.DerivedSubject(
        run_id=RUN_ID,
        gate_id=GATE_ID,
        subject_sha256=subject,
        admission_sha256=subject,
        prepared_sha256=subject,
        policy_revision=POLICY_REVISION,
        allowed_actor=actor,
    )


def gate_kind(subject: str = SUBJECT_SHA256, *, actor: str = ACTOR) -> submission.GateKind:
    """A gate kind whose resolver stands in for the ledger derivation.

    The real resolver is exercised in `test_submission_resolver`, against real
    recorded state. What this one substitutes is only *where the derived subject
    came from*: everything the client does with it -- the comparison, the
    assembly, the journal, the update -- is the production path.
    """

    return submission.GateKind(
        name="test-replay-verification",
        update_name="submit_verification_decision",
        matches=lambda gate_id, run_id: gate_id == f"{run_id}-final-verification-gate",
        resolve=lambda run_id: derived(subject, actor=actor),
    )


class Stubs:
    """Activities registered under their real names, so the Workflow is real."""

    def __init__(self, *, published_subject: str = SUBJECT_SHA256) -> None:
        self.published_subject = published_subject
        self.admissions: list[ReplayVerificationAdmissionV1] = []
        self.verify_released = asyncio.Event()
        self.verify_released.set()
        recorder = self

        @activity.defn(name="prepare_replay_plan_activity")
        async def plan_stub(handle: AdmittedReplayHandleV1) -> ReplayExecutionPlanV1:
            return ReplayExecutionPlanV1(
                1,
                handle.run_id,
                handle.admitted_replay_sha256,
                REPLAY_STAGES_WITHOUT_FRAMEWORK,
                tuple(STAGE_BUDGET_SECONDS for _ in REPLAY_STAGES_WITHOUT_FRAMEWORK),
            )

        @activity.defn(name="prepare_replay_verification_gate_activity")
        async def gate_stub(handle: AdmittedReplayHandleV1) -> ReplayVerificationGateV1:
            return ReplayVerificationGateV1(
                1,
                handle.run_id,
                GATE_ID,
                recorder.published_subject,
                ACTOR,
                POLICY_REVISION,
            )

        @activity.defn(name="admit_replay_verification_grant_activity")
        async def grant_stub(
            admission: ReplayVerificationAdmissionV1,
        ) -> ReplayVerificationGrantHandleV1:
            recorder.admissions.append(admission)
            return ReplayVerificationGrantHandleV1(1, "grant-1", "c" * 64)

        @activity.defn(name="replay_verify_final_apk_stage_activity")
        async def verify_stub(handle: ReplayVerificationGrantHandleV1) -> ArtifactRef:
            await recorder.verify_released.wait()
            return artifact("replay-final-apk-verification-receipt-v1", "f" * 64, "op-verify")

        def stage_stub(name: str):
            @activity.defn(name=name)
            async def stub(handle: AdmittedReplayHandleV1) -> ArtifactRef:
                return artifact("replay-stage-receipt-v1", "e" * 64, "op-stage")

            return stub

        self.activities = [
            plan_stub,
            gate_stub,
            grant_stub,
            verify_stub,
            *(
                stage_stub(name)
                for name in (
                    "replay_install_frameworks_stage_activity",
                    "replay_decode_stage_activity",
                    "replay_apply_tree_stage_activity",
                    "replay_build_patched_apk_stage_activity",
                )
            ),
        ]


class SubmissionClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.task_queue = "submission-tests"
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.journal = self.root / "submissions"
        self.principal = submission.Principal(1, 0, ACTOR)

    async def asyncTearDown(self) -> None:
        await self.environment.shutdown()
        self.directory.cleanup()

    def worker(self, stubs: Stubs) -> Worker:
        return Worker(
            self.environment.client,
            task_queue=self.task_queue,
            workflows=[ReplayRunWorkflow],
            activities=stubs.activities,
            max_cached_workflows=0,
            deployment_config=WorkerDeploymentConfig(
                version=TEST_DEPLOYMENT_VERSION,
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
            ),
        )

    async def start(self):
        return await self.environment.client.start_workflow(
            ReplayRunWorkflow.run,
            ReplayRunRequestV1(
                1, AdmittedReplayHandleV1(1, RUN_ID, ADMITTED_REPLAY_SHA256), GATE_TIMEOUT_SECONDS
            ),
            id=RUN_ID,
            task_queue=self.task_queue,
            versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
        )

    async def separate_client(self) -> Client:
        """A connection the worker never saw, as the deployed client would be."""

        return await Client.connect(
            self.environment.client.service_client.config.target_host,
            identity=f"dfinsta-submission:{ACTOR}",
        )

    async def wait_for_gate(self, handle) -> GateRequest:
        for _ in range(500):
            status = await handle.query(ReplayRunWorkflow.status)
            if status.state == "awaiting-verification-approval" and status.gate is not None:
                return status.gate
            await self.environment.sleep(0.01)
        raise AssertionError("gate never opened")

    async def wait_for_admission(self, stubs) -> None:
        """The update returns when the handler runs; the grant Activity is later."""

        for _ in range(500):
            if stubs.admissions:
                return
            await self.environment.sleep(0.01)
        raise AssertionError("the grant activity never ran")

    async def workflow_state(self, handle) -> str:
        status = await handle.query(ReplayRunWorkflow.status)
        return status.state

    # ------------------------------------------------------------------ tests

    async def test_client_answers_a_real_gate_end_to_end(self) -> None:
        stubs = Stubs()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)

                client = await self.separate_client()
                now = datetime.now(timezone.utc)
                pending = await submission.read_pending_gate(
                    client, RUN_ID, now=now, kinds=(gate_kind(),)
                )
                self.assertEqual(pending.derived.subject_sha256, SUBJECT_SHA256)
                self.assertEqual(pending.published.gate_id, GATE_ID)

                submission.check_confirmation(pending, SUBJECT_SHA256[:12])
                outcome = await submission.submit_answer(
                    client,
                    pending,
                    self.principal,
                    submission.Answer("approve", "the receipt binds the build I inspected"),
                    journal_root=self.journal,
                    issued_at=now,
                )
                self.assertTrue(outcome.accepted)
                self.assertFalse(outcome.resubmitted)
                self.assertTrue(outcome.decision_id.startswith(submission.DECISION_ID_PREFIX))

                result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.verification_decision_id, outcome.decision_id)
        # The Workflow acted on the decision the client assembled, not on one it
        # made up: the admitted grant carries it by value.
        self.assertEqual(len(stubs.admissions), 1)
        submitted = stubs.admissions[0].decision
        self.assertEqual(submitted.decision_id, outcome.decision_id)
        self.assertEqual(submitted.actor, ACTOR)
        self.assertEqual(submitted.subject_sha256, SUBJECT_SHA256)
        self.assertEqual(submitted.rationale, "the receipt binds the build I inspected")
        # Derived, not chosen: the ids are a function of the decision's content.
        expected_ids = submission.decision_identity(
            {
                "actor": ACTOR,
                "run_id": RUN_ID,
                "gate_id": GATE_ID,
                "subject_sha256": SUBJECT_SHA256,
                "admission_sha256": SUBJECT_SHA256,
                "prepared_sha256": SUBJECT_SHA256,
                "policy_revision": POLICY_REVISION,
                "decision": "approve",
                "rationale": "the receipt binds the build I inspected",
                "issued_at": submitted.issued_at,
            }
        )
        self.assertEqual((submitted.decision_id, submitted.idempotency_id), expected_ids)

    async def test_refuses_a_subject_it_cannot_reproduce_and_submits_nothing(self) -> None:
        """The central property, driven against a live gate.

        A refusal that had already submitted something would be worse than no
        refusal, so the Workflow's state after the refusal is asserted too.
        """

        stubs = Stubs(published_subject=OTHER_SUBJECT_SHA256)
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)
                client = await self.separate_client()

                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.read_pending_gate(
                        client, RUN_ID, now=datetime.now(timezone.utc), kinds=(gate_kind(),)
                    )
                self.assertIn("refusing to sign an unverified subject", str(raised.exception))
                self.assertIn(OTHER_SUBJECT_SHA256, str(raised.exception))
                self.assertIn(SUBJECT_SHA256, str(raised.exception))

                self.assertEqual(
                    await self.workflow_state(handle), "awaiting-verification-approval"
                )
                self.assertEqual(stubs.admissions, [])
                self.assertFalse(self.journal.exists())
                await handle.cancel()
                with self.assertRaises(Exception):
                    await handle.result()

    async def test_unregistered_gate_is_refused_rather_than_trusted(self) -> None:
        stubs = Stubs()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)
                client = await self.separate_client()

                # The shipped registry holds only the real replay resolver, and
                # this gate id is not the shape it mints for a different run.
                never_matches = submission.GateKind(
                    name="other",
                    update_name="submit_verification_decision",
                    matches=lambda gate_id, run_id: False,
                    resolve=lambda run_id: derived(),
                )
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.read_pending_gate(
                        client,
                        RUN_ID,
                        now=datetime.now(timezone.utc),
                        kinds=(never_matches,),
                    )
                self.assertIn("No resolver is registered", str(raised.exception))
                self.assertIn("cannot independently reproduce", str(raised.exception))
                self.assertEqual(stubs.admissions, [])
                await handle.cancel()
                with self.assertRaises(Exception):
                    await handle.result()

    async def test_resubmission_after_a_client_restart_is_the_same_decision(self) -> None:
        """The journal is what makes a retry idempotent rather than merely similar.

        The retry is given a different `issued_at`, which must be ignored: the
        decision that was already submitted is the one recorded, and
        re-timestamping it would mint a new decision the Workflow has to refuse
        — leaving a human unable to tell a dropped connection from a rejection.
        """

        stubs = Stubs()
        stubs.verify_released.clear()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)

                first_client = await self.separate_client()
                now = datetime.now(timezone.utc)
                pending = await submission.read_pending_gate(
                    first_client, RUN_ID, now=now, kinds=(gate_kind(),)
                )
                answer = submission.Answer("approve", "checked the receipt")
                first = await submission.submit_answer(
                    first_client,
                    pending,
                    self.principal,
                    answer,
                    journal_root=self.journal,
                    issued_at=now,
                )

                # A brand-new connection, as a re-run of the CLI would have, and
                # a clock three hours further on.
                second_client = await self.separate_client()
                second = await submission.submit_answer(
                    second_client,
                    pending,
                    self.principal,
                    answer,
                    journal_root=self.journal,
                    issued_at=now + timedelta(hours=3),
                )

                self.assertEqual(first.decision_id, second.decision_id)
                self.assertFalse(first.resubmitted)
                self.assertTrue(second.resubmitted)
                self.assertTrue(second.accepted)
                stubs.verify_released.set()
                result = await handle.result()

        self.assertEqual(result.state, "completed")
        # Exactly one decision reached the Workflow, and it is the approval.
        self.assertEqual(len(stubs.admissions), 1)
        self.assertEqual(stubs.admissions[0].decision.decision, "approve")
        self.assertEqual(stubs.admissions[0].decision.rationale, "checked the receipt")

    async def test_a_changed_answer_never_resubmits_the_journalled_one(self) -> None:
        """Changing your mind must not silently submit the answer you replaced.

        The journal is a cache for exactly one decision. A human who answered,
        lost the connection, and re-ran with a different verdict means the second
        verdict; submitting the first because it happens to be on disk would put
        words in their mouth. The refusal names the file, because deleting it is
        the only correct recovery and the human cannot guess that.
        """

        stubs = Stubs()
        stubs.verify_released.clear()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)
                client = await self.separate_client()
                now = datetime.now(timezone.utc)
                pending = await submission.read_pending_gate(
                    client, RUN_ID, now=now, kinds=(gate_kind(),)
                )
                await submission.submit_answer(
                    client,
                    pending,
                    self.principal,
                    submission.Answer("approve", "checked the receipt"),
                    journal_root=self.journal,
                    issued_at=now,
                )
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.submit_answer(
                        client,
                        pending,
                        self.principal,
                        submission.Answer("reject", "on reflection, no"),
                        journal_root=self.journal,
                        issued_at=now,
                    )
                message = str(raised.exception)
                self.assertIn("verdict", message)
                self.assertIn("'approve'", message)
                self.assertIn("'reject'", message)
                self.assertIn(str(self.journal), message)

                await self.wait_for_admission(stubs)
                self.assertEqual(len(stubs.admissions), 1)
                self.assertEqual(stubs.admissions[0].decision.decision, "approve")
                stubs.verify_released.set()
                await handle.result()

    async def test_refuses_when_no_gate_is_open(self) -> None:
        stubs = Stubs()
        stubs.verify_released.clear()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                client = await self.separate_client()
                # Before the gate: the run is still planning or running stages.
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.read_pending_gate(
                        client, RUN_ID, now=datetime.now(timezone.utc), kinds=(gate_kind(),)
                    )
                self.assertIn("no open gate", str(raised.exception))

                gate = await self.wait_for_gate(handle)
                self.assertIsNotNone(gate)
                pending = await submission.read_pending_gate(
                    client, RUN_ID, now=datetime.now(timezone.utc), kinds=(gate_kind(),)
                )
                await submission.submit_answer(
                    client,
                    pending,
                    self.principal,
                    submission.Answer("approve", "fine"),
                    journal_root=self.journal,
                    issued_at=datetime.now(timezone.utc),
                )
                # After a decision is recorded, reading the gate again refuses
                # rather than offering a second signature over the same subject.
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.read_pending_gate(
                        client, RUN_ID, now=datetime.now(timezone.utc), kinds=(gate_kind(),)
                    )
                self.assertIn("already recorded decision", str(raised.exception))
                stubs.verify_released.set()
                await handle.result()

    async def test_expired_gate_is_refused_by_the_client_not_by_the_validator(self) -> None:
        """The client checks the window itself so the human gets a clear reason.

        Letting the update fail instead leaves a message about an update, and a
        human unsure whether their decision landed.
        """

        stubs = Stubs()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                gate = await self.wait_for_gate(handle)
                client = await self.separate_client()
                expired = datetime.fromisoformat(gate.expires_at) + timedelta(seconds=1)
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.read_pending_gate(
                        client, RUN_ID, now=expired, kinds=(gate_kind(),)
                    )
                self.assertIn("Gate expired at", str(raised.exception))
                self.assertEqual(stubs.admissions, [])
                await handle.cancel()
                with self.assertRaises(Exception):
                    await handle.result()

    async def test_actor_mismatch_refuses_before_the_update_is_sent(self) -> None:
        stubs = Stubs()
        async with self.worker(stubs):
            with self.environment.auto_time_skipping_disabled():
                handle = await self.start()
                await self.wait_for_gate(handle)
                client = await self.separate_client()
                pending = await submission.read_pending_gate(
                    client,
                    RUN_ID,
                    now=datetime.now(timezone.utc),
                    kinds=(gate_kind(actor="somebody-else"),),
                )
                with self.assertRaises(submission.SubmissionRefused) as raised:
                    await submission.submit_answer(
                        client,
                        pending,
                        self.principal,
                        submission.Answer("approve", "not mine to approve"),
                        journal_root=self.journal,
                        issued_at=datetime.now(timezone.utc),
                    )
                self.assertIn("somebody-else", str(raised.exception))
                self.assertIn(ACTOR, str(raised.exception))
                self.assertEqual(stubs.admissions, [])
                # Nothing was journalled either: a refused answer is not a record.
                self.assertFalse(self.journal.exists())
                await handle.cancel()
                with self.assertRaises(Exception):
                    await handle.result()


if __name__ == "__main__":
    unittest.main()
