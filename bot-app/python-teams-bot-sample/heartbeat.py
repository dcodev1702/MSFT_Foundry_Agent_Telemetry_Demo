# ════════════════════════════════════════════════════════════════
# heartbeat.py — Automatic heartbeat service
# Broadcasts bot health metrics to all stored conversations
# every 15 minutes, and provides on-demand heartbeat text.
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from conversation_store import JsonConversationStore
    from job_dispatcher import FileJobDispatcher
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)


class HeartbeatService:
    """Broadcasts live bot health metrics every 15 minutes."""

    INTERVAL = 900  # 15 minutes in seconds

    def __init__(
        self,
        proactive: ProactiveMessenger,
        store: JsonConversationStore,
        dispatcher: FileJobDispatcher,
    ):
        self._proactive = proactive
        self._store = store
        self._dispatcher = dispatcher
        self._running = False
        self._started_at = datetime.now(timezone.utc)
        self._last_response_utc = "No response sent yet."

    def stop(self) -> None:
        self._running = False

    def update_last_response(self, utc_str: str) -> None:
        """Called by the message handler to track the last response time."""
        self._last_response_utc = utc_str

    async def run(self) -> None:
        """Background loop — broadcasts heartbeat every INTERVAL seconds."""
        self._running = True
        logger.info("HeartbeatService started — interval: %ds", self.INTERVAL)

        while self._running:
            await asyncio.sleep(self.INTERVAL)
            if not self._running:
                break
            try:
                text = self.get_heartbeat_text()
                sent = await self._proactive.broadcast(text)
                logger.info("Heartbeat broadcast to %d conversations", sent)
            except Exception as e:
                logger.error("Heartbeat broadcast error: %s", e)

    def get_heartbeat_text(
        self,
        requester: str | None = None,
        team_id: str | None = None,
        channel_id: str | None = None,
    ) -> str:
        """Build the heartbeat status message with live system metrics."""
        now = datetime.now(timezone.utc)
        uptime = now - self._started_at
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        mem_mb = mem_info.rss / (1024 * 1024)

        lines = [
            "🟢 Status: Online ✅",
            "📜 Script: foundry-teams-bot (M365 Agents SDK)",
            f"🆔 PID: {os.getpid()}",
            f"🐍 Python: {sys.version.split()[0]}",
            f"⏱️ Uptime: {uptime_str}",
            f"🧠 Memory: {mem_mb:.1f} MB",
            f"📦 Queue depth: {self._dispatcher.queue_depth()}",
            f"💬 Last response: {self._last_response_utc}",
        ]

        if team_id and channel_id:
            lines.append(f"📢 Listening in: team={team_id} channel={channel_id}")

        if requester:
            lines.append(f"👤 Identity: {requester}")

        lines.append(f"🕒 Checked at: {now.strftime('%Y-%m-%d %H:%M:%SZ')}")

        return "<br>".join(lines)
