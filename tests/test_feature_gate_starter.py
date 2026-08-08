"""`assessment_record.raise_gate` — the line that was missing for a fortnight.

`FeatureAssessmentRunWorkflow` was registered, answerable and started by nothing:
`client.start_workflow` for it existed only inside `tests/integration/`, so the
one way to raise it in anger was to copy a line out of a test. That is the
disconnection at the far end from the three this project has recorded, where the
*consumer* was missing — and a gate that can be answered but not asked is not a
working gate.

**Why this is an ordinary test and not an opt-in integration script.**
`WorkflowEnvironment.start_time_skipping()` runs a local test server and exposes
its address as `client.service_client.config.target_host`, so a starter — whose
whole job is `Client.connect(endpoint)` — can be pointed at it. Every other
starter-shaped thing in this repo needs a live Temporal at `localhost:7233` and
is therefore run by hand, which is exactly how one of them came to be missing
without anybody noticing.

The starter is called through `asyncio.to_thread` so the **real** synchronous
function runs, `asyncio.run` and all. Awaiting a private async core instead would
test everything except the thing the command line actually calls.
"""

from __future__ import annotations

import asyncio
import unittest

from temporalio.testing import WorkflowEnvironment

from dfinsta_pipeline import assessment_record

RUN_ID = "port-441-assessment"
TASK_QUEUE = "starter-tests"


class StarterTests(unittest.IsolatedAsyncioTestCase):
    """The starter, against a real server, with no worker running.

    No worker on purpose. A starter's job ends when the Workflow is enqueued with
    the right type, id and argument; whether the first Activity then succeeds is
    the Workflow's business and is covered where the fixtures for it live. Testing
    them together would mean this file could only fail for reasons that already
    have their own tests.
    """

    async def asyncSetUp(self) -> None:
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.addAsyncCleanup(self.environment.shutdown)
        self.endpoint = self.environment.client.service_client.config.target_host

    async def start(self, starter, run_id: str) -> str:
        return await asyncio.to_thread(
            starter, self.endpoint, TASK_QUEUE, run_id, gate_timeout_seconds=600
        )

    async def described(self, workflow_id: str):
        return await self.environment.client.get_workflow_handle(workflow_id).describe()

    async def test_the_feature_gate_starter_enqueues_the_right_workflow(self) -> None:
        """Type, id and task queue — the three ways a starter is silently wrong."""

        workflow_id = await self.start(assessment_record.raise_gate, RUN_ID)

        self.assertEqual(RUN_ID, workflow_id)
        description = await self.described(workflow_id)
        self.assertEqual("FeatureAssessmentRunWorkflow", description.workflow_type)
        self.assertEqual(TASK_QUEUE, description.task_queue)

    async def test_the_workflow_id_is_the_run_id(self) -> None:
        """The property `submission show <workflow_id>` depends on.

        A human answering a gate has one identifier: the run id. If a starter
        minted its own workflow id — a uuid, or a prefixed name — the gate would
        be running and unreachable by the only client that can answer it, and
        every unit test would still pass.
        """

        self.assertEqual(
            "port-442-assessment",
            await self.start(assessment_record.raise_gate, "port-442-assessment"),
        )

    async def test_starting_the_same_run_twice_is_refused(self) -> None:
        """Temporal refuses a duplicate workflow id, and that is the behaviour wanted.

        Two gates open for one run is the state where nobody can say which one a
        human answered. Asserted rather than assumed, because it is Temporal's
        rule and not this code's — and code that later minted a unique id per call
        would silently take the protection away.
        """

        await self.start(assessment_record.raise_gate, "port-443-assessment")
        with self.assertRaises(Exception) as caught:
            await self.start(assessment_record.raise_gate, "port-443-assessment")
        self.assertIn("already", str(caught.exception).lower())


