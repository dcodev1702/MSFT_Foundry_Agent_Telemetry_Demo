# ════════════════════════════════════════════════════════════════
# storage_config.py — Shared Azure Storage client factory
#
# Creates DefaultAzureCredential and Azure Storage clients for
# Queue Storage (job dispatch) and Blob Storage (conversation state).
# Authenticates via Entra ID / RBAC — no storage account keys.
#
# Environment variables:
#   AZURE_STORAGE_ACCOUNT  — storage account name (required)
#   AZURE_QUEUE_NAME       — queue name (default: botjobs)
#   AZURE_BLOB_CONTAINER   — blob container name (default: botstate)
#   AZURE_CLIENT_ID        — UAMI client ID (for managed identity selection)
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import os
import logging

from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient
from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

QUEUE_NAME = os.getenv("AZURE_QUEUE_NAME", "botjobs")
BLOB_CONTAINER_NAME = os.getenv("AZURE_BLOB_CONTAINER", "botstate")

_credential: DefaultAzureCredential | None = None


def get_storage_account_name() -> str:
    storage_account_name = os.getenv("AZURE_STORAGE_ACCOUNT")
    if storage_account_name:
        return storage_account_name

    raise RuntimeError("AZURE_STORAGE_ACCOUNT is required for storage access.")


def get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        managed_identity_client_id = os.getenv("AZURE_CLIENT_ID")
        _credential = DefaultAzureCredential(
            managed_identity_client_id=managed_identity_client_id,
        )
        logger.info(
            "DefaultAzureCredential initialised (AZURE_CLIENT_ID=%s)",
            managed_identity_client_id or "<not set — using CLI fallback>",
        )
    return _credential


def get_queue_client() -> QueueClient:
    return QueueClient(
        account_url=f"https://{get_storage_account_name()}.queue.core.windows.net",
        queue_name=QUEUE_NAME,
        credential=get_credential(),
    )


def get_blob_service_client() -> BlobServiceClient:
    return BlobServiceClient(
        account_url=f"https://{get_storage_account_name()}.blob.core.windows.net",
        credential=get_credential(),
    )
