from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import ArtifactRef, GateDecision, RunSpec, StageInput, canonical_json, canonical_sha256
from .ledger import Ledger
from .store import ContentStore


@dataclass
class ActivityRuntime:
    store: ContentStore
    ledger: Ledger


_runtime: ActivityRuntime | None = None


def configure_runtime(state_root: Path) -> None:
    global _runtime
    _runtime = ActivityRuntime(
        store=ContentStore(state_root / "cas"),
        ledger=Ledger(state_root / "ledger.sqlite3"),
    )


def runtime() -> ActivityRuntime:
    if _runtime is None:
        raise RuntimeError("Activity runtime is not configured")
    return _runtime


def operation_key(kind: str, value: object) -> str:
    return canonical_sha256({"kind": kind, "input": value})


def _adopt_existing(key: str, existing: ArtifactRef | None) -> ArtifactRef | None:
    if existing is None:
        return None
    runtime().store.read_bytes(existing)
    return runtime().ledger.complete_operation(key, existing)


@activity.defn
async def admit_activity(spec: RunSpec) -> ArtifactRef:
    key = operation_key("phase_a_admit", spec)
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(key, "phase_a_admit", canonical_sha256(spec)),
    )
    if existing:
        return existing
    output = runtime().store.put_bytes(
        kind="phase-a-admission",
        data=canonical_json(spec).encode("utf-8"),
        producer_operation_id=key,
        input_hashes=(
            spec.subject_sha256,
            spec.intent_sha256,
            spec.resolution_sha256,
            spec.executor_capability_sha256,
        ),
    )
    runtime().ledger.record_effect(key, output)
    return runtime().ledger.complete_operation(key, output)


@activity.defn
async def prepare_activity(stage: StageInput) -> ArtifactRef:
    if len(stage.upstream) != 1 or stage.upstream[0].kind != "phase-a-admission":
        raise ValueError("Prepare requires an admission artifact")
    key = operation_key("phase_a_prepare", stage)
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(key, "phase_a_prepare", canonical_sha256(stage)),
    )
    if existing:
        return existing
    output = runtime().store.put_bytes(
        kind="phase-a-prepared",
        data=f"prepared:{stage.spec.run_id}:{stage.upstream[0].sha256}".encode("utf-8"),
        producer_operation_id=key,
        input_hashes=stage.input_hashes,
    )
    runtime().ledger.record_effect(key, output)
    return runtime().ledger.complete_operation(key, output)


@activity.defn
async def record_decision_activity(decision: GateDecision) -> None:
    runtime().ledger.record_decision(decision)


@activity.defn
async def apply_activity(stage: StageInput) -> ArtifactRef:
    if (
        tuple(reference.kind for reference in stage.upstream)
        != ("phase-a-admission", "phase-a-prepared")
        or stage.decision is None
    ):
        raise ValueError("Apply requires approved admission and prepared artifacts")
    if not runtime().ledger.has_decision(stage.decision):
        raise ValueError("Apply decision is not recorded")
    key = operation_key("phase_a_apply", stage)
    existing = _adopt_existing(
        key,
        runtime().ledger.begin_operation(key, "phase_a_apply", canonical_sha256(stage)),
    )
    if existing:
        return existing
    try:
        output = runtime().store.put_bytes(
            kind="phase-a-output",
            data=(
                f"applied:{stage.spec.run_id}:{stage.upstream[-1].sha256}:"
                f"{canonical_sha256(stage.decision)}"
            ).encode("utf-8"),
            producer_operation_id=key,
            input_hashes=stage.input_hashes,
        )
        runtime().ledger.record_effect(key, output)
        for elapsed in range(stage.spec.apply_delay_seconds):
            activity.heartbeat({"elapsed_seconds": elapsed})
            await asyncio.sleep(1)
        if stage.spec.crash_after_effect and activity.info().attempt == 1:
            raise ApplicationError("Injected post-effect failure", type="InjectedPostEffect")
        return runtime().ledger.complete_operation(key, output)
    except asyncio.CancelledError:
        runtime().ledger.quarantine_operation(key)
        raise
