from __future__ import annotations

import json
from pathlib import Path

from models import QueuedJob


class FileJobDispatcher:
    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self.pending_path = self.base_path / "pending"
        self.pending_path.mkdir(parents=True, exist_ok=True)

    def enqueue(self, job: QueuedJob) -> Path:
        job_path = self.pending_path / f"{job.job_id}.json"
        job_path.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        return job_path

    def queue_depth(self) -> int:
        return len(list(self.pending_path.glob("*.json")))
