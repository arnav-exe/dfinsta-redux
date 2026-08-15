"""Regenerate the committed replay-History corpus under `tests/histories/`.

Run by hand, never from the test suite. A test that regenerated the corpus it
replays would have destroyed the only thing the corpus is for: `tests/histories/`
is a record of shapes that were *already durably recorded*, and the replay tests
only bite while those bytes stay still. (A test writing into committed evidence
is also a mistake this project has made once already, at the cost of 36
fabricated rows.)

    PYTHONPATH=src .venv/bin/python tools/capture_history_corpus.py

Everything is generated against `WorkflowEnvironment.start_time_skipping()`,
which runs a local test server and needs nothing external -- no dev server, no
network, no APK, no device. The Activities are the real ones wherever a real one
exists: the two gate Workflows run against a real SQLite ledger and a real
content store in a temp directory holding a real recorded assessment and a real
recorded docket, so the payloads in the fixtures are the payloads production
would record. `ReplayRunWorkflow` is the exception and uses
`tests/test_phase_b_replay_workflow.py`'s stubs, because its real Activities run
apktool over a decoded APK for the better part of an hour; the stubs return the
same contract types, so the command stream is the same one a real port records.

The setups are the ones the behavioural tests already stand up, imported rather
than reinvented, so a change to what a docket or an assessment looks like arrives
here as a failed capture rather than as a corpus that quietly describes something
the pipeline no longer produces.

===============================================================================
  WHAT IS REWRITTEN AFTER CAPTURE
===============================================================================

Every protobuf field named `identity`, and nothing else. Temporal defaults client
and worker identity to `pid@hostname`; the identity is set explicitly here as
well, but the rewrite is what makes the guarantee, because it also catches an
identity the SDK starts recording somewhere new. `tests/history_corpus.py`
documents what is deliberately left alone and why, and `leaks()` -- run over
every fixture before it is written, and again by the test suite -- is what makes
the claim falsifiable.

A capture that still contains an absolute path, a temp-directory name or a
`pid@host` identity is **refused** rather than written. That is the point at
which a leak is cheap to fix.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# `tools/` is not a package and this script is run by path, so sys.path[0] is
# `tools/`. The corpus registry, the fixture setups and the Workflows all live
# above it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from google.protobuf.message import Message  # noqa: E402
from temporalio.client import Client, WorkflowHandle  # noqa: E402
from temporalio.common import (  # noqa: E402
    PinnedVersioningOverride,
    VersioningBehavior,
    WorkerDeploymentVersion,
)
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker, WorkerDeploymentConfig  # noqa: E402

from dfinsta_pipeline import activities, assessment_record  # noqa: E402
from dfinsta_pipeline.activities import (  # noqa: E402
    admit_feature_dispositions_activity,
    configure_runtime,
    prepare_feature_gate_activity,
    runtime,
)
from dfinsta_pipeline.contracts import GateDecision, canonical_json  # noqa: E402
from dfinsta_pipeline.feature_gate import (  # noqa: E402
    DISPOSITIONS_ARTIFACT_KIND,
    FeatureDispositionsV1,
    FeatureDispositionV1,
    FeatureGateSubmissionV1,
    FeatureRunRequestV1,
)
from dfinsta_pipeline.feature_workflow import FeatureAssessmentRunWorkflow  # noqa: E402
from dfinsta_pipeline.replay_contracts import REPLAY_STAGE_ORDER  # noqa: E402
from dfinsta_pipeline.replay_workflow import ReplayRunWorkflow  # noqa: E402

from tests.history_corpus import CAPTURE_IDENTITY, FIXTURES, histories_directory, leaks  # noqa: E402
from tests.test_assessment import write_manifest  # noqa: E402
from tests.test_assessment_record import write_fake_index  # noqa: E402
from tests.test_phase_b_replay_workflow import (  # noqa: E402
    ReplayStubs,
    replay_request,
    verification_decision,
)


#: One deployment version for the whole corpus. It lands in History and is not
#: machine-specific; naming it after the corpus rather than after a test file
#: keeps a fixture from looking like it came from a particular test run.
CORPUS_DEPLOYMENT_VERSION = WorkerDeploymentVersion("dfinsta-pipeline-corpus", "history-corpus-v1")

#: A queue per capture rather than one for the corpus; see `_task_queue`.
TASK_QUEUE_PREFIX = "capture"

#: Neither a person nor a machine. The behavioural tests use `arnav` and
#: `sam.operator`; a committed fixture should name neither.
ACTOR = "operator"

#: Fixed, so no human's words are pinned into a replayable log for ever. A
#: rationale is required by the contract, so it cannot simply be blank.
RATIONALE = "fixture: recorded for the replay-History corpus"

#: A week. Long enough that the open fixtures record the multi-day wait the gates
#: exist for, and inside every contract's ceiling.
GATE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60


def _task_queue(run_id: str) -> str:
    """A queue per capture, named so it does not contain the run id.

    Two reasons, and the second is the surprising one. Time skipping is global to
    the environment, so awaiting a later capture's result fires the gate timer of
    an earlier open one; sharing a queue would deliver that woken execution to
    whichever worker is polling and fail it as an unregistered Workflow type.
    (Already-fetched fixtures are unaffected -- every open History is fetched
    before any later capture skips time -- but a tool that prints a wall of
    unrelated errors teaches its reader to ignore them.)

    And the task queue name is recorded in *plaintext* in History, while the run
    id travels only inside base64 payloads. `test_the_search_surface_is_not_empty`
    asserts exactly that asymmetry to prove the leak scan really decodes payloads
    rather than grepping JSON. A queue named after the run would make that control
    pass for the wrong reason -- and it did, which is how this comment exists.
    """

    return f"{TASK_QUEUE_PREFIX}-{run_id.removeprefix('corpus-')}"


def _sanitise(message: Message) -> int:
    """Rewrite every string field named `identity`, recursively. Returns a count.

    Reflection rather than a list of known attribute types: `identity` appears on
    the execution-started event, on every workflow- and activity-task-started
    event and inside update metadata, and a Temporal version that records it
    somewhere new would otherwise reintroduce `pid@hostname` silently.
    """

    rewritten = 0
    for field, value in message.ListFields():
        # Dispatched on the *value*, not on the descriptor: `FieldDescriptor.label`
        # was removed in the protobuf 5 upb implementation, and the shapes below
        # are stable across every version this project could be built against.
        if isinstance(value, Message):
            rewritten += _sanitise(value)
        elif hasattr(value, "values") and callable(value.values):  # map<..., Message>
            for item in value.values():
                if isinstance(item, Message):
                    rewritten += _sanitise(item)
        elif isinstance(value, (list, tuple)) or (
            hasattr(value, "__iter__") and not isinstance(value, (str, bytes))
        ):
            for item in value:
                if isinstance(item, Message):
                    rewritten += _sanitise(item)
        elif field.name == "identity" and isinstance(value, str):
            setattr(message, field.name, CAPTURE_IDENTITY)
            rewritten += 1
    return rewritten


async def _history_json(handle: WorkflowHandle) -> str:
    """Fetch, sanitise, and render exactly as `WorkflowHistory.to_json()` does.

    Sanitising the protobuf and re-rendering, rather than editing the JSON text,
    so the committed bytes are byte-for-byte what Temporal's own serialiser
    produces -- one reader (`WorkflowHistory.from_json`) works for every fixture,
    including the one captured before this tool existed.
    """

    history = await handle.fetch_history()
    rewritten = sum(_sanitise(event) for event in history.events)
    if rewritten == 0:
        raise SystemExit("No identity field was rewritten; the sanitiser found nothing to do")
    return history.to_json()


def _worker(client: Client, task_queue: str, workflows: list, activities_: list) -> Worker:
    """Every capture's worker: no sticky cache, pinned version, fixed identity.

    `max_cached_workflows=0` for the reason the behavioural tests use it -- every
    workflow task then reconstructs from History, so the recorded stream is one
    that really replays rather than one a cached instance short-circuited.
    """

    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities_,
        max_cached_workflows=0,
        identity=CAPTURE_IDENTITY,
        deployment_config=WorkerDeploymentConfig(
            version=CORPUS_DEPLOYMENT_VERSION,
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
        ),
    )


async def _wait_for_state(environment: WorkflowEnvironment, handle, query, expected: str):
    """Poll the Workflow's own `status` query until it parks where we want it."""

    for _ in range(400):
        status = await handle.query(query)
        if status.state == expected:
            return status
        await environment.sleep(0.01)
    raise SystemExit(f"Workflow never reached {expected}")


