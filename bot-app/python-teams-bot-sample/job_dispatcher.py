# ════════════════════════════════════════════════════════════════
# job_dispatcher.py — Azure Queue Storage job dispatcher
#
# Sends queued jobs to Azure Queue Storage and reports queue depth.
# Authenticates via DefaultAzureCredential (Entra ID / RBAC).
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging

from azure.storage.queue import QueueClient

from models import QueuedJob
from storage_config import get_queue_client

logger = logging.getLogger(__name__)


class AzureQueueJobDispatcher:
    """Dispatches jobs to Azure Queue Storage."""

    def __init__(self, queue_client: QueueClient | None = None):
        self._queue_client = queue_client or get_queue_client()

    def enqueue(self, job: QueuedJob) -> str:
        """Send a job to the Azure queue. Returns the message ID."""
        message_body = json.dumps(job.to_dict())
        receipt = self._queue_client.send_message(message_body)
        logger.info("Enqueued job %s — message_id=%s", job.job_id, receipt["id"])
        return receipt["id"]

    def queue_depth(self) -> int:
        """Return approximate message count in the queue."""
        props = self._queue_client.get_queue_properties()
        return props.approximate_message_count
