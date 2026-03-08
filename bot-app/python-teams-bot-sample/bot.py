from __future__ import annotations

import os
from datetime import datetime, timezone

from botbuilder.core import MessageFactory, TurnContext
from botbuilder.core.teams import TeamsActivityHandler

from command_parser import parse_command
from conversation_store import JsonConversationStore
from job_dispatcher import FileJobDispatcher
from models import QueuedJob


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class FoundryTeamsBot(TeamsActivityHandler):
    def __init__(self, dispatcher: FileJobDispatcher, store: JsonConversationStore):
        self.dispatcher = dispatcher
        self.store = store
        self._last_response_utc = "No Teams response has been sent yet."

    async def on_message_activity(self, turn_context: TurnContext):
        self._save_conversation(turn_context)

        command = parse_command(turn_context.activity.text)
        if command.kind == "help":
            await self._send(turn_context, self._help_text())
            return

        if command.kind == "unknown":
            await self._send(
                turn_context,
                "I did not recognize that command.\n\n" + self._help_text(),
            )
            return

        if command.kind == "heartbeat":
            await self._send(turn_context, self._heartbeat_text(turn_context))
            return

        if command.kind == "listener-status":
            await self._send(turn_context, self._listener_status_text())
            return

        job = QueuedJob(
            operation=command.kind,
            requested_by=self._requester(turn_context),
            conversation_id=turn_context.activity.conversation.id,
            conversation_scope=self._conversation_scope(turn_context),
            model=command.model,
            resource_group=command.resource_group,
            source_command=command.raw_text,
            arguments={
                "requiresConfirmation": command.requires_confirmation,
            },
        )
        job_path = self.dispatcher.enqueue(job)

        if command.kind == "build":
            ack = (
                f"Queued `build it` as job `{job.job_id}`.\n"
                f"Model: `{command.model or 'prompt later / default selection path'}`\n"
                "The worker should post `🚧 One moment ..the Bob's are still building! 🚧` every 1 minute during active deployment."
            )
        elif command.kind == "teardown":
            ack = (
                f"Queued `teardown` for `{command.resource_group}` as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`\n"
                "The worker should post `🚧 Pls hold while we teardown: <resource-group> 🚧` every 1 minute during active cleanup."
            )
        elif command.kind == "build-status":
            ack = (
                f"Queued `build status` for `{command.resource_group}` as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`"
            )
        else:
            ack = (
                f"Queued `list builds` as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`"
            )

        await self._send(turn_context, ack)

    def _save_conversation(self, turn_context: TurnContext) -> None:
        activity = turn_context.activity
        channel_data = activity.channel_data or {}
        team = channel_data.get("team", {})
        channel = channel_data.get("channel", {})
        tenant = channel_data.get("tenant", {})

        self.store.save(
            activity.conversation.id,
            {
                "conversationId": activity.conversation.id,
                "conversationType": getattr(activity.conversation, "conversation_type", None),
                "serviceUrl": activity.service_url,
                "channelId": activity.channel_id,
                "teamId": team.get("id"),
                "teamsChannelId": channel.get("id"),
                "tenantId": tenant.get("id"),
                "savedUtc": utc_now(),
            },
        )

    def _requester(self, turn_context: TurnContext) -> str:
        from_property = turn_context.activity.from_property
        if not from_property:
            return "unknown-user"
        return (
            getattr(from_property, "name", None)
            or getattr(from_property, "aad_object_id", None)
            or getattr(from_property, "id", None)
            or "unknown-user"
        )

    def _conversation_scope(self, turn_context: TurnContext) -> str:
        channel_data = turn_context.activity.channel_data or {}
        team = channel_data.get("team", {})
        channel = channel_data.get("channel", {})
        return f"team:{team.get('id', 'unknown')}|channel:{channel.get('id', turn_context.activity.conversation.id)}"

    def _heartbeat_text(self, turn_context: TurnContext) -> str:
        channel_data = turn_context.activity.channel_data or {}
        channel = channel_data.get("channel", {})
        team = channel_data.get("team", {})
        listening_in = channel.get("id") or turn_context.activity.conversation.id

        return "\n".join(
            [
                "🟢 Status: Online ✅",
                "📜 Script: python-teams-bot-sample",
                "🆔 PID: <runtime-pid>",
                f"🖥️ pwsh version: {os.getenv('SAMPLE_WORKER_PWSH_VERSION', '<worker-pwsh-version>')}",
                "⏱️ Uptime: <uptime>",
                "🧠 Memory: <memory-usage>",
                f"💬 Last response: {self._last_response_utc}",
                "🔗 Graph API: <Connected|Disconnected> 🔌",
                f"📢 Listening in: team={team.get('id', 'unknown')} channel={listening_in}",
                f"👤 Identity: {self._requester(turn_context)}",
                f"🕒 Checked at: {utc_now()}",
            ]
        )

    def _listener_status_text(self) -> str:
        return "\n".join(
            [
                "🟢 Bot status: Online ✅",
                "⚙️ Worker status: Waiting for queue processing",
                f"📦 Queue depth: {self.dispatcher.queue_depth()}",
                "🤖 Bot identity: <bot-app-id>",
                "🔐 Automation identity: <automation-app-id>",
                f"🕒 Checked at: {utc_now()}",
            ]
        )

    def _help_text(self) -> str:
        return "\n".join(
            [
                "Supported commands:",
                "- build it",
                "- build it <model>",
                "- list builds",
                "- build status <resource-group>",
                "- teardown <resource-group>",
                "- heartbeat",
                "- listener status",
                "- help",
            ]
        )

    async def _send(self, turn_context: TurnContext, text: str) -> None:
        await turn_context.send_activity(MessageFactory.text(text))
        self._last_response_utc = utc_now()
