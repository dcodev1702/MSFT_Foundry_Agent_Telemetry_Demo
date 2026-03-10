# ════════════════════════════════════════════════════════════════
# bot.py — Handler registration for the Foundry Teams Bot
#
# Uses the M365 Agents SDK decorator pattern to register message
# and conversation-update handlers on an AgentApplication instance.
#
# All Bot Framework SDK imports have been replaced with their
# M365 Agents SDK equivalents.
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from microsoft_agents.hosting.core import (
    AgentApplication,
    MessageFactory,
    TurnContext,
    TurnState,
)

from command_parser import parse_command
from conversation_store import BlobConversationStore
from job_dispatcher import AzureQueueJobDispatcher
from models import ALLOWED_MODELS, BuildSession, QueuedJob


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ── Module-level state ────────────────────────────────────────
_last_response_utc: str = "No Teams response has been sent yet."
_build_sessions: dict[str, BuildSession] = {}
SESSION_TIMEOUT = 600  # 10 minutes


# ── Helpers ────────────────────────────────────────────────────

def strip_bot_mention(text: str | None) -> str:
    """Remove Teams <at>…</at> mention tags from message text."""
    if not text:
        return ""
    cleaned = re.sub(r"<at>.*?</at>", "", text, flags=re.IGNORECASE)
    return cleaned.strip()


def _get_requester(activity) -> str:
    """Extract a human-readable requester name from the activity."""
    from_property = activity.from_property
    if not from_property:
        return "unknown-user"
    return (
        getattr(from_property, "name", None)
        or getattr(from_property, "aad_object_id", None)
        or getattr(from_property, "id", None)
        or "unknown-user"
    )


def _get_conversation_scope(activity) -> str:
    """Build a team:channel scope string from channel_data."""
    channel_data = activity.channel_data or {}
    team = channel_data.get("team", {})
    channel = channel_data.get("channel", {})
    return (
        f"team:{team.get('id', 'unknown')}"
        f"|channel:{channel.get('id', activity.conversation.id)}"
    )


def _get_help_text() -> str:
    return "\n".join([
        "**Supported commands:**",
        "- `build it` — deploy (prompts for model selection)",
        "- `build it <model>` — deploy with specified model",
        "- `list builds` — list all active Foundry deployments",
        "- `build status <resource-group>` — check a specific deployment",
        "- `teardown <resource-group>` — remove a Foundry deployment",
        "- `heartbeat` — check bot health and metrics",
        "- `listener status` — check worker and queue status",
        "- `help` — show this message",
    ])


def _get_listener_status_text(dispatcher: FileJobDispatcher) -> str:
    app_id = os.getenv(
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "<bot-app-id>"
    )
    return "\n".join([
        "🟢 Bot status: Online ✅",
        "⚙️ Worker status: Running",
        f"📦 Queue depth: {dispatcher.queue_depth()}",
        f"🤖 Bot identity: {app_id}",
        f"🕒 Checked at: {_utc_now()}",
    ])


# ── Build-session helpers ─────────────────────────────────────

def _expire_sessions() -> None:
    """Remove build sessions older than SESSION_TIMEOUT."""
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        cid for cid, s in _build_sessions.items()
        if now - s.created_at > SESSION_TIMEOUT
    ]
    for cid in expired:
        del _build_sessions[cid]


def _model_selection_prompt() -> str:
    lines = ["**Select a model for your build:**\n"]
    for i, model in enumerate(ALLOWED_MODELS, 1):
        lines.append(f"{i}. `{model}`")
    lines.append("\nReply with a **number** or the **model name**.")
    lines.append("Type `cancel` to abort.")
    return "\n".join(lines)


# ── Save helpers ───────────────────────────────────────────────