class PinnedStartTests(unittest.IsolatedAsyncioTestCase):
    """What a pinned start needs, learned from a real server rather than a doc.

    The workflow is `versioning_behavior=PINNED`, and the consequences only
    appear against a server:

    1. A `PINNED` workflow may only run on a worker with
       `use_worker_versioning=True` — a plain worker fails every activation with
       *"versioning behavior cannot be specified without deployment options"*.
    2. A versioned worker is dispatched tasks only for its deployment's **current**
       version, which nothing in this project sets. Started with no override, the
       Workflow is accepted, shows as RUNNING in the UI, and is picked up by
       nobody; queries then time out with no error naming the cause.
    3. Started **with** an override, the server refuses until a worker for that
       exact version has polled the queue.

    The first two are why `build_id` exists on the starter; the third is why it
    retries. Both integration harnesses already passed an override and already
    retried, and neither said why — so the knowledge existed only as two lines of
    code nobody had a reason to read.
    """

    async def asyncSetUp(self) -> None:
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.addAsyncCleanup(self.environment.shutdown)
        self.endpoint = self.environment.client.service_client.config.target_host

    async def test_a_pinned_start_with_no_worker_refuses_and_says_what_to_do(self) -> None:
        """The refusal a human meets when they raise before starting the worker.

        **The condition is injected rather than provoked, and that is deliberate.**
        The time-skipping test server accepts a pinned start for a version no
        worker has ever polled; the real dev server refuses it with *"Pinned
        version 'dfinsta-pipeline:<build>' is not present in task queue … of type
        'Workflow'"*. Verified by hand against a live server on 2026-08-08 — the
        in-process one simply does not enforce it, so provoking it here would make
        this test assert a behaviour that cannot occur in the environment it runs
        in.

        What is worth guaranteeing is not Temporal's enforcement but **this
        module's response to it**: retry while a worker might still be starting,
        and then refuse with a message that names the build, the queue and the fix.
        A bare `RPCError` would send a reader to check the run id and the task
        queue, both of which are correct.
        """
        from temporalio.service import RPCError, RPCStatusCode

        refusal = RPCError(
            "Pinned version 'dfinsta-pipeline:never-started' is not present in task "
            "queue 'queue-with-no-worker' of type 'Workflow'",
            RPCStatusCode.NOT_FOUND,
            b"",
        )

        class NeverPolled:
            async def start_workflow(self, *args, **kwargs):
                raise refusal

        async def connect(*args, **kwargs):
            return NeverPolled()

        from temporalio import client as client_module

        real = client_module.Client.connect
        client_module.Client.connect = connect
        self.addCleanup(setattr, client_module.Client, "connect", real)

        with self.assertRaises(RuntimeError) as caught:
            await asyncio.to_thread(
                assessment_record.raise_gate,
                self.endpoint,
                "queue-with-no-worker",
                "port-unwatched-assessment",
                gate_timeout_seconds=600,
                build_id="never-started",
                wait_for_worker_seconds=1.0,
            )
        message = str(caught.exception)
        self.assertIn("never-started", message)
        self.assertIn("queue-with-no-worker", message)
        self.assertIn("--build-id", message)

    async def test_an_unrelated_rpc_error_is_not_retried(self) -> None:
        """The retry is scoped to one message, so a real fault is not swallowed.

        A loop that retried every `RPCError` would turn an unreachable server or a
        rejected namespace into a 30-second wait and then the wrong explanation.
        """
        from temporalio.service import RPCError, RPCStatusCode

        other = RPCError("namespace not found", RPCStatusCode.NOT_FOUND, b"")

        class Broken:
            async def start_workflow(self, *args, **kwargs):
                raise other

        async def connect(*args, **kwargs):
            return Broken()

        from temporalio import client as client_module

        real = client_module.Client.connect
        client_module.Client.connect = connect
        self.addCleanup(setattr, client_module.Client, "connect", real)

        with self.assertRaises(RPCError):
            await asyncio.to_thread(
                assessment_record.raise_gate,
                self.endpoint,
                "q",
                "port-broken-assessment",
                gate_timeout_seconds=600,
                build_id="b",
                wait_for_worker_seconds=30.0,
            )

    async def test_an_unpinned_start_is_accepted_without_a_worker(self) -> None:
        """The other correct configuration, and the reason `build_id` defaults to "".

        With no override the server accepts the start immediately — it will simply
        wait for whatever worker the deployment's current version names. That is
        the right default for an operator who manages versions out of band, and
        the wrong one for anybody who has not, which is what the flag's help says.
        """
        workflow_id = await asyncio.to_thread(
            assessment_record.raise_gate,
            self.endpoint,
            "queue-with-no-worker",
            "port-unpinned-assessment",
            gate_timeout_seconds=600,
        )
        self.assertEqual("port-unpinned-assessment", workflow_id)


if __name__ == "__main__":
    unittest.main()
