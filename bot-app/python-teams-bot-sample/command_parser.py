from __future__ import annotations

import re

from models import FoundryCommand

RESOURCE_GROUP_PATTERN = re.compile(r"^zolab-ai-[a-z0-9-]+$", re.IGNORECASE)


def _strip_quotes(value: str) -> str:
    return value.strip().strip("'\"")


def parse_command(text: str | None) -> FoundryCommand:
    raw_text = (text or "").strip()
    normalized = raw_text.lower()

    if not raw_text:
        return FoundryCommand(kind="help", raw_text=raw_text)

    if normalized in {"help", "?", "commands"}:
        return FoundryCommand(kind="help", raw_text=raw_text)

    if normalized == "heartbeat":
        return FoundryCommand(kind="heartbeat", raw_text=raw_text)

    if normalized == "listener status":
        return FoundryCommand(kind="listener-status", raw_text=raw_text)

    if normalized == "list builds":
        return FoundryCommand(kind="list-builds", raw_text=raw_text)

    if normalized.startswith("build it"):
        parts = raw_text.split(maxsplit=2)
        model = parts[2].strip() if len(parts) == 3 else None
        return FoundryCommand(
            kind="build",
            raw_text=raw_text,
            model=model,
            requires_confirmation=True,
        )

    if normalized.startswith("build status "):
        resource_group = _strip_quotes(raw_text[len("build status "):])
        if RESOURCE_GROUP_PATTERN.match(resource_group):
            return FoundryCommand(
                kind="build-status",
                raw_text=raw_text,
                resource_group=resource_group,
                requires_confirmation=True,
            )

    if normalized == "teardown":
        return FoundryCommand(
            kind="teardown",
            raw_text=raw_text,
            requires_confirmation=True,
        )

    if normalized.startswith("teardown "):
        resource_group = _strip_quotes(raw_text[len("teardown "):])
        if RESOURCE_GROUP_PATTERN.match(resource_group):
            return FoundryCommand(
                kind="teardown",
                raw_text=raw_text,
                resource_group=resource_group,
                requires_confirmation=True,
            )

    return FoundryCommand(kind="unknown", raw_text=raw_text)
