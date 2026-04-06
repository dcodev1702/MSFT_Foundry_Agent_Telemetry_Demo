from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_framework_orchestrator import AgentFrameworkCommandOrchestrator


class AgentFrameworkCommandOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_docs_lookup_falls_back_when_disabled(self) -> None:
        docs_service = AsyncMock()
        docs_service.get_docs_text = AsyncMock(return_value="direct docs")
        orchestrator = AgentFrameworkCommandOrchestrator(
            docs_service,
            enabled=False,
        )

        result = await orchestrator.get_msft_docs_text("managed identity")

        self.assertEqual(result, "direct docs")
        docs_service.get_docs_text.assert_awaited_once_with("managed identity")

    async def test_docs_lookup_uses_agent_runner_when_enabled(self) -> None:
        docs_service = AsyncMock()
        docs_service.get_docs_text = AsyncMock(return_value="direct docs")
        captured: dict[str, object] = {}

        async def fake_runner(**kwargs) -> str:
            captured.update(kwargs)
            return "agent answer\nwith grounding"

        orchestrator = AgentFrameworkCommandOrchestrator(
            docs_service,
            enabled=True,
            agent_runner=fake_runner,
        )

        result = await orchestrator.get_msft_docs_text("how do I use managed identity")

        self.assertEqual(result, "agent answer\nwith grounding")
        self.assertEqual(captured["name"], "MicrosoftLearnCommandAgent")
        self.assertEqual(captured["prompt"], "how do I use managed identity")
        self.assertEqual(len(captured["tools"]), 1)
        docs_service.get_docs_text.assert_not_awaited()

    async def test_build_guidance_falls_back_to_supported_models(self) -> None:
        docs_service = AsyncMock()
        docs_service.get_docs_text = AsyncMock(return_value="unused")
        orchestrator = AgentFrameworkCommandOrchestrator(
            docs_service,
            enabled=False,
            allowed_models=["gpt-4.1-mini", "gpt-5.4"],
        )

        result = await orchestrator.get_build_guidance("build it", invalid_model="bad-model")

        self.assertIn("Unknown model `bad-model`.", result)
        self.assertIn("`gpt-4.1-mini`", result)
        self.assertIn("`gpt-5.4`", result)

    async def test_build_guidance_uses_agent_runner_when_enabled(self) -> None:
        docs_service = AsyncMock()
        docs_service.get_docs_text = AsyncMock(return_value="unused")
        captured: dict[str, object] = {}

        async def fake_runner(**kwargs) -> str:
            captured.update(kwargs)
            return "Pick gpt-5.4 for broadest coverage."

        orchestrator = AgentFrameworkCommandOrchestrator(
            docs_service,
            enabled=True,
            agent_runner=fake_runner,
        )

        result = await orchestrator.get_build_guidance("build it", invalid_model=None)

        self.assertEqual(result, "Pick gpt-5.4 for broadest coverage.")
        self.assertEqual(captured["name"], "FoundryBuildGuidanceAgent")
        self.assertIn("Explain how to continue", captured["prompt"])


if __name__ == "__main__":
    unittest.main()