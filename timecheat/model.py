"""Domain model: issues, worklogs, the local working copy and the day layout.

Every time in here is a *naive wall-clock time in Europe/Budapest*. Time zones
only exist at the Jira boundary (see ``jira_client``), which keeps the calendar
arithmetic trivial and immune to the browser's own time zone.

The working copy (``Timesheet``) keeps two dicts keyed by a local ``uid``:

* ``remote``  - the worklogs exactly as last read from (or written to) Jira,
* ``current`` - what the user wants Jira to look like.

Pending changes are not tracked separately; they are the difference between
the two, which makes "revert", "discard all" and "what will be uploaded" all
fall out of the same comparison.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

REMOTE_PREFIX = "jira-"
DRAFT_PREFIX = "draft-"


@dataclass(frozen=True)
class Issue:
    key: str
    summary: str
    issue_type: str
    status: str = ""
    project: str = ""

    @property
    def label(self) -> str:
        return f"{self.key} · [{self.issue_type}] {self.summary}"

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "summary": self.summary,
            "issue_type": self.issue_type,
            "status": self.status,
            "project": self.project,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Issue:
        return cls(
            key=data["key"],
            summary=data.get("summary", ""),
            issue_type=data.get("issue_type", ""),
            status=data.get("status", ""),
            project=data.get("project", ""),
        )


MATCH_JIRA = "jira"  # found in the iCalUId comment of a Jira issue - survives restarts
MATCH_SESSION = "session"  # chosen by hand earlier in this session - memory only


@dataclass(frozen=True)
class MeetingRef:
    """Where a draft came from when it was imported from Outlook."""

    event_id: str
    subject: str
    series_id: str
    recurring: bool
    match: str = ""  # how the issue was chosen automatically: MATCH_JIRA, MATCH_SESSION or ""

    @property
    def auto_matched(self) -> bool:
        return bool(self.match)


@dataclass(frozen=True)
class Worklog:
    uid: str
    issue_key: str
    start: datetime
    seconds: int
    comment: str = ""
    worklog_id: str | None = None
    # Set on drafts imported from Outlook; an empty ``issue_key`` means "not assigned yet".
    meeting: MeetingRef | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def is_remote(self) -> bool:
        return self.worklog_id is not None

    @property
    def is_assigned(self) -> bool:
        return bool(self.issue_key)


def remote_uid(worklog_id: str) -> str:
    return f"{REMOTE_PREFIX}{worklog_id}"


def new_draft_uid() -> str:
    return f"{DRAFT_PREFIX}{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# working copy
# --------------------------------------------------------------------------- #

ChangeKind = Literal["create", "update", "move", "delete"]


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    uid: str
    before: Worklog | None
    after: Worklog | None

    @property
    def sort_key(self) -> tuple[datetime, str]:
        worklog = self.after or self.before
        assert worklog is not None
        return worklog.start, self.uid


class Timesheet:
    def __init__(self, remote: Iterable[Worklog] = ()) -> None:
        self.remote: dict[str, Worklog] = {w.uid: w for w in remote}
        self.current: dict[str, Worklog] = dict(self.remote)

    # -- reading ------------------------------------------------------------ #

    def on_day(self, day: date) -> list[Worklog]:
        return sorted(
            (w for w in self.current.values() if w.day == day),
            key=lambda w: (w.start, w.uid),
        )

    def between(self, first: date, last: date) -> list[Worklog]:
        return sorted(
            (w for w in self.current.values() if first <= w.day <= last),
            key=lambda w: (w.start, w.uid),
        )

    def is_pending(self, uid: str) -> bool:
        return self.current.get(uid) != self.remote.get(uid)

    def changes(self) -> list[Change]:
        result: list[Change] = []
        for uid in self.remote.keys() | self.current.keys():
            before = self.remote.get(uid)
            after = self.current.get(uid)
            if before == after:
                continue
            if before is None:
                kind: ChangeKind = "create"
            elif after is None:
                kind = "delete"
            elif before.issue_key != after.issue_key:
                # Jira cannot move a worklog to another issue.
                kind = "move"
            else:
                kind = "update"
            result.append(Change(kind, uid, before, after))
        return sorted(result, key=lambda c: c.sort_key)

    # -- editing ------------------------------------------------------------ #

    def put(self, worklog: Worklog) -> None:
        self.current[worklog.uid] = worklog

    def add_draft(
        self, issue_key: str, start: datetime, seconds: int, comment: str = "", meeting: MeetingRef | None = None
    ) -> Worklog:
        worklog = Worklog(new_draft_uid(), issue_key, start, seconds, comment, meeting=meeting)
        self.current[worklog.uid] = worklog
        return worklog

    def unassigned(self) -> list[Worklog]:
        return sorted(
            (w for w in self.current.values() if not w.is_assigned),
            key=lambda w: (w.start, w.uid),
        )

    def assign_series(self, series_id: str, issue_key: str, match: str = MATCH_SESSION) -> list[Worklog]:
        """Give every still unassigned draft of a recurring meeting series the same issue."""
        assigned: list[Worklog] = []
        for worklog in list(self.current.values()):
            meeting = worklog.meeting
            if worklog.is_assigned or meeting is None or not meeting.recurring or meeting.series_id != series_id:
                continue
            updated = replace(worklog, issue_key=issue_key, meeting=replace(meeting, match=match))
            self.current[worklog.uid] = updated
            assigned.append(updated)
        return assigned

    def imported_event_ids(self, day: date) -> set[str]:
        return {w.meeting.event_id for w in self.current.values() if w.meeting is not None and w.day == day}

    def remove(self, uid: str) -> None:
        self.current.pop(uid, None)

    def revert(self, uid: str) -> None:
        if uid in self.remote:
            self.current[uid] = self.remote[uid]
        else:
            self.current.pop(uid, None)

    def discard_all(self) -> None:
        self.current = dict(self.remote)

    def copy_day(self, source: date, target: date) -> list[Worklog]:
        shift = target - source
        return [
            self.add_draft(w.issue_key, w.start + shift, w.seconds, w.comment)
            for w in self.on_day(source)
            if w.is_assigned
        ]

    # -- syncing with Jira -------------------------------------------------- #

    def merge_remote(self, first: date, last: date, fresh: Iterable[Worklog]) -> None:
        """Replace the Jira baseline for ``first..last`` and keep local edits."""
        fresh_by_uid = {w.uid: w for w in fresh}
        old_uids = {uid for uid, w in self.remote.items() if first <= w.day <= last}
        dirty = {uid for uid in old_uids if self.is_pending(uid)}

        for uid in old_uids:
            if uid not in dirty:
                self.current.pop(uid, None)
            del self.remote[uid]

        for uid, worklog in fresh_by_uid.items():
            self.remote[uid] = worklog
            if uid not in dirty:
                self.current[uid] = worklog

        # Edited locally but deleted in Jira meanwhile: keep the edit as a new entry.
        for uid in dirty - fresh_by_uid.keys():
            edited = self.current.pop(uid, None)
            if edited is not None:
                draft = replace(edited, uid=new_draft_uid(), worklog_id=None)
                self.current[draft.uid] = draft

    def confirm_create(self, change_uid: str, created: Worklog) -> None:
        # For a move the old uid stays in ``remote`` and therefore becomes a pending delete.
        self.current.pop(change_uid, None)
        self.remote[created.uid] = created
        self.current[created.uid] = created

    def confirm_update(self, updated: Worklog) -> None:
        self.remote[updated.uid] = updated
        self.current[updated.uid] = updated

    def confirm_delete(self, uid: str) -> None:
        self.remote.pop(uid, None)
        self.current.pop(uid, None)


# --------------------------------------------------------------------------- #
# day layout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Placed:
    """A worklog as drawn on the calendar: overlapping entries are queued up."""

    worklog: Worklog
    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.worklog.seconds)

    @property
    def shifted(self) -> bool:
        return self.start != self.worklog.start


GRID = timedelta(minutes=30)


def _since_grid(moment: datetime) -> timedelta:
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (moment - midnight) % GRID


def snap_to_grid(moment: datetime) -> datetime:
    """The nearest grid point (half past rounds up)."""
    offset = _since_grid(moment)
    return moment - offset if offset < GRID / 2 else moment - offset + GRID


def round_up_to_grid(moment: datetime, max_gap: timedelta) -> datetime:
    """The next grid point if it is at most ``max_gap`` away, otherwise ``moment`` unchanged."""
    offset = _since_grid(moment)
    if offset and GRID - offset <= max_gap:
        return moment - offset + GRID
    return moment


def layout_day(worklogs: Iterable[Worklog]) -> list[Placed]:
    placed: list[Placed] = []
    cursor: datetime | None = None
    for worklog in sorted(worklogs, key=lambda w: (w.start, w.uid)):
        start = worklog.start if cursor is None or worklog.start >= cursor else cursor
        placed.append(Placed(worklog, start))
        cursor = start + timedelta(seconds=worklog.seconds)
    return placed


# --------------------------------------------------------------------------- #
# day status
# --------------------------------------------------------------------------- #

DayState = Literal["ok", "under", "over", "off", "future"]


@dataclass(frozen=True)
class DayStatus:
    day: date
    logged_seconds: int
    target_seconds: int
    state: DayState

    @property
    def label(self) -> str:
        return f"{format_hours(self.logged_seconds)}/{format_hours(self.target_seconds)}h"


def day_status(day: date, logged_seconds: int, target_seconds: int, today: date) -> DayStatus:
    if logged_seconds > target_seconds:
        state: DayState = "over"
    elif target_seconds == 0:
        state = "off"
    elif logged_seconds == target_seconds:
        state = "ok"
    elif day > today and logged_seconds == 0:
        state = "future"
    else:
        state = "under"
    return DayStatus(day, logged_seconds, target_seconds, state)


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


def format_hours(seconds: int) -> str:
    hours = round(seconds / 3600, 2)
    return f"{hours:g}"


def format_duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"