def _decision(gate, *, decision_id: str, gate_id: str | None = None) -> GateDecision:
    """A decision that binds the published gate, stamped at the gate's own clock.

    `issued_at` is the gate's issue time and not `datetime.now()`: the Workflow
    clock belongs to the time-skipping server, and a capture must not depend on
    the two agreeing.
    """

    return GateDecision(
        1,
        decision_id,
        f"request-{decision_id}",
        ACTOR,
        gate.run_id,
        gate_id or gate.gate_id,
        gate.subject_sha256,
        gate.admission_sha256,
        gate.prepared_sha256,
        gate.policy_revision,
        "approve",
        RATIONALE,
        gate.issued_at,
    )


# ---------------------------------------------------------------------- replay


async def _capture_replay(environment: WorkflowEnvironment, run_id: str, *, approve: bool) -> str:
    stubs = ReplayStubs(REPLAY_STAGE_ORDER)
    task_queue = _task_queue(run_id)
    async with _worker(environment.client, task_queue, [ReplayRunWorkflow], stubs.activities):
        handle = await environment.client.start_workflow(
            ReplayRunWorkflow.run,
            replay_request(run_id, gate_timeout_seconds=GATE_TIMEOUT_SECONDS),
            id=run_id,
            task_queue=task_queue,
            versioning_override=PinnedVersioningOverride(CORPUS_DEPLOYMENT_VERSION),
        )
        status = await _wait_for_state(
            environment, handle, ReplayRunWorkflow.status, "awaiting-verification-approval"
        )
        if not approve:
            return await _history_json(handle)
        await handle.execute_update(
            ReplayRunWorkflow.submit_verification_decision,
            verification_decision(status.gate, decision_id="corpus-verify-approval"),
        )
        await handle.result()
        return await _history_json(handle)


