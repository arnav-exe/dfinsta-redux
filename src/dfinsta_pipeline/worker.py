from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from .activities import (
    admit_activity,
    apply_activity,
    configure_runtime,
    prepare_activity,
    record_decision_activity,
)
from .workflow import PortRunWorkflow


async def run_worker(
    endpoint: str,
    task_queue: str,
    state_root: Path,
    deployment_name: str,
    build_id: str,
) -> None:
    configure_runtime(state_root)
    client = await Client.connect(endpoint)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[PortRunWorkflow],
        activities=[admit_activity, prepare_activity, record_decision_activity, apply_activity],
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
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            args.endpoint,
            args.task_queue,
            args.state_root,
            args.deployment_name,
            args.build_id,
        )
    )


if __name__ == "__main__":
    main()
