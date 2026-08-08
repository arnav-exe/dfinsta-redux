"""The reversal gate, end to end, on a real Temporal environment.

`tests/test_reversal_gate.py` covers the wire contracts and
`tests/test_reversal_record.py` covers the producer and the consumer. This file
exists for the join, because the join is what this project keeps getting wrong:
four separate times a gate has been shipped complete at one end and reaching
nothing at the other — `the-gates-rulings-have-no-consumer`,
`nothing-computes-a-stage-4a-assessment`, `the-post-build-gate-cannot-be-satisfied`,
`the-feature-gate-has-no-producer`.

So the central test runs the whole chain and then asks the *consumer* whether
anything happened:

    record a docket  →  raise the Workflow  →  a human answers through the
    client's own submission shape  →  the Activity admits  →  publish  →
    `reversal.withdrawn` finds the row and `manifest/hooks.json` no longer
    declares the endpoint

A test that stopped at "the Workflow returned completed" would have passed for
every one of those four failures.

`WorkflowEnvironment.start_time_skipping()` runs a local test server, so nothing
here needs an externally provisioned Temporal. The Activities are the **real**
ones against a real SQLite ledger and a real content store in a temp directory —
stubbing them would test the Workflow's control flow and none of the authority
that matters.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio.client import WorkflowUpdateFailedError
from temporalio.common import PinnedVersioningOverride, VersioningBehavior, WorkerDeploymentVersion
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, WorkerDeploymentConfig

from dfinsta_pipeline import activities, reversal, reversal_record
from dfinsta_pipeline.activities import (
    admit_reversal_rulings_activity,
    configure_runtime,
    prepare_reversal_gate_activity,
)
from dfinsta_pipeline.contracts import GateDecision, canonical_json
from dfinsta_pipeline.reversal_gate import (
    RULINGS_ARTIFACT_KIND,
    ReversalGateSubmissionV1,
    ReversalRulingsAdmissionV1,
    ReversalRulingsV1,
    ReversalRulingV1,
    ReversalRunRequestV1,
    derive_reversal_gate_request,
)
from dfinsta_pipeline.reversal_workflow import ReversalRunWorkflow
from tests.test_reversal_record import (
    ACTOR,
    BLOCK_DECISION,
    EXPLORE,
    FEED,
    LIVING,
    RETIRE_DECISION,
    RUN_ID,
    STAMP,
    VERSION,
    write_reversal_fixture,
)

GATE_TIMEOUT_SECONDS = 3600
TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-test", "reversal-1")


def why(error: BaseException) -> str:
    """The whole cause chain as text.

    Temporal wraps a refusal twice — `WorkflowFailureError("Workflow execution
    failed")` over an `ApplicationError` carrying the message that matters — so
    asserting on `str(error)` alone would pass for *any* failure, including one
    caused by the fixture rather than by the clause under test.
    """

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = getattr(current, "cause", None) or current.__cause__
    return " | ".join(parts)


class ReversalGateChainTests(unittest.IsolatedAsyncioTestCase):
    """The whole chain, once, plus the refusals that must not reach the consumer."""

    async def asyncSetUp(self) -> None:
        previous = getattr(activities, "_runtime", None)
        self.addCleanup(setattr, activities, "_runtime", previous)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        write_reversal_fixture(self.root)
        self.state = self.root / "state"
        self.manifest = self.root / "manifest" / "hooks.json"

        self.recorded = reversal_record.record(
            self.state,
            run_id=RUN_ID,
            version=VERSION,
            allowed_actor=ACTOR,
            owner_token="owner-1",
            root=self.root,
        )
        configure_runtime(self.state)
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.addAsyncCleanup(self.environment.shutdown)
        self.task_queue = "reversal-tests"

    def worker(self) -> Worker:
        return Worker(
            self.environment.client,
            task_queue=self.task_queue,
            workflows=[ReversalRunWorkflow],
            activities=[prepare_reversal_gate_activity, admit_reversal_rulings_activity],
            max_cached_workflows=0,
            deployment_config=WorkerDeploymentConfig(
                version=TEST_DEPLOYMENT_VERSION,
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
            ),
        )

    async def start(self, *, gate_timeout_seconds: int = GATE_TIMEOUT_SECONDS):
        return await self.environment.client.start_workflow(
            ReversalRunWorkflow.run,
            ReversalRunRequestV1(1, RUN_ID, gate_timeout_seconds),
            id=RUN_ID,
            task_queue=self.task_queue,
            versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
        )

    async def wait_for_gate(self, handle):
        for _ in range(400):
            status = await handle.query(ReversalRunWorkflow.status)
            if status.state == "awaiting-reversal-rulings":
                assert status.gate is not None
                return status.gate
            await asyncio.sleep(0.01)
        self.fail("Workflow never reached the reversal gate")

    def answer(
        self,
        gate,
        *,
        verdict="withdraw",
        actor=ACTOR,
        rationale="The evidence no longer supports it.",
        items=None,
        decision="approve",
        digests=None,
    ):
        """A submission built exactly as the client would build one."""
        store = activities.runtime().store
        chosen = self.recorded.items if items is None else items
        digests = digests or {i.item_id: i.item_sha256 for i in self.recorded.items}
        document = ReversalRulingsV1(
            1,
            self.recorded.docket.sha256,
            self.recorded.version,
            self.recorded.policy_revision,
            tuple(
                ReversalRulingV1(1, item.item_id, verdict, rationale, digests[item.item_id])
                for item in chosen
            ),
        )
        body = canonical_json(document.to_dict()).encode("utf-8")
        reference = store.put_bytes(
            kind=RULINGS_ARTIFACT_KIND,
            data=body,
            producer_operation_id=f"client-{document.sha256}",
            input_hashes=(self.recorded.docket.sha256,),
        )
        subject = gate.subject_sha256
        gate_decision = GateDecision(
            schema_version=1,
            decision_id=f"decision-{document.sha256[:16]}",
            idempotency_id=f"idempotency-{document.sha256[:16]}",
            actor=actor,
            run_id=RUN_ID,
            gate_id=gate.gate_id,
            subject_sha256=subject,
            admission_sha256=subject,
            prepared_sha256=subject,
            policy_revision=gate.policy_revision,
            decision=decision,
            rationale=rationale,
            issued_at=datetime.now(timezone.utc).isoformat(),
        )
        return ReversalGateSubmissionV1(1, gate_decision, reference)

    def publish(self, **overrides):
        arguments = {"recorded_at": STAMP, "confirm": True, "root": self.root}
        arguments.update(overrides)
        return reversal_record.publish_admitted(self.state, RUN_ID, **arguments)

    # ------------------------------------------------------------------ the join

    async def test_an_approved_withdrawal_reaches_the_record_and_the_manifest(self) -> None:
        """The whole point. Anything short of this has been shipped broken before.

        Not "the Workflow completed" — that was true of every disconnected gate
        this project has built. The assertions are that the permanent record gains
        the withdrawal rows and that the app's manifest stops declaring the
        endpoints, which is what actually changes what the build blocks.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            receipt = await handle.execute_update(
                ReversalRunWorkflow.submit_reversal_rulings, self.answer(gate)
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual("completed", result.state)
        self.assertIsNotNone(result.rulings)

        published = self.publish()
        self.assertEqual({FEED, EXPLORE, LIVING}, set(published.withdrawn))

        blocks = reversal.withdrawn("block", self.root)
        self.assertEqual({(BLOCK_DECISION, FEED), (BLOCK_DECISION, EXPLORE)}, set(blocks))
        # The row points back at the decision a human signed, not at an id this
        # pipeline minted for itself afterwards.
        self.assertEqual(
            {result.decision_id}, {row.decision_id for row in blocks.values()}
        )
        self.assertEqual({ACTOR}, {row.ruled_by for row in blocks.values()})

        retirements = reversal.withdrawn("retirement", self.root)
        # Derived, never chosen: the version AFTER the port the docket was built
        # from, so a hook cannot be restored into a port already assessed.
        self.assertEqual("442", retirements[(RETIRE_DECISION, LIVING)].effective_from)

        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            [], [d for h in document["hooks"] for d in h.get("semantic_deps") or ()]
        )

    async def test_a_keep_admits_and_withdraws_nothing(self) -> None:
        """`keep` is an answer: the questioned decision stands."""
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            await handle.execute_update(
                ReversalRunWorkflow.submit_reversal_rulings,
                self.answer(gate, verdict="keep", rationale="Dormant, not gone."),
            )
            result = await handle.result()

        self.assertEqual("completed", result.state)
        published = self.publish()
        self.assertEqual((), published.withdrawn)
        self.assertEqual(3, len(published.kept))
        self.assertEqual([], reversal.read_reversals(self.root))
        self.assertIn(
            f"/{FEED}",
            json.loads(self.manifest.read_text(encoding="utf-8"))["hooks"][0]["semantic_deps"],
        )

    async def test_an_unanswered_gate_blocks_and_withdraws_nothing(self) -> None:
        """Timeout is `blocked`, never an implicit approval.

        A gate that defaulted to approval would un-block endpoints by
        inattention, which is the opposite of what every recorded decision here
        was for.
        """
        async with self.worker():
            handle = await self.start(gate_timeout_seconds=60)
            await self.wait_for_gate(handle)
            await self.environment.sleep(timedelta(seconds=61))
            result = await handle.result()

        self.assertEqual("blocked", result.state)
        self.assertIsNone(result.decision_id)
        # Refused, not "nothing to withdraw": nobody has answered, and reporting
        # that as a clean publish is this project's most repeated defect.
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual([], reversal.read_reversals(self.root))

    async def test_a_rejected_or_deferred_gate_withdraws_nothing(self) -> None:
        """Three states, and `deferred` is not `rejected`.

        The Workflow must *complete* on a `defer` with `state="deferred"` and
        admit nothing — not fail. A `defer` that reached the Activity would be
        refused there and take the whole run down with it, which reads in the UI
        as a broken gate rather than as a human saying "not yet".
        """
        for decision, expected in (("reject", "rejected"), ("defer", "deferred")):
            with self.subTest(decision=decision):
                await self.asyncSetUp()
                async with self.worker():
                    handle = await self.start()
                    gate = await self.wait_for_gate(handle)
                    await handle.execute_update(
                        ReversalRunWorkflow.submit_reversal_rulings,
                        self.answer(gate, decision=decision, rationale="Not yet."),
                    )
                    result = await handle.result()

                self.assertEqual(expected, result.state)
                self.assertIsNone(result.rulings)
                self.assertIsNotNone(result.decision_id)
                with self.assertRaises(ValueError):
                    self.publish()

    async def test_an_unauthorized_actor_is_refused(self) -> None:
        """Checked by the sandbox filter AND by the Activity, not by one of them."""
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            with self.assertRaises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    ReversalRunWorkflow.submit_reversal_rulings,
                    self.answer(gate, actor="intruder"),
                )

    async def test_a_docket_item_left_unruled_is_refused(self) -> None:
        """Silence is not a `keep`. The Activity is what catches this.

        The sandbox validator cannot: reading the rulings needs the content store.
        So this is the clause that proves the authority does more than the filter
        rather than merely as much.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            # Either call may be the one that raises, and which it is depends on
            # whether the update result is delivered before the run fails —
            # a race, not a contract. What IS the contract is that the run does
            # not complete and nothing is admitted, so both are inside the guard.
            with self.assertRaises(Exception) as caught:
                await handle.execute_update(
                    ReversalRunWorkflow.submit_reversal_rulings,
                    self.answer(gate, items=self.recorded.items[:1]),
                )
                await handle.result()
        self.assertIn("No ruling for", why(caught.exception))
        self.assertEqual([], reversal.read_reversals(self.root))
        with self.assertRaises(ValueError):
            self.publish()

    async def test_a_ruling_that_answers_a_different_item_is_refused(self) -> None:
        """Also authority-only, and this gate has it where the retirement gate
        does not: the digest is what the permanent record names as the evidence."""
        swapped = {
            self.recorded.items[0].item_id: self.recorded.items[1].item_sha256,
            self.recorded.items[1].item_id: self.recorded.items[0].item_sha256,
            self.recorded.items[2].item_id: self.recorded.items[2].item_sha256,
        }
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            with self.assertRaises(Exception) as caught:
                await handle.execute_update(
                    ReversalRunWorkflow.submit_reversal_rulings,
                    self.answer(gate, digests=swapped),
                )
                await handle.result()
        self.assertIn("answers docket item", why(caught.exception))
        self.assertEqual([], reversal.read_reversals(self.root))
        with self.assertRaises(ValueError):
            self.publish()

    async def test_the_admitting_activity_refuses_a_decision_that_is_not_an_approval(
        self,
    ) -> None:
        """Reached directly, because an Activity is reachable independently of the
        Workflow that normally calls it.

        `validate_submission` deliberately does not judge the verdict, so a
        `defer` handed straight to this function would otherwise be admitted as an
        approval and published as a withdrawal.
        """
        request = derive_reversal_gate_request(
            self.recorded.run_id,
            self.recorded.docket,
            self.recorded.version,
            self.recorded.policy_revision,
            self.recorded.allowed_actor,
            self.recorded.items,
        )

        class _Gate:
            subject_sha256 = request.sha256
            gate_id = request.gate_id
            policy_revision = request.policy_revision

        submission = self.answer(_Gate, decision="defer")
        with self.assertRaises(Exception) as caught:
            await admit_reversal_rulings_activity(
                ReversalRulingsAdmissionV1(1, RUN_ID, submission)
            )
        self.assertIn("not approved", why(caught.exception))
        with self.assertRaises(ValueError):
            self.publish()

    async def test_the_workflow_is_registered_and_pinned(self) -> None:
        """A gate hosted by no worker is a gate nobody can answer."""
        from dfinsta_pipeline import worker as worker_module

        self.assertIn(ReversalRunWorkflow, worker_module.REGISTERED_WORKFLOWS)
        self.assertIn(
            prepare_reversal_gate_activity, worker_module.REGISTERED_ACTIVITIES
        )
        self.assertIn(admit_reversal_rulings_activity, worker_module.REGISTERED_ACTIVITIES)


class StarterTests(unittest.TestCase):
    """The starter exists and names this Workflow. Nothing else raises this gate."""

    def starter_source(self) -> str:
        """Read relative to this file, never to the process CWD.

        `Path.resolve` is called inside a method rather than at module scope: the
        Temporal sandbox restricts it and re-imports any module defining a
        Workflow class, and a module-level `resolve()` on that import path has
        cost this project a whole test file before.
        """
        return (
            Path(__file__).resolve().parents[1]
            / "src/dfinsta_pipeline/reversal_record.py"
        ).read_text(encoding="utf-8")

    def test_the_module_starts_its_own_workflow(self) -> None:
        """A gate with no starter is raisable by hand and by nothing else, which
        is the disconnection at the far end from the one `publish_admitted`
        closes. `FeatureAssessmentRunWorkflow` shipped in exactly that state."""
        source = self.starter_source()
        self.assertIn("ReversalRunWorkflow.run", source)
        self.assertIn("start_workflow", source)

    def test_the_starter_waits_for_a_worker_to_poll(self) -> None:
        """A pinned start is refused until a worker for that exact deployment
        version has polled the queue, and raising a gate right after starting a
        worker is the normal order of operations."""
        source = self.starter_source()
        self.assertIn("not present in task queue", source)
        self.assertIn("wait_for_worker_seconds", source)
        self.assertIn("PinnedVersioningOverride", source)

    def test_the_no_worker_case_is_a_refusal_and_names_the_cause(self) -> None:
        """The failure this project has actually shipped, exercised.

        A pinned start against a server with no worker on that deployment version
        is accepted-then-never-dispatched, and every later query times out with no
        error that names why. So the starter retries and then **refuses**. Driven
        with a fake client rather than a server: what is under test is the retry
        loop and the exception it converts, not Temporal.

        The source-level assertions above cannot reach this — they would pass
        against a loop that raised a bare `RuntimeError`, which is exactly the
        state the sibling `retirement_record.raise_gate` is in.
        """
        import temporalio.client
        from temporalio.service import RPCError, RPCStatusCode

        from dfinsta_pipeline.reversal_record import RecordError

        class _Client:
            async def start_workflow(self, *args, **kwargs):
                raise RPCError(
                    "Pinned version 'dfinsta-pipeline:b1' is not present in task "
                    "queue 'dfinsta' of type 'Workflow'",
                    RPCStatusCode.FAILED_PRECONDITION,
                    b"",
                )

        async def _connect(*args, **kwargs):
            return _Client()

        original = temporalio.client.Client.connect
        temporalio.client.Client.connect = _connect
        self.addCleanup(setattr, temporalio.client.Client, "connect", original)

        with self.assertRaises(RecordError) as caught:
            reversal_record.raise_gate(
                "localhost:7233",
                "dfinsta",
                "reconsider-441",
                gate_timeout_seconds=60,
                build_id="b1",
                wait_for_worker_seconds=0.0,
            )
        message = str(caught.exception)
        self.assertIn("no worker for dfinsta-pipeline:b1", message)
        self.assertIn("--build-id", message)

    def test_an_unreachable_server_is_a_refusal_too(self) -> None:
        """The other half, and the one whose exception type had to be measured:
        the Rust bridge reports a refused connection as a bare `RuntimeError`."""
        from dfinsta_pipeline.reversal_record import RecordError

        with self.assertRaises(RecordError) as caught:
            reversal_record.raise_gate(
                "127.0.0.1:1",
                "dfinsta",
                "reconsider-441",
                gate_timeout_seconds=60,
                wait_for_worker_seconds=0.0,
            )
        self.assertIn("could not reach a Temporal server", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
