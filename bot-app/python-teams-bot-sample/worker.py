# ════════════════════════════════════════════════════════════════
# worker.py — Background job worker (Azure Queue Storage)
#
# Polls Azure Queue Storage for queued jobs, executes them via
# PowerShell, and sends results back via proactive messaging.
# Authenticates via DefaultAzureCredential (Entra ID / RBAC).
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from azure.storage.queue import QueueClient

if TYPE_CHECKING:
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)
WORKER_BUILD_INFO_PATH = Path(__file__).resolve().parents[2] / "worker-build-info.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _load_worker_build_info() -> dict[str, str]:
    try:
        payload = json.loads(WORKER_BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "build_utc": "unknown",
            "build_commit": "unknown",
            "build_source": "unknown",
        }

    return {
        "build_utc": str(payload.get("build_utc") or "unknown"),
        "build_commit": str(payload.get("build_commit") or "unknown"),
        "build_source": str(payload.get("build_source") or "unknown"),
    }


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
        self._worker_build_info = _load_worker_build_info()

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
                    f"Job `{job_id}` (`{operation}`) completed:<br><pre>{truncated}</pre>",
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
        requested_by = job_data.get("requested_by")
        requested_by_upn = job_data.get("requested_by_upn")
        requested_by_object_id = job_data.get("requested_by_object_id")

        if operation == "build":
            return await self._run_build(
                job_id,
                model,
                conversation_id,
                requested_by=requested_by,
                requested_by_upn=requested_by_upn,
                requested_by_object_id=requested_by_object_id,
            )
        elif operation == "teardown":
            return await self._run_teardown(
                job_id,
                resource_group,
                conversation_id,
                requested_by=requested_by,
                requested_by_upn=requested_by_upn,
                requested_by_object_id=requested_by_object_id,
            )
        elif operation == "build-status":
            return await self._run_build_status(resource_group)
        elif operation == "list-builds":
            return await self._run_list_builds()
        else:
            return f"Unknown operation: {operation}"

    async def _run_build(
        self,
        job_id: str,
        model: str | None,
        conversation_id: str,
        *,
        requested_by: str | None,
        requested_by_upn: str | None,
        requested_by_object_id: str | None,
    ) -> str:
        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
        ]
        requester_identity = requested_by_upn or requested_by
        if requester_identity:
            args.extend(["-RequestedBy", requester_identity])
        if requested_by_object_id:
            args.extend(["-RequestedByObjectId", requested_by_object_id])
        if model:
            args.extend(["-SelectedAiModel", model])

        output = await self._run_powershell(
            args,
            conversation_id,
            progress_msg="🚧 👷 The Bobs Are Still Building 👷🚧 ",
        )

        # Upload build_info file to blob storage and notify the channel
        await self._upload_build_info(conversation_id)

        return output

    async def _upload_build_info(self, conversation_id: str) -> None:
        """Find build_info-*.json, upload to blob storage, send download link."""
        try:
            from storage_config import get_blob_service_client, BLOB_CONTAINER_NAME

            # build_info is written to /app/ (parent of deployment/)
            build_info_dir = self._deploy_script.parent.parent
            files = sorted(
                glob.glob(str(build_info_dir / "build_info-*.json")),
                key=lambda f: Path(f).stat().st_mtime,
                reverse=True,
            )
            if not files:
                logger.warning("No build_info-*.json found after build")
                return

            latest = Path(files[0])
            blob_name = f"builds/{latest.name}"

            loop = asyncio.get_event_loop()
            blob_client = get_blob_service_client().get_blob_client(
                container=BLOB_CONTAINER_NAME, blob=blob_name,
            )

            data = latest.read_bytes()
            await loop.run_in_executor(
                None,
                lambda: blob_client.upload_blob(data, overwrite=True),
            )
            logger.info("Uploaded %s to blob %s", latest.name, blob_name)

            # Build download URL via the bot's own endpoint
            import os
            fqdn = os.getenv(
                "BOT_FQDN",
                "zolab-bot-ca-botprd.wonderfulisland-c279134c.eastus2.azurecontainerapps.io",
            )
            download_url = f"https://{fqdn}/api/download/{latest.name}"

            await self._proactive.send_to_conversation(
                conversation_id,
                f"📄 Build info: [`{latest.name}`]({download_url})",
            )
        except Exception as e:
            logger.error("Failed to upload build_info: %s", e)

    async def _run_teardown(
        self,
        job_id: str,
        resource_group: str | None,
        conversation_id: str,
        *,
        requested_by: str | None,
        requested_by_upn: str | None,
        requested_by_object_id: str | None,
    ) -> str:
        if not resource_group:
            return "Error: No resource group specified for teardown."

        args = [
            "pwsh", "-NoProfile", "-NonInteractive", "-File",
            str(self._deploy_script),
            "-Cleanup", "-CleanupResourceGroup", resource_group,
        ]
        requester_identity = requested_by_upn or requested_by
        if requester_identity:
            args.extend(["-RequestedBy", requester_identity])
        if requested_by_object_id:
            args.extend(["-RequestedByObjectId", requested_by_object_id])

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
        output = stdout.decode("utf-8", errors="replace")
        return self._prepend_worker_metadata(output)

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
        output = stdout.decode("utf-8", errors="replace")
        return self._prepend_worker_metadata(output)

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

    def _prepend_worker_metadata(self, output: str) -> str:
        metadata_lines = [
            "=== Worker Runtime Metadata ===",
            f"worker_host: {os.getenv('HOSTNAME', 'unknown')}",
            f"worker_build_utc: {self._worker_build_info['build_utc']}",
            f"worker_build_commit: {self._worker_build_info['build_commit']}",
            f"worker_build_source: {self._worker_build_info['build_source']}",
            f"worker_executed_utc: {_utc_now()}",
            f"worker_deploy_script: {self._deploy_script}",
            "",
        ]
        return "\n".join(metadata_lines) + output
