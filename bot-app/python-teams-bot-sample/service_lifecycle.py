from __future__ import annotations

import asyncio
import logging


logger = logging.getLogger(__name__)


async def start_background_services(
    app,
    *,
    worker,
    heartbeat_service,
    worker_enabled: bool,
    heartbeat_enabled: bool,
) -> None:
    if worker_enabled:
        logger.info("Starting background worker …")
        app["worker_task"] = asyncio.create_task(worker.run())
    else:
        logger.info("Worker disabled (WORKER_ENABLED=false) — ACI handles execution")

    if heartbeat_enabled:
        logger.info("Starting heartbeat service …")
        app["heartbeat_task"] = asyncio.create_task(heartbeat_service.run())
    else:
        logger.info("Heartbeat disabled (HEARTBEAT_ENABLED=false)")


async def stop_background_services(
    app,
    *,
    worker,
    heartbeat_service,
    worker_enabled: bool,
    heartbeat_enabled: bool,
) -> None:
    if worker_enabled:
        worker.stop()

    if heartbeat_enabled:
        heartbeat_service.stop()

    for task_name in ("worker_task", "heartbeat_task"):
        task = app.get(task_name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass