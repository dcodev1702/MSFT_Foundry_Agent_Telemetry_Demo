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
from microsoft_agents.hosting.teams import TeamsInfo

from command_parser import parse_command
from conversation_store import BlobConversationStore
from job_dispatcher import AzureQueueJobDispatcher
from msft_docs_service import MicrosoftLearnMcpService
from models import (
    ALLOWED_MODELS,
    BuildSession,
    PendingConfirmation,
    QueuedJob,
    TeardownSession,
)
from weather_service import WeatherService


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ── Module-level state ────────────────────────────────────────
_last_response_utc: str = "No Teams response has been sent yet."
_build_sessions: dict[str, BuildSession] = {}
_teardown_sessions: dict[str, TeardownSession] = {}
_pending_confirmations: dict[str, PendingConfirmation] = {}
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


async def _get_requester_identity(context: TurnContext) -> dict[str, str | None]:
    """Resolve the strongest available Teams identity for queue handoff."""
    activity = context.activity
    from_property = activity.from_property
    if not from_property:
        return {
            "requested_by": "unknown-user",
            "requested_by_upn": None,
            "requested_by_object_id": None,
        }

    requested_by = (
        getattr(from_property, "name", None)
        or getattr(from_property, "id", None)
        or "unknown-user"
    )
    requested_by_upn = (
        getattr(from_property, "user_principal_name", None)
        or getattr(from_property, "email", None)
    )
    requested_by_object_id = getattr(from_property, "aad_object_id", None)

    member_id = getattr(from_property, "id", None)
    if member_id and (not requested_by_upn or not requested_by_object_id):
        try:
            member = await TeamsInfo.get_member(context, member_id)
        except Exception:
            member = None

        if member:
            requested_by = requested_by or getattr(member, "name", None) or requested_by
            requested_by_upn = (
                requested_by_upn
                or getattr(member, "user_principal_name", None)
                or getattr(member, "email", None)
            )
            requested_by_object_id = (
                requested_by_object_id
                or getattr(member, "aad_object_id", None)
            )

    requested_by = requested_by_upn or requested_by or requested_by_object_id or "unknown-user"

    return {
        "requested_by": requested_by,
        "requested_by_upn": requested_by_upn,
        "requested_by_object_id": requested_by_object_id,
    }


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
        "- `weather <city>` — current weather for a city",
        "- `msft_docs <question>` — search Microsoft Learn docs via MCP",
        "- `list builds` — list all active Foundry deployments",
        "- `build status <resource-group>` — check a specific deployment",
        "- `teardown` — remove a Foundry deployment (prompts for build selection)",
        "- `teardown <resource-group>` — remove a specific deployment",
        "- `heartbeat` — check bot health and metrics",
        "- `listener status` — check worker and queue status",
        "- `help` — show this message",
    ])


def _get_listener_status_text(dispatcher: AzureQueueJobDispatcher) -> str:
    bot_client_id = os.getenv(
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "<bot-app-id>"
    )
    managed_identity_name = os.getenv(
        "AZURE_MANAGED_IDENTITY_NAME", "unknown-managed-identity"
    )
    managed_identity_client_id = os.getenv("AZURE_CLIENT_ID", "<managed-identity-client-id>")
    return "<br>".join([
        "🟢 Bot status: Online ✅",
        "⚙️ Worker status: Running",
        f"📦 Queue depth: {dispatcher.queue_depth()}",
        "🤖 Bot Identity:",
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Mode: UserAssignedMSI",
        f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Client ID: {bot_client_id}",
        "🔑 Azure User Managed Identity:",
        f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Name: {managed_identity_name}",
        f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Client ID: {managed_identity_client_id}",
        f"🕒 Checked at: {_utc_now()}",
    ])


# ── Build-session helpers ─────────────────────────────────────

def _expire_sessions() -> None:
    """Remove stale sessions and pending confirmations older than SESSION_TIMEOUT."""
    now = datetime.now(timezone.utc).timestamp()
    for store in (_build_sessions, _teardown_sessions, _pending_confirmations):
        expired = [
            cid for cid, s in store.items()
            if now - s.created_at > SESSION_TIMEOUT
        ]
        for cid in expired:
            del store[cid]


