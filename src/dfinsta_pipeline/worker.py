from __future__ import annotations

import argparse
import asyncio
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
    prepare_replay_verification_gate_activity,
    admit_replay_verification_grant_activity,
    replay_verify_final_apk_stage_activity,
    prepare_feature_gate_activity,
    admit_feature_dispositions_activity,
)

REGISTERED_WORKFLOWS = (PortRunWorkflow, ReplayRunWorkflow, FeatureAssessmentRunWorkflow)

# Worker shutdown cancels running Activities, and every replay stage quarantines
# its operation on cancellation. Quarantine is terminal: the key is refused
# forever, and because operation keys derive from admitted content, recovery
# needs a new run id, a new run spec and a new human gate decision.
#
# Temporal's default is zero, which cancels immediately. This default is not a
# fix -- a stage can run for 40 minutes, so no shutdown timeout makes stopping
# the worker mid-stage safe. It only prevents the most common accident, an
# operator stopping a worker during the short ledger-only stages. The real rule
# is operational: do not stop a worker while a replay stage is running.
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 300


async def run_worker(
    endpoint: str,
    task_queue: str,
    state_root: Path,
    deployment_name: str,
    build_id: str,
    graceful_shutdown_seconds: int = DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
) -> None:
    configure_runtime(state_root)
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
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            args.endpoint,
            args.task_queue,
            args.state_root,
            args.deployment_name,
            args.build_id,
            args.graceful_shutdown_seconds,
        )
    )


if __name__ == "__main__":
    main()
