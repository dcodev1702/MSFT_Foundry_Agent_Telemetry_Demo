from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


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
    model: str | None = None
    resource_group: str | None = None
    source_command: str | None = None
    job_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    submitted_utc: str = field(default_factory=utc_now)
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