def _save_conversation(
    activity, store: JsonConversationStore, context: TurnContext
) -> None:
    """Persist conversation metadata and reference for proactive messaging."""
    channel_data = activity.channel_data or {}
    team = channel_data.get("team", {})
    channel = channel_data.get("channel", {})
    tenant = channel_data.get("tenant", {})

    store.save(
        activity.conversation.id,
        {
            "conversationId": activity.conversation.id,
            "conversationType": getattr(
                activity.conversation, "conversation_type", None
            ),
            "serviceUrl": activity.service_url,
            "channelId": activity.channel_id,
            "teamId": team.get("id"),
            "teamsChannelId": channel.get("id"),
            "tenantId": tenant.get("id"),
            "savedUtc": _utc_now(),
        },
    )

    # Save full ConversationReference for proactive messaging
    try:
        ref = activity.get_conversation_reference()
        store.save_reference(
            activity.conversation.id, ref.model_dump(by_alias=True)
        )
    except Exception:
        pass  # best-effort

    # Save ClaimsIdentity for proactive messaging auth
    try:
        identity = context.identity
        if identity:
            store.save_identity(
                activity.conversation.id,
                {
                    "claims": dict(identity.claims) if identity.claims else {},
                    "is_authenticated": identity.is_authenticated,
                    "authentication_type": identity.authentication_type,
                },
            )
    except Exception:
        pass  # best-effort


# ── Build-selection handler ────────────────────────────────────

async def _handle_build_selection(
    context: TurnContext,
    raw_text: str,
    conversation_id: str,
    *,
    dispatcher: FileJobDispatcher,
    heartbeat_service=None,
) -> None:
    """Process a user's model-selection reply during an active BuildSession."""
    global _last_response_utc
    activity = context.activity
    choice = raw_text.strip()

    # Cancel
    if choice.lower() == "cancel":
        del _build_sessions[conversation_id]
        await context.send_activity(
            MessageFactory.text("Build cancelled.")
        )
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Resolve model — by number or name
    selected_model: str | None = None

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(ALLOWED_MODELS):
            selected_model = ALLOWED_MODELS[idx - 1]
    else:
        for model in ALLOWED_MODELS:
            if choice.lower() == model.lower():
                selected_model = model
                break

    if selected_model is None:
        await context.send_activity(
            MessageFactory.text(
                f"Invalid selection: `{choice}`\n\n" + _model_selection_prompt()
            )
        )
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Valid model — clear session and queue the build
    del _build_sessions[conversation_id]

    job = QueuedJob(
        operation="build",
        requested_by=_get_requester(activity),
        conversation_id=conversation_id,
        conversation_scope=_get_conversation_scope(activity),
        model=selected_model,
        source_command=f"build it {selected_model}",
    )
    dispatcher.enqueue(job)

    ack = (
        f"✅ Queued `build it` as job `{job.job_id}`.\n"
        f"Model: `{selected_model}`\n"
        "The worker will post progress updates every 60 seconds "
        "during deployment."
    )
    await context.send_activity(MessageFactory.text(ack))
    _last_response_utc = _utc_now()
    if heartbeat_service:
        heartbeat_service.update_last_response(_last_response_utc)


# ════════════════════════════════════════════════════════════════
#  HANDLER REGISTRATION
# ════════════════════════════════════════════════════════════════

