"""An in-memory Jira for trying the UI without a server: ``python run.py --demo``.

Seeds a few projects, issues assigned to the demo user and worklogs for the
previous, current and next month (with some overlapping entries on purpose).
Every call sleeps a little so the busy indicators can be seen. Booking on
``DEMO-13`` always fails, to show how upload errors are reported.
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from datetime import date, datetime, timedelta

from .jira_client import JiraError, ProgressCallback, Project, User
from .model import Issue, Worklog, remote_uid

LATENCY = 0.25
LOCKED_ISSUE = "DEMO-13"

_TYPES = ["Story", "Task", "Bug", "Sub-task", "Epic"]
_TOPICS = [
    "Kafka consumer lag alert", "BigQuery cost report", "Spark job tuning", "Daily stand-up",
    "Loader refactoring", "Data quality checks", "Airflow DAG migration", "Code review",
    "Customer hub init load", "Geoloc LTE fix", "Release preparation", "Archiver monitor",
    "Clickstream schema change", "Locked: closed sprint", "Documentation", "Support ticket triage",
]


class FakeJira:
    def __init__(self, today: date | None = None, latency: float = LATENCY) -> None:
        self.latency = latency
        self.user = User(name="demo", key="demo", display_name="Demo User")
        self._lock = threading.Lock()
        self._ids = itertools.count(10_000)
        self._projects = [Project("DEMO", "Demo project"), Project("ACME", "Another project"),
                          Project("OPS", "Operations")]
        rng = random.Random(42)
        self._issues: dict[str, Issue] = {}
        self._assigned: set[str] = set()
        for project in self._projects:
            for n in range(1, 41 if project.key == "DEMO" else 26):
                key = f"{project.key}-{n}"
                topic = _TOPICS[(n - 1) % len(_TOPICS)]
                self._issues[key] = Issue(key, f"{topic} #{n}", rng.choice(_TYPES), "In Progress", project.key)
                if n % 4 == 1:
                    self._assigned.add(key)
        self._issues[LOCKED_ISSUE] = Issue(LOCKED_ISSUE, "Locked: closed sprint", "Task", "Closed", "DEMO")
        self._worklogs: dict[str, Worklog] = {}
        self._seed((today or date.today()), rng)

    def _seed(self, today: date, rng: random.Random) -> None:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        keys = sorted(self._assigned)
        day = first
        while day < today:
            if day.weekday() < 5 and rng.random() > 0.15:
                cursor = datetime.combine(day, datetime.min.time()).replace(hour=8 + rng.choice([0, 0, 1]))
                remaining = rng.choice([8, 8, 8, 6, 7.5, 4]) * 3600
                while remaining > 0:
                    seconds = int(min(remaining, rng.choice([1, 1.5, 2, 3]) * 3600))
                    overlap = timedelta(minutes=30) if rng.random() < 0.12 else timedelta()
                    self._add(rng.choice(keys), cursor - overlap, seconds, "")
                    cursor += timedelta(seconds=seconds)
                    remaining -= seconds
            day += timedelta(days=1)

    def _add(self, key: str, start: datetime, seconds: int, comment: str) -> Worklog:
        worklog_id = str(next(self._ids))
        worklog = Worklog(remote_uid(worklog_id), key, start, seconds, comment, worklog_id)
        self._worklogs[worklog_id] = worklog
        return worklog

    def _sleep(self) -> None:
        time.sleep(self.latency)

    # -- JiraBackend -------------------------------------------------------- #

    def projects(self) -> list[Project]:
        self._sleep()
        return list(self._projects)

    def project_issues(self, project: str, resolved_within_days: int) -> list[Issue]:
        self._sleep()
        return [i for i in self._issues.values() if i.project == project]

    def assigned_issues(self) -> list[Issue]:
        self._sleep()
        return [self._issues[k] for k in sorted(self._assigned)]

    def my_worklogs(
        self, first: date, last: date, progress: ProgressCallback | None = None
    ) -> tuple[list[Worklog], list[Issue]]:
        with self._lock:
            worklogs = [w for w in self._worklogs.values() if first <= w.day <= last]
        keys = sorted({w.issue_key for w in worklogs})
        for n in range(len(keys) + 1):
            if progress:
                progress(n, len(keys))
            time.sleep(self.latency / 4)
        return worklogs, [self._issues[k] for k in keys]

    def create_worklog(self, worklog: Worklog) -> Worklog:
        self._sleep()
        self._check(worklog.issue_key)
        with self._lock:
            return self._add(worklog.issue_key, worklog.start, worklog.seconds, worklog.comment)

    def update_worklog(self, worklog: Worklog) -> Worklog:
        self._sleep()
        self._check(worklog.issue_key)
        with self._lock:
            if worklog.worklog_id not in self._worklogs:
                raise JiraError(f"Worklog {worklog.worklog_id} does not exist.")
            self._worklogs[worklog.worklog_id] = worklog
        return worklog

    def delete_worklog(self, issue_key: str, worklog_id: str) -> None:
        self._sleep()
        self._check(issue_key)
        with self._lock:
            if self._worklogs.pop(worklog_id, None) is None:
                raise JiraError(f"Worklog {worklog_id} does not exist.")

    def _check(self, key: str) -> None:
        if key not in self._issues:
            raise JiraError(f"Issue {key} does not exist.")
        if key == LOCKED_ISSUE:
            raise JiraError(f"{key}: you cannot log work on a closed issue (demo error).")
