# ════════════════════════════════════════════════════════════════
# app.py — Foundry Teams Bot entry point (M365 Agents SDK)
#
# Replaces the Bot Framework SDK adapter with the Microsoft 365
# Agents SDK CloudAdapter + AgentApplication pattern.
#
# Start:  python app.py
# Routes: POST /api/messages  — Teams webhook
#         GET  /api/messages  — health check
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import os
from os import environ
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnState,
)

from conversation_store import BlobConversationStore
from job_dispatcher import AzureQueueJobDispatcher
from proactive import ProactiveMessenger
from storage_config import get_queue_client
from worker import BackgroundWorker
from heartbeat import HeartbeatService

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────
BASE_PATH = Path(__file__).resolve().parent

# Default deploy script path: two levels up → deployment/
DEFAULT_DEPLOY_SCRIPT = str(
    BASE_PATH.parent.parent / "deployment" / "deploy-foundry-env.ps1"
)

# ── Environment ────────────────────────────────────────────────
load_dotenv(BASE_PATH / ".env")
PORT = int(os.getenv("PORT", "3978"))
DEPLOY_SCRIPT = Path(os.getenv("DEPLOY_SCRIPT_PATH", DEFAULT_DEPLOY_SCRIPT))
WORKER_ENABLED = os.getenv("WORKER_ENABLED", "true").lower() in ("true", "1", "yes")

# ── M365 Agents SDK Configuration ─────────────────────────────
agents_sdk_config = load_configuration_from_env(environ)

# ── Core SDK Services ─────────────────────────────────────────
storage = MemoryStorage()
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)
authorization = Authorization(storage, connection_manager, **agents_sdk_config)

# ── Agent Application (decorator-based handler model) ─────────
agent_app = AgentApplication[TurnState](
    storage=storage,
    adapter=adapter,
    authorization=authorization,
    **agents_sdk_config.get("AGENTAPPLICATION", {}),
)

# ── Business Services ─────────────────────────────────────────
queue_client = get_queue_client()
dispatcher = AzureQueueJobDispatcher(queue_client=queue_client)
store = BlobConversationStore()
proactive = ProactiveMessenger(adapter=adapter, store=store)

worker = BackgroundWorker(
    queue_client=queue_client,
    proactive=proactive,
    deploy_script=DEPLOY_SCRIPT,
)

heartbeat_service = HeartbeatService(
    proactive=proactive,
    store=store,
    dispatcher=dispatcher,
)

# ── Register Bot Handlers (Phase 1) ───────────────────────────
from bot import register_handlers  # noqa: E402

register_handlers(
    agent_app,
    dispatcher=dispatcher,
    store=store,
    heartbeat_service=heartbeat_service,
)


# ── Global Error Handler ──────────────────────────────────────
@agent_app.error
async def on_error(context, err: Exception):
    logger.error("Bot turn error: %s", err, exc_info=True)
    await context.send_activity(f"⚠️ Bot error: {err}")


# ── aiohttp Route Handlers ────────────────────────────────────
async def agent_entry_point(request: web.Request) -> web.Response:
    """POST /api/messages — receives inbound Teams activities."""
    response = await start_agent_process(request, agent_app, adapter)
    return response or web.Response(status=202)


async def health_check(request: web.Request) -> web.Response:
    """GET /api/messages — lightweight health probe."""
    return web.json_response({"status": "healthy", "service": "foundry-teams-bot"})


# ── Lifecycle Hooks ────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    if WORKER_ENABLED:
        logger.info("Starting background worker and heartbeat service …")
        app["worker_task"] = asyncio.create_task(worker.run())
        app["heartbeat_task"] = asyncio.create_task(heartbeat_service.run())
    else:
        logger.info("Worker disabled (WORKER_ENABLED=false) — ACI handles execution")


async def on_shutdown(app: web.Application) -> None:
    if WORKER_ENABLED:
        logger.info("Shutting down background services …")
        worker.stop()
        heartbeat_service.stop()

        for task_name in ("worker_task", "heartbeat_task"):
            task = app.get(task_name)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ── Application Factory ───────────────────────────────────────
def create_app(argv=None) -> web.Application:
    app = web.Application(middlewares=[jwt_authorization_middleware])

    # Store SDK objects on the app for middleware access
    app["adapter"] = adapter
    app["agent_app"] = agent_app
    agent_config = connection_manager.get_default_connection_configuration()
    app["agent_configuration"] = agent_config

    # Routes
    app.router.add_post("/api/messages", agent_entry_point)
    app.router.add_get("/api/messages", health_check)

    # Lifecycle
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


app = create_app()

if __name__ == "__main__":
    logger.info("Starting Foundry Teams Bot on port %d …", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)
