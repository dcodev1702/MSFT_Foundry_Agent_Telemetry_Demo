# ════════════════════════════════════════════════════════════════
# worker.py — Background job worker (Azure Queue Storage)
#
# Polls Azure Queue Storage for queued jobs, executes them via
# PowerShell, and sends results back via proactive messaging.
# Authenticates via DefaultAzureCredential (Entra ID / RBAC).
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from azure.storage.queue import QueueClient

if TYPE_CHECKING:
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class BackgroundWorker:
    """Async background worker that processes queued jobs from Azure Queue Storage."""

    POLL_INTERVAL = 5        # seconds between queue polls
    PROGRESS_INTERVAL = 60   # seconds between progress messages
    VISIBILITY_TIMEOUT = 600 # 10 min — job must complete within this window

    def __init__(
        self,
        queue_client: QueueClient,
        proactive: ProactiveMessenger,
        deploy_script: Path,
    ):
        self._queue_client = queue_client
        self._proactive = proactive
        self._deploy_script = deploy_script
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main poll loop — runs until stop() is called."""
        self._running = True
        logger.info("BackgroundWorker started — polling Azure Queue Storage")

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error("Worker poll error: %s", e)
            await asyncio.sleep(self.POLL_INTERVAL)

    # ── Internal ───────────────────────────────────────────────

    async def _poll_once(self) -> None:
        loop = asyncio.get_event_loop()

        # receive_messages is synchronous — run in executor
        messages = await loop.run_in_executor(
            None,
            lambda: list(
                self._queue_client.receive_messages(
                    messages_per_page=1,
                    visibility_timeout=self.VISIBILITY_TIMEOUT,
                )
            ),
        )

        if not messages:
            return

        msg = messages[0]
        job_data = json.loads(msg.content)
        job_id = job_data.get("job_id", "unknown")
        operation = job_data.get("operation", "unknown")
        conversation_id = job_data.get("conversation_id", "")

        logger.info(
            "Processing job %s — operation: %s (dequeue_count=%d)",
            job_id, operation, msg.dequeue_count,
        )

        try:
            result = await self._execute_job(job_data, conversation_id)

            # Delete message on success
            await loop.run_in_executor(
                None, lambda: self._queue_client.delete_message(msg),
            )

            if result:
                truncated = result[:3000]
                await self._proactive.send_to_conversation(
                    conversation_id,
                    f"Job `{job_id}` (`{operation}`) completed:\n\n```\n{truncated}\n```",
                )
        except Exception as e:
            logger.error("Job %s failed: %s", job_id, e)

            # Delete the poison message
            await loop.run_in_executor(
                None, lambda: self._queue_client.delete_message(msg),
            )

            await self._proactive.send_to_conversation(
                conversation_id,
                f"Job `{job_id}` (`{operation}`) failed:\n\n```\n{str(e)[:2000]}\n```",
            )

    async def _execute_job(self, job_data: dict, conversation_id: str) -> str:
        operation = job_data.get("operation")
        model = job_data.get("model")
        resource_group = job_data.get("resource_group")
        job_id = job_data.get("job_id", "unknown")

        if operation == "build":
            return await self._run_build(job_id, model, conversation_id)
        elif operation == "teardown":
            return await self._run_teardown(job_id, resource_group, conversation_id)
        elif operation == "build-status":
            return await self._run_build_status(resource_group)
        elif operation == "list-builds":
            return await self._run_list_builds()
        else:
            return f"Unknown operation: {operation}"

    async def _run_build(
        self, job_id: str, model: str | None, conversation_id: str
    ) -> str:
        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
        ]
        if model:
            args.extend(["-SelectedAiModel", model])

        return await self._run_powershell(
            args,
            conversation_id,
            progress_msg="One moment ..the Bob's are still building!",
        )

    async def _run_teardown(
        self, job_id: str, resource_group: str | None, conversation_id: str
    ) -> str:
        if not resource_group:
            return "Error: No resource group specified for teardown."

        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
            "-Cleanup", "-CleanupResourceGroup", resource_group,
        ]
        return await self._run_powershell(
            args,
            conversation_id,
            progress_msg=f"Pls hold while we teardown: {resource_group}",
        )

    async def _run_build_status(self, resource_group: str | None) -> str:
        if not resource_group:
            return "Error: No resource group specified for build status."

        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
            "-BuildStatusResourceGroup", resource_group,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")

    async def _run_list_builds(self) -> str:
        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
            "-ListBuilds",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")

    async def _run_powershell(
        self,
        args: list[str],
        conversation_id: str,
        progress_msg: str,
    ) -> str:
        """Execute a PowerShell command with periodic progress updates."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _progress_reporter():
            while True:
                await asyncio.sleep(self.PROGRESS_INTERVAL)
                if proc.returncode is not None:
                    break
                await self._proactive.send_to_conversation(conversation_id, progress_msg)

        progress_task = asyncio.create_task(_progress_reporter())
        try:
            stdout, _ = await proc.communicate()
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

        output = stdout.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(
                f"PowerShell exited with code {proc.returncode}:\n{output[:2000]}"
            )

        return output
