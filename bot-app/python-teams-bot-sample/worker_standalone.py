# ════════════════════════════════════════════════════════════════
# worker_standalone.py — Standalone BackgroundWorker entrypoint
#
# Runs the BackgroundWorker without the aiohttp web server.
# Designed for ACI deployment where the worker polls Azure Queue
# Storage for queued jobs and sends results via proactive messaging.
#
# Start:  python worker_standalone.py
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import os
from os import environ
from pathlib import Path

from dotenv import load_dotenv

from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter

from conversation_store import BlobConversationStore
from proactive import ProactiveMessenger
from storage_config import get_queue_client
from worker import BackgroundWorker

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────
BASE_PATH = Path(__file__).resolve().parent

DEFAULT_DEPLOY_SCRIPT = str(
    BASE_PATH.parent.parent / "deployment" / "deploy-foundry-env.ps1"
)

# ── Environment ────────────────────────────────────────────────
load_dotenv(BASE_PATH / ".env")
DEPLOY_SCRIPT = Path(os.getenv("DEPLOY_SCRIPT_PATH", DEFAULT_DEPLOY_SCRIPT))

# ── SDK Bootstrap (outbound messaging only — no web server) ───
agents_sdk_config = load_configuration_from_env(environ)
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)

# ── Business Services ─────────────────────────────────────────
queue_client = get_queue_client()
store = BlobConversationStore()
proactive = ProactiveMessenger(adapter=adapter, store=store)

worker = BackgroundWorker(
    queue_client=queue_client,
    proactive=proactive,
    deploy_script=DEPLOY_SCRIPT,
)


async def main() -> None:
    logger.info("Standalone worker starting — polling Azure Queue Storage")
    logger.info("Deploy script: %s", DEPLOY_SCRIPT)
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped by user")
