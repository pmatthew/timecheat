"""Turning Outlook meetings into worklog drafts: conflicts, free-slot clipping, comment matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import Worklog, layout_day, round_up_to_grid
from .outlook import Meeting, series_id

MIN_FRAGMENT = timedelta(minutes=5)
# Outlook often ends meetings 5-10 minutes early (10:00-10:25); such ends are booked up to the half hour.
ROUND_UP_MAX = timedelta(minutes=10)
ICAL_COMMENT_RE = re.compile(r'"?iCalUId"?\s*[:=]\s*"?([0-9A-Fa-f]{32,})')


@dataclass(frozen=True)
class Fragment:
    meeting: Meeting
    start: datetime
    end: datetime
    rounded: bool = False

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def clipped(self) -> bool:
        if self.start != self.meeting.start:
            return True
        return self.end < self.meeting.end if self.rounded else self.end != self.meeting.end


@dataclass(frozen=True)
class FillResult:
    fragments: list[Fragment]
    skipped: list[tuple[Meeting, str]]


def find_conflicts(meetings: Iterable[Meeting]) -> list[list[Meeting]]:
    """Groups of meetings that overlap each other (chained overlaps form one group)."""
    groups: list[list[Meeting]] = []
    current: list[Meeting] = []
    current_end: datetime | None = None
    for meeting in sorted(meetings, key=lambda m: (m.start, m.end, m.id)):
        if current and current_end is not None and meeting.start < current_end:
            current.append(meeting)
            current_end = max(current_end, meeting.end)
            continue
        if len(current) > 1:
            groups.append(current)
        current, current_end = [meeting], meeting.end
    if len(current) > 1:
        groups.append(current)
    return groups


def _subtract(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    parts = [(start, end)]
    for busy_start, busy_end in busy:
        next_parts = []
        for part_start, part_end in parts:
            if busy_end <= part_start or busy_start >= part_end:
                next_parts.append((part_start, part_end))
                continue
            if part_start < busy_start:
                next_parts.append((part_start, busy_start))
            if busy_end < part_end:
                next_parts.append((busy_end, part_end))
        parts = next_parts
    return parts


def fill_day(day_worklogs: Iterable[Worklog], meetings: Iterable[Meeting], already_imported: set[str]) -> FillResult:
    """Fit meetings into the free time of a day, as the day view displays it.

    Meetings are processed chronologically; each one takes the free parts of its
    own time range (pieces shorter than ``MIN_FRAGMENT`` are dropped) and then
    counts as busy for the following ones. An end at most ``ROUND_UP_MAX`` before
    the next half hour is extended to it, as far as that time is free and no other
    meeting starts in it.
    """
    busy = [(p.start, p.end) for p in layout_day(day_worklogs)]
    todo = sorted((m for m in meetings if m.id not in already_imported), key=lambda m: (m.start, m.end, m.id))
    fragments: list[Fragment] = []
    skipped: list[tuple[Meeting, str]] = []
    for meeting in sorted(meetings, key=lambda m: (m.start, m.end, m.id)):
        if meeting.id in already_imported:
            skipped.append((meeting, "already imported"))
            continue
        parts = [(s, e) for s, e in _subtract(meeting.start, meeting.end, busy) if e - s >= MIN_FRAGMENT]
        if not parts:
            skipped.append((meeting, "no free time left in its slot"))
            continue
        for start, end in parts:
            rounded_end = _rounded_end(meeting, todo, busy) if end == meeting.end else end
            fragments.append(Fragment(meeting, start, rounded_end, rounded=rounded_end != end))
            busy.append((start, rounded_end))
    return FillResult(fragments, skipped)


def _rounded_end(meeting: Meeting, others: list[Meeting], busy: list[tuple[datetime, datetime]]) -> datetime:
    end = round_up_to_grid(meeting.end, ROUND_UP_MAX)
    blockers = [s for s, e in busy if e > meeting.end]
    blockers += [m.start for m in others if m is not meeting and m.end > meeting.end]
    return min([end] + [max(b, meeting.end) for b in blockers])


def parse_ical_comments(texts: Iterable[str]) -> set[str]:
    return {series_id(match) for text in texts for match in ICAL_COMMENT_RE.findall(text or "")}


def ical_comment(series: str) -> str:
    return f'"iCalUId": "{series}"'