def _model_selection_prompt() -> str:
    lines = ["**Select a model for your build:**<br>"]
    for i, model in enumerate(ALLOWED_MODELS, 1):
        lines.append(f"{i}. `{model}`")
    lines.append("<br>Reply with a **number** or the **model name**.")
    lines.append("Type `cancel` to abort.")
    return "<br>".join(lines)


def _list_foundry_builds() -> list[str]:
    """Query Azure for zolab-ai-* resource groups (active Foundry builds)."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import ResourceManagementClient

        client_id = os.getenv("AZURE_CLIENT_ID")
        credential = DefaultAzureCredential(
            managed_identity_client_id=client_id
        )
        sub_id = os.getenv("AZURE_SUBSCRIPTION_ID", "08fdc492-f5aa-4601-84ae-03a37449c2ba")
        client = ResourceManagementClient(credential, sub_id)
        groups = client.resource_groups.list()
        return sorted(
            rg.name for rg in groups
            if rg.name and rg.name.startswith("zolab-ai-")
        )
    except Exception:
        return []


def _teardown_selection_prompt(builds: list[str]) -> str:
    lines = ["**Select a build to teardown:**<br>"]
    for i, rg in enumerate(builds, 1):
        lines.append(f"{i}. `{rg}`")
    lines.append("<br>Reply with a **number** or the **resource group name**.")
    lines.append("Type `cancel` to abort.")
    return "<br>".join(lines)


def _build_confirmation_prompt(pending: PendingConfirmation) -> str:
    """Build the yes/no confirmation message for a destructive operation."""
    if pending.operation == "build":
        return "<br>".join([
            f"⚠️ **Confirm deployment:**",
            f"Model: `{pending.model}`",
            f"Requested by: {pending.requester}",
            "",
            "Reply `yes` to proceed or `no` to cancel.",
        ])
    # teardown
    return "<br>".join([
        f"⚠️ **Confirm teardown:**",
        f"Resource group: `{pending.resource_group}`",
        f"Requested by: {pending.requester}",
        "",
        "⚠️ This will **permanently delete** all resources in the group.",
        "Reply `yes` to proceed or `no` to cancel.",
    ])


# ── Save helpers ───────────────────────────────────────────────

def _save_conversation(
    activity, store: BlobConversationStore, context: TurnContext
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
    dispatcher: AzureQueueJobDispatcher,
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
                f"Invalid selection: `{choice}`<br><br>" + _model_selection_prompt()
            )
        )
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Valid model — clear build session, move to confirmation
    del _build_sessions[conversation_id]

    requester_identity = await _get_requester_identity(context)

    pending = PendingConfirmation(
        operation="build",
        requester=requester_identity["requested_by"],
        requester_upn=requester_identity["requested_by_upn"],
        requester_object_id=requester_identity["requested_by_object_id"],
        model=selected_model,
        source_command=f"build it {selected_model}",
        conversation_scope=_get_conversation_scope(activity),
    )
    _pending_confirmations[conversation_id] = pending

    await context.send_activity(
        MessageFactory.text(_build_confirmation_prompt(pending))
    )
    _last_response_utc = _utc_now()
    if heartbeat_service:
        heartbeat_service.update_last_response(_last_response_utc)


# ── Teardown-selection handler ────────────────────────────────

async def _handle_teardown_selection(
    context: TurnContext,
    raw_text: str,
    conversation_id: str,
    *,
    heartbeat_service=None,
) -> None:
    """Process a user's build-selection reply during an active TeardownSession."""
    global _last_response_utc
    activity = context.activity
    choice = raw_text.strip()
    session = _teardown_sessions[conversation_id]

    # Cancel
    if choice.lower() == "cancel":
        del _teardown_sessions[conversation_id]
        await context.send_activity(MessageFactory.text("Teardown cancelled."))
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Resolve resource group — by number or name
    selected_rg: str | None = None

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(session.builds):
            selected_rg = session.builds[idx - 1]
    else:
        for rg in session.builds:
            if choice.lower() == rg.lower():
                selected_rg = rg
                break

    if selected_rg is None:
        await context.send_activity(
            MessageFactory.text(
                f"Invalid selection: `{choice}`<br><br>"
                + _teardown_selection_prompt(session.builds)
            )
        )
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Valid selection — clear session, move to confirmation
    del _teardown_sessions[conversation_id]

    requester_identity = await _get_requester_identity(context)

    pending = PendingConfirmation(
        operation="teardown",
        requester=requester_identity["requested_by"],
        requester_upn=requester_identity["requested_by_upn"],
        requester_object_id=requester_identity["requested_by_object_id"],
        resource_group=selected_rg,
        source_command=f"teardown {selected_rg}",
        conversation_scope=_get_conversation_scope(activity),
    )
    _pending_confirmations[conversation_id] = pending

    await context.send_activity(
        MessageFactory.text(_build_confirmation_prompt(pending))
    )
    _last_response_utc = _utc_now()
    if heartbeat_service:
        heartbeat_service.update_last_response(_last_response_utc)