def register_handlers(
    agent_app: AgentApplication,
    *,
    dispatcher: AzureQueueJobDispatcher,
    store: BlobConversationStore,
    heartbeat_service=None,
) -> None:
    """Register all bot message and event handlers on the AgentApplication."""

    # ── Message Handler ────────────────────────────────────────
    @agent_app.activity("message")
    async def on_message(context: TurnContext, state: TurnState) -> None:
        global _last_response_utc

        activity = context.activity
        _save_conversation(activity, store, context)

        # Strip Teams @mention tags before parsing
        raw_text = strip_bot_mention(activity.text)
        conversation_id = activity.conversation.id

        # ── Active build-session? Handle model selection ──────
        _expire_sessions()
        if conversation_id in _build_sessions:
            await _handle_build_selection(
                context, raw_text, conversation_id,
                dispatcher=dispatcher,
                heartbeat_service=heartbeat_service,
            )
            return

        command = parse_command(raw_text)

        # ── Immediate-response commands ────────────────────────

        if command.kind == "help":
            await context.send_activity(MessageFactory.text(_get_help_text()))
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        if command.kind == "unknown":
            msg = "I did not recognize that command.\n\n" + _get_help_text()
            await context.send_activity(MessageFactory.text(msg))
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        if command.kind == "heartbeat":
            channel_data = activity.channel_data or {}
            channel = channel_data.get("channel", {})
            team = channel_data.get("team", {})
            listening_in = channel.get("id") or activity.conversation.id

            if heartbeat_service:
                text = heartbeat_service.get_heartbeat_text(
                    requester=_get_requester(activity),
                    team_id=team.get("id", "unknown"),
                    channel_id=listening_in,
                )
            else:
                text = "\n".join([
                    "🟢 Status: Online ✅",
                    "📜 Script: foundry-teams-bot (M365 Agents SDK)",
                    f"🆔 PID: {os.getpid()}",
                    f"💬 Last response: {_last_response_utc}",
                    f"📢 Listening in: team={team.get('id', 'unknown')} "
                    f"channel={listening_in}",
                    f"👤 Identity: {_get_requester(activity)}",
                    f"🕒 Checked at: {_utc_now()}",
                ])

            await context.send_activity(MessageFactory.text(text))
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        if command.kind == "listener-status":
            text = _get_listener_status_text(dispatcher)
            await context.send_activity(MessageFactory.text(text))
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        # ── Queue-based commands ───────────────────────────────

        # Build requires a valid model — start selection if missing/invalid
        if command.kind == "build":
            if command.model is None:
                _build_sessions[conversation_id] = BuildSession()
                await context.send_activity(
                    MessageFactory.text(_model_selection_prompt())
                )
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            if command.model.lower() not in [m.lower() for m in ALLOWED_MODELS]:
                _build_sessions[conversation_id] = BuildSession()
                await context.send_activity(
                    MessageFactory.text(
                        f"Unknown model `{command.model}`.\n\n"
                        + _model_selection_prompt()
                    )
                )
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

        job = QueuedJob(
            operation=command.kind,
            requested_by=_get_requester(activity),
            conversation_id=conversation_id,
            conversation_scope=_get_conversation_scope(activity),
            model=command.model,
            resource_group=command.resource_group,
            source_command=command.raw_text,
            arguments={"requiresConfirmation": command.requires_confirmation},
        )
        dispatcher.enqueue(job)

        if command.kind == "build":
            ack = (
                f"Queued `build it` as job `{job.job_id}`.\n"
                f"Model: `{command.model}`\n"
                "The worker will post progress updates every 60 seconds "
                "during deployment."
            )
        elif command.kind == "teardown":
            ack = (
                f"Queued `teardown` for `{command.resource_group}` "
                f"as job `{job.job_id}`.\n"
                "The worker will post progress updates every 60 seconds "
                "during cleanup."
            )
        elif command.kind == "build-status":
            ack = (
                f"Queued `build status` for `{command.resource_group}` "
                f"as job `{job.job_id}`."
            )
        else:  # list-builds
            ack = f"Queued `list builds` as job `{job.job_id}`."

        await context.send_activity(MessageFactory.text(ack))
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)

    # ── Conversation Update Handler ────────────────────────────
    @agent_app.conversation_update("membersAdded")
    async def on_members_added(context: TurnContext, state: TurnState) -> None:
        _save_conversation(context.activity, store, context)

        for member in context.activity.members_added or []:
            # Only send welcome when the bot itself is added
            if member.id != context.activity.recipient.id:
                continue

            welcome = (
                "👋 **Hello! I'm the Foundry Bot.**\n\n"
                "I manage Azure AI Foundry deployments from this "
                "Teams channel.\n\n" + _get_help_text()
            )
            await context.send_activity(MessageFactory.text(welcome))
