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
        self.assertEqual(
            kwargs["progress_msg"],
            "🚧 👷 The Bobs Are Still Tearing Down: zolab-ai-abc123 👷🚧",
        )

    async def test_completion_messages_preserve_full_teardown_output_across_chunks(self) -> None:
        worker = BackgroundWorker(
            queue_client=object(),
            proactive=object(),
            deploy_script=Path("/tmp/deploy-foundry-env.ps1"),
        )

        result = "\n".join([
            "Resolved subscriptions:",
            "Managed role matched: Reader @ /subscriptions/example/resourceGroups/zolab-ai-abc123",
            "Removing managed role: Reader @ /subscriptions/example/resourceGroups/zolab-ai-abc123",
            "Removing deployment record 'foundry-ai-env-abc123'...",
            "Removed build info file '/app/build_info-abc123.json' for 'zolab-ai-abc123'.",
            "=== Cleanup complete ===",
            "● 🧹🗑️ Teardown complete!",
        ])

        messages = worker._build_job_completion_messages(
            "job-123",
            "teardown",
            result * 40,
        )

        self.assertGreater(len(messages), 1)
        combined = "\n".join(messages)
        self.assertIn("Removing managed role: Reader", combined)
        self.assertIn("Removing deployment record 'foundry-ai-env-abc123'...", combined)
        self.assertIn("Removed build info file '/app/build_info-abc123.json'", combined)
        self.assertIn("● 🧹🗑️ Teardown complete!", combined)

    async def test_run_list_builds_reports_no_active_builds(self) -> None:
        worker = BackgroundWorker(
            queue_client=object(),
            proactive=object(),
            deploy_script=Path("/tmp/deploy-foundry-env.ps1"),
        )
        worker._get_foundry_resource_group_names = lambda: []
        worker._get_build_info_paths = lambda: []

        result = await worker._run_list_builds()

        self.assertIn("● 📚 Foundry builds", result)
        self.assertIn("No active managed resource groups found ℹ️", result)

    async def test_run_list_builds_includes_active_and_orphaned_build_info(self) -> None:
        worker = BackgroundWorker(
            queue_client=object(),
            proactive=object(),
            deploy_script=Path("/tmp/deploy-foundry-env.ps1"),
        )
        active_path = Path("/tmp/build_info-abc123.json")
        orphan_path = Path("/tmp/build_info-orphan.json")

        worker._get_foundry_resource_group_names = lambda: ["zolab-ai-abc123"]
        worker._get_build_info_paths = lambda: [active_path, orphan_path]
        worker._get_build_info_record_for_resource_group = AsyncMock(
            return_value=(active_path, {"genai_model": "gpt-5.4", "rg": "zolab-ai-abc123"})
        )
        worker._load_build_info_file = lambda path: {
            "rg": "zolab-ai-orphan1" if path == orphan_path else "zolab-ai-abc123"
        }

        result = await worker._run_list_builds()

        self.assertIn(
            "1. zolab-ai-abc123 — model: gpt-5.4 — build info: build_info-abc123.json ✅",
            result,
        )
        self.assertIn("Orphaned build info files:", result)
        self.assertIn(
            "- build_info-orphan.json — resource group: zolab-ai-orphan1 ⚠️",
            result,
        )


if __name__ == "__main__":
    unittest.main()