from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonConversationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def save(self, conversation_id: str, payload: dict[str, Any]) -> None:
        data = self._read_all()
        data[conversation_id] = payload
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        return self._read_all().get(conversation_id)

    def _read_all(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))
