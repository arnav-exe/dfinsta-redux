"""The retirement gate, end to end, on a real Temporal environment.

`tests/test_retirement.py` covers what a case is and `tests/test_retirement_gate.py`
covers the wire contracts. This file exists for the join, because the join is what
this project keeps getting wrong: three separate times a gate has been shipped
complete at one end and reaching nothing at the other —
`the-gates-rulings-have-no-consumer`, `nothing-computes-a-stage-4a-assessment`,
`the-post-build-gate-cannot-be-satisfied`.

So the central test here runs the whole chain and then asks the *consumer* whether
anything happened:

    record a docket  →  raise the Workflow  →  a human answers through the client's
    own submission shape  →  the Activity admits  →  publish  →
    `expectation.retired_by` stops expecting the hook, at the right version

A test that stopped at "the Workflow returned completed" would have passed for
every one of those three failures.

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

from dfinsta_pipeline import activities, expectation, retirement_record
from dfinsta_pipeline.activities import (
    admit_retirement_rulings_activity,
    configure_runtime,
    prepare_retirement_gate_activity,
)
from dfinsta_pipeline.contracts import GateDecision, canonical_json
from dfinsta_pipeline.retirement import RetirementCase, case_sha256
from dfinsta_pipeline.retirement_gate import (
    RULINGS_ARTIFACT_KIND,
    RetirementGateSubmissionV1,
    RetirementRulingsV1,
    RetirementRulingV1,
    RetirementRunRequestV1,
    derive_retirement_gate_request,
)
from dfinsta_pipeline.retirement_workflow import HookRetirementRunWorkflow

RUN_ID = "retire-441"
ACTOR = "arnav"
GATE_TIMEOUT_SECONDS = 3600
DEAD = "replace_reels_discover_endpoint"
ALIVE = "set_app_context"
TEST_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-test", "retirement-1")

BUILD = "b" * 64


def _claim(hook: str, kind: str, version: str, verdict: str, detail: dict) -> str:
    row = {
        "actor": "tests",
        "confidence": None,
        "decision_id": None,
        "detail": detail,
        "hook_id": hook,
        "kind": kind,
        # Per kind, because the ledger enforces it: `runtime_probe` and
        # `differential` may only come from `device`, and the whole reason that
        # kind exists is that its producer is independent of the proposer.
        "producer": "deterministic" if kind == "static_verified" else "device",
        "rationale": "",
        "recorded_at": f"2026-08-0{version[-1]}T00:00:00+00:00",
        "schema_version": 1,
        "summary": f"{hook} {kind}",
        "supersedes": None,
        "verdict": verdict,
        "version": version,
    }
    if kind == "static_verified":
        row["build_sha256"] = BUILD
    return json.dumps(row, sort_keys=True)


class RetirementGateChainTests(unittest.IsolatedAsyncioTestCase):
    """The whole chain, once, plus the refusals that must not reach the consumer."""

    async def asyncSetUp(self) -> None:
        previous = getattr(activities, "_runtime", None)
        self.addCleanup(setattr, activities, "_runtime", previous)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.state = self.root / "state"
        self.manifest = self.root / "manifest"
        (self.manifest / "static_evidence").mkdir(parents=True)
        (self.manifest / "runtime_evidence").mkdir(parents=True)
        (self.manifest / "differentials").mkdir(parents=True)

        (self.manifest / "hooks.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_revision": "2026-08-01",
                    "hooks": [
                        {"hook_id": ALIVE, "intent": "set the app context",
                         "tier": "robust", "status": "active"},
                        {"hook_id": DEAD, "intent": "block the discover endpoint",
                         "tier": "fragile", "status": "active"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # ALIVE is release-ready on both versions; DEAD is measured and never
        # passes, which is what makes it a candidate and not a regression.
        for version in ("440", "441"):
            (self.manifest / "static_evidence" / f"{version}.jsonl").write_text(
                "\n".join(
                    _claim(hook, "static_verified", version, "passed",
                           {"attribution": "sole", "build_verification_passed": True})
                    for hook in (ALIVE, DEAD)
                ) + "\n",
                encoding="utf-8",
            )
            (self.manifest / "runtime_evidence" / f"{version}.jsonl").write_text(
                "\n".join(
                    [
                        _claim(ALIVE, "runtime_probe", version, "passed",
                               {"hooks_that_ran": [ALIVE]}),
                        _claim(DEAD, "runtime_probe", version, "inconclusive",
                               {"hooks_that_ran": []}),
                    ]
                ) + "\n",
                encoding="utf-8",
            )
        (self.manifest / "differentials" / "440-441.jsonl").write_text(
            "\n".join(
                [
                    _claim(ALIVE, "differential", "441", "passed",
                           {"baseline_version": "440"}),
                    _claim(DEAD, "differential", "441", "inconclusive",
                           {"baseline_version": "440", "reason": "baseline_not_a_pass"}),
                ]
            ) + "\n",
            encoding="utf-8",
        )

        (self.root / "investigations.json").write_text(
            json.dumps(
                {
                    DEAD: {
                        "investigated_by": "claude-opus-5",
                        "summary": "Instagram removed the discover surface in 441.",
                        "findings": ["the anchor now matches a dead code path"],
                        "recommendation": "retire",
                    }
                }
            ),
            encoding="utf-8",
        )

        self.recorded = retirement_record.record(
            self.state,
            run_id=RUN_ID,
            version="441",
            investigations_path=self.root / "investigations.json",
            allowed_actor=ACTOR,
            owner_token="owner-1",
            root=self.root,
        )
        configure_runtime(self.state)
        self.environment = await WorkflowEnvironment.start_time_skipping()
        self.addAsyncCleanup(self.environment.shutdown)
        self.task_queue = "retirement-tests"

    def worker(self) -> Worker:
        return Worker(
            self.environment.client,
            task_queue=self.task_queue,
            workflows=[HookRetirementRunWorkflow],
            activities=[prepare_retirement_gate_activity, admit_retirement_rulings_activity],
            max_cached_workflows=0,
            deployment_config=WorkerDeploymentConfig(
                version=TEST_DEPLOYMENT_VERSION,
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
            ),
        )

    async def start(self, *, gate_timeout_seconds: int = GATE_TIMEOUT_SECONDS):
        return await self.environment.client.start_workflow(
            HookRetirementRunWorkflow.run,
            RetirementRunRequestV1(1, RUN_ID, gate_timeout_seconds),
            id=RUN_ID,
            task_queue=self.task_queue,
            versioning_override=PinnedVersioningOverride(TEST_DEPLOYMENT_VERSION),
        )

    async def wait_for_gate(self, handle):
        for _ in range(400):
            status = await handle.query(HookRetirementRunWorkflow.status)
            if status.state == "awaiting-retirement-rulings":
                assert status.gate is not None
                return status.gate
            await asyncio.sleep(0.01)
        self.fail("Workflow never reached the retirement gate")

    def answer(self, gate, *, verdict="retire", actor=ACTOR, rationale="Surface removed in 441.",
               hooks=None, decision="approve"):
        """A submission built exactly as the client would build one."""
        store = activities.runtime().store
        cases = {
            case["hook_id"]: case_sha256(RetirementCase.from_dict(case))
            for case in self.recorded.document["cases"]
        }
        chosen = self.recorded.hook_ids if hooks is None else hooks
        document = RetirementRulingsV1(
            1,
            self.recorded.docket.sha256,
            self.recorded.version,
            self.recorded.policy_revision,
            tuple(
                RetirementRulingV1(1, hook, verdict, rationale, cases[hook])
                for hook in chosen
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
        return RetirementGateSubmissionV1(1, gate_decision, reference)

    # ------------------------------------------------------------------ the join

    async def test_an_approved_retirement_reaches_the_expectation(self) -> None:
        """The whole point. Anything short of this has been shipped broken before.

        Not "the Workflow completed" — that was true of every disconnected gate
        this project has built. The assertion is that `expectation`, which is what
        actually gates a release, stops expecting the hook, and does so at the
        version *after* the one the case was built from.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            receipt = await handle.execute_update(
                HookRetirementRunWorkflow.submit_retirement_rulings, self.answer(gate)
            )
            self.assertTrue(receipt.accepted)
            result = await handle.result()

        self.assertEqual("completed", result.state)
        self.assertIsNotNone(result.rulings)

        retired = retirement_record.publish_admitted(self.state, RUN_ID, root=self.root)
        self.assertEqual([DEAD], retired)

        recorded = expectation.read_retirements(self.root)
        self.assertEqual([DEAD], sorted(recorded))
        # Derived, not chosen: the case was built from 441, so 441 is untouched.
        self.assertEqual("442", recorded[DEAD].effective_from)
        self.assertEqual({}, expectation.retired_by("441", recorded))
        self.assertEqual([DEAD], sorted(expectation.retired_by("442", recorded)))
        # The row points back at the decision a human signed, not at an id this
        # pipeline minted for itself afterwards.
        self.assertEqual(
            recorded[DEAD].decision_id,
            retirement_record.admitted_rulings(
                activities.runtime().ledger, activities.runtime().store, RUN_ID
            )[1],
        )
        self.assertEqual(ACTOR, recorded[DEAD].ruled_by)

    async def test_a_keep_admits_and_writes_nothing(self) -> None:
        """`keep` is an answer, and the file's only meaning is "no longer expected"."""
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            await handle.execute_update(
                HookRetirementRunWorkflow.submit_retirement_rulings,
                self.answer(gate, verdict="keep", rationale="Dormant, not gone."),
            )
            result = await handle.result()

        self.assertEqual("completed", result.state)
        self.assertEqual([], retirement_record.publish_admitted(self.state, RUN_ID, root=self.root))
        self.assertEqual({}, expectation.read_retirements(self.root))

    async def test_an_unanswered_gate_blocks_and_retires_nothing(self) -> None:
        """Timeout is `blocked`, never an implicit approval.

        For this gate the distinction is the whole design: a question nobody got
        round to must leave every hook still expected. A gate that defaulted to
        approval would retire hooks by inattention.
        """
        async with self.worker():
            handle = await self.start(gate_timeout_seconds=60)
            await self.wait_for_gate(handle)
            await self.environment.sleep(timedelta(seconds=61))
            result = await handle.result()

        self.assertEqual("blocked", result.state)
        self.assertIsNone(result.decision_id)
        with self.assertRaises(ValueError):
            retirement_record.publish_admitted(self.state, RUN_ID, root=self.root)
        self.assertEqual({}, expectation.read_retirements(self.root))

    async def test_a_rejected_gate_retires_nothing(self) -> None:
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            await handle.execute_update(
                HookRetirementRunWorkflow.submit_retirement_rulings,
                self.answer(gate, decision="reject", rationale="Not yet."),
            )
            result = await handle.result()

        self.assertEqual("rejected", result.state)
        with self.assertRaises(ValueError):
            retirement_record.publish_admitted(self.state, RUN_ID, root=self.root)

    async def test_an_unauthorized_actor_is_refused(self) -> None:
        """Checked by the sandbox filter AND by the Activity, not by one of them."""
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            with self.assertRaises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    HookRetirementRunWorkflow.submit_retirement_rulings,
                    self.answer(gate, actor="intruder"),
                )

    async def test_a_docket_hook_left_unruled_is_refused(self) -> None:
        """Silence is not a `keep`. The Activity is what catches this.

        The sandbox validator cannot: reading the rulings needs the content store.
        So this is the clause that proves the authority does more than the filter
        rather than merely as much.
        """
        async with self.worker():
            handle = await self.start()
            gate = await self.wait_for_gate(handle)
            with self.assertRaises(Exception) as caught:
                await handle.execute_update(
                    HookRetirementRunWorkflow.submit_retirement_rulings,
                    self.answer(gate, hooks=()),
                )
        self.assertNotIn("completed", str(caught.exception))
        self.assertEqual({}, expectation.read_retirements(self.root))


if __name__ == "__main__":
    unittest.main()