# ── Confirmation handler ──────────────────────────────────────

async def _handle_confirmation(
    context: TurnContext,
    raw_text: str,
    conversation_id: str,
    *,
    dispatcher: AzureQueueJobDispatcher,
    heartbeat_service=None,
) -> None:
    """Process a user's yes/no reply to a pending confirmation."""
    global _last_response_utc
    activity = context.activity
    answer = raw_text.strip().lower()
    pending = _pending_confirmations[conversation_id]

    if answer in ("no", "n", "cancel", "abort", "deny"):
        del _pending_confirmations[conversation_id]
        op_label = "Build" if pending.operation == "build" else "Teardown"
        await context.send_activity(
            MessageFactory.text(f"{op_label} cancelled.")
        )
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    if answer in ("yes", "y", "confirm", "ok", "proceed"):
        del _pending_confirmations[conversation_id]

        job = QueuedJob(
            operation=pending.operation,
            requested_by=pending.requester,
            requested_by_upn=pending.requester_upn,
            requested_by_object_id=pending.requester_object_id,
            conversation_id=conversation_id,
            conversation_scope=pending.conversation_scope or "",
            model=pending.model,
            resource_group=pending.resource_group,
            source_command=pending.source_command,
        )
        dispatcher.enqueue(job)

        if pending.operation == "build":
            ack = (
                f"✅ Queued `build it` as job `{job.job_id}`.<br>"
                f"Model: `{pending.model}`<br>"
                "The worker will post progress updates every 60 seconds "
                "during deployment."
            )
        else:  # teardown
            ack = (
                f"🚧 👷 Queued `teardown` for `{pending.resource_group}` "
                f"as job `{job.job_id}` 👷🚧<br>"
                "The worker will post progress updates every 60 seconds "
                "during cleanup."
            )

        await context.send_activity(MessageFactory.text(ack))
        _last_response_utc = _utc_now()
        if heartbeat_service:
            heartbeat_service.update_last_response(_last_response_utc)
        return

    # Unrecognized reply — re-prompt
    await context.send_activity(
        MessageFactory.text(
            f"Please reply `yes` or `no`.<br><br>"
            + _build_confirmation_prompt(pending)
        )
    )
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
    weather_service: WeatherService,
    msft_docs_service: MicrosoftLearnMcpService,
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

        # ── Active sessions? Handle pending confirmation or selection
        _expire_sessions()
        if conversation_id in _pending_confirmations:
            await _handle_confirmation(
                context, raw_text, conversation_id,
                dispatcher=dispatcher,
                heartbeat_service=heartbeat_service,
            )
            return
        if conversation_id in _teardown_sessions:
            await _handle_teardown_selection(
                context, raw_text, conversation_id,
                heartbeat_service=heartbeat_service,
            )
            return
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
            msg = "I did not recognize that command.<br><br>" + _get_help_text()
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
                scope_parts = []
                tid = team.get('id')
                if tid:
                    scope_parts.append(f"team={tid}")
                scope_parts.append(f"channel={listening_in}")
                text = "<br>".join([
                    "🟢 Status: Online ✅",
                    "📜 Script: Bot-the-Builder (M365 Agents SDK)",
                    f"🆔 PID: {os.getpid()}",
                    f"💬 Last response: {_last_response_utc}",
                    f"📢 Listening in: {' '.join(scope_parts)}",
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

        if command.kind == "weather":
            text = await weather_service.get_weather_text(command.location)
            await context.send_activity(MessageFactory.text(text))
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        if command.kind == "msft-docs":
            text = await msft_docs_service.get_docs_text(command.query)
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
                        f"Unknown model `{command.model}`.<br><br>"
                        + _model_selection_prompt()
                    )
                )
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            # Valid model provided — require confirmation before queueing
            requester_identity = await _get_requester_identity(context)
            pending = PendingConfirmation(
                operation="build",
                requester=requester_identity["requested_by"],
                requester_upn=requester_identity["requested_by_upn"],
                requester_object_id=requester_identity["requested_by_object_id"],
                model=command.model,
                source_command=command.raw_text,
                conversation_scope=_get_conversation_scope(activity),
            )
            _pending_confirmations[conversation_id] = pending
            await context.send_activity(
                MessageFactory.text(_build_confirmation_prompt(pending))
            )
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        # Teardown — bare command lists builds for selection; with RG goes to confirm
        if command.kind == "teardown":
            if command.resource_group is None:
                # Bare teardown — fetch builds and prompt for selection
                builds = _list_foundry_builds()
                if not builds:
                    await context.send_activity(
                        MessageFactory.text("No active Foundry builds found.")
                    )
                    _last_response_utc = _utc_now()
                    if heartbeat_service:
                        heartbeat_service.update_last_response(_last_response_utc)
                    return
                _teardown_sessions[conversation_id] = TeardownSession(builds=builds)
                await context.send_activity(
                    MessageFactory.text(_teardown_selection_prompt(builds))
                )
                _last_response_utc = _utc_now()
                if heartbeat_service:
                    heartbeat_service.update_last_response(_last_response_utc)
                return

            # Resource group provided — go straight to confirmation
            requester_identity = await _get_requester_identity(context)
            pending = PendingConfirmation(
                operation="teardown",
                requester=requester_identity["requested_by"],
                requester_upn=requester_identity["requested_by_upn"],
                requester_object_id=requester_identity["requested_by_object_id"],
                resource_group=command.resource_group,
                source_command=command.raw_text,
                conversation_scope=_get_conversation_scope(activity),
            )
            _pending_confirmations[conversation_id] = pending
            await context.send_activity(
                MessageFactory.text(_build_confirmation_prompt(pending))
            )
            _last_response_utc = _utc_now()
            if heartbeat_service:
                heartbeat_service.update_last_response(_last_response_utc)
            return

        # Non-destructive commands — queue immediately
        requester_identity = await _get_requester_identity(context)
        job = QueuedJob(
            operation=command.kind,
            requested_by=requester_identity["requested_by"],
            requested_by_upn=requester_identity["requested_by_upn"],
            requested_by_object_id=requester_identity["requested_by_object_id"],
            conversation_id=conversation_id,
            conversation_scope=_get_conversation_scope(activity),
            model=command.model,
            resource_group=command.resource_group,
            source_command=command.raw_text,
        )
        dispatcher.enqueue(job)

        if command.kind == "build-status":
            ack = (
                f"🔍 Queued `build status` for `{command.resource_group}` "
                f"as job `{job.job_id}`."
            )
        else:  # list-builds
            ack = f"📋 Queued `list builds` as job `{job.job_id}`."

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
                "👋 **Hello! I'm Bot-the-Builder.**<br><br>"
                "I manage Azure AI Foundry deployments from this "
                "Teams channel.<br><br>" + _get_help_text()
            )
            await context.send_activity(MessageFactory.text(welcome))
