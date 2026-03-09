# ════════════════════════════════════════════════════════════════
# proactive.py — Proactive messaging via stored ConversationReference
# Uses the M365 Agents SDK continue_conversation pattern to deliver
# messages without an inbound activity.
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from microsoft_agents.activity import ConversationReference
from microsoft_agents.hosting.core import MessageFactory, TurnContext
from microsoft_agents.hosting.core.authorization import ClaimsIdentity

if TYPE_CHECKING:
    from microsoft_agents.hosting.aiohttp import CloudAdapter
    from conversation_store import JsonConversationStore

logger = logging.getLogger(__name__)


class ProactiveMessenger:
    """Sends proactive messages to Teams conversations using stored references."""

    def __init__(self, adapter: CloudAdapter, store: JsonConversationStore):
        self._adapter = adapter
        self._store = store

    async def send_to_conversation(self, conversation_id: str, message: str) -> bool:
        """Send a proactive message to a specific conversation. Returns True on success."""
        ref_dict = self._store.get_reference(conversation_id)
        if not ref_dict:
            logger.warning("No conversation reference for %s", conversation_id)
            return False

        identity_dict = self._store.get_identity(conversation_id)

        try:
            reference = ConversationReference.model_validate(ref_dict)
            continuation_activity = reference.get_continuation_activity()

            async def _callback(turn_context: TurnContext) -> None:
                await turn_context.send_activity(MessageFactory.text(message))

            if identity_dict:
                claims_identity = ClaimsIdentity(
                    claims=identity_dict.get("claims", {}),
                    is_authenticated=identity_dict.get("is_authenticated", False),
                    authentication_type=identity_dict.get("authentication_type"),
                )
                await self._adapter.continue_conversation_with_claims(
                    claims_identity=claims_identity,
                    continuation_activity=continuation_activity,
                    callback=_callback,
                )
            else:
                app_id = os.getenv(
                    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", ""
                )
                await self._adapter.continue_conversation(
                    agent_app_id=app_id,
                    continuation_activity=continuation_activity,
                    callback=_callback,
                )

            return True

        except Exception as e:
            logger.error("Failed to send proactive message to %s: %s", conversation_id, e)
            return False

    async def broadcast(self, message: str) -> int:
        """Send a proactive message to ALL stored conversations. Returns count of successes."""
        sent = 0
        for conv_id in self._store.get_all_reference_ids():
            if await self.send_to_conversation(conv_id, message):
                sent += 1
        return sent
