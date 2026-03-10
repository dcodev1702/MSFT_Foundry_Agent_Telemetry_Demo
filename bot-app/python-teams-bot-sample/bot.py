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

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from microsoft_agents.hosting.core import (
    AgentApplication,
    MessageFactory,
    TurnContext,
    TurnState,
)

from command_parser import parse_command
from conversation_store import JsonConversationStore
from job_dispatcher import FileJobDispatcher
from models import ALLOWED_MODELS, BuildSession, QueuedJob, TeardownSession

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ── Module-level state ────────────────────────────────────────
_last_response_utc: str = "No Teams response has been sent yet."

# Session timeout for build and teardown flows
SESSION_TIMEOUT = 300  # 5 minutes

# Build model-selection sessions — keyed by conversation ID
_build_sessions: dict[str, BuildSession] = {}

# Teardown session state — keyed by conversation ID
_teardown_sessions: dict[str, TeardownSession] = {}


# ── Session helpers ───────────────────────────────────────────

def _expire_sessions() -> None:
    """Remove expired build and teardown sessions."""
    now = time.time()
    expired_build = [k for k, v in _build_sessions.items()
                     if now - v.created_at > SESSION_TIMEOUT]
    for k in expired_build:
        del _build_sessions[k]

    expired_teardown = [k for k, v in _teardown_sessions.items()
                        if now - v.created_at > SESSION_TIMEOUT]
    for k in expired_teardown:
        del _teardown_sessions[k]


# Regex patterns for parsing -ListBuilds output
_BUILD_LINE_WITH_MODEL = re.compile(
    r"^\s*(\d+)\.\s+(zolab-ai-\S+)\s+.*?model:\s+(\S+)", re.IGNORECASE
)
_BUILD_LINE_NO_MODEL = re.compile(
    r"^\s*(\d+)\.\s+(zolab-ai-\S+)\s+.*?build info file missing", re.IGNORECASE
)


async def _run_list_builds_async(deploy_script: Path) -> str:
    """Run PowerShell -ListBuilds and return stdout."""
    args = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File",
        str(deploy_script), "-ListBuilds",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


def _parse_build_list(output: str) -> list[dict]:
    """Parse PowerShell -ListBuilds output into structured build list."""
    builds: list[dict] = []
    for line in output.splitlines():
        match = _BUILD_LINE_WITH_MODEL.match(line)
        if match:
            builds.append({
                "number": int(match.group(1)),
                "rg": match.group(2),
                "model": match.group(3),
            })
            continue
        match = _BUILD_LINE_NO_MODEL.match(line)
        if match:
            builds.append({
                "number": int(match.group(1)),
                "rg": match.group(2),
                "model": "unknown",
            })
    return builds


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
    return "<br>".join([
        "**Supported commands:**",
        "- `build it` — deploy (prompts for model selection)",
        "- `build it <model>` — deploy with specified model",
        "- `list builds` — list all active Foundry deployments",
        "- `build status <resource-group>` — check a specific deployment",
        "- `teardown` — select and remove a Foundry deployment",
        "- `teardown <resource-group>` — remove a specific Foundry deployment",
        "- `heartbeat` — check bot health and metrics",
        "- `listener status` — check worker and queue status",
        "- `help` — show this message",
    ])


def _get_listener_status_text(dispatcher: FileJobDispatcher) -> str:
    app_id = os.getenv(
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "<bot-app-id>"
    )
    return "<br>".join([
        "🟢 Bot status: Online ✅",
        "⚙️ Worker status: Running",
        f"📦 Queue depth: {dispatcher.queue_depth()}",
        f"🤖 Bot identity: {app_id}",
        f"🕒 Checked at: {_utc_now()}",
    ])


# ── Build model-selection helpers ─────────────────────────────

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


# ── Teardown session response handler ─────────────────────────

async def _handle_teardown_response(
    context: TurnContext,
    conv_id: str,
    raw_text: str,
    activity,
    dispatcher: FileJobDispatcher,
    heartbeat_service,
) -> bool:
    """Handle a user reply during an active teardown session.

    Returns True if the message was consumed by the session flow.
    """
    session = _teardown_sessions[conv_id]
    normalized = raw_text.lower().strip()

    if session.state == "selecting":
        # User is picking a build number or cancelling
        if normalized == "none":
            del _teardown_sessions[conv_id]
            await context.send_activity(
                MessageFactory.text("❌ Teardown cancelled.")
            )
            return True

        try:
            selection = int(normalized)
        except ValueError:
            await context.send_activity(MessageFactory.text(
                f"Invalid input. Reply with a number "
                f"(1–{len(session.builds)}) or `none` to cancel."
            ))
            return True

        if selection < 1 or selection > len(session.builds):
            await context.send_activity(MessageFactory.text(
                f"Out of range. Reply with a number "
                f"(1–{len(session.builds)}) or `none` to cancel."
            ))
            return True

        build = session.builds[selection - 1]
        session.state = "confirming"
        session.selected_rg = build["rg"]

        await context.send_activity(MessageFactory.text(
            f"⚠️ **Confirm teardown of `{build['rg']}`?**<br><br>"
            f"Reply `confirm` to proceed or `abort` to cancel."
        ))
        return True

    if session.state == "confirming":
        if normalized == "confirm":
            rg = session.selected_rg
            del _teardown_sessions[conv_id]

            # Queue the teardown job
            job = QueuedJob(
                operation="teardown",
                requested_by=_get_requester(activity),
                conversation_id=conv_id,
                conversation_scope=_get_conversation_scope(activity),
                resource_group=rg,
                source_command=f"teardown {rg}",
                arguments={"requiresConfirmation": True},
            )
            job_path = dispatcher.enqueue(job)

            ack = (
                f"✅ Queued `teardown` for `{rg}` "
                f"as job `{job.job_id}`.<br>"
                f"Queue file: `{job_path.name}`<br>"
                "The worker will post progress updates every 60 seconds "
                "during cleanup."
            )
            await context.send_activity(MessageFactory.text(ack))
            return True

        if normalized == "abort":
            del _teardown_sessions[conv_id]
            await context.send_activity(
                MessageFactory.text("❌ Teardown cancelled.")
            )
            return True

        # Unrecognized input during confirmation
        await context.send_activity(MessageFactory.text(
            "Reply `confirm` to proceed or `abort` to cancel."
        ))
        return True

    return False


