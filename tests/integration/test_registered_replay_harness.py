"""Drive a real 340/430 replay through the REGISTERED Temporal Workflow.

`test_real_replay_harness` proves the five checkpoint Activities work by calling
them in-process. It is deliberately not this: it never starts a worker, never
starts a Workflow, and issues its own final-verification grant. So everything
the registration slice added -- the handle, the stage wrappers, the plan
Activity, the mid-run gate, the update validator, the worker's own runtime
binding -- has never executed against a real port.

This harness closes that. It reuses the other harness's authority construction
**by import** rather than restating it, because the two must admit byte-identical
authority for the same run id or the derived gate identifiers they each pin would
be about two different runs.

Three processes, on purpose, because that is how it is actually operated:

* this one builds the authority, starts the Workflow and collects evidence;
* `python -m dfinsta_pipeline.worker` hosts the registered Workflow and
  Activities. Running it as the documented CLI is what proved the CLI could not
  supply a source root or an executor path, which no in-process worker would
  have shown;
* `python -m dfinsta_pipeline.submission` answers the gate. The human's side of
  the run goes through the client a human would use, including quoting back the
  confirmation prefix the client derived -- not through a decision this file
  constructs.

What it does NOT establish: cancellation behaviour, heartbeats, or anything about
signing and installation. It establishes that the registered path runs, that the
stage sequence is derived rather than declared, that the gate is answerable by
the documented client, and that History stays within budget for the heavy target.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.api.history.v1 import History
from temporalio.client import Client
from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion

from dfinsta_pipeline.activities import configure_runtime, runtime
from dfinsta_pipeline.ledger import Ledger
from dfinsta_pipeline.replay_contracts import (
    REPLAY_STAGE_ORDER,
    REPLAY_STAGES_WITHOUT_FRAMEWORK,
    AdmittedReplayHandleV1,
    ReplayRunRequestV1,
)
from dfinsta_pipeline.replay_workflow import ReplayRunWorkflow

from .test_real_replay_harness import (
    JAVA_SHA256,
    REPOSITORY_ROOT,
    TARGETS,
    TargetConfig,
    _create_authority,
    _ledger_evidence,
    _load_target_inputs,
    _publish_outcome,
    _run_checked,
    authority_run_id,
    preflight_tools,
    select_targets,
    strict_json_bytes,
    validate_run_root,
)


def expected_stages(config: TargetConfig) -> tuple[str, ...]:
    """The stage names the WORKFLOW uses, which are not the sibling harness's.

    `test_real_replay_harness.stage_order` says `framework`; the Workflow appends
    whatever `ReplayExecutionPlanV1` names, which is `install_framework`, and
    `ReplayRunResultV1` rejects any stage outside `REPLAY_STAGE_ORDER` — so
    `framework` is unreachable by construction. The two vocabularies coincide
    exactly when there is no framework stage, which is why reusing the sibling's
    would have passed on 340 and been impossible on 430.
    """

    return REPLAY_STAGE_ORDER if config.framework_sha256 else REPLAY_STAGES_WITHOUT_FRAMEWORK


#: The Activities a complete run schedules, in order. Sourced from History rather
#: than from the Workflow's own result, which names stages and so can never
#: mention the plan Activity, the gate or the grant — the three this harness
#: exists to execute for the first time.
def expected_activity_sequence(config: TargetConfig) -> tuple[str, ...]:
    stages = expected_stages(config)
    return (
        "prepare_replay_plan_activity",
        *(f"replay_{_STAGE_ACTIVITY[stage]}_stage_activity" for stage in stages[:-1]),
        "resolve_replay_verification_grant_activity",
        "prepare_replay_verification_gate_activity",
        "admit_replay_verification_grant_activity",
        "replay_verify_final_apk_stage_activity",
    )


_STAGE_ACTIVITY = {
    "install_framework": "install_frameworks",
    "decode": "decode",
    "apply": "apply_tree",
    "build": "build_patched_apk",
}


#: Wall-clock ceiling for one target. The recorded stage durations sum to about
#: 65 minutes for 430; three hours leaves room for a cold page cache without
#: leaving a wedged run to sit until someone notices.
TARGET_DEADLINE_SECONDS = 3 * 60 * 60

#: How long the Workflow leaves the verification gate open. The answering client
#: runs seconds after the gate opens here, but the value is part of the published
#: `expires_at`, so it is set to something an actual human could meet.
VERIFICATION_GATE_TIMEOUT_SECONDS = 60 * 60

#: `docs/WORKFLOW_REGISTRATION_DESIGN.md` section 3 item 3. The budget is a
#: guarantee extended to this Workflow deliberately, and 340 is the target that
#: fails it under the design that passes the admitted replay by value.
HISTORY_BUDGET_BYTES = 256 * 1024

TARGET_EVIDENCE_KEYS = frozenset(
    {
        "target",
        "run_id",
        "workflow_id",
        "task_queue",
        "admitted_replay_sha256",
        "derived_verification_identifiers",
        "worker_command",
        "worker_outcome",
        "submission_show",
        "submission_submit",
        "workflow_result",
        "history",
        "ledger",
        "verification_receipt",
        "stage_first_observed_seconds",
        "published_gate",
        "worker_query_responsiveness",
        "stage_heartbeats",
        "worst_heartbeat_gap_seconds",
    }
)

#: Every history key an assertion reads. Bound to `_history_evidence`'s output by
#: a fast test, because the two static defects that made this harness unable to
#: produce a success marker -- a key renamed on one side only, and a stage
#: vocabulary borrowed from the wrong module -- both cost a full run to discover.
ASSERTED_HISTORY_KEYS = frozenset(
    {
        "activity_failures",
        "scheduled_activity_sequence",
        "within_budget",
        "json_bytes",
        "decoded_payloads",
        "control_found_in_surface",
        "control_visible_in_raw_json",
        "contains_repository_path",
        "contains_source_tree_marker",
    }
)


def _venv_python() -> str:
    """The interpreter this harness is running under.

    Named rather than assumed, so the three processes share one interpreter and
    one installed `dfinsta_pipeline`. It pins the interpreter, not the import
    path: both children inherit this process's environment and working directory,
    so a different `PYTHONPATH` would still reach a different tree. The run root
    records the command it used, which is what makes that checkable afterwards.
    """

    return sys.executable


def worker_command(
    state_root: Path,
    task_queue: str,
    build_id: str,
    java: Path,
    *,
    endpoint: str,
    attempts_root: Path,
) -> tuple[str, ...]:
    return (
        _venv_python(),
        "-m",
        "dfinsta_pipeline.worker",
        "--endpoint",
        endpoint,
        "--task-queue",
        task_queue,
        "--state-root",
        str(state_root),
        "--attempts-root",
        str(attempts_root),
        "--build-id",
        build_id,
        "--source-root",
        str(REPOSITORY_ROOT),
        "--executor-path",
        f"{JAVA_SHA256}={java}",
    )


def write_principal(path: Path, actor: str) -> Path:
    """The actor file the submission client requires, for this run's actor.

    Written per run rather than to `~/.config/dfinsta/principal.json` because the
    allowed actor is `real-replay-{target}-operator`, which is authority this
    fixture invents; installing it as the host's default principal would leave a
    test identity lying around for whoever answers the next real gate.
    """

    path.write_text(
        json.dumps({"schema_version": 1, "uid": os.geteuid(), "actor": actor}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


_CONFIRM = re.compile(r"^to answer, pass --confirm ([0-9a-f]+)$", re.MULTILINE)


def submission_command(
    state_root: Path, principal: Path, workflow_id: str, *rest: str, endpoint: str
) -> tuple[str, ...]:
    return (
        _venv_python(),
        "-m",
        "dfinsta_pipeline.submission",
        "--endpoint",
        endpoint,
        "--state-root",
        str(state_root),
        "--principal",
        str(principal),
        *rest,
        workflow_id,
    )


def answer_gate(
    state_root: Path,
    principal: Path,
    workflow_id: str,
    *,
    endpoint: str,
    rationale: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the documented two-step: read the gate, then answer what was read.

    The confirmation prefix is taken from the client's own `show` output rather
    than computed here. That is the whole point of the step: the value a human
    types back is the hash the CLIENT derived, so a harness that computed it
    independently would be checking its own arithmetic instead of the client's.
    """

    show = subprocess.run(
        submission_command(state_root, principal, workflow_id, "show", endpoint=endpoint),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if show.returncode != 0:
        raise AssertionError(f"submission show failed ({show.returncode}): {show.stderr}")
    match = _CONFIRM.search(show.stdout)
    if match is None:
        raise AssertionError(f"submission show published no confirmation prefix: {show.stdout}")
    confirmation = match.group(1)
    submit = subprocess.run(
        submission_command(
            state_root,
            principal,
            workflow_id,
            "submit",
            "--verdict",
            "approve",
            "--rationale",
            rationale,
            "--confirm",
            confirmation,
            endpoint=endpoint,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if submit.returncode != 0:
        raise AssertionError(
            f"submission submit failed ({submit.returncode}): {submit.stderr}"
        )
    return (
        {"stdout": show.stdout, "confirmation": confirmation},
        {"stdout": submit.stdout},
    )


def _history_evidence(history: Any, control: str) -> dict[str, Any]:
    """Size and content of the recorded History.

    Two measurements, because they answer different questions. `json_bytes` is
    `len(to_json().encode())`, the same quantity `test_phase_a_temporal` sizes
    against 256 KB and `test_phase_b_replay_workflow` sizes against a tighter
    128 KB, so this number is comparable to both; `serialized_bytes` is the wire
    form the server actually persists, recorded but not the budget.

    The privacy search runs over `history_search_surface`, not the raw JSON.
    `to_json()` base64-encodes every payload body, so a plain substring search
    for a repository path would pass on a History that carried it in full -- an
    absence assertion that cannot fail.

    `control` is a string that MUST be in the surface: the admitted replay digest,
    which travels inside the handle in the WorkflowExecutionStarted payload. Three
    facts together make the absence assertions mean something -- at least one
    payload decoded, the control is found in the surface, and the control is NOT
    in the raw JSON. The third is what proves the decode is doing the work.
    """

    from ..history_search import decoded_payload_count, history_search_surface

    history_json = history.to_json()
    json_bytes = len(history_json.encode("utf-8"))
    serialized = History(events=list(history.events)).SerializeToString()
    surface = history_search_surface(history_json)
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    # A failed attempt records a stack trace, and Temporal writes that as plain
    # text rather than as an encoded payload -- so it carries absolute source
    # paths into History and would trip the privacy check. The count is collected
    # so the refusal can say which of the two happened.
    failures = sum(
        1 for event in history.events if event.HasField("activity_task_failed_event_attributes")
    )
    return {
        "activity_failures": failures,
        "event_count": len(history.events),
        "json_bytes": json_bytes,
        "serialized_bytes": len(serialized),
        "budget_bytes": HISTORY_BUDGET_BYTES,
        "within_budget": json_bytes <= HISTORY_BUDGET_BYTES,
        "decoded_payloads": decoded_payload_count(history_json),
        "control_found_in_surface": control in surface,
        "control_visible_in_raw_json": control in history_json,
        "contains_repository_path": str(REPOSITORY_ROOT) in surface,
        "contains_source_tree_marker": "dfinsta_source_" in surface,
        "activity_types": sorted(set(scheduled)),
        "scheduled_activity_sequence": scheduled,
    }


def derived_verification_identifiers(run_id: str) -> dict[str, str]:
    """The three ids `replay_gate` derives, computed the way it computes them.

    Pinned in the evidence because `docs/WORKFLOW_REGISTRATION_DESIGN.md` F1
    deferred re-pinning them until a real run could check them against something
    actually observed.
    """

    from dfinsta_pipeline import replay_gate

    return {
        "grant_id": replay_gate.derived_identifier(
            run_id, replay_gate.GRANT_ID_SUFFIX, "verification grant id"
        ),
        "gate_id": replay_gate.derived_identifier(
            run_id, replay_gate.GATE_ID_SUFFIX, "verification gate id"
        ),
        "capability_id": replay_gate.derived_identifier(
            run_id, replay_gate.CAPABILITY_ID_SUFFIX, "verification capability id"
        ),
    }


async def refuse_open_workflow(endpoint: str, workflow_id: str) -> None:
    """Refuse to start over a run that is still open, and say how to clear it.

    The workflow id is the run id on purpose, so a second attempt after a failed
    one collides. Temporal would report that collision as
    `WorkflowAlreadyStartedError` from deep inside `start_workflow`; saying it
    here, with the command that resolves it, is the difference between a refusal
    and a traceback.
    """

    client = await Client.connect(endpoint)
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except Exception:  # noqa: BLE001 - absent is the normal case
        return
    if description.close_time is None:
        raise AssertionError(
            f"Workflow {workflow_id} is still open ({description.status}). Clear it "
            f"with: temporal workflow terminate -w {workflow_id} --address {endpoint} "
            '--reason "superseded"'
        )


async def start_when_worker_ready(
    client: Client,
    request: ReplayRunRequestV1,
    *,
    workflow_id: str,
    task_queue: str,
    build_id: str,
    deadline: float,
) -> Any:
    """Start the run, waiting for the worker to appear on the task queue.

    `versioning_override` pins the execution to this worker's deployment version,
    and the server refuses a version it has never seen poll -- so starting before
    the worker registers fails outright rather than queueing. Retrying that exact
    refusal is the wait: it needs no second way of asking whether a worker is up,
    and it cannot succeed against a queue no worker is serving.
    """

    from temporalio.service import RPCError

    override = PinnedVersioningOverride(WorkerDeploymentVersion("dfinsta-pipeline", build_id))
    last: RPCError | None = None
    while time.monotonic() < deadline:
        try:
            return await client.start_workflow(
                ReplayRunWorkflow.run,
                request,
                id=workflow_id,
                task_queue=task_queue,
                versioning_override=override,
            )
        except RPCError as error:
            if "is not present in task queue" not in str(error):
                raise
            last = error
            await asyncio.sleep(2.0)
    raise TimeoutError(f"Worker never registered on {task_queue}: {last}")


def _terminate(process: subprocess.Popen[bytes], *, completed: bool) -> int | None:
    """Stop the worker, by the rule that fits what is actually in flight.

    `completed=True` means the Workflow finished, so no Activity is claimed and
    a delivered cancellation has nothing to quarantine: SIGINT is safe and exits
    cleanly. `completed=False` means a stage may be running, and there the rule
    inverts -- `worker.py` and the design doc's 3b-corrected both record it. A
    *stop* that exhausts the graceful window delivers `WORKER_SHUTDOWN`, which
    quarantines the operation permanently; a *kill* delivers no cancellation at
    all, leaves the claim `pending`, and `release_pending_operation` can hand it
    to a later attempt. So on the failure path this SIGKILLs deliberately: the
    run is left wedged rather than burned.
    """

    if process.poll() is not None:
        return process.returncode
    if not completed:
        process.kill()
        return process.wait(timeout=60)
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=60)


async def _run_target(
    config: TargetConfig,
    target_root: Path,
    java: Path,
    build_id: str,
    *,
    endpoint: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_root = target_root / "state"
    attempts_root = target_root / "attempts"
    task_queue = f"registered-replay-{config.target}"
    run_id = authority_run_id(config.target)
    if diagnostics is not None:
        diagnostics["target_root"] = str(target_root)
        diagnostics["ledger_path"] = str(state_root / "ledger.sqlite3")
        diagnostics["task_queue"] = task_queue

    # This process writes the authority and then never writes again: the worker
    # owns the ledger for the rest of the run.
    configure_runtime(
        state_root,
        attempts_root=attempts_root,
        source_root=REPOSITORY_ROOT,
        executor_paths={JAVA_SHA256: java},
    )
    inputs = _load_target_inputs(config)
    admitted, _ = _create_authority(config, inputs)
    handle_value = AdmittedReplayHandleV1(1, admitted.run_spec.run_id, admitted.sha256)
    principal = write_principal(
        target_root / "principal.json", admitted.run_spec.allowed_actor
    )

    command = worker_command(
        state_root,
        task_queue,
        build_id,
        java,
        endpoint=endpoint,
        attempts_root=attempts_root,
    )
    log_path = target_root / "worker.log"
    started = time.monotonic()
    deadline = started + TARGET_DEADLINE_SECONDS
    stage_marks: dict[str, float] = {}
    responsiveness: list[dict[str, Any]] = []
    heartbeats: list[dict[str, Any]] = []
    completed = False
    await refuse_open_workflow(endpoint, run_id)
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            client = await Client.connect(endpoint)
            workflow_handle = await start_when_worker_ready(
                client,
                ReplayRunRequestV1(1, handle_value, VERIFICATION_GATE_TIMEOUT_SECONDS),
                workflow_id=run_id,
                task_queue=task_queue,
                build_id=build_id,
                deadline=min(deadline, started + 180.0),
            )
            status = await _watch_until_gate(
                workflow_handle,
                deadline,
                stage_marks,
                responsiveness,
                heartbeats,
                process,
                log_path,
            )
            show, submit = answer_gate(
                state_root,
                principal,
                run_id,
                endpoint=endpoint,
                rationale=(
                    "Separately gate-approved self-issued test-only final APK mechanical "
                    "verification through the registered workflow; not authenticated, "
                    "production, signing, or runtime authority"
                ),
            )
            result = await asyncio.wait_for(
                workflow_handle.result(), timeout=max(60.0, deadline - time.monotonic())
            )
            stage_marks["verify"] = time.monotonic()
            history = await workflow_handle.fetch_history()
            completed = True
        finally:
            # Never let stopping the worker replace the failure that got us here.
            # `run_registered_replay` records `type(error).__name__` and `str(error)`
            # into failure.json; an exception raised in this block would be the one
            # recorded, and the pointer to the worker log would be lost with it.
            try:
                code = _terminate(process, completed=completed)
            except BaseException as error:  # noqa: BLE001 - recorded, never raised
                code = f"{type(error).__name__}: {error}"
            worker_outcome = {"exit_code": code, "log": str(log_path)}
            if diagnostics is not None:
                diagnostics.update(worker_outcome)

    expected = expected_stages(config)
    if tuple(result.stages_completed) != expected:
        raise AssertionError(f"Workflow ran {result.stages_completed}, expected {expected}")
    if result.state != "completed":
        raise AssertionError(f"Workflow finished {result.state!r}, not completed")
    if result.final_verification is None:
        raise AssertionError("Completed run published no final verification reference")

    history_evidence = _history_evidence(history, admitted.sha256)
    # The one observation sourced from the server rather than from a table in this
    # file. `result.stages_completed` names stages, so it can never mention the
    # plan Activity, the gate or the grant -- the three the registered path adds.
    scheduled = tuple(history_evidence["scheduled_activity_sequence"])
    if scheduled != expected_activity_sequence(config):
        raise AssertionError(
            f"Workflow scheduled {scheduled}, expected {expected_activity_sequence(config)}"
        )
    if not history_evidence["within_budget"]:
        raise AssertionError(
            f"History is {history_evidence['json_bytes']} bytes, over the "
            f"{HISTORY_BUDGET_BYTES} budget"
        )
    # Absence assertions are worthless without a control. The admitted replay's
    # digest travels inside the handle in the WorkflowExecutionStarted payload, so
    # a surface that cannot find IT cannot find anything, and the same digest must
    # be invisible in the raw JSON or the base64 decode is not happening.
    if history_evidence["decoded_payloads"] < 1:
        raise AssertionError("No History payload decoded; the privacy search proves nothing")
    if not history_evidence["control_found_in_surface"]:
        raise AssertionError(
            "The admitted replay digest is absent from the search surface, so the "
            "absence assertions below cannot fail"
        )
    if history_evidence["control_visible_in_raw_json"]:
        raise AssertionError("History payloads are not encoded; the control is not a control")
    if history_evidence["contains_repository_path"]:
        raise AssertionError(
            "History carries a private repository path"
            + (
                f" (after {history_evidence['activity_failures']} activity failure(s); "
                "Temporal writes stack traces into History as plain text, so this "
                "points at the retry rather than at the payload design)"
                if history_evidence["activity_failures"]
                else ""
            )
        )
    if history_evidence["contains_source_tree_marker"]:
        raise AssertionError("History carries source tree paths")

    # Read back what the WORKER wrote. This process configured its runtime before
    # the worker existed and has not written since; both open the ledger per call
    # and the store per read, so what follows is the other process's state, not a
    # cached view of this one's.
    verification = strict_json_bytes(runtime().store.read_bytes(result.final_verification))
    if verification.get("success") is not True:
        raise AssertionError("Final verification receipt does not report success")
    if not verification.get("assertion_results"):
        raise AssertionError("Final verification receipt carries no assertions")
    ledger = _ledger_evidence(runtime().ledger)
    if any(claim["status"] != "completed" for claim in ledger["claims"]):
        raise AssertionError("Not every replay operation completed")
    recorded_handle = Ledger.admitted_replay_handle_for_run(runtime().ledger, run_id)
    if recorded_handle != handle_value:
        raise AssertionError("Recorded handle does not match the one this run admitted")

    identifiers = derived_verification_identifiers(run_id)
    if identifiers["gate_id"] != f"{run_id}-final-verification-gate":
        raise AssertionError("Derived gate id drifted")
    if status["gate"]["gate_id"] != identifiers["gate_id"]:
        raise AssertionError("Workflow published a gate id the derivation does not produce")
    # The grant id checked against a row the WORKER wrote, not against the same
    # derivation twice. The capability id has no observable counterpart here; it
    # is pinned at rest by `RealReplayHarnessFastTests`.
    grant = Ledger.admitted_replay_verification_resumption(
        runtime().ledger, identifiers["grant_id"]
    )
    if grant is None:
        raise AssertionError("The run recorded no verification grant under the derived id")
    if grant.decision_id != result.verification_decision_id:
        raise AssertionError("The recorded grant names a different decision from the result")
    # And that decision is the one the documented client sent, rather than merely
    # the one this run happens to have ended with.
    if grant.decision_id not in submit["stdout"]:
        raise AssertionError("The submission client did not report the admitted decision")

    evidence = {
        "target": config.target,
        "run_id": run_id,
        "workflow_id": run_id,
        "task_queue": task_queue,
        "admitted_replay_sha256": admitted.sha256,
        "derived_verification_identifiers": identifiers,
        "worker_command": list(command),
        "worker_outcome": worker_outcome,
        "submission_show": show,
        "submission_submit": submit,
        "workflow_result": asdict(result),
        "history": history_evidence,
        "ledger": ledger,
        "verification_receipt": verification,
        "stage_first_observed_seconds": [
            {"activity": name, "elapsed_seconds": round(mark - started, 3)}
            for name, mark in sorted(stage_marks.items(), key=lambda item: item[1])
        ],
        "published_gate": status["gate"],
        "worker_query_responsiveness": responsiveness,
        "stage_heartbeats": heartbeats,
        "worst_heartbeat_gap_seconds": _worst_heartbeat_gaps(heartbeats),
    }
    if frozenset(evidence) != TARGET_EVIDENCE_KEYS:
        raise AssertionError("Target evidence schema drifted")
    return evidence


GATE_ACTIVITY = "prepare_replay_verification_gate_activity"


def _activity_progress(events: Any) -> tuple[frozenset[str], str | None]:
    """Read stage progress out of History, which the server serves by itself.

    Progress is NOT read from the `status` query, and that is the whole point of
    this function. A replay stage blocks the worker's event loop -- the stock APK
    read, the workspace write and `capture_decoded_tree_fd` are all synchronous,
    and only `await execute(...)` yields -- so for minutes at a time the worker
    cannot answer a query at all. Measured, not deduced: the first attempt at this
    harness polled `query("status")` and died with `RPCError: Timeout expired`, and
    `temporal workflow query`, an unrelated client, reports `query timed out before
    a worker could process it` against the same running stage.

    History has no such dependency. The server records scheduled and completed
    activity events whether or not any worker is responsive.
    """

    scheduled: dict[int, str] = {}
    completed: set[str] = set()
    running: str | None = None
    for event in events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            scheduled[event.event_id] = name
            running = name
        elif event.HasField("activity_task_completed_event_attributes"):
            attributes = event.activity_task_completed_event_attributes
            name = scheduled.get(attributes.scheduled_event_id)
            if name is not None:
                completed.add(name)
                if running == name:
                    running = None
    return frozenset(completed), running


def _sample_heartbeats(description: Any) -> list[dict[str, Any]]:
    """Read the details every running stage last heartbeated, from the server.

    The stage wrappers report `{stage, beats, elapsed_seconds, worst_gap_seconds}`
    every 30 s, and `worst_gap_seconds` is the number that decides whether
    `_STAGE_HEARTBEAT_TIMEOUT` can come down. The first reading of it -- 30.9 s on
    a 340 decode -- was taken by hand with `temporal activity describe` and so was
    **not reproducible from the tree**, which is the exact regret already written
    down about the standalone threading benchmark. Recorded here instead.

    Read from `describe()` rather than from a query, on purpose: pending-activity
    state comes from the server's own record and arrives even while the worker is
    too busy to answer anything, which is precisely the condition being measured.
    """

    from temporalio.api.common.v1 import Payloads

    samples: list[dict[str, Any]] = []
    for pending in description.raw_description.pending_activities:
        sample: dict[str, Any] = {
            "activity": pending.activity_type.name,
            "attempt": pending.attempt,
        }
        if pending.HasField("last_heartbeat_time"):
            sample["last_heartbeat"] = pending.last_heartbeat_time.ToJsonString()
        payloads: Payloads = pending.heartbeat_details
        for payload in payloads.payloads:
            try:
                sample["details"] = json.loads(payload.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                # Recorded, never raised: a measurement that cannot be decoded
                # must not fail a port that is otherwise running correctly.
                sample["details_error"] = f"{type(error).__name__}: {error}"
        samples.append(sample)
    return samples


def _worst_heartbeat_gaps(samples: list[dict[str, Any]]) -> dict[str, float]:
    """The largest gap each stage ever reported. The number a timeout is set from."""

    worst: dict[str, float] = {}
    for sample in samples:
        details = sample.get("details")
        if not isinstance(details, dict):
            continue
        stage = details.get("stage")
        gap = details.get("worst_gap_seconds")
        if isinstance(stage, str) and isinstance(gap, (int, float)):
            worst[stage] = max(worst.get(stage, 0.0), float(gap))
    return worst


async def _sample_query(workflow_handle: Any) -> tuple[bool, str | None]:
    """Can the worker answer right now? Recorded rather than relied on.

    The answer is evidence about the property above: a stage that blocks the loop
    also blocks anything the worker would have to send from it, which is exactly
    what a heartbeat is. `docs/WORKFLOW_REGISTRATION_DESIGN.md` 3b-corrected
    argues a heartbeater in the wrapper works because every long operation yields;
    these samples are the measurement that claim never had.
    """

    from temporalio.service import RPCError

    try:
        await workflow_handle.query("status", rpc_timeout=timedelta(seconds=5))
    except RPCError as error:
        return False, str(error)
    except Exception as error:  # noqa: BLE001 - recorded, never acted on
        return False, f"{type(error).__name__}: {error}"
    return True, None


def _require_live_worker(process: subprocess.Popen[bytes] | None, log_path: Path) -> None:
    """A dead worker must not cost the whole window and then blame the timeout."""

    if process is None or process.poll() is None:
        return
    raise AssertionError(
        f"Worker exited with {process.returncode} while the run was in flight; "
        f"see {log_path}"
    )


async def _watch_until_gate(
    workflow_handle: Any,
    deadline: float,
    stage_marks: dict[str, float],
    responsiveness: list[dict[str, Any]],
    heartbeats: list[dict[str, Any]],
    process: subprocess.Popen[bytes] | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Follow the run through History, then read the gate once it is open."""

    started = time.monotonic()
    while time.monotonic() < deadline:
        _require_live_worker(process, log_path or Path("worker.log"))
        history = await workflow_handle.fetch_history()
        completed, running = _activity_progress(history.events)
        for name in completed:
            stage_marks.setdefault(name, time.monotonic())
        answered, detail = await _sample_query(workflow_handle)
        responsiveness.append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "running_activity": running,
                "query_answered": answered,
                "detail": detail,
            }
        )
        description = await workflow_handle.describe()
        for sample in _sample_heartbeats(description):
            heartbeats.append({"elapsed_seconds": round(time.monotonic() - started, 1)} | sample)
        if GATE_ACTIVITY in completed:
            return await _read_open_gate(workflow_handle, deadline)
        if description.close_time is not None:
            raise AssertionError(
                f"Workflow closed ({description.status}) without opening the gate"
            )
        await asyncio.sleep(15.0)
    raise TimeoutError("Workflow did not open the verification gate before the deadline")


async def _read_open_gate(workflow_handle: Any, deadline: float) -> dict[str, Any]:
    """Query for the published gate, retrying while the worker is still busy.

    Safe to query here where it was not before: the Workflow is parked in
    `wait_condition`, so no Activity is holding the loop. The retry covers the
    handful of seconds between the gate Activity completing and the Workflow
    processing the task that opens it.
    """

    from temporalio.service import RPCError

    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = await workflow_handle.query("status", rpc_timeout=timedelta(seconds=30))
        except RPCError as error:
            last = error
            await asyncio.sleep(5.0)
            continue
        if type(status) is not dict:
            raise AssertionError(f"Workflow status query returned {type(status).__name__}")
        state = status.get("state")
        if state == "awaiting-verification-approval":
            if status.get("gate") is None:
                raise AssertionError("Workflow is awaiting approval with no published gate")
            return status
        if state in {"blocked", "rejected", "deferred", "completed"}:
            raise AssertionError(f"Workflow reached {state!r} without opening the gate")
        await asyncio.sleep(5.0)
    raise TimeoutError(f"Gate never opened after its Activity completed: {last}")


async def run_registered_replay(
    targets: tuple[int, ...], run_root: Path, endpoint: str
) -> Path:
    java, versions = preflight_tools()
    build_id = _run_checked(("git", "rev-parse", "HEAD")).stdout.strip()
    run_root.mkdir(mode=0o700, parents=True)
    diagnostics: dict[str, Any] = {"targets": [], "current": None}
    try:
        target_evidence = []
        for target in targets:
            current: dict[str, Any] = {"target": target}
            diagnostics["current"] = current
            target_root = run_root / str(target)
            target_root.mkdir(mode=0o700)
            evidence = await _run_target(
                TARGETS[target],
                target_root,
                java,
                build_id,
                endpoint=endpoint,
                diagnostics=current,
            )
            target_evidence.append(evidence)
            diagnostics["targets"].append({"target": target, "state": "completed"})
            diagnostics["current"] = None
        summary = {
            "schema_version": 1,
            "status": "registered-workflow-success-not-production-authority",
            "scope": (
                "self-issued test-only authority driven through the registered "
                "ReplayRunWorkflow on a real Temporal server; not authenticated, "
                "production, signing or runtime authority"
            ),
            "targets": list(targets),
            "endpoint": endpoint,
            "build_id": build_id,
            "tool_versions": versions,
            "git": {
                "head": build_id,
                "status_short": _run_checked(
                    ("git", "status", "--short", "--untracked-files=all")
                ).stdout,
            },
            "target_evidence": target_evidence,
        }
        return _publish_outcome(run_root, "success.json", summary)
    except BaseException as error:
        failure = {
            "schema_version": 1,
            "status": "failure-not-replay-evidence",
            "error_type": type(error).__name__,
            "error": str(error),
            "targets": list(targets),
            "diagnostics": diagnostics,
        }
        try:
            _publish_outcome(run_root, "failure.json", failure)
        except BaseException as write_error:
            error.add_note(f"Could not write exclusive failure marker: {write_error}")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="localhost:7233")
    parser.add_argument("--targets", default=None, help="default: 340,430")
    parser.add_argument("--run-root", required=True, help="absolute, must not exist")
    arguments = parser.parse_args(argv)
    targets = select_targets(arguments.targets)
    run_root = validate_run_root(arguments.run_root)
    path = asyncio.run(run_registered_replay(targets, run_root, arguments.endpoint))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
