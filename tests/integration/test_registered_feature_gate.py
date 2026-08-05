"""Drive the feature-assessment gate through the registered Workflow, on a real server.

`FeatureAssessmentRunWorkflow` was proven end to end against
`WorkflowEnvironment.start_time_skipping()` — the roadmap says so plainly, "with no
Temporal server". This closes that: a real worker, a real client, a real
`python -m dfinsta_pipeline.submission` in its own process, and a run id as the
only thing that travels between them.

Unlike the replay harness this costs seconds, not an hour: every Activity here is
ledger and content-store work. There is no apktool, no decode and no APK.

**What it is actually testing** is the property the whole gate rests on: a human
answering holds nothing but a run id, and everything they sign — the candidate ids
and their order, the assessment digest, the policy revision, the actor, all three
subject hashes — is re-derived from recorded state by a client that cannot write.
A time-skipping environment proves the Workflow's logic; only a real server proves
that the update, the query and the payload survive the wire.
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

from temporalio.client import Client
from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion

from dfinsta_pipeline import assessment_record
from dfinsta_pipeline.activities import configure_runtime, runtime
from dfinsta_pipeline.feature_gate import FeatureRunRequestV1
from dfinsta_pipeline.feature_workflow import FeatureAssessmentRunWorkflow

from .test_real_replay_harness import (
    REPOSITORY_ROOT,
    _publish_outcome,
    _run_checked,
    validate_run_root,
)
from .test_registered_replay_harness import (
    _CONFIRM,
    _venv_python,
    submission_command,
    write_principal,
)

#: Seconds the Workflow leaves the gate open. Short, because the answering client
#: runs immediately — but not so short that a slow first `python -m` import races
#: the timer and turns a passing run into a `blocked` one.
GATE_TIMEOUT_SECONDS = 600

DEADLINE_SECONDS = 600

TARGET_EVIDENCE_KEYS = frozenset(
    {
        "run_id",
        "index_dir",
        "actor",
        "recorded_assessment",
        "candidate_ids",
        "published_gate",
        "client_derived_subject",
        "rulings",
        "submission_show",
        "submission_submit",
        "workflow_result",
        "admitted_dispositions",
        "worker_command",
        "worker_outcome",
    }
)


def worker_command(state_root: Path, task_queue: str, build_id: str, *, endpoint: str) -> tuple[str, ...]:
    """No `--source-root` and no `--executor-path`: this workflow launches nothing.

    Deliberately spelled out. The replay worker needs both and could not run a
    stage without them; this one touches only the ledger and the store, so a
    reader can see which inputs belong to which kind of work.
    """

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
        "--build-id",
        build_id,
    )


def record_assessment(state_root: Path, index_dir: Path, run_id: str, actor: str) -> Any:
    """Record the assessment this gate will be about, from a real index."""

    configure_runtime(state_root)
    return assessment_record.record(
        state_root,
        run_id=run_id,
        index_dir=index_dir,
        manifest_path=REPOSITORY_ROOT / "manifest" / "hooks.json",
        allowed_actor=actor,
        owner_token=f"{run_id}-owner",
    )


def rulings_for(candidate_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """A ruling per candidate, because the gate refuses anything less.

    `ignore` throughout, and that is the honest verdict for a harness: this run
    exercises the *mechanism*, and a `block` would edit the shipped manifest as a
    side effect of a test. `ignore` is also the one verdict whose rationale the
    contract does not require, so the rationale here is a courtesy — the run
    would be valid without it.
    """

    return {
        candidate: {
            "verdict": "ignore",
            "rationale": (
                "harness run against a live Temporal server; exercises the gate "
                "mechanism and rules nothing about this endpoint"
            ),
        }
        for candidate in candidate_ids
    }


async def wait_for_gate(handle: Any, deadline: float) -> dict[str, Any]:
    """Poll `status` until the gate opens.

    Safe to poll here where the replay harness could not: nothing in this
    Workflow blocks the worker's event loop, because nothing in it walks a
    decoded tree.
    """

    while time.monotonic() < deadline:
        status = await handle.query("status", rpc_timeout=timedelta(seconds=10))
        if type(status) is not dict:
            raise AssertionError(f"status query returned {type(status).__name__}")
        state = status.get("state")
        if state == "awaiting-feature-dispositions" or status.get("gate") is not None:
            return status
        if state in {"blocked", "rejected", "deferred", "completed"}:
            raise AssertionError(f"Workflow reached {state!r} without opening the gate")
        await asyncio.sleep(0.5)
    raise TimeoutError("Workflow did not open the feature gate before the deadline")


def answer_gate(
    state_root: Path,
    principal: Path,
    workflow_id: str,
    rulings_path: Path,
    *,
    endpoint: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`show` then `submit`, the documented two-step, in its own process."""

    show = subprocess.run(
        submission_command(
            state_root, principal, workflow_id, "show", "--rulings-template", endpoint=endpoint
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if show.returncode != 0:
        raise AssertionError(f"submission show failed ({show.returncode}): {show.stderr}")
    match = _CONFIRM.search(show.stdout)
    if match is None:
        raise AssertionError(f"submission show published no confirmation: {show.stdout}")
    submit = subprocess.run(
        submission_command(
            state_root,
            principal,
            workflow_id,
            "submit",
            "--verdict",
            "approve",
            "--rationale",
            "harness run against a live Temporal server",
            "--rulings",
            str(rulings_path),
            "--confirm",
            match.group(1),
            endpoint=endpoint,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if submit.returncode != 0:
        raise AssertionError(f"submission submit failed ({submit.returncode}): {submit.stderr}")
    return (
        {"stdout": show.stdout, "confirmation": match.group(1)},
        {"stdout": submit.stdout},
    )


async def run_registered_feature_gate(
    run_root: Path, index_dir: Path, endpoint: str
) -> Path:
    run_root.mkdir(mode=0o700, parents=True)
    state_root = run_root / "state"
    run_id = "live-feature-gate"
    actor = "live-gate-operator"
    task_queue = "registered-feature-gate"
    build_id = _run_checked(("git", "rev-parse", "HEAD")).stdout.strip()
    diagnostics: dict[str, Any] = {}
    try:
        recorded = record_assessment(state_root, index_dir, run_id, actor)
        candidates = tuple(recorded.candidate_ids)
        if not candidates:
            raise AssertionError("the recorded assessment names no candidates to gate on")
        rulings_path = run_root / "rulings.json"
        rulings = rulings_for(candidates)
        rulings_path.write_text(json.dumps(rulings, indent=2) + "\n", encoding="utf-8")
        principal = write_principal(run_root / "principal.json", actor)

        command = worker_command(state_root, task_queue, build_id, endpoint=endpoint)
        log_path = run_root / "worker.log"
        deadline = time.monotonic() + DEADLINE_SECONDS
        completed = False
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                client = await Client.connect(endpoint)
                handle = await _start_when_ready(
                    client, run_id, task_queue, build_id, deadline
                )
                status = await wait_for_gate(handle, deadline)
                show, submit = answer_gate(
                    state_root, principal, run_id, rulings_path, endpoint=endpoint
                )
                result = await asyncio.wait_for(
                    handle.result(), timeout=max(30.0, deadline - time.monotonic())
                )
                completed = True
            finally:
                try:
                    code = _terminate(process, completed=completed)
                except BaseException as error:  # noqa: BLE001 - recorded, never raised
                    code = f"{type(error).__name__}: {error}"
                worker_outcome = {"exit_code": code, "log": str(log_path)}
                diagnostics.update(worker_outcome)

        if result.state != "completed":
            raise AssertionError(f"Workflow finished {result.state!r}, not completed")
        if result.dispositions is None:
            raise AssertionError("a completed run admitted no dispositions")

        # Read back what the WORKER admitted, by run id, the way `rulings.py` does.
        configure_runtime(state_root, read_only=True)
        row = runtime().ledger.admitted_dispositions_for_run(run_id)
        if row["dispositions_sha256"] != result.dispositions.sha256:
            raise AssertionError("the recorded row names a different document from the result")
        # And the client signed the same subject the Workflow published.
        derived = _client_derived_subject(run_id)
        if derived["subject_sha256"] != status["gate"]["subject_sha256"]:
            raise AssertionError("client derivation does not match the published gate")

        evidence = {
            "run_id": run_id,
            "index_dir": str(index_dir),
            "actor": actor,
            "recorded_assessment": {
                "operation_key": recorded.operation_key,
                "assessment": asdict(recorded.assessment),
                "policy_revision": recorded.policy_revision,
            },
            "candidate_ids": list(candidates),
            "published_gate": status["gate"],
            "client_derived_subject": derived,
            "rulings": rulings,
            "submission_show": show,
            "submission_submit": submit,
            "workflow_result": asdict(result),
            "admitted_dispositions": dict(row),
            "worker_command": list(command),
            "worker_outcome": worker_outcome,
        }
        if frozenset(evidence) != TARGET_EVIDENCE_KEYS:
            raise AssertionError("evidence schema drifted")
        summary = {
            "schema_version": 1,
            "status": "registered-feature-gate-success-not-production-authority",
            "endpoint": endpoint,
            "build_id": build_id,
            "git": {
                "head": build_id,
                "status_short": _run_checked(
                    ("git", "status", "--short", "--untracked-files=all")
                ).stdout,
            },
            "evidence": evidence,
        }
        return _publish_outcome(run_root, "success.json", summary)
    except BaseException as error:
        failure = {
            "schema_version": 1,
            "status": "failure-not-gate-evidence",
            "error_type": type(error).__name__,
            "error": str(error),
            "diagnostics": diagnostics,
        }
        try:
            _publish_outcome(run_root, "failure.json", failure)
        except BaseException as write_error:
            error.add_note(f"Could not write exclusive failure marker: {write_error}")
        raise


def _client_derived_subject(run_id: str) -> dict[str, str]:
    """What the trusted client computes from the run id, and nothing else."""

    from dfinsta_pipeline.submission import FEATURE_ASSESSMENT_GATE

    derived = FEATURE_ASSESSMENT_GATE.resolve(run_id)
    return {
        "run_id": derived.run_id,
        "gate_id": derived.gate_id,
        "subject_sha256": derived.subject_sha256,
        "policy_revision": derived.policy_revision,
        "allowed_actor": derived.allowed_actor,
    }


async def _start_when_ready(
    client: Client, run_id: str, task_queue: str, build_id: str, deadline: float
) -> Any:
    from temporalio.service import RPCError

    override = PinnedVersioningOverride(WorkerDeploymentVersion("dfinsta-pipeline", build_id))
    last: RPCError | None = None
    while time.monotonic() < deadline:
        try:
            return await client.start_workflow(
                FeatureAssessmentRunWorkflow.run,
                FeatureRunRequestV1(1, run_id, GATE_TIMEOUT_SECONDS),
                id=run_id,
                task_queue=task_queue,
                versioning_override=override,
            )
        except RPCError as error:
            if "is not present in task queue" not in str(error):
                raise
            last = error
            await asyncio.sleep(1.0)
    raise TimeoutError(f"Worker never registered on {task_queue}: {last}")


def _terminate(process: subprocess.Popen[bytes], *, completed: bool) -> int | None:
    if process.poll() is not None:
        return process.returncode
    if not completed:
        process.kill()
        return process.wait(timeout=30)
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="localhost:7233")
    parser.add_argument("--run-root", required=True, help="absolute, must not exist")
    parser.add_argument(
        "--index",
        type=Path,
        default=REPOSITORY_ROOT / "work" / "440-clean" / "index",
        help="a real index directory to assess",
    )
    arguments = parser.parse_args(argv)
    path = asyncio.run(
        run_registered_feature_gate(
            validate_run_root(arguments.run_root), arguments.index, arguments.endpoint
        )
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