# --------------------------------------------------------------- feature gate


def _record_assessment(state: Path, root: Path, run_id: str):
    """Stage 4a's real producer over `tests/test_assessment*.py`'s fixtures."""

    return assessment_record.record(
        state,
        run_id=run_id,
        index_dir=write_fake_index(root / f"index-{run_id}"),
        manifest_path=write_manifest(root / f"hooks-{run_id}.json"),
        allowed_actor=ACTOR,
        owner_token=f"corpus-owner-{run_id}",
    )


async def _capture_feature(
    environment: WorkflowEnvironment, run_id: str, recorded, *, approve: bool
) -> str:
    task_queue = _task_queue(run_id)
    async with _worker(
        environment.client,
        task_queue,
        [FeatureAssessmentRunWorkflow],
        [prepare_feature_gate_activity, admit_feature_dispositions_activity],
    ):
        handle = await environment.client.start_workflow(
            FeatureAssessmentRunWorkflow.run,
            FeatureRunRequestV1(1, run_id, GATE_TIMEOUT_SECONDS),
            id=run_id,
            task_queue=task_queue,
            versioning_override=PinnedVersioningOverride(CORPUS_DEPLOYMENT_VERSION),
        )
        status = await _wait_for_state(
            environment,
            handle,
            FeatureAssessmentRunWorkflow.status,
            "awaiting-feature-dispositions",
        )
        if not approve:
            return await _history_json(handle)

        document = FeatureDispositionsV1(
            1,
            recorded.assessment.sha256,
            recorded.policy_revision,
            tuple(
                FeatureDispositionV1(1, candidate, "offer_toggle", f"a switch for {candidate}")
                for candidate in recorded.candidate_ids
            ),
        )
        reference = runtime().store.put_bytes(
            kind=DISPOSITIONS_ARTIFACT_KIND,
            data=canonical_json(document).encode("utf-8"),
            producer_operation_id=f"client-{document.sha256}",
            input_hashes=(),
        )
        await handle.execute_update(
            FeatureAssessmentRunWorkflow.submit_feature_dispositions,
            FeatureGateSubmissionV1(
                1, _decision(status.gate, decision_id="corpus-feature-approval"), reference
            ),
        )
        await handle.result()
        return await _history_json(handle)


