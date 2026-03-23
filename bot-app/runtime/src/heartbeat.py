# ════════════════════════════════════════════════════════════════
# heartbeat.py — Automatic heartbeat service
# Broadcasts bot health metrics to all stored conversations
# every 4 hours by default, and provides on-demand heartbeat text.
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
    from conversation_store import BlobConversationStore
    from job_dispatcher import AzureQueueJobDispatcher
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)


class HeartbeatService:
    """Broadcasts live bot health metrics every 4 hours by default."""

    INTERVAL = 14400  # 4 hours in seconds
    DEFAULT_LLM_MODEL = "gpt-5.3-chat"

    @classmethod
    def resolve_interval_seconds(cls, raw_value: str | None) -> int:
        """Parse the configured heartbeat interval or fall back to the default."""
        if raw_value is None or not raw_value.strip():
            return cls.INTERVAL

        try:
            interval_seconds = int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid HEARTBEAT_INTERVAL_SECONDS=%r; defaulting to %d",
                raw_value,
                cls.INTERVAL,
            )
            return cls.INTERVAL

        if interval_seconds <= 0:
            logger.warning(
                "Non-positive HEARTBEAT_INTERVAL_SECONDS=%r; defaulting to %d",
                raw_value,
                cls.INTERVAL,
            )
            return cls.INTERVAL

        return interval_seconds

    def __init__(
        self,
        proactive: ProactiveMessenger,
        store: BlobConversationStore,
        dispatcher: AzureQueueJobDispatcher,
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
        llm_model = (os.getenv("WEATHER_LLM_MODEL", self.DEFAULT_LLM_MODEL).strip() or self.DEFAULT_LLM_MODEL)

        lines = [
            "🟢 Status: Online ✅",
            "📜 Script: Bot-the-Builder (M365 Agents SDK)",
            f"🤖 LLM: {llm_model}",
            f"🆔 PID: {os.getpid()}",
            f"🐍 Python: {sys.version.split()[0]}",
            f"⏱️ Uptime: {uptime_str}",
            f"🧠 Memory: {mem_mb:.1f} MB",
            f"📦 Queue depth: {self._dispatcher.queue_depth()}",
            f"💬 Last response: {self._last_response_utc}",
        ]

        if channel_id:
            if team_id and team_id != "unknown":
                lines.append(f"📢 Listening in: team={team_id} channel={channel_id}")
            else:
                lines.append(f"📢 Listening in: channel={channel_id}")

        if requester:
            lines.append(f"👤 Identity: {requester}")

        lines.append(f"🕒 Checked at: {now.strftime('%Y-%m-%d %H:%M:%SZ')}")

        return "<br>".join(lines)
