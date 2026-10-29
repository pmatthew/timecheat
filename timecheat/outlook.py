"""Outlook calendar access through Microsoft Graph, with a token pasted from Graph Explorer.

The token is a delegated access token of the signed-in user. It is held in memory
only: it carries every scope ever consented to in Graph Explorer, so it is treated
like a password and never written to disk or logged.

``iCalUId`` differs for every occurrence of a recurring meeting. For Exchange
items it is a "global object ID" whose bytes 17-20 hold the occurrence date;
zeroing them gives an ID shared by the whole series (the "clean" global object
ID). ``series_id`` does exactly that, and auto-matching compares series IDs.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import requests

from .jira_client import BUDAPEST

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_EXPLORER_URL = "https://developer.microsoft.com/en-us/graph/graph-explorer"
EXCHANGE_GOID_PREFIX = "040000008200E00074C5B7101A82E008"
SELECT = ",".join([
    "id", "subject", "start", "end", "isAllDay", "isCancelled", "showAs", "responseStatus",
    "sensitivity", "type", "iCalUId", "seriesMasterId",
])
PAGE_SIZE = 100
SKIPPED_SENSITIVITY = {"private", "personal", "confidential"}
SKIPPED_RESPONSES = {"declined": "declined", "notResponded": "not responded", "tentativelyAccepted": "tentative"}
SKIPPED_SHOW_AS = {"free": "shown as free", "tentative": "tentative"}
_HEX = re.compile(r"^[0-9A-F]+$")


class GraphError(RuntimeError):
    pass


class GraphAuthError(GraphError):
    """The token is missing, expired or invalid: ask the user for a new one."""


@dataclass(frozen=True)
class Meeting:
    id: str
    subject: str
    start: datetime
    end: datetime
    type: str = "singleInstance"
    ical_uid: str = ""

    @property
    def is_recurring(self) -> bool:
        return self.type in ("occurrence", "exception")

    @property
    def series_id(self) -> str:
        return series_id(self.ical_uid)

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(frozen=True)
class SkippedEvent:
    subject: str
    start: datetime | None
    reason: str


class CalendarSource(Protocol):
    """Implemented by ``GraphClient`` and ``demo.FakeGraph``."""

    def calendar_view(self, first: date, last: date) -> list[dict[str, Any]]: ...


def series_id(ical_uid: str) -> str:
    value = (ical_uid or "").strip().upper()
    if len(value) >= 40 and value.startswith(EXCHANGE_GOID_PREFIX) and _HEX.match(value):
        return value[:32] + "00000000" + value[40:]
    return value


def token_expiry(token: str) -> datetime | None:
    """The ``exp`` claim of a JWT as a local naive datetime, or None if it is not a JWT."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]))
    except (ValueError, KeyError, TypeError):
        return None


def clean_token(value: str) -> str:
    token = value.strip().strip('"').strip()
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def _parse_time(value: dict[str, Any]) -> datetime:
    # With the Prefer header Graph returns Budapest wall-clock times like 2026-10-27T09:00:00.0000000.
    moment = datetime.fromisoformat(value["dateTime"][:19])
    if value.get("timeZone", "").upper() == "UTC":
        moment = moment.replace(tzinfo=timezone.utc).astimezone(BUDAPEST).replace(tzinfo=None)
    return moment


def filter_meetings(events: list[dict[str, Any]]) -> tuple[list[Meeting], list[SkippedEvent]]:
    kept: list[Meeting] = []
    skipped: list[SkippedEvent] = []
    for event in events:
        subject = event.get("subject") or "(no subject)"
        try:
            start = _parse_time(event["start"])
            end = _parse_time(event["end"])
        except (KeyError, ValueError):
            skipped.append(SkippedEvent(subject, None, "unreadable start/end"))
            continue
        response = (event.get("responseStatus") or {}).get("response", "")
        show_as = event.get("showAs", "")
        sensitivity = event.get("sensitivity", "normal")
        if event.get("type") == "seriesMaster":
            continue
        if sensitivity in SKIPPED_SENSITIVITY:
            reason = sensitivity
        elif event.get("isCancelled"):
            reason = "cancelled"
        elif event.get("isAllDay"):
            reason = "all-day"
        elif response in SKIPPED_RESPONSES:
            reason = SKIPPED_RESPONSES[response]
        elif show_as in SKIPPED_SHOW_AS:
            reason = SKIPPED_SHOW_AS[show_as]
        elif end <= start:
            reason = "no duration"
        else:
            reason = ""
        if reason:
            # Private items keep their subject out of the UI as well.
            shown = "(private)" if sensitivity in SKIPPED_SENSITIVITY else subject
            skipped.append(SkippedEvent(shown, start, reason))
            continue
        kept.append(Meeting(
            id=event.get("id", ""),
            subject=subject,
            start=start,
            end=end,
            type=event.get("type", "singleInstance"),
            ical_uid=event.get("iCalUId", ""),
        ))
    kept.sort(key=lambda m: (m.start, m.end, m.id))
    return kept, skipped


class GraphClient:
    def __init__(self, token: str, timeout: float = 30) -> None:
        self.token = clean_token(token)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Prefer": 'outlook.timezone="Europe/Budapest"',
        })

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as error:
            raise GraphError(f"Cannot reach Microsoft Graph: {error}") from error
        if response.status_code == 401:
            raise GraphAuthError("The Graph access token is expired or invalid. Paste a fresh one.")
        if response.status_code == 403:
            raise GraphError(
                "Access denied by Microsoft Graph. In Graph Explorer open 'Modify permissions' and "
                "consent to Calendars.Read, then copy a fresh token."
            )
        if not response.ok:
            raise GraphError(f"Microsoft Graph error ({response.status_code}): {_graph_error(response)}")
        return response.json()

    def calendar_view(self, first: date, last: date) -> list[dict[str, Any]]:
        start = datetime.combine(first, datetime.min.time()).replace(tzinfo=BUDAPEST)
        end = datetime.combine(last + timedelta(days=1), datetime.min.time()).replace(tzinfo=BUDAPEST)
        url: str | None = f"{GRAPH_ROOT}/me/calendarView"
        params: dict[str, Any] | None = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": SELECT,
            "$top": PAGE_SIZE,
            "$orderby": "start/dateTime",
        }
        events: list[dict[str, Any]] = []
        while url:
            page = self._get(url, params)
            events.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
            params = None  # the next link already carries every parameter
        return events


def _graph_error(response: requests.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return response.text[:300] or response.reason
    return f"{error.get('code', '')}: {error.get('message', '')}".strip(": ") or response.reason
