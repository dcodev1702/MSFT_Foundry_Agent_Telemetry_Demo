from __future__ import annotations

import sys
import unittest
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from heartbeat import HeartbeatService
from service_lifecycle import start_background_services, stop_background_services


class HeartbeatServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_interval_seconds_uses_six_hour_default(self) -> None:
        self.assertEqual(HeartbeatService.resolve_interval_seconds(None), 21600)
        self.assertEqual(HeartbeatService.resolve_interval_seconds(""), 21600)

    def test_resolve_interval_seconds_accepts_positive_override(self) -> None:
        self.assertEqual(HeartbeatService.resolve_interval_seconds("3600"), 3600)

    def test_resolve_interval_seconds_rejects_invalid_values(self) -> None:
        self.assertEqual(HeartbeatService.resolve_interval_seconds("abc"), 21600)
        self.assertEqual(HeartbeatService.resolve_interval_seconds("0"), 21600)

    async def test_run_broadcasts_to_all_stored_conversations_each_interval(self) -> None:
        proactive = SimpleNamespace(broadcast=AsyncMock(return_value=2))
        store = MagicMock()
        dispatcher = SimpleNamespace(queue_depth=lambda: 3)

        heartbeat = HeartbeatService(
            proactive=proactive,
            store=store,
            dispatcher=dispatcher,
        )
        heartbeat.INTERVAL = 0.01

        task = __import__("asyncio").create_task(heartbeat.run())
        await __import__("asyncio").sleep(0.03)
        heartbeat.stop()
        await task

        proactive.broadcast.assert_awaited()
        sent_text = proactive.broadcast.await_args_list[0].args[0]
        self.assertIn("Status: Online", sent_text)
        self.assertIn("Queue depth: 3", sent_text)

    async def test_heartbeat_includes_llm_model_line(self) -> None:
        proactive = SimpleNamespace(broadcast=AsyncMock(return_value=1))
        store = MagicMock()
        dispatcher = SimpleNamespace(queue_depth=lambda: 0)

        heartbeat = HeartbeatService(
            proactive=proactive,
            store=store,
            dispatcher=dispatcher,
        )

        previous_value = environ.get("WEATHER_LLM_MODEL")
        environ["WEATHER_LLM_MODEL"] = "gpt-5.3-chat"
        try:
            text = heartbeat.get_heartbeat_text()
        finally:
            if previous_value is None:
                environ.pop("WEATHER_LLM_MODEL", None)
            else:
                environ["WEATHER_LLM_MODEL"] = previous_value

        self.assertIn("🤖 LLM: gpt-5.3-chat", text)


class BackgroundServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_background_services_runs_heartbeat_even_when_worker_disabled(self) -> None:
        app = {}
        worker = SimpleNamespace(run=AsyncMock(), stop=MagicMock())
        heartbeat_service = SimpleNamespace(run=AsyncMock(), stop=MagicMock())

        await start_background_services(
            app,
            worker=worker,
            heartbeat_service=heartbeat_service,
            worker_enabled=False,
            heartbeat_enabled=True,
        )

        self.assertNotIn("worker_task", app)
        self.assertIn("heartbeat_task", app)

        await stop_background_services(
            app,
            worker=worker,
            heartbeat_service=heartbeat_service,
            worker_enabled=False,
            heartbeat_enabled=True,
        )

        heartbeat_service.stop.assert_called_once()
        worker.stop.assert_not_called()

    async def test_start_background_services_can_run_both_worker_and_heartbeat(self) -> None:
        app = {}
        worker = SimpleNamespace(run=AsyncMock(), stop=MagicMock())
        heartbeat_service = SimpleNamespace(run=AsyncMock(), stop=MagicMock())

        await start_background_services(
            app,
            worker=worker,
            heartbeat_service=heartbeat_service,
            worker_enabled=True,
            heartbeat_enabled=True,
        )

        self.assertIn("worker_task", app)
        self.assertIn("heartbeat_task", app)

        await stop_background_services(
            app,
            worker=worker,
            heartbeat_service=heartbeat_service,
            worker_enabled=True,
            heartbeat_enabled=True,
        )

        worker.stop.assert_called_once()
        heartbeat_service.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()