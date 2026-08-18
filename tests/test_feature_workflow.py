"""The Workflow that raises the feature-assessment gate, driven end to end.

`feature_gate.py` had every contract the gate needs and nothing that raised it;
`assessment_record.py` gave it a producer; `submission.py` gave it an answerer.
`FeatureAssessmentRunWorkflow` is the piece that makes the chain reachable, and
these tests drive that chain rather than a model of it.

**Nothing here is stubbed except where a stub is the subject.** The worker
registers the *real* `prepare_feature_gate_activity` and
`admit_feature_dispositions_activity` against a real SQLite ledger and a real
content store under a `tempfile` state root, holding a real recorded stage 4a
assessment. That is not thoroughness for its own sake: the whole design claim is
that the Workflow holds no authority and the Activities re-derive from recorded
state, and a test whose Activities are stubs cannot tell a re-derivation from a
copy. Two stubs do appear, both registered under the real Activity name in the
style of `tests/test_phase_b_replay_workflow.py`, and both only *delay* the real
Activity so the Workflow can be observed in a state it otherwise passes through
in microseconds.

===============================================================================
  THE SPLIT THESE TESTS EXIST TO PIN
===============================================================================

The module's docstring states it: **the validator is a filter and the admitting
Activity is the authority.** A validator runs in the sandbox with no I/O, so it
can check that a decision binds this gate and that the dispositions reference is
a dispositions reference — and it cannot read the document the rulings live in.
So the tests come in two halves that must not be confused:

* FACTS 3-8 refuse a submission at the *validator*, observable as
  `WorkflowUpdateFailedError` — the update never happened, and where it makes
  sense the Workflow is shown still answerable afterwards.
* FACTS 9-11 refuse a submission that the validator **accepted**, observable as
  a receipt followed by a workflow *failure*. The receipt is the load-bearing
  half of those tests: it is what proves the refusal came from the authority and
  not from the filter.

`decision_count()` is asserted throughout because the ledger is the one place
that records what was admitted, and `admit_feature_dispositions_activity` is the
only writer of it in this chain. A refused submission that still left a decision
in the ledger would be the failure the whole gate exists to prevent, and no
assertion about workflow *state* would see it.

===============================================================================
  TWO OF THE VALIDATOR'S CLAUSES ARE SHADOWED, AND THE TESTS SAY WHICH
===============================================================================

Both still fail closed. Neither is a defect. But a test that did not know which
clause fires would be pinning a message rather than a check, so each is stated
where it is met.

**The wrong-artifact-kind clause cannot fire on a payload that arrived over the
wire.** `FeatureGateSubmissionV1.__post_init__` refuses a reference of the wrong
kind, and Temporal's payload converter *constructs the dataclass* while decoding
the update's arguments — before the validator runs, and before the update is
accepted (`_apply_do_update` converts arguments inside the same `try` that
rejects the update). So the contract always wins the race, and a test that only
drove Temporal would pass whether the validator checked the kind or not. FACT 5
therefore does both: the wire path, which pins that the confusion is refused
*somewhere*, and a direct call on the validator with a submission built past
`__post_init__`, which pins that this validator refuses it *itself*.

**The already-submitted clause cannot fire on a sequential client.** It is the
validator's last check, and by the time `execute_update` has returned a receipt
the workflow task that ran the handler has also moved the state past
`awaiting-feature-dispositions`. So the state guard refuses every second
submission first, and the duplicate clause behind it guards only two updates
delivered in one activation. FACT 7 pins the refusal a human actually meets and
the invariant that holds either way: exactly one decision is ever admitted.

Fixtures are `tests/test_assessment_record.py`'s index writer and
`tests/test_assessment.py`'s manifest, so the candidates ruled on here are the
ones stage 4a really mints. The harness is `tests/test_phase_a_temporal.py`'s:
`WorkflowEnvironment.start_time_skipping()`, a worker helper, and a polling
`wait_for_gate`.

Not covered here, by arrangement: the exact registered activity and workflow
sets, which `tests/test_phase_b_worker_registration.py` pins and this file
deliberately does not repeat. FACT 15 adds only what that file cannot see — the
update's *name*, whose disagreement with `submission.FEATURE_ASSESSMENT_GATE`
would leave every real gate unanswerable while every unit test still passed.
"""

import asyncio
import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.common import (
    PinnedVersioningOverride,
    VersioningBehavior,
    WorkerDeploymentVersion,
)
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, WorkerDeploymentConfig
from temporalio.workflow import _Definition as WorkflowDefinition

from dfinsta_pipeline import activities, assessment_record, feature_gate, worker as worker_module
from dfinsta_pipeline.activities import (
    admit_feature_dispositions_activity,
    configure_runtime,
    prepare_feature_gate_activity,
    runtime,
)
from dfinsta_pipeline.contracts import (
    ArtifactRef,
    GateDecision,
    GateRequest,
    WorkflowStatus,
    canonical_json,
)
from dfinsta_pipeline.feature_gate import (
    ASSESSMENT_ARTIFACT_KIND,
    DISPOSITIONS_ARTIFACT_KIND,
    FeatureAssessmentGateV1,
    FeatureDispositionsAdmissionV1,
    FeatureDispositionsV1,
    FeatureDispositionV1,
    FeatureGateSubmissionV1,
    FeatureRunRequestV1,
    FeatureRunResultV1,
    derive_feature_gate_request,
)
from dfinsta_pipeline.feature_workflow import FeatureAssessmentRunWorkflow
from dfinsta_pipeline.submission import (
    FEATURE_ASSESSMENT_GATE,
    gate_request_from_dict,
    read_pending_gate,
)

from tests.test_assessment import CURATED_MEMBERS, NOVEL_MEMBERS, surface_for, write_manifest
from tests.test_assessment_record import DESCRIPTOR, write_fake_index


TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-pipeline-tests", "feature-gate-v1")

