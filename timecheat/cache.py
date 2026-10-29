"""On-disk cache of a project's issues, used for the issue search box."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .model import Issue
from .settings import server_host


@dataclass(frozen=True)
class CachedProject:
    project: str
    fetched_at: datetime
    issues: list[Issue]


class IssueCache:
    def __init__(self, root: Path, server: str) -> None:
        self.dir = root / "cache" / server_host(server).replace(":", "_")

    def _path(self, project: str) -> Path:
        return self.dir / f"{project}.json"

    def load(self, project: str) -> CachedProject | None:
        try:
            data = json.loads(self._path(project).read_text(encoding="utf-8"))
            return CachedProject(
                project=project,
                fetched_at=datetime.fromisoformat(data["fetched_at"]),
                issues=[Issue.from_dict(i) for i in data["issues"]],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, project: str, issues: list[Issue], fetched_at: datetime | None = None) -> CachedProject:
        cached = CachedProject(project, fetched_at or datetime.now(), issues)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(project).with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "project": project,
                    "fetched_at": cached.fetched_at.isoformat(timespec="seconds"),
                    "issues": [i.to_dict() for i in issues],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path(project))
        return cached