# ------------------------------------------------------------------- the run


async def capture_all(root: Path) -> dict[str, str]:
    """Every fixture, keyed by filename. One state root, one server, one process."""

    state = root / "state"

    # Recorded before `configure_runtime`, exactly as the behavioural tests do
    # it: the producers create the ledger, and the Activities then open the one
    # that is already there.
    feature_completed = _record_assessment(state, root, "corpus-feature-completed")
    feature_open = _record_assessment(state, root, "corpus-feature-open")
    configure_runtime(state)

    environment = await WorkflowEnvironment.start_time_skipping(identity=CAPTURE_IDENTITY)
    try:
        return {
            "replay_run_completed_v1.json": await _capture_replay(
                environment, "corpus-replay-completed", approve=True
            ),
            "replay_run_open_at_verification_gate_v1.json": await _capture_replay(
                environment, "corpus-replay-open", approve=False
            ),
            "feature_gate_completed_v1.json": await _capture_feature(
                environment, "corpus-feature-completed", feature_completed, approve=True
            ),
            "feature_gate_open_v1.json": await _capture_feature(
                environment, "corpus-feature-open", feature_open, approve=False
            ),
        }
    finally:
        await environment.shutdown()


def main() -> None:
    previous = getattr(activities, "_runtime", None)
    with tempfile.TemporaryDirectory() as directory:
        captured = asyncio.run(capture_all(Path(directory).resolve()))
    activities._runtime = previous

    destination = histories_directory()
    registered = {fixture.filename for fixture in FIXTURES}
    # TWO PHASES, and the split is the whole point. Checking inside the write
    # loop protected only the offending file: a leak planted in the fixture that
    # sorts last was correctly refused *after* five of the seven had already been
    # overwritten, leaving the corpus half-rewritten by a run that reported
    # "refusing to write". A guard placed correctly and cancelled by its ordering
    # is a failure this project has shipped before.
    refusals: list[str] = []
    for filename, text in sorted(captured.items()):
        found = leaks(text)
        if found:
            refusals.append(f"{filename}: leaked {sorted(set(found))}")
        if filename not in registered:
            refusals.append(
                f"{filename}: add a Fixture row to tests/history_corpus.py first"
            )
    if refusals:
        # Every refusal, not the first. Regenerating is slow; discovering the
        # second problem on the next run is a wasted ninety seconds.
        raise SystemExit(
            "refusing to write ANY fixture:\n  " + "\n  ".join(refusals)
        )

    digests: dict[str, str] = {}
    for filename, text in sorted(captured.items()):
        # No trailing newline: `WorkflowHistory.to_json()` does not emit one, and
        # the fixture captured before this tool existed does not carry one.
        (destination / filename).write_text(text, encoding="utf-8")
        digests[filename] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    print(f"captured {len(digests)} histories at {datetime.now(timezone.utc).isoformat()}")
    print("update tests/history_corpus.py FIXTURES with:")
    for filename, digest in digests.items():
        print(f'    {filename}\n        "{digest}",')


if __name__ == "__main__":
    main()
