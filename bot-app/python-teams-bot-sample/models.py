from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# Must match PowerShell's Get-AllowedAiModelChoices (deploy-foundry-env.ps1)
ALLOWED_MODELS: list[str] = [
    "gpt-4.1-mini",
    "gpt-5.3",
    "gpt-5.4",
    "grok-4-1-fast-reasoning",
]


@dataclass(slots=True)
class BuildSession:
    """Tracks model selection + confirmation state for build commands."""
    state: str = "selecting"              # "selecting" or "confirming"
    selected_model: str | None = None
    created_at: float = field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp()
    )


@dataclass(slots=True)
class TeardownSession:
    """Tracks multi-turn teardown selection state for a conversation."""
    builds: list[str] = field(default_factory=list)
    state: str = "selecting"
    selected_rg: str | None = None
    created_at: float = field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp()
    )


@dataclass(slots=True)
class PendingConfirmation:
    """Tracks a pending yes/no confirmation for a destructive operation."""
    operation: str  # "build" or "teardown"
    requester: str
    requester_upn: str | None = None
    requester_object_id: str | None = None
    model: str | None = None
    resource_group: str | None = None
    source_command: str | None = None
    conversation_scope: str | None = None
    created_at: float = field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp()
    )


@dataclass(slots=True)
class FoundryCommand:
    kind: str
    raw_text: str
    model: str | None = None
    resource_group: str | None = None
    requires_confirmation: bool = False


@dataclass(slots=True)
class QueuedJob:
    operation: str
    requested_by: str
    conversation_id: str
    conversation_scope: str
    requested_by_upn: str | None = None
    requested_by_object_id: str | None = None
    model: str | None = None
    resource_group: str | None = None
    source_command: str | None = None
    job_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    submitted_utc: str = field(default_factory=utc_now)
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