RUN_ID = "run-feature-assessment-1"
#: A second run carrying a *different* recorded assessment. FACT 11's whole
#: subject: a document that is a perfectly good answer to this run's gate and no
#: answer at all to the other's.
STALE_RUN_ID = "run-feature-assessment-stale"
ALLOWED_ACTOR = "sam.operator"
OWNER_TOKEN = "feature-workflow-owner-1"
OTHER_CONTENT_HASH = "cd" * 32

#: The candidate ids stage 4a mints for the fixture index, derived from the same
#: tuple the assessment tests use so a change to the producer's spelling arrives
#: here as a failed fixture rather than as rulings about ids nobody produces.
CANDIDATES = tuple(f"gap:{literal}" for literal in NOVEL_MEMBERS)

#: Large enough that no test races the gate timer, and the timeout test names its
#: own. `timedelta(seconds=...)` is what the Workflow builds the window from.
GATE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60

#: A hash that is a valid SHA-256 and is not any hash this fixture produces.
OTHER_SHA256 = "9" * 64

UNBOUND = "Decision does not match the pending feature gate"
OUTSIDE_WINDOW = "Decision timestamp is outside the gate validity period"


class FeatureAssessmentRunWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """One temp state root, one recorded assessment, one time-skipping server."""

    async def asyncSetUp(self) -> None:
        # `configure_runtime` sets a module global. Restore whatever was there so
        # a later test in the same process is not handed this one's state root.
        previous_runtime = activities._runtime
        self.addCleanup(setattr, activities, "_runtime", previous_runtime)

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # `.resolve()` because `record` and `configure_runtime` both resolve the
        # state root and `/tmp` is a symlink on some systems.
        self.tmp = Path(holder.name).resolve()
        self.state = self.tmp / "state"
        self.index = write_fake_index(self.tmp / "index")
        self.manifest = write_manifest(self.tmp / "hooks.json")
        self.recorded = assessment_record.record(
            self.state,
            run_id=RUN_ID,
            index_dir=self.index,
            manifest_path=self.manifest,
            allowed_actor=ALLOWED_ACTOR,
            owner_token=OWNER_TOKEN,
        )
        self.assertEqual(self.recorded.candidate_ids, CANDIDATES)
        # Derived here, from the recorded values and nothing the Workflow
        # produced. Every published gate below is compared against this.
        self.request = self.derive(self.recorded)

        configure_runtime(self.state)
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.task_queue = "feature-assessment-tests"

    async def asyncTearDown(self) -> None:
        await self.environment.shutdown()

    # ------------------------------------------------------------- the harness

    @staticmethod
    def derive(recorded) -> feature_gate.FeatureGateRequestV1:
        return derive_feature_gate_request(
            recorded.run_id,
            recorded.assessment,
            recorded.policy_revision,
            recorded.allowed_actor,
            recorded.candidate_ids,
        )

    def worker(self, *overrides) -> Worker:
        """The registered Workflow, and the *real* Activities unless overridden.

        An override is registered under the real Activity's name, so the Workflow
        still passes the real callable to `execute_activity` and the Worker
        resolves the stub by name — the pattern
        `tests/test_phase_b_replay_workflow.py` uses.
        """
        registered = {
            activity._Definition.from_callable(fn).name: fn  # type: ignore[attr-defined,union-attr]
            for fn in (prepare_feature_gate_activity, admit_feature_dispositions_activity)
        }
        for override in overrides:
            name = activity._Definition.from_callable(  # type: ignore[union-attr]
                override
            ).name
            self.assertIn(name, registered, "an override must replace a real Activity")
            registered[name] = override
        return Worker(
            self.environment.client,
            task_queue=self.task_queue,
            workflows=[FeatureAssessmentRunWorkflow],
            activities=list(registered.values()),
            max_cached_workflows=0,
            deployment_config=WorkerDeploymentConfig(
                version=TEST_DEPLOYMENT_VERSION,
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
            ),
        )

    async def start(
        self,
        run_id: str = RUN_ID,
        *,
        gate_timeout_seconds: int = GATE_TIMEOUT_SECONDS,
        workflow_id: str | None = None,
    ):
        return await self.environment.client.start_workflow(
            FeatureAssessmentRunWorkflow.run,
            FeatureRunRequestV1(1, run_id, gate_timeout_seconds),
            id=workflow_id or run_id,
            task_queue=self.task_queue,
            versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
        )

    async def wait_for_gate(self, handle) -> GateRequest:
        for _ in range(200):
            status = await handle.query(FeatureAssessmentRunWorkflow.status)
            if status.state == "awaiting-feature-dispositions":
                assert status.gate is not None
                return status.gate
            await self.environment.sleep(0.01)
        self.fail("Workflow did not reach the feature-assessment gate")

    async def wait_for_state(self, handle, expected: str) -> None:
        for _ in range(200):
            status = await handle.query(FeatureAssessmentRunWorkflow.status)
            if status.state == expected:
                return
            await asyncio.sleep(0.01)
        self.fail(f"Workflow did not reach {expected}")

    async def submit(self, handle, submission: FeatureGateSubmissionV1):
        return await handle.execute_update(
            FeatureAssessmentRunWorkflow.submit_feature_dispositions, submission
        )

    async def refuses(self, handle, submission: FeatureGateSubmissionV1, reason: str) -> None:
        """The update was refused, naming `reason` somewhere in its cause chain.

        The chain rather than the top cause, because a refusal raised while the
        update's arguments are being *decoded* is wrapped in a "Failed decoding
        arguments" `ApplicationError` whose own cause carries the real message.
        Reading only the top would make FACT 5 unable to say which check fired.
        """
        with self.assertRaises(WorkflowUpdateFailedError) as raised:
            await self.submit(handle, submission)
        self.assertIn(reason, _failure_chain(raised.exception))

    def admission_failure(self, error: WorkflowFailureError, kind: str) -> ApplicationError:
        """Unwrap a workflow failure that came out of the admitting Activity.

        Both halves are asserted, because they fail differently: `retry_state`
        says the *server* stopped retrying because the failure said so, and
        `non_retryable` says the flag survived the round trip to this client. A
        refusal made retryable would still name the same type, still fail the
        run, and only these two say the run failed on the first attempt rather
        than after three trips through a check that could never change its mind.
        """
        cause = error.cause
        self.assertIsInstance(cause, ActivityError)
        assert isinstance(cause, ActivityError)
        self.assertEqual(cause.retry_state, RetryState.NON_RETRYABLE_FAILURE)
        application = cause.cause
        self.assertIsInstance(application, ApplicationError)
        assert isinstance(application, ApplicationError)
        self.assertEqual(application.type, kind)
        self.assertTrue(application.non_retryable)
        return application

    # -------------------------------------------------------- payload builders

    def document(
        self,
        *,
        assessment_sha256: str | None = None,
        candidate_ids: tuple[str, ...] | None = None,
        policy_revision: str | None = None,
        verdict: str = "defer",
    ) -> FeatureDispositionsV1:
        """A dispositions document for every candidate this run covers.

        `defer` rather than `offer_toggle`, and the reason is load-bearing: these
        tests record against a state root with no observation store, so every
        candidate reaches the gate reading "no device has looked for this", and
        `block` and `offer_toggle` are refused for such a candidate. `defer` is
        not — it is a ruling, it satisfies completeness, and it still carries a
        rationale — so every clause these tests are about is exercised unchanged.
        The refusal itself is covered in `tests/test_feature_gate.py` and end to
        end in `tests/test_submission_feature_gate.py`.
        """
        return FeatureDispositionsV1(
            1,
            assessment_sha256 or self.recorded.assessment.sha256,
            policy_revision or self.recorded.policy_revision,
            tuple(
                FeatureDispositionV1(1, candidate, verdict, f"revisit {candidate}", "unsolicited")
                for candidate in (
                    self.recorded.candidate_ids if candidate_ids is None else candidate_ids
                )
            ),
        )

    def publish(self, document: FeatureDispositionsV1) -> ArtifactRef:
        """Put a dispositions document in CAS exactly as the client does."""
        return self.publish_bytes(
            canonical_json(document).encode("utf-8"), f"client-{document.sha256}"
        )

    def publish_bytes(self, body: bytes, operation_id: str = "client-unreadable") -> ArtifactRef:
        return runtime().store.put_bytes(
            kind=DISPOSITIONS_ARTIFACT_KIND,
            data=body,
            producer_operation_id=operation_id,
            input_hashes=(),
        )

    def decision(
        self,
        gate: GateRequest,
        *,
        decision_id: str = "feature-approval-1",
        idempotency_id: str | None = None,
        actor: str = ALLOWED_ACTOR,
        run_id: str | None = None,
        gate_id: str | None = None,
        subject: str | None = None,
        admission: str | None = None,
        prepared: str | None = None,
        policy_revision: str | None = None,
        verdict: str = "approve",
        issued_at: str | None = None,
    ) -> GateDecision:
        """A decision binding the published gate unless a field is overridden.

        `issued_at` defaults to the gate's own issue time rather than to
        wall-clock time: the Workflow clock is the test server's, and a
        skipped-time run must not depend on the two agreeing.
        """
        return GateDecision(
            1,
            decision_id,
            idempotency_id or f"request-{decision_id}",
            actor,
            run_id or gate.run_id,
            gate_id or gate.gate_id,
            subject or gate.subject_sha256,
            admission or gate.admission_sha256,
            prepared or gate.prepared_sha256,
            policy_revision or gate.policy_revision,
            verdict,  # type: ignore[arg-type]
            "every candidate carries a ruling",
            issued_at or gate.issued_at,
        )

    def submission(
        self, gate: GateRequest, *, dispositions: ArtifactRef | None = None, **overrides
    ) -> FeatureGateSubmissionV1:
        reference = self.publish(self.document()) if dispositions is None else dispositions
        return FeatureGateSubmissionV1(1, self.decision(gate, **overrides), reference)

    # ------------------------------------------------------------ the happy path

    async def test_the_published_gate_is_derived_and_rulings_signed_against_it_are_admitted(
        self,
    ) -> None:
        """FACT 1. End to end over a real recorded assessment.

        Three claims, and the third is the one that would be easy to fake. The
        gate publishes the *derived* request hash in all three fields, an
        independent derivation from the recorded values reproduces it, and the
        `ArtifactRef` the run reports is the one that was submitted rather than
        one the Workflow minted on the way past.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)

            self.assertEqual(gate.run_id, RUN_ID)
            self.assertEqual(gate.gate_id, f"{RUN_ID}-feature-assessment-gate")
            self.assertEqual(gate.policy_revision, self.recorded.policy_revision)
            # All three, because `GateDecision` requires all three and the client
            # compares all three against what it re-derived. This gate's subject
            # is one derived object.
            self.assertEqual(
                {gate.subject_sha256, gate.admission_sha256, gate.prepared_sha256},
                {self.request.sha256},
            )
            independent = self.derive(assessment_record.resolve(self.state, RUN_ID))
            self.assertEqual(independent.sha256, gate.subject_sha256)
            # Positive control: the hash moves when what it covers moves, so the
            # agreement above is agreement rather than a hash ignoring its input.
            moved = derive_feature_gate_request(
                self.recorded.run_id,
                dataclasses.replace(self.recorded.assessment, input_hashes=()),
                self.recorded.policy_revision,
                self.recorded.allowed_actor,
                self.recorded.candidate_ids,
            )
            self.assertNotEqual(moved.sha256, gate.subject_sha256)

            issued = datetime.fromisoformat(gate.issued_at)
            expires = datetime.fromisoformat(gate.expires_at)
            self.assertIsNotNone(issued.tzinfo)
            self.assertEqual(expires - issued, timedelta(seconds=GATE_TIMEOUT_SECONDS))

            submission = self.submission(gate)
            receipt = await self.submit(handle, submission)
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.decision_id, "feature-approval-1")
            result = await handle.result()

        self.assertIsInstance(result, FeatureRunResultV1)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.run_id, RUN_ID)
        self.assertEqual(result.decision_id, "feature-approval-1")
        # The same reference, field for field: the run names the artifact it
        # admitted rather than one it merely passed along.
        self.assertEqual(result.dispositions, submission.dispositions)
        assert result.dispositions is not None
        self.assertEqual(result.dispositions.kind, DISPOSITIONS_ARTIFACT_KIND)
        self.assertEqual(runtime().ledger.decision_count(), 1)
        # And the admitted bytes are still the document whose digest was signed.
        body = runtime().store.read_blob(
            result.dispositions.sha256, result.dispositions.size
        )
        admitted = FeatureDispositionsV1.from_dict(json.loads(body.decode("utf-8")))
        self.assertEqual(
            [item.candidate_id for item in admitted.dispositions], list(CANDIDATES)
        )

    async def test_the_status_query_is_exactly_the_shape_the_submission_client_consumes(
        self,
    ) -> None:
        """FACT 2. Queried by name, parsed by the client, or unanswerable.

        `submission.read_pending_gate` holds no Workflow class: it queries
        `"status"` by name and requires `{state, gate, decision_id}` with `gate` a
        plain `GateRequest`. So the reply is taken here in its wire form — a
        plain dict — and pushed through the client's own parsing path rather
        than through an assertion about what that path would have done.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)

            # Queried by name and with no result type, which is what the client
            # does and what makes the reply arrive as plain JSON.
            raw = await handle.query("status")
            self.assertIs(type(raw), dict)
            self.assertEqual(set(raw), {"state", "gate", "decision_id"})
            self.assertEqual(
                set(raw), {field.name for field in dataclasses.fields(WorkflowStatus)}
            )
            self.assertEqual(raw["state"], "awaiting-feature-dispositions")
            self.assertIsNone(raw["decision_id"])
            self.assertEqual(
                set(raw["gate"]), {field.name for field in dataclasses.fields(GateRequest)}
            )
            self.assertEqual(gate_request_from_dict(raw["gate"]), gate)

            # The client's own path, over the real query: it reproduces the
            # subject from the ledger and refuses on any disagreement.
            client = _HandleClient(handle)
            pending = await read_pending_gate(
                client, RUN_ID, now=datetime.fromisoformat(gate.issued_at) + timedelta(seconds=1)
            )
            self.assertEqual(client.asked, [RUN_ID])
            self.assertIs(pending.kind, FEATURE_ASSESSMENT_GATE)
            self.assertEqual(pending.published, gate)
            self.assertEqual(pending.derived.run_id, RUN_ID)
            self.assertEqual(pending.derived.gate_id, gate.gate_id)
            self.assertEqual(pending.derived.allowed_actor, ALLOWED_ACTOR)
            self.assertEqual(
                {
                    pending.derived.subject_sha256,
                    pending.derived.admission_sha256,
                    pending.derived.prepared_sha256,
                },
                {self.request.sha256},
            )

            # After an answer the same query reports the decision, which is the
            # field the client reads to refuse answering a gate twice.
            await self.submit(handle, self.submission(gate))
            answered = await handle.query("status")
            self.assertEqual(answered["decision_id"], "feature-approval-1")
            result = await handle.result()
        self.assertEqual(result.state, "completed")

    # ------------------------------------------------- the validator, as a filter

    async def test_a_decision_from_an_actor_the_gate_does_not_allow_is_refused(self) -> None:
        """FACT 3. The gate names who may answer, and this is the ONLY check of it.

        Worth saying precisely, because the natural assumption is wrong.
        `FeatureGateRequestV1` puts `allowed_actor` inside the derived bytes and
        its docstring gives the reason — carried only on the envelope, "who may
        answer" would be checked solely by the Workflow validator "and never by
        the hash chain, so the admitting Activity could not independently verify
        it". It is in the bytes, and the admitting side still does not verify it:
        `feature_gate.validate_submission` compares subject, run, gate and policy
        and never compares `decision.actor` to `request.allowed_actor`. The
        replay gate's admitting side does exactly that comparison
        (`AdmittedReplayVerificationGrantV1.__post_init__`), so the asymmetry is
        with this project's own precedent rather than with an outside standard.

        **Closed 2026-08-03**, in response to this test finding it: the actor is
        compared in `validate_submission` as well, so the authority now checks
        what the filter checks. The second half below asserts the closure rather
        than the gap — an intruder's submission is refused by the admitting side
        even when it never meets the Workflow's validator at all.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            await self.refuses(
                handle,
                self.submission(gate, decision_id="intruder-1", actor="intruder"),
                "Decision actor is not authorized",
            )
            # Still answerable: the refusal rejected an update, not the gate.
            receipt = await self.submit(handle, self.submission(gate))
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.decision_id, "feature-approval-1")
        # One decision reached the ledger, and it is not the intruder's.
        self.assertEqual(runtime().ledger.decision_count(), 1)

        # And the authority refuses the same submission on its own. This is the
        # half that matters: the Workflow validator is a filter running in a
        # sandbox, so "who may answer" cannot rest on it alone.
        intruder = self.submission(gate, decision_id="intruder-2", actor="intruder")
        document = self.document()
        self.assertEqual(intruder.dispositions.sha256, document.sha256)
        self.assertEqual(self.request.allowed_actor, ALLOWED_ACTOR)
        with self.assertRaises(ValueError) as raised:
            feature_gate.validate_submission(
                self.request, intruder, document, self.recorded.document
            )
        self.assertIn("actor", str(raised.exception))
        # Positive control: the same call with the allowed actor is admitted, so
        # the refusal above is about the actor and not about the fixture.
        self.assertIsNone(
            feature_gate.validate_submission(
                self.request,
                self.submission(gate, decision_id="control-1"),
                document,
                self.recorded.document,
            )
        )

    async def test_a_decision_that_leaves_any_bound_field_unbound_is_refused(self) -> None:
        """FACT 4. Every field the gate binds, driven one at a time.

        Separately rather than together, because a validator that checked only
        `subject_sha256` would pass a single test that moved all six. The two
        interesting ones are `admission_sha256` and `prepared_sha256`: this
        gate's subject is *one* derived object, so all three hashes must equal
        the request hash. `feature_gate.validate_submission` used to check only
        the subject, so nothing downstream caught the other two — the same gap
        FACT 3 records for the actor, found by this test and **closed
        2026-08-03**. The closing assertions now pin the closure.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            unbound = (
                ("subject_sha256", {"subject": OTHER_SHA256}),
                ("admission_sha256", {"admission": OTHER_SHA256}),
                ("prepared_sha256", {"prepared": OTHER_SHA256}),
                ("run_id", {"run_id": "run-somewhere-else"}),
                ("gate_id", {"gate_id": "some-other-gate"}),
                ("policy_revision", {"policy_revision": "2026-08-02"}),
            )
            for field, override in unbound:
                with self.subTest(field=field):
                    await self.refuses(
                        handle,
                        self.submission(gate, decision_id=f"unbound-{len(field)}", **override),
                        UNBOUND,
                    )
            receipt = await self.submit(handle, self.submission(gate))
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)

        # The authority binds all three, not just the subject. Driven directly
        # against `validate_submission` rather than through the Workflow, because
        # the sandbox validator would refuse these first and the question here is
        # what the *authority* does when reached by any other route.
        document = self.document()
        for field, override in (
            ("subject_sha256", {"subject": OTHER_SHA256}),
            ("admission_sha256", {"admission": OTHER_SHA256}),
            ("prepared_sha256", {"prepared": OTHER_SHA256}),
        ):
            with self.subTest(unbound=field):
                with self.assertRaises(ValueError) as raised:
                    feature_gate.validate_submission(
                        self.request,
                        self.submission(gate, decision_id=f"authority-{field}", **override),
                        document,
                        self.recorded.document,
                    )
                self.assertIn("does not bind", str(raised.exception))
        # Positive control: with every field bound, the same call is admitted —
        # so the three refusals are about the fields and not about the fixture.
        self.assertIsNone(
            feature_gate.validate_submission(
                self.request,
                self.submission(gate, decision_id="authority-control"),
                document,
                self.recorded.document,
            )
        )

    async def test_a_reference_of_the_wrong_artifact_kind_is_refused(self) -> None:
        """FACT 5. The one confusion that would submit an assessment as its answer.

        Two layers, because they fire in an order that matters. Over the wire the
        *contract* wins: Temporal's converter constructs
        `FeatureGateSubmissionV1` while decoding the update's arguments, so
        `__post_init__` refuses the reference before the validator is reached —
        the refusal arrives as "Failed decoding arguments" wrapping the
        contract's own message. That is why the second half calls the validator
        directly on a submission built past `__post_init__`; otherwise the
        validator's identical clause would be untested and its removal
        invisible.

        The direct half pairs a wrong-kind reference with a decision that is
        *also* unbound, so the assertion is about ordering rather than about a
        message: with the kind check present the kind is named, and without it
        the binding would be. The second failing field is the binding rather
        than the actor so that this test does not go quiet when a different
        clause is what breaks.
        """
        assessment_reference = self.recorded.assessment
        self.assertEqual(assessment_reference.kind, ASSESSMENT_ARTIFACT_KIND)

        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)

            # Over the wire. Built past the contract, sent anyway; refused by the
            # contract at decode, which is one layer earlier than the validator.
            confused = _unchecked_submission(
                self.decision(gate, decision_id="confused-1"), assessment_reference
            )
            with self.assertRaises(WorkflowUpdateFailedError) as raised:
                await self.submit(handle, confused)
            self.assertIn(
                "Invalid feature gate dispositions kind", _failure_chain(raised.exception)
            )
            # And the gate is untouched: a refused decode is not an answer.
            self.assertIsNone(
                (await handle.query(FeatureAssessmentRunWorkflow.status)).decision_id
            )

            # Directly at the validator, in the state the Workflow puts it in.
            validator = FeatureAssessmentRunWorkflow()
            validator._gate = FeatureAssessmentGateV1(
                1, RUN_ID, gate.gate_id, self.request.sha256, ALLOWED_ACTOR, gate.policy_revision
            )
            validator._gate_request = gate
            validator._state = "awaiting-feature-dispositions"

            unbound = {"decision_id": "confused-2", "run_id": "run-somewhere-else"}
            wrong_kind = _unchecked_submission(
                self.decision(gate, **unbound), assessment_reference
            )
            with self.assertRaises(ValueError) as refused:
                validator.validate_submit_feature_dispositions(wrong_kind)
            self.assertIn(
                "Submitted artifact is not a feature dispositions document", str(refused.exception)
            )
            # The control: the same unbound decision, a right-kind reference. The
            # kind check is passed and the next one fires, so the message above
            # was the kind check rather than a validator refusing everything.
            right_kind = FeatureGateSubmissionV1(
                1, self.decision(gate, **unbound), self.publish(self.document())
            )
            with self.assertRaises(ValueError) as refused:
                validator.validate_submit_feature_dispositions(right_kind)
            self.assertIn(UNBOUND, str(refused.exception))

            receipt = await self.submit(handle, self.submission(gate))
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)

    async def test_a_decision_timestamped_outside_the_gate_window_is_refused(self) -> None:
        """FACT 6. Before it was issued, after it expires, or too far ahead.

        The skew clause is the one worth stating: it is not zero, because the
        client stamps `issued_at` before the round trip and the two clocks are
        different machines — and it is not large, because the point is that a
        decision cannot be pre-dated into a window it was not issued in.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            issued = datetime.fromisoformat(gate.issued_at)
            stale = (
                ("before-issue", "2000-01-01T00:00:00+00:00", OUTSIDE_WINDOW),
                ("at-expiry", gate.expires_at, OUTSIDE_WINDOW),
                (
                    "beyond-skew",
                    (issued + timedelta(minutes=6)).isoformat(),
                    OUTSIDE_WINDOW,
                ),
                (
                    "naive",
                    issued.replace(tzinfo=None).isoformat(),
                    "Decision timestamp requires a UTC offset",
                ),
            )
            for label, issued_at, reason in stale:
                with self.subTest(timestamp=label):
                    await self.refuses(
                        handle,
                        self.submission(gate, decision_id=f"stale-{label}", issued_at=issued_at),
                        reason,
                    )
            # The control the skew case needs: five minutes ahead is inside the
            # allowance, so "beyond-skew" was refused for being beyond it rather
            # than for being ahead at all.
            receipt = await self.submit(
                handle,
                self.submission(
                    gate, issued_at=(issued + timedelta(minutes=4)).isoformat()
                ),
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)

    async def test_a_second_submission_after_one_was_accepted_is_refused(self) -> None:
        """FACT 7. One answer per gate: a second is refused, whatever it says.

        The admitting Activity is held open by a stub so the Workflow is still
        alive when the second submission arrives; without it the run completes
        first and the second update fails as "workflow already completed", which
        would pass this test while proving nothing about the validator.

        **Which clause refuses it is worth stating exactly, because it is not the
        obvious one.** The validator's `_decision_ids` / `_idempotency_ids` /
        `_submission is not None` clause is last, and by the time a *sequential*
        client can send a second update the Workflow has already left
        `awaiting-feature-dispositions` — `execute_update` returns only once the
        workflow task that ran the handler completed, and that same task moved
        the state on. So the state guard fires first, every time, and the
        duplicate clause behind it is a guard for two updates delivered in one
        activation, which no client here can arrange. This test pins the refusal
        that a human actually meets, and the invariant that survives either way:
        exactly one decision is admitted and the run names the first one.
        """
        released = asyncio.Event()

        # Annotated, and it must stay annotated: the argument type is how the
        # Worker knows to decode the payload into the contract the real Activity
        # expects, and an unannotated stub would hand it a bare dict.
        @activity.defn(name="admit_feature_dispositions_activity")
        async def admit_stub(admission: FeatureDispositionsAdmissionV1) -> ArtifactRef:
            await released.wait()
            return await admit_feature_dispositions_activity(admission)

        with self.environment.auto_time_skipping_disabled():
            async with self.worker(admit_stub):
                handle = await self.start()
                gate = await self.wait_for_gate(handle)
                accepted = self.submission(gate)
                receipt = await self.submit(handle, accepted)
                self.assertTrue(receipt.accepted)
                # Still running, held at the admitting Activity: what follows
                # reaches the validator rather than a completed execution.
                status = await handle.query(FeatureAssessmentRunWorkflow.status)
                self.assertEqual(status.state, "admitting-feature-dispositions")
                self.assertEqual(status.decision_id, accepted.decision.decision_id)

                for label, override in (
                    ("identical", {}),
                    ("replayed-decision-id", {"idempotency_id": "request-second"}),
                    (
                        "replayed-idempotency-id",
                        {
                            "decision_id": "feature-approval-2",
                            "idempotency_id": "request-feature-approval-1",
                        },
                    ),
                    (
                        "wholly-different",
                        {
                            "decision_id": "feature-approval-3",
                            "verdict": "reject",
                        },
                    ),
                ):
                    with self.subTest(second=label):
                        await self.refuses(
                            handle,
                            self.submission(gate, **override),
                            "Workflow is not awaiting feature dispositions",
                        )

                released.set()
                result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.decision_id, accepted.decision.decision_id)
        self.assertEqual(result.dispositions, accepted.dispositions)
        self.assertEqual(runtime().ledger.decision_count(), 1)

    async def test_a_submission_arriving_before_the_gate_is_open_is_refused(self) -> None:
        """FACT 8. A gate that is not yet raised cannot be answered.

        The state guard is checked first and outside every other clause, which
        matters because the fields it protects — `_gate` and `_gate_request` —
        are `None` until the preparing Activity returns. The same Workflow
        answers the same shape of submission a moment later, so the refusal is
        about *when* rather than about what arrived.
        """
        released = asyncio.Event()

        @activity.defn(name="prepare_feature_gate_activity")
        async def gate_stub(run_id: str) -> FeatureAssessmentGateV1:
            await released.wait()
            return await prepare_feature_gate_activity(run_id)

        with self.environment.auto_time_skipping_disabled():
            async with self.worker(gate_stub):
                handle = await self.start()
                await self.wait_for_state(handle, "preparing-feature-gate")
                status = await handle.query(FeatureAssessmentRunWorkflow.status)
                self.assertIsNone(status.gate)
                self.assertIsNone(status.decision_id)

                # A submission built against the subject the client would derive
                # for itself: everything a valid one carries except a gate to
                # answer.
                now = datetime.now(timezone.utc)
                unopened = GateRequest(
                    1,
                    RUN_ID,
                    self.request.gate_id,
                    self.request.sha256,
                    self.request.sha256,
                    self.request.sha256,
                    self.request.policy_revision,
                    now.isoformat(),
                    (now + timedelta(seconds=GATE_TIMEOUT_SECONDS)).isoformat(),
                )
                await self.refuses(
                    handle,
                    self.submission(unopened, decision_id="too-early-1"),
                    "Workflow is not awaiting feature dispositions",
                )

                released.set()
                gate = await self.wait_for_gate(handle)
                receipt = await self.submit(handle, self.submission(gate))
                self.assertTrue(receipt.accepted)
                result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(runtime().ledger.decision_count(), 1)

    # ---------------------------------------------- the Activity, as the authority

    async def test_a_submission_the_validator_accepts_is_refused_for_the_wrong_candidates(
        self,
    ) -> None:
        """FACT 9. The design's central consequence, made observable.

        The validator cannot read the dispositions document — it runs in a
        sandbox with no content store — so it cannot know that these rulings are
        about a different candidate list. It accepts, and the Activity refuses.

        The accepted receipt is the load-bearing assertion. Without it this test
        would pass identically if the validator had rejected the submission, and
        the claim "a submission that passes the validator can still be refused by
        the Activity" would be untested.
        """
        cases = (
            (
                "missing",
                self.recorded.candidate_ids[:-1],
                "Candidate carries no disposition",
            ),
            (
                "unknown",
                (*self.recorded.candidate_ids, "gap:feed/never_indexed/"),
                "Disposition names an unknown candidate",
            ),
        )
        for label, candidates, reason in cases:
            with self.subTest(candidates=label):
                async with self.worker():
                    handle = await self.start(workflow_id=f"{RUN_ID}-{label}")
                    gate = await self.wait_for_gate(handle)
                    reference = self.publish(self.document(candidate_ids=candidates))
                    # A decision id per case, so a case that is wrongly admitted
                    # cannot make the next one fail for the unrelated reason that
                    # the ledger already holds that decision.
                    submission = self.submission(
                        gate, dispositions=reference, decision_id=f"feature-{label}-1"
                    )

                    receipt = await self.submit(handle, submission)
                    self.assertTrue(receipt.accepted, "the validator must have passed this")

                    with self.assertRaises(WorkflowFailureError) as raised:
                        # A bounded wait, because "fails" and "hangs forever" are
                        # the two outcomes this distinguishes.
                        await asyncio.wait_for(handle.result(), timeout=60)
                failure = self.admission_failure(raised.exception, "FeatureDispositionsRefused")
                self.assertIn(reason, str(failure))
                # Nothing was admitted: `record_decision` runs only after
                # `validate_submission` returns.
                self.assertEqual(runtime().ledger.decision_count(), 0)

    async def test_a_reference_to_bytes_that_are_not_a_dispositions_document_is_refused(
        self,
    ) -> None:
        """FACT 10. Unreadable is refused, not skipped, and not retried.

        The reference is a genuine CAS reference of the right kind and the right
        digest and size, so `read_blob` hands the bytes over without complaint.
        The refusal has to come from decoding them.
        """
        cases = (
            ("not-json", b"{ this is not a document"),
            ("json-but-not-the-document", b'{"schema_version":1}'),
            ("json-but-not-an-object", b"[1, 2, 3]"),
        )
        for label, body in cases:
            with self.subTest(body=label):
                async with self.worker():
                    handle = await self.start(workflow_id=f"{RUN_ID}-{label}")
                    gate = await self.wait_for_gate(handle)
                    reference = self.publish_bytes(body, f"client-{label}")
                    self.assertEqual(reference.kind, DISPOSITIONS_ARTIFACT_KIND)
                    # The store hands these bytes back, so the refusal below is
                    # about the document rather than about the fetch.
                    self.assertEqual(
                        runtime().store.read_blob(reference.sha256, reference.size), body
                    )

                    receipt = await self.submit(
                        handle,
                        self.submission(
                            gate, dispositions=reference, decision_id=f"feature-{label}-1"
                        ),
                    )
                    self.assertTrue(receipt.accepted, "the validator must have passed this")

                    with self.assertRaises(WorkflowFailureError) as raised:
                        await asyncio.wait_for(handle.result(), timeout=60)
                self.admission_failure(raised.exception, "FeatureDispositionsUnreadable")
                self.assertEqual(runtime().ledger.decision_count(), 0)

    async def test_the_admitting_activity_re_derives_the_request_from_the_ledger(self) -> None:
        """FACT 11. Not the Workflow's copy of the subject. The ledger's.

        The Workflow's gate is six scalars: it carries no assessment reference
        and no candidate list, so nothing it holds could tell these rulings apart
        from the right ones. Only a re-derivation from the run-keyed authority
        row can, and this is what that looks like when it bites.

        A second run carries a *different* recorded assessment. Its dispositions
        document is submitted against this run's gate with a decision that binds
        this gate perfectly — and the positive control is the same document
        submitted against its own run's gate, which is admitted. So the document
        is a valid answer to a gate; the only thing separating the two outcomes
        is which request the Activity derived.
        """
        stale_index = write_fake_index(
            self.tmp / "stale-index",
            content_hash=OTHER_CONTENT_HASH,
            api_paths=surface_for(
                {DESCRIPTOR: tuple(m for m in CURATED_MEMBERS if m != NOVEL_MEMBERS[-1])}
            ),
        )
        stale = assessment_record.record(
            self.state,
            run_id=STALE_RUN_ID,
            index_dir=stale_index,
            manifest_path=self.manifest,
            allowed_actor=ALLOWED_ACTOR,
            owner_token=OWNER_TOKEN,
        )
        # A genuinely different assessment, or the test proves nothing.
        self.assertNotEqual(stale.assessment.sha256, self.recorded.assessment.sha256)
        self.assertNotEqual(stale.candidate_ids, self.recorded.candidate_ids)
        self.assertEqual(stale.policy_revision, self.recorded.policy_revision)

        stale_document = self.document(
            assessment_sha256=stale.assessment.sha256, candidate_ids=stale.candidate_ids
        )
        stale_reference = self.publish(stale_document)

        async with self.worker():
            handle = await self.start(workflow_id=f"{RUN_ID}-stale")
            gate = await self.wait_for_gate(handle)
            self.assertEqual(gate.subject_sha256, self.request.sha256)

            receipt = await self.submit(
                handle, self.submission(gate, dispositions=stale_reference)
            )
            self.assertTrue(receipt.accepted, "the validator must have passed this")
            with self.assertRaises(WorkflowFailureError) as raised:
                await asyncio.wait_for(handle.result(), timeout=60)
        failure = self.admission_failure(raised.exception, "FeatureDispositionsRefused")
        self.assertIn("Dispositions do not bind the assessed document", str(failure))
        self.assertEqual(runtime().ledger.decision_count(), 0)

        # The control. Same bytes, same reference, its own gate: admitted.
        async with self.worker():
            handle = await self.start(STALE_RUN_ID)
            stale_gate = await self.wait_for_gate(handle)
            self.assertEqual(stale_gate.subject_sha256, self.derive(stale).sha256)
            self.assertNotEqual(stale_gate.subject_sha256, self.request.sha256)
            receipt = await self.submit(
                handle,
                FeatureGateSubmissionV1(
                    1,
                    self.decision(stale_gate, decision_id="feature-approval-stale"),
                    stale_reference,
                ),
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.dispositions, stale_reference)
        self.assertEqual(runtime().ledger.decision_count(), 1)

    # ------------------------------------------------------ outcomes and timeout

    async def test_a_rejected_gate_ends_the_run_without_admitting_dispositions(self) -> None:
        """FACT 12. A human said no, and nothing was admitted on their authority.

        `decision_count()` is the assertion that matters. The admitting Activity
        is the only writer of a decision in this chain, so an empty ledger is
        what "no dispositions were admitted" actually means — a `None` in the
        result would also be true of a run that admitted them and forgot to say.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            receipt = await self.submit(
                handle, self.submission(gate, decision_id="feature-reject-1", verdict="reject")
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "rejected")
        self.assertEqual(result.decision_id, "feature-reject-1")
        self.assertIsNone(result.dispositions)
        self.assertEqual(runtime().ledger.decision_count(), 0)

    async def test_a_deferred_gate_ends_the_run_deferred(self) -> None:
        """FACT 13. `defer` is a decision, and a different one from `reject`.

        The two share a branch in the Workflow, so a mutation that collapsed them
        would keep FACT 12 passing.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            receipt = await self.submit(
                handle, self.submission(gate, decision_id="feature-defer-1", verdict="defer")
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual(result.state, "deferred")
        self.assertEqual(result.decision_id, "feature-defer-1")
        self.assertIsNone(result.dispositions)
        self.assertEqual(runtime().ledger.decision_count(), 0)

    async def test_an_unanswered_gate_ends_blocked_and_never_rejected(self) -> None:
        """FACT 14. Nobody decided anything, and the run must not say they did.

        `rejected` would record that a human looked at this and said no. What
        happened is that the gate went unanswered, and the difference is the
        whole reason `blocked` exists as a separate state: a rejected run carries
        a decision id, and there is no decision to name.
        """
        timeout = 3 * 24 * 60 * 60
        with self.environment.auto_time_skipping_disabled():
            async with self.worker():
                handle = await self.start(gate_timeout_seconds=timeout)
                await self.wait_for_gate(handle)
                await self.environment.sleep(timeout - 1)
                status = await handle.query(FeatureAssessmentRunWorkflow.status)
                self.assertEqual(status.state, "awaiting-feature-dispositions")
                await self.environment.sleep(2)
                result = await handle.result()

        self.assertEqual(result.state, "blocked")
        self.assertNotEqual(result.state, "rejected", "nobody decided anything")
        self.assertIsNone(result.decision_id)
        self.assertIsNone(result.dispositions)
        self.assertEqual(runtime().ledger.decision_count(), 0)

    # -------------------------------------------------------------- registration

    async def test_the_update_name_is_the_one_the_submission_client_sends(self) -> None:
        """FACT 15. A name mismatch here is unanswerable in practice.

        `tests/test_phase_b_worker_registration.py` pins the registered activity
        and workflow sets and is deliberately not repeated. What no unit test
        there can see is the *name* of the update: `submission.py` submits by
        name, holding no Workflow class, so a rename on either side leaves every
        real gate unanswerable while every test that submits through the typed
        method reference keeps passing. This is the trap this project has hit
        twice, so the name is pinned statically *and* answered over the wire.
        """
        definition = WorkflowDefinition.from_class(FeatureAssessmentRunWorkflow)
        self.assertEqual(set(definition.updates), {"submit_feature_dispositions"})
        self.assertEqual(FEATURE_ASSESSMENT_GATE.update_name, "submit_feature_dispositions")
        self.assertIn(FEATURE_ASSESSMENT_GATE.update_name, definition.updates)
        # The query is submitted by name too, by `read_pending_gate`.
        self.assertEqual(set(definition.queries), {"status"})
        self.assertIn(FeatureAssessmentRunWorkflow, worker_module.REGISTERED_WORKFLOWS)
        self.assertEqual(definition.versioning_behavior, VersioningBehavior.PINNED)

        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            submission = self.submission(gate)
            # By name and by name only, exactly as the client sends it.
            receipt = await handle.execute_update(
                FEATURE_ASSESSMENT_GATE.update_name,
                submission,
                id=submission.decision.idempotency_id,
            )
            self.assertEqual(receipt, {"decision_id": "feature-approval-1", "accepted": True})
            result = await handle.result()

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.dispositions, submission.dispositions)


class _HandleClient:
    """The smallest thing `read_pending_gate` can talk to: a real handle.

    It is not a stub of the query. `read_pending_gate` needs an object with
    `get_workflow_handle`, and the handle it gets back is the live one, so the
    status it parses is the status the Workflow actually published.
    """

    def __init__(self, handle) -> None:
        self._handle = handle
        self.asked: list[str] = []

    def get_workflow_handle(self, workflow_id: str):
        self.asked.append(workflow_id)
        return self._handle


def _failure_chain(error: BaseException) -> str:
    """Every message in a failure's cause chain, flattened.

    Temporal wraps an argument-decoding failure in a second `ApplicationError`,
    so the message that says which check refused a payload is one level down.
    Bounded, because a converted failure chain is server-supplied.
    """
    messages: list[str] = []
    seen: BaseException | None = error
    while seen is not None and len(messages) < 10:
        messages.append(f"{type(seen).__name__}: {seen}")
        seen = getattr(seen, "cause", None) or seen.__cause__
    return "\n".join(messages)


def _unchecked_submission(
    decision: GateDecision, dispositions: ArtifactRef
) -> FeatureGateSubmissionV1:
    """A `FeatureGateSubmissionV1` built past its own `__post_init__`.

    The contract refuses a reference of the wrong kind, which is correct and is
    exactly why the validator's identical clause cannot be reached by any
    ordinary construction. Building one this way is the only way to ask whether
    the validator would refuse it — and the answer matters, because the two
    checks are in different processes' hands: the contract's runs wherever the
    payload is decoded, and the validator's is the Workflow's own statement about
    what may answer its gate.
    """
    submission = object.__new__(FeatureGateSubmissionV1)
    object.__setattr__(submission, "schema_version", 1)
    object.__setattr__(submission, "decision", decision)
    object.__setattr__(submission, "dispositions", dispositions)
    return submission


if __name__ == "__main__":
    unittest.main()
