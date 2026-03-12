# ════════════════════════════════════════════════════════════════
# conversation_store.py — Azure Blob Storage conversation persistence
#
# Stores conversation metadata, references, and identities as JSON
# blobs for proactive messaging.  Authenticates via
# DefaultAzureCredential (Entra ID / RBAC).
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

from storage_config import get_blob_service_client, BLOB_CONTAINER_NAME

logger = logging.getLogger(__name__)

_CONVERSATIONS_BLOB = "conversations.json"
_REFERENCES_BLOB = "references.json"
_IDENTITIES_BLOB = "identities.json"


class BlobConversationStore:
    """Persists conversation metadata and proactive-messaging references
    to Azure Blob Storage.

    Three blobs in the container:
      - conversations.json  — channel/team metadata for each conversation
      - references.json     — serialised ConversationReference dicts
      - identities.json     — serialised ClaimsIdentity dicts
    """

    def __init__(
        self,
        blob_service_client: BlobServiceClient | None = None,
        container_name: str = BLOB_CONTAINER_NAME,
    ):
        self._client = blob_service_client or get_blob_service_client()
        self._container: ContainerClient = self._client.get_container_client(
            container_name
        )
        try:
            self._container.get_container_properties()
        except ResourceNotFoundError:
            self._container.create_container()
            logger.info("Created blob container: %s", container_name)

    # ── Conversation metadata ────────────────────────────────────

    def save(self, conversation_id: str, payload: dict[str, Any]) -> None:
        data = self._read_blob(_CONVERSATIONS_BLOB)
        data[conversation_id] = payload
        self._write_blob(_CONVERSATIONS_BLOB, data)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_blob(_CONVERSATIONS_BLOB).get(conversation_id)

    # ── Conversation references (for proactive messaging) ────────

    def save_reference(self, conversation_id: str, ref_dict: dict[str, Any]) -> None:
        data = self._read_blob(_REFERENCES_BLOB)
        data[conversation_id] = ref_dict
        self._write_blob(_REFERENCES_BLOB, data)

    def get_reference(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_blob(_REFERENCES_BLOB).get(conversation_id)

    def get_all_reference_ids(self) -> list[str]:
        return list(self._read_blob(_REFERENCES_BLOB).keys())

    # ── Claims identities (for proactive messaging auth) ─────────

    def save_identity(self, conversation_id: str, identity_dict: dict[str, Any]) -> None:
        data = self._read_blob(_IDENTITIES_BLOB)
        data[conversation_id] = identity_dict
        self._write_blob(_IDENTITIES_BLOB, data)

    def get_identity(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_blob(_IDENTITIES_BLOB).get(conversation_id)

    # ── Internal helpers ─────────────────────────────────────────

    def _read_blob(self, blob_name: str) -> dict[str, Any]:
        try:
            blob_client = self._container.get_blob_client(blob_name)
            content = blob_client.download_blob().readall()
            return json.loads(content)
        except ResourceNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in blob %s — returning empty dict", blob_name)
            return {}

    def _write_blob(self, blob_name: str, data: dict[str, Any]) -> None:
        blob_client = self._container.get_blob_client(blob_name)
        blob_client.upload_blob(
            json.dumps(data, indent=2),
            overwrite=True,
        )
