"""An in-memory Jira for trying the UI without a server: ``python run.py --demo``.

Seeds a few projects, issues assigned to the demo user and worklogs for the
previous, current and next month (with some overlapping entries on purpose).
Every call sleeps a little so the busy indicators can be seen. Booking on
``DEMO-13`` always fails, to show how upload errors are reported.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import threading
import time
from datetime import date, datetime, timedelta

from .jira_client import ChildIssue, JiraError, ProgressCallback, Project, User
from .model import Issue, Worklog, remote_uid
from .outlook import EXCHANGE_GOID_PREFIX, GraphAuthError, series_id

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
        self._parents: dict[str, str] = {}
        self._comments: dict[str, list[str]] = {}
        self._issues[MEETING_STORY] = Issue(MEETING_STORY, "General meetings", "Story", "In Progress", "DEMO")
        for key, summary, comments in (
            ("DEMO-51", "Daily stand-up", [f'Recurring meeting.\n"iCalUId": "{series_id(goid(STANDUP_SERIES, None))}"']),
            ("DEMO-52", "Sprint sync", []),
        ):
            self._issues[key] = Issue(key, summary, "Sub-task", "In Progress", "DEMO")
            self._parents[key] = MEETING_STORY
            self._comments[key] = comments
        self._next_issue = itertools.count(60)
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

    def find_stories(self, project: str, summary: str) -> list[Issue]:
        self._sleep()
        wanted = summary.strip().casefold()
        return [i for i in self._issues.values() if i.project == project and i.summary.casefold() == wanted]

    def children_with_comments(self, parent_key: str) -> list[ChildIssue]:
        self._sleep()
        with self._lock:
            return [
                ChildIssue(self._issues[key], f"2026-10-{n:02d}T10:00:00.000+0100", list(self._comments.get(key, [])))
                for n, (key, parent) in enumerate(sorted(self._parents.items()), start=1)
                if parent == parent_key
            ]

    def create_subtask(self, project: str, parent_key: str, summary: str, description: str) -> Issue:
        self._sleep()
        if parent_key not in self._issues:
            raise JiraError(f"Parent {parent_key} does not exist.")
        if parent_key in self._parents:
            raise JiraError(f"{parent_key} is a sub-task; a sub-task cannot be its parent.")
        if not summary.strip():
            raise JiraError("summary: You must specify a summary of the issue.")
        with self._lock:
            key = f"{project}-{next(self._next_issue)}"
            issue = Issue(key, summary.strip(), "Sub-task", "Open", project)
            self._issues[key] = issue
            self._parents[key] = parent_key
            self._comments[key] = []
            self._assigned.add(key)
        return issue

    def add_comment(self, issue_key: str, body: str) -> None:
        self._sleep()
        self._check(issue_key)
        with self._lock:
            self._comments.setdefault(issue_key, []).append(body)


# --------------------------------------------------------------------------- #
# Outlook
# --------------------------------------------------------------------------- #

MEETING_STORY = "DEMO-50"
STANDUP_SERIES = "6C1F0A2B9D3E4F5061728394A5B6C7D8"
SYNC_SERIES = "11223344556677889900AABBCCDDEEFF"
RETRO_SERIES = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def goid(series: str, day: date | None) -> str:
    """An Exchange global object ID like Graph returns as iCalUId (day=None: the series ID)."""
    stamp = f"{day.year:04X}{day.month:02X}{day.day:02X}" if day else "00000000"
    return f"{EXCHANGE_GOID_PREFIX}{stamp}C465B82C7551DD01000000000000000010000000{series}"


class FakeGraph:
    """Answers ``calendar_view`` with a repeatable week pattern of meetings.

    Any token is accepted except ``expired``, which behaves like an expired token.
    """

    def __init__(self, token: str, latency: float = LATENCY) -> None:
        self.token = token.strip()
        self.latency = latency

    def calendar_view(self, first: date, last: date) -> list[dict]:
        time.sleep(self.latency * 2)
        if self.token.lower() == "expired":
            raise GraphAuthError("The Graph access token is expired or invalid. Paste a fresh one.")
        events: list[dict] = []
        day = first
        while day <= last:
            events.extend(self._day(day))
            day += timedelta(days=1)
        return events

    @staticmethod
    def _event(day: date, subject: str, start: str, end: str, *, series: str | None = None, **extra: object) -> dict:
        uid = goid(series, day) if series else goid(hashlib.md5(f"{subject}{day}".encode()).hexdigest().upper(), None)
        event = {
            "id": f"evt-{day.isoformat()}-{subject}",
            "subject": subject,
            "start": {"dateTime": f"{day.isoformat()}T{start}:00.0000000", "timeZone": "Europe/Budapest"},
            "end": {"dateTime": f"{day.isoformat()}T{end}:00.0000000", "timeZone": "Europe/Budapest"},
            "isAllDay": False,
            "isCancelled": False,
            "showAs": "busy",
            "responseStatus": {"response": "accepted"},
            "sensitivity": "normal",
            "type": "occurrence" if series else "singleInstance",
            "iCalUId": uid,
        }
        event.update(extra)
        return event

    def _day(self, day: date) -> list[dict]:
        weekday = day.weekday()
        if weekday >= 5:
            return []
        e = self._event
        events = [e(day, "Daily stand-up", "09:30", "09:45", series=STANDUP_SERIES)]
        if weekday == 0:
            events += [
                e(day, "Office day", "00:00", "00:00", isAllDay=True, showAs="free"),
                e(day, "Planning (cancelled)", "11:00", "12:00", isCancelled=True),
                e(day, "Backlog refinement", "13:00", "14:30"),
            ]
        elif weekday == 1:
            events += [e(day, "Sprint sync", "14:00", "15:00", series=SYNC_SERIES)]
        elif weekday == 2:
            events += [
                e(day, "Architecture review", "10:00", "11:30"),
                e(day, "Vendor call", "11:00", "12:00"),
                e(day, "Optional: Lunch & learn", "12:00", "13:00", showAs="tentative",
                  responseStatus={"response": "tentativelyAccepted"}),
            ]
        elif weekday == 3:
            events += [
                e(day, "Doctor", "15:00", "16:00", sensitivity="private"),
                e(day, "Town hall", "16:00", "17:00", responseStatus={"response": "declined"}),
                e(day, "Customer workshop", "10:00", "12:00"),
            ]
        else:
            events += [e(day, "Team retro", "13:00", "14:00", series=RETRO_SERIES)]
        return events
