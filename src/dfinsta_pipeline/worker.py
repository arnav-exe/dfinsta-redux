from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from .activities import (
    admit_activity,
    admit_feature_dispositions_activity,
    admit_replay_verification_grant_activity,
    apply_activity,
    configure_runtime,
    prepare_activity,
    prepare_feature_gate_activity,
    prepare_replay_plan_activity,
    prepare_replay_verification_gate_activity,
    record_decision_activity,
    replay_apply_tree_stage_activity,
    replay_build_patched_apk_stage_activity,
    replay_decode_stage_activity,
    replay_install_frameworks_stage_activity,
    replay_verify_final_apk_stage_activity,
    resolve_replay_verification_grant_activity,
)
from .feature_workflow import FeatureAssessmentRunWorkflow
from .replay_workflow import ReplayRunWorkflow
from .workflow import PortRunWorkflow

# Only stage wrappers are registered. The proven checkpoint Activities
# (replay_*_checkpoint_activity) are deliberately absent: they take a full
# AdmittedReplayV3, which embeds the intent, resolution and source manifest by
# value and would put over 100 KB of recipe and source paths into Temporal
# History on every stage. The wrappers take a hash-pinned handle instead and
# load the same authority from the ledger.
REGISTERED_ACTIVITIES = (
    admit_activity,
    prepare_activity,
    record_decision_activity,
    apply_activity,
    prepare_replay_plan_activity,
    replay_install_frameworks_stage_activity,
    replay_decode_stage_activity,
    replay_apply_tree_stage_activity,
    replay_build_patched_apk_stage_activity,
    resolve_replay_verification_grant_activity,
    prepare_replay_verification_gate_activity,
    admit_replay_verification_grant_activity,
    replay_verify_final_apk_stage_activity,
    prepare_feature_gate_activity,
    admit_feature_dispositions_activity,
)

REGISTERED_WORKFLOWS = (PortRunWorkflow, ReplayRunWorkflow, FeatureAssessmentRunWorkflow)

# Worker shutdown cancels running Activities, and a replay stage quarantines its
# operation on cancellation. Quarantine is terminal: the key is refused forever,
# and because operation keys derive from admitted content, recovery from THAT
# needs a new run id, a new run spec and a new human gate decision.
#
# Set above the longest stage budget on purpose, and this is the whole mitigation
# rather than a partial one. A replay stage can only receive a cancellation it
# can act on through two channels: a heartbeat response, and a local
# `WORKER_SHUTDOWN` after this timeout. **No replay stage heartbeats**, so today
# `WORKER_SHUTDOWN` is the only cancellation that reaches one — and a timeout
# longer than any stage means it cannot arrive mid-stage at all. The destructive
# path closes by construction.
#
# 10,800 is the verify budget: `_STAGE_BUDGET_MULTIPLIER["verify"] == 18` against
# a 600-second decode plan. Every other stage is shorter (build 5,400; decode and
# apply 3,600; install_framework 1,800).
#
# **This must be raised again before heartbeats are added.** Heartbeating is what
# opens the channel for server-originated cancellation — a workflow cancel, a
# timeout, or a transient network failure while recording a heartbeat — and every
# one of those lands in the same `except asyncio.CancelledError` that
# quarantines. Adding heartbeats before cancellation is non-destructive would
# convert a flaky thirty seconds of network into a burned admitted run.
#
# THE OPERATING RULE INVERTS what this comment used to say. A worker *kill*
# (SIGKILL) delivers no cancellation at all: it leaves the claim `pending`, which
# `release_pending_operation` can hand to a later attempt, so the run is wedged
# for one `start_to_close` rather than burned. A worker *stop* that reaches the
# end of this window is the destructive one. Until cancellation is genuinely
# non-destructive, killing is the safe way to stop a worker mid-stage.
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 10_800


def parse_executor_path(value: str) -> tuple[str, Path]:
    """Read one `--executor-path SHA256=PATH` argument.

    The digest is the key an admitted capability names, so the mapping cannot be
    derived: a capability pins `executable_sha256`, and only whoever deploys the
    worker knows where a binary with that digest lives on this host. Splitting on
    the FIRST `=` is deliberate -- a path may contain one, a SHA-256 may not.
    """

    if type(value) is not str:
        raise argparse.ArgumentTypeError("Executor path must be a string")
    digest, separator, path = value.partition("=")
    if not separator or not path:
        raise argparse.ArgumentTypeError(
            f"Executor path must be SHA256=PATH, got {value!r}"
        )
    digest = digest.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise argparse.ArgumentTypeError(
            f"Executor path key must be a SHA-256, got {digest!r}"
        )
    return digest, Path(path)


async def run_worker(
    endpoint: str,
    task_queue: str,
    state_root: Path,
    deployment_name: str,
    build_id: str,
    graceful_shutdown_seconds: int = DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
    source_root: Path | None = None,
    executor_paths: Mapping[str, Path] | None = None,
    attempts_root: Path | None = None,
) -> None:
    # A state root alone is not enough to run a replay. `replay_apply_tree` and
    # `replay_verify_final_apk` both refuse without a source root, and every stage
    # that launches a subprocess resolves its executable through `executor_paths`
    # by the digest the admitted capability pins. Neither is derivable from the
    # state root, and neither can live in the CAS -- one is the checked-out source
    # tree, the other must be executable at a real path. So they are deployment
    # arguments, and a worker started without them hosts the registered Workflow
    # and cannot run a single real stage of it.
    configure_runtime(
        state_root,
        attempts_root=attempts_root,
        source_root=source_root,
        executor_paths=executor_paths,
    )
    client = await Client.connect(endpoint)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(REGISTERED_WORKFLOWS),
        activities=list(REGISTERED_ACTIVITIES),
        graceful_shutdown_timeout=timedelta(seconds=graceful_shutdown_seconds),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name, build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.UNSPECIFIED,
        ),
    )
    await worker.run()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="localhost:7233")
    parser.add_argument("--task-queue", default="dfinsta-phase-a")
    parser.add_argument("--state-root", type=Path, default=Path(".pipeline-state"))
    parser.add_argument("--deployment-name", default="dfinsta-pipeline")
    parser.add_argument("--build-id", required=True)
    parser.add_argument(
        "--graceful-shutdown-seconds",
        type=int,
        default=DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="checked-out source tree the apply and verify stages read; without it "
        "both stages refuse",
    )
    parser.add_argument(
        "--executor-path",
        type=parse_executor_path,
        action="append",
        default=[],
        dest="executor_paths",
        metavar="SHA256=PATH",
        help="where a binary with this digest lives on this host; repeatable. An "
        "admitted capability names its executable by digest and nothing else "
        "resolves it to a path",
    )
    parser.add_argument("--attempts-root", type=Path, default=None)
    args = parser.parse_args()
    # Last-one-wins on a repeated digest would silently run a different binary
    # from the one the operator meant, and the capability check would still pass
    # because both paths hash the same only if they are the same file.
    seen: dict[str, Path] = {}
    for digest, path in args.executor_paths:
        if digest in seen and seen[digest] != path:
            parser.error(f"--executor-path names {digest} twice with different paths")
        seen[digest] = path
    asyncio.run(
        run_worker(
            args.endpoint,
            args.task_queue,
            args.state_root,
            args.deployment_name,
            args.build_id,
            args.graceful_shutdown_seconds,
            source_root=args.source_root,
            executor_paths=seen,
            attempts_root=args.attempts_root,
        )
    )


if __name__ == "__main__":
    main()
