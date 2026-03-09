# ════════════════════════════════════════════════════════════════
# conversation_store.py — JSON-file-based conversation persistence
# Stores conversation metadata, references, and identities for
# proactive messaging.
# ════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonConversationStore:
    """Persists conversation metadata and proactive-messaging references to JSON files.

    Three files under the parent directory of `path`:
      - conversations.json  — channel/team metadata for each conversation
      - references.json     — serialised ConversationReference dicts
      - identities.json     — serialised ClaimsIdentity dicts
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

        # Separate files for references and identities
        self._refs_path = self.path.parent / "references.json"
        self._identity_path = self.path.parent / "identities.json"
        for p in (self._refs_path, self._identity_path):
            if not p.exists():
                p.write_text("{}", encoding="utf-8")

    # ── Conversation metadata ──────────────────────────────────

    def save(self, conversation_id: str, payload: dict[str, Any]) -> None:
        data = self._read_all()
        data[conversation_id] = payload
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_all().get(conversation_id)

    # ── Conversation references (for proactive messaging) ──────

    def save_reference(self, conversation_id: str, ref_dict: dict[str, Any]) -> None:
        data = self._read_json(self._refs_path)
        data[conversation_id] = ref_dict
        self._write_json(self._refs_path, data)

    def get_reference(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_json(self._refs_path).get(conversation_id)

    def get_all_reference_ids(self) -> list[str]:
        return list(self._read_json(self._refs_path).keys())

    # ── Claims identities (for proactive messaging auth) ───────

    def save_identity(self, conversation_id: str, identity_dict: dict[str, Any]) -> None:
        data = self._read_json(self._identity_path)
        data[conversation_id] = identity_dict
        self._write_json(self._identity_path, data)

    def get_identity(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_json(self._identity_path).get(conversation_id)

    # ── Internal helpers ───────────────────────────────────────

    def _read_all(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
