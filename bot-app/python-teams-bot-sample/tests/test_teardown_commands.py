from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


SAMPLE_DIR = Path(__file__).resolve().parents[1]
if str(SAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(SAMPLE_DIR))

azure_module = sys.modules.setdefault("azure", types.ModuleType("azure"))
storage_module = sys.modules.setdefault("azure.storage", types.ModuleType("azure.storage"))
queue_module = sys.modules.setdefault("azure.storage.queue", types.ModuleType("azure.storage.queue"))


class _QueueClient:
    pass


queue_module.QueueClient = _QueueClient
storage_module.queue = queue_module
azure_module.storage = storage_module

from command_parser import parse_command
from models import TeardownSession
from worker import BackgroundWorker


class TeardownCommandTests(unittest.TestCase):
    def test_parse_command_accepts_bare_teardown(self) -> None:
        command = parse_command("teardown")

        self.assertEqual(command.kind, "teardown")
        self.assertIsNone(command.resource_group)
        self.assertTrue(command.requires_confirmation)

    def test_parse_command_accepts_targeted_teardown(self) -> None:
        command = parse_command("teardown zolab-ai-abc123")

        self.assertEqual(command.kind, "teardown")
        self.assertEqual(command.resource_group, "zolab-ai-abc123")
        self.assertTrue(command.requires_confirmation)

    def test_teardown_session_defaults_to_selecting_state(self) -> None:
        session = TeardownSession(builds=["zolab-ai-abc123"])

        self.assertEqual(session.builds, ["zolab-ai-abc123"])
        self.assertEqual(session.state, "selecting")
        self.assertIsNone(session.selected_rg)


class WorkerTeardownRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_job_passes_requester_identity_to_targeted_teardown(self) -> None:
        worker = BackgroundWorker(
            queue_client=object(),
            proactive=object(),
            deploy_script=Path("/tmp/deploy-foundry-env.ps1"),
        )
        worker._run_powershell = AsyncMock(return_value="ok")

        result = await worker._execute_job(
            {
                "operation": "teardown",
                "resource_group": "zolab-ai-abc123",
                "requested_by": "requester-display-name",
                "requested_by_upn": "requester@dibsecurity.onmicrosoft.com",
                "requested_by_object_id": "entra-object-id",
                "job_id": "job-123",
            },
            "conversation-123",
        )

        self.assertEqual(result, "ok")
        worker._run_powershell.assert_awaited_once()
        args, kwargs = worker._run_powershell.await_args

        self.assertEqual(
            args[0],
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                "/tmp/deploy-foundry-env.ps1",
                "-Cleanup",
                "-CleanupResourceGroup",
                "zolab-ai-abc123",
                "-RequestedBy",
                "requester@dibsecurity.onmicrosoft.com",
                "-RequestedByObjectId",
                "entra-object-id",
            ],
        )
        self.assertEqual(args[1], "conversation-123")
        self.assertEqual(kwargs["progress_msg"], "Pls hold while we teardown: zolab-ai-abc123")


if __name__ == "__main__":
    unittest.main()