# ── Build-selection handler ────────────────────────────────────

async def _handle_build_selection(
    context: TurnContext,
    raw_text: str,
    conv_id: str,
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
        del _build_sessions[conv_id]
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
    del _build_sessions[conv_id]

    job = QueuedJob(
        operation="build",
        requested_by=_get_requester(activity),
        conversation_id=conv_id,
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
    dispatcher: FileJobDispatcher,
    store: JsonConversationStore,
    heartbeat_service=None,
    deploy_script: Path | None = None,
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
        conv_id = activity.conversation.id

        # ── Check for active sessions ─────────────────────────
        _expire_sessions()

        if conv_id in _build_sessions:
            await _handle_build_selection(
                context, raw_text, conv_id,
                dispatcher=dispatcher,
                heartbeat_service=heartbeat_service,
            )
            return

        if conv_id in _teardown_sessions:
            handled = await _handle_teardown_response(
                context, conv_id, raw_text, activity,
                dispatcher, heartbeat_service,
            )
            if handled:
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
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
                text = "<br>".join([
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

        # ── Interactive teardown (bare "teardown" command) ─────
        if command.kind == "teardown-select":
            if not deploy_script or not deploy_script.exists():
                await context.send_activity(MessageFactory.text(
                    "❌ Deploy script not found. Cannot list builds."
                ))
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            await context.send_activity(MessageFactory.text(
                "🔍 Fetching active builds…"
            ))

            try:
                output = await _run_list_builds_async(deploy_script)
                builds = _parse_build_list(output)
            except Exception as e:
                logger.error("Failed to list builds: %s", e)
                await context.send_activity(MessageFactory.text(
                    f"❌ Failed to list builds: {e}"
                ))
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            if not builds:
                await context.send_activity(MessageFactory.text(
                    "ℹ️ No active Foundry builds found."
                ))
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            # Store session and present numbered list
            _teardown_sessions[conv_id] = TeardownSession(
                builds=builds, state="selecting",
            )

            lines = ["📋 **Active Foundry Builds:**", ""]
            for b in builds:
                lines.append(
                    f"{b['number']}. `{b['rg']}` — model: {b['model']}"
                )
            lines.append("")
            lines.append(
                "Reply with a number to select, or `none` to cancel."
            )

            await context.send_activity(
                MessageFactory.text("<br>".join(lines))
            )
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        # ── Queue-based commands ───────────────────────────────

        # Build requires a valid model — start selection if missing/invalid
        if command.kind == "build":
            if command.model is None:
                _build_sessions[conv_id] = BuildSession()
                await context.send_activity(
                    MessageFactory.text(_model_selection_prompt())
                )
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            if command.model.lower() not in [m.lower() for m in ALLOWED_MODELS]:
                _build_sessions[conv_id] = BuildSession()
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
            conversation_id=conv_id,
            conversation_scope=_get_conversation_scope(activity),
            model=command.model,
            resource_group=command.resource_group,
            source_command=command.raw_text,
            arguments={"requiresConfirmation": command.requires_confirmation},
        )
        job_path = dispatcher.enqueue(job)

        if command.kind == "build":
            ack = (
                f"✅ Queued `build it` as job `{job.job_id}`.\n"
                f"Model: `{command.model}`\n"
                "The worker will post progress updates every 60 seconds "
                "during deployment."
            )
        elif command.kind == "teardown":
            ack = (
                f"✅ Queued `teardown` for `{command.resource_group}` "
                f"as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`\n"
                "The worker will post progress updates every 60 seconds "
                "during cleanup."
            )
        elif command.kind == "build-status":
            ack = (
                f"✅ Queued `build status` for `{command.resource_group}` "
                f"as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`"
            )
        else:  # list-builds
            ack = (
                f"✅ Queued `list builds` as job `{job.job_id}`.\n"
                f"Queue file: `{job_path.name}`"
            )

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
