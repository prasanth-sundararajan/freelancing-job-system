"""Job sources for the Scout agent. Only official, allowed channels."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class FileSource:
    """Reads jobs from a JSON file (a list of job objects). Used for testing and manual imports."""

    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self) -> list[dict]:
        records = json.loads(self.path.read_text(encoding="utf-8"))
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in records:
            r.setdefault("posted_at", stamp)
            r.setdefault("source", "file")
        return records


# The Freelancer.com source lives in freelancer.py (FreelancerAPISource).
