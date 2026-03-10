# ════════════════════════════════════════════════════════════════
# worker.py — Background job worker
# Polls .queue/pending/ for queued jobs, executes them via
# PowerShell, and sends results back via proactive messaging.
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from microsoft_agents.activity import Activity, ActivityTypes, Attachment

if TYPE_CHECKING:
    from proactive import ProactiveMessenger

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class BackgroundWorker:
    """Async background worker that processes queued jobs by invoking PowerShell."""

    POLL_INTERVAL = 5       # seconds between queue polls
    PROGRESS_INTERVAL = 60  # seconds between progress messages

    def __init__(
        self,
        queue_path: Path,
        proactive: ProactiveMessenger,
        deploy_script: Path,
    ):
        self._queue_path = queue_path
        self._proactive = proactive
        self._deploy_script = deploy_script
        self._running = False

        # Ensure queue subdirectories exist
        for sub in ("pending", "running", "completed", "failed"):
            (self._queue_path / sub).mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main poll loop — runs until stop() is called."""
        self._running = True
        logger.info("BackgroundWorker started — polling %s", self._queue_path / "pending")

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error("Worker poll error: %s", e)
            await asyncio.sleep(self.POLL_INTERVAL)

    # ── Internal ───────────────────────────────────────────────

    async def _poll_once(self) -> None:
        pending_dir = self._queue_path / "pending"
        jobs = sorted(pending_dir.glob("*.json"))
        if not jobs:
            return

        job_file = jobs[0]
        job_data = json.loads(job_file.read_text(encoding="utf-8"))
        job_id = job_data.get("job_id", job_file.stem)
        operation = job_data.get("operation", "unknown")
        conversation_id = job_data.get("conversation_id", "")

        # Move to running
        running_file = self._queue_path / "running" / job_file.name
        job_file.rename(running_file)
        logger.info("Processing job %s — operation: %s", job_id, operation)

        try:
            result = await self._execute_job(job_data, conversation_id)

            # Move to completed
            completed_file = self._queue_path / "completed" / job_file.name
            job_data["completed_utc"] = _utc_now()
            job_data["result"] = result[:4000] if result else ""
            completed_file.write_text(json.dumps(job_data, indent=2), encoding="utf-8")
            running_file.unlink(missing_ok=True)

            # Send final result to conversation
            if result:
                sent_with_attachment = False

                # For build operations, attach the build_info JSON file
                if operation == "build":
                    build_info = self._find_build_info(result)
                    if build_info:
                        file_path, file_content = build_info
                        activity = self._build_info_activity(
                            job_id, operation, file_path, file_content,
                        )
                        sent_with_attachment = (
                            await self._proactive.send_activity_to_conversation(
                                conversation_id, activity,
                            )
                        )
                        if sent_with_attachment:
                            logger.info(
                                "Sent build_info attachment %s for job %s",
                                file_path.name, job_id,
                            )
                        else:
                            logger.warning(
                                "Failed to send build_info attachment for job %s, "
                                "falling back to text",
                                job_id,
                            )

                # Fallback: text-only (non-build ops, or attachment failed)
                if not sent_with_attachment:
                    truncated = result[:3000]
                    await self._proactive.send_to_conversation(
                        conversation_id,
                        f"✅ Job `{job_id}` (`{operation}`) completed:\n\n```\n{truncated}\n```",
                    )
        except Exception as e:
            logger.error("Job %s failed: %s", job_id, e)

            # Move to failed
            failed_file = self._queue_path / "failed" / job_file.name
            job_data["failed_utc"] = _utc_now()
            job_data["error"] = str(e)
            failed_file.write_text(json.dumps(job_data, indent=2), encoding="utf-8")
            running_file.unlink(missing_ok=True)

            await self._proactive.send_to_conversation(
                conversation_id,
                f"❌ Job `{job_id}` (`{operation}`) failed:\n\n```\n{str(e)[:2000]}\n```",
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
            progress_msg="🚧 One moment ..the Bob's are still building! 🚧",
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
            progress_msg=f"🚧 Pls hold while we teardown: {resource_group} 🚧",
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

    # ── Build-info attachment helpers ─────────────────────────

    _SUFFIX_RE = re.compile(r"Generated suffix:\s*(\w+)")

    def _find_build_info(self, stdout_output: str) -> tuple[Path, dict] | None:
        """Locate and read the build_info JSON created by a successful build.

        Strategy 1: parse stdout for 'Generated suffix: <suffix>' (deterministic).
        Strategy 2: glob for newest build_info-*.json in repo root (fallback).
        """
        repo_root = self._deploy_script.parent.parent  # deployment/ → repo root

        # Strategy 1 — extract suffix from PowerShell stdout
        match = self._SUFFIX_RE.search(stdout_output)
        if match:
            suffix = match.group(1)
            candidate = repo_root / f"build_info-{suffix}.json"
            if candidate.is_file():
                try:
                    content = json.loads(candidate.read_text(encoding="utf-8"))
                    return (candidate, content)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read %s: %s", candidate, exc)

        # Strategy 2 — newest build_info-*.json by mtime
        candidates = sorted(
            repo_root.glob("build_info-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                content = json.loads(candidate.read_text(encoding="utf-8"))
                return (candidate, content)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read %s: %s", candidate, exc)

        return None

    @staticmethod
    def _build_info_activity(
        job_id: str,
        operation: str,
        file_path: Path,
        file_content: dict,
    ) -> Activity:
        """Create an Activity with an Adaptive Card + downloadable file attachment."""
        filename = file_path.name
        pretty_json = json.dumps(file_content, indent=2)

        # Adaptive Card — renders nicely in Teams / Playground
        adaptive_card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"✅ Build Completed — {filename}",
                    "weight": "Bolder",
                    "size": "Medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"```json\n{pretty_json}\n```",
                    "wrap": True,
                    "fontType": "Monospace",
                    "size": "Small",
                },
            ],
        }
        card_attachment = Attachment(
            content_type="application/vnd.microsoft.card.adaptive",
            content=adaptive_card,
        )

        # Raw JSON file attachment for download
        raw_bytes = file_path.read_bytes()
        b64_content = base64.b64encode(raw_bytes).decode("ascii")
        file_attachment = Attachment(
            content_type="application/json",
            content_url=f"data:application/json;base64,{b64_content}",
            name=filename,
        )

        # Text fallback for channels that don't render Adaptive Cards
        text_fallback = (
            f"✅ Job `{job_id}` (`{operation}`) completed.\n\n"
            f"**{filename}:**\n```json\n{pretty_json}\n```"
        )

        return Activity(
            type=ActivityTypes.message,
            text=text_fallback,
            attachments=[card_attachment, file_attachment],
        )
