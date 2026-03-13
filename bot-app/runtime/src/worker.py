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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from azure.storage.queue import QueueClient

if TYPE_CHECKING:
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)
WORKER_BUILD_INFO_PATH = Path(__file__).resolve().parents[2] / "worker-build-info.json"
RESULT_MESSAGE_CHUNK_SIZE = 12000
FOUNDRY_RESOURCE_GROUP_PATTERN = re.compile(r"^zolab-ai-.{4,}$")
FOUNDRY_RESOURCE_GROUP_SUFFIX_PATTERN = re.compile(r"^zolab-ai-(.+)$")


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


def _get_bot_download_base_url() -> str | None:
    configured_base_url = (os.getenv("BOT_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured_base_url:
        if configured_base_url.startswith(("http://", "https://")):
            return configured_base_url
        return f"https://{configured_base_url}"

    bot_fqdn = (os.getenv("BOT_FQDN") or "").strip().rstrip("/")
    if bot_fqdn:
        return f"https://{bot_fqdn}"

    return None


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
                for message in self._build_job_completion_messages(job_id, operation, result):
                    await self._proactive.send_to_conversation(conversation_id, message)
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

            bot_download_base_url = _get_bot_download_base_url()
            if not bot_download_base_url:
                logger.error(
                    "Build info uploaded but BOT_FQDN/BOT_PUBLIC_BASE_URL is not configured; cannot publish a download link"
                )
                await self._proactive.send_to_conversation(
                    conversation_id,
                    f"📄 Build info uploaded: `{latest.name}`. Download link unavailable because the bot public URL is not configured.",
                )
                return

            download_url = f"{bot_download_base_url}/api/download/{latest.name}"

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
            progress_msg=f"🚧 👷 The Bobs Are Still Tearing Down: {resource_group} 👷🚧",
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
        resource_groups = self._get_foundry_resource_group_names()
        build_info_paths = self._get_build_info_paths()
        matched_build_info_paths: set[Path] = set()

        lines = [
            "",
            "● 📚 Foundry builds",
            "",
        ]

        if not resource_groups:
            lines.append("No active managed resource groups found ℹ️")
        else:
            lines.append("Active resource groups:")
            for index, resource_group_name in enumerate(resource_groups, start=1):
                build_info_record = await self._get_build_info_record_for_resource_group(
                    resource_group_name
                )
                if build_info_record is None:
                    lines.append(
                        f"{index}. {resource_group_name} — build info file missing ❌"
                    )
                    continue

                build_info_path, build_info = build_info_record
                matched_build_info_paths.add(build_info_path)
                model_name = str(build_info.get("genai_model") or "unknown")
                lines.append(
                    f"{index}. {resource_group_name} — model: {model_name} — "
                    f"build info: {build_info_path.name} ✅"
                )

        orphaned_build_info_paths = [
            path for path in build_info_paths if path not in matched_build_info_paths
        ]
        if orphaned_build_info_paths:
            lines.extend([
                "",
                "Orphaned build info files:",
            ])
            for path in orphaned_build_info_paths:
                try:
                    build_info = self._load_build_info_file(path)
                    resource_group_name = str(build_info.get("rg") or "unknown")
                    lines.append(
                        f"- {path.name} — resource group: {resource_group_name} ⚠️"
                    )
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    lines.append(f"- {path.name} — unreadable ⚠️")

        return "\n".join(lines)

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

    def _build_job_completion_messages(
        self,
        job_id: str,
        operation: str,
        result: str,
    ) -> list[str]:
        chunks = self._split_output_chunks(result, RESULT_MESSAGE_CHUNK_SIZE)
        if len(chunks) == 1:
            return [f"Job `{job_id}` (`{operation}`) completed:<br><pre>{chunks[0]}</pre>"]

        messages: list[str] = []
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            suffix = "" if index == 1 else f" (continued {index}/{total})"
            messages.append(
                f"Job `{job_id}` (`{operation}`) completed{suffix}:<br><pre>{chunk}</pre>"
            )
        return messages

    @staticmethod
    def _split_output_chunks(output: str, max_chars: int) -> list[str]:
        if len(output) <= max_chars:
            return [output]

        chunks: list[str] = []
        remaining = output
        while len(remaining) > max_chars:
            split_at = remaining.rfind("\n", 0, max_chars)
            if split_at <= 0:
                split_at = max_chars
            chunk = remaining[:split_at].rstrip("\n")
            if not chunk:
                chunk = remaining[:max_chars]
                split_at = max_chars
            chunks.append(chunk)
            remaining = remaining[split_at:]
            remaining = remaining.lstrip("\n")

        if remaining:
            chunks.append(remaining)

        return chunks

    def _get_build_info_directory(self) -> Path:
        return self._deploy_script.parent.parent

    def _get_build_info_paths(self) -> list[Path]:
        build_info_dir = self._get_build_info_directory()
        paths = sorted(
            build_info_dir.glob("build_info-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        legacy_path = build_info_dir / "build_info.json"
        if legacy_path.exists():
            paths.append(legacy_path)

        return paths

    @staticmethod
    def _load_build_info_file(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _get_foundry_deployment_suffix(resource_group_name: str) -> str:
        match = FOUNDRY_RESOURCE_GROUP_SUFFIX_PATTERN.match(resource_group_name)
        if match is None:
            raise ValueError(
                f"Resource group '{resource_group_name}' does not match the expected "
                "zolab-ai-<suffix> naming pattern."
            )
        return match.group(1)

    def _get_foundry_resource_group_names(self) -> list[str]:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import ResourceManagementClient

        client_id = os.getenv("AZURE_CLIENT_ID")
        credential = DefaultAzureCredential(managed_identity_client_id=client_id)
        subscription_id = os.getenv(
            "AZURE_SUBSCRIPTION_ID",
            "08fdc492-f5aa-4601-84ae-03a37449c2ba",
        )
        client = ResourceManagementClient(credential, subscription_id)

        return sorted(
            resource_group.name
            for resource_group in client.resource_groups.list()
            if resource_group.name and FOUNDRY_RESOURCE_GROUP_PATTERN.match(resource_group.name)
        )

    async def _sync_build_info_from_blob_if_available(self, suffix: str) -> Path | None:
        storage_account_name = os.getenv("AZURE_STORAGE_ACCOUNT")
        blob_container_name = os.getenv("AZURE_BLOB_CONTAINER")
        if not storage_account_name or not blob_container_name:
            return None

        target_path = self._get_build_info_directory() / f"build_info-{suffix}.json"

        try:
            from storage_config import BLOB_CONTAINER_NAME, get_blob_service_client

            blob_client = get_blob_service_client().get_blob_client(
                container=BLOB_CONTAINER_NAME,
                blob=f"builds/build_info-{suffix}.json",
            )
            loop = asyncio.get_event_loop()
            download = await loop.run_in_executor(None, blob_client.download_blob)
            data = await loop.run_in_executor(None, download.readall)
        except Exception:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            return None

        await asyncio.get_event_loop().run_in_executor(None, target_path.write_bytes, data)
        return target_path if target_path.exists() else None

    async def _get_build_info_record_for_resource_group(
        self,
        resource_group_name: str,
    ) -> tuple[Path, dict] | None:
        suffix = self._get_foundry_deployment_suffix(resource_group_name)
        suffix_path = self._get_build_info_directory() / f"build_info-{suffix}.json"
        if suffix_path.exists():
            return suffix_path, self._load_build_info_file(suffix_path)

        downloaded_path = await self._sync_build_info_from_blob_if_available(suffix)
        if downloaded_path is not None:
            return downloaded_path, self._load_build_info_file(downloaded_path)

        for path in self._get_build_info_paths():
            try:
                build_info = self._load_build_info_file(path)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            if build_info.get("rg") == resource_group_name:
                return path, build_info

        return None
