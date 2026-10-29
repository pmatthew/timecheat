import base64
import json
from datetime import date, datetime

import pytest
import requests

from timecheat.demo import RETRO_SERIES, STANDUP_SERIES, FakeGraph, FakeJira, goid
from timecheat.jira_client import JiraClient
from timecheat.meetings import fill_day, find_conflicts, ical_comment, parse_ical_comments
from timecheat.model import MeetingRef, Timesheet, Worklog, remote_uid
from timecheat.outlook import (
    GraphAuthError,
    GraphClient,
    GraphError,
    Meeting,
    clean_token,
    filter_meetings,
    series_id,
    token_expiry,
)
from timecheat.upload import build_plan

SAMPLE = "040000008200E00074C5B7101A82E00807EA0A1BC465B82C7551DD0100000000000000001000000022E2912D225B4D4396AEDC5AB3E53D0E"


def meeting(mid: str, start: str, end: str, subject: str = "m", series: str = "") -> Meeting:
    return Meeting(mid, subject, datetime.fromisoformat(start), datetime.fromisoformat(end),
                   "occurrence" if series else "singleInstance", series)


def wl(worklog_id: str, start: str, minutes: int) -> Worklog:
    return Worklog(remote_uid(worklog_id), "A-1", datetime.fromisoformat(start), minutes * 60, "", worklog_id)


# --------------------------------------------------------------------------- #
# iCalUId / token
# --------------------------------------------------------------------------- #


def test_series_id_zeroes_the_occurrence_date():
    assert series_id(SAMPLE) == SAMPLE[:32] + "00000000" + SAMPLE[40:]
    other_occurrence = SAMPLE[:32] + "07EA0A1C" + SAMPLE[40:]
    assert series_id(other_occurrence) == series_id(SAMPLE)
    assert series_id(SAMPLE.lower()) == series_id(SAMPLE)
    assert series_id(series_id(SAMPLE)) == series_id(SAMPLE)
    assert series_id(" something@google.com ") == "SOMETHING@GOOGLE.COM"


def test_comment_parsing_accepts_occurrence_and_series_ids():
    texts = [
        f'Recurring: "iCalUId": "{SAMPLE}"',
        "iCalUId=" + goid(STANDUP_SERIES, None).lower(),
        "unrelated comment 040000008200E00074C5B7101A82E008",
    ]
    assert parse_ical_comments(texts) == {series_id(SAMPLE), series_id(goid(STANDUP_SERIES, None))}
    assert parse_ical_comments([ical_comment(series_id(SAMPLE))]) == {series_id(SAMPLE)}


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def test_token_helpers():
    assert token_expiry(_jwt({"exp": 1_800_000_000})) == datetime.fromtimestamp(1_800_000_000)
    assert token_expiry("not-a-jwt") is None
    assert token_expiry("a.b.c") is None
    assert clean_token('  "Bearer abc.def.ghi" ') == "abc.def.ghi"


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #


def test_filter_meetings_skips_what_was_agreed():
    events = FakeGraph("t", latency=0).calendar_view(date(2026, 10, 26), date(2026, 11, 1))
    kept, skipped = filter_meetings(events)
    reasons = {s.reason for s in skipped}
    assert reasons == {"all-day", "cancelled", "tentative", "private", "declined"}
    assert all(s.subject == "(private)" for s in skipped if s.reason == "private")
    assert all(m.start.date().weekday() < 5 for m in kept)
    standups = [m for m in kept if m.subject == "Daily stand-up"]
    assert len(standups) == 5 and len({m.ical_uid for m in standups}) == 5
    assert len({m.series_id for m in standups}) == 1 and all(m.is_recurring for m in standups)


def test_filter_handles_utc_times_and_other_skips():
    base = {"subject": "x", "isAllDay": False, "isCancelled": False, "showAs": "busy",
            "responseStatus": {"response": "organizer"}, "sensitivity": "normal", "type": "singleInstance"}
    events = [
        dict(base, id="1", start={"dateTime": "2026-07-01T07:00:00.0000000", "timeZone": "UTC"},
             end={"dateTime": "2026-07-01T08:00:00.0000000", "timeZone": "UTC"}),
        dict(base, id="2", showAs="free", start={"dateTime": "2026-07-01T09:00:00", "timeZone": "Europe/Budapest"},
             end={"dateTime": "2026-07-01T10:00:00", "timeZone": "Europe/Budapest"}),
        dict(base, id="3", sensitivity="confidential", start={"dateTime": "2026-07-01T09:00:00"},
             end={"dateTime": "2026-07-01T10:00:00"}),
        dict(base, id="4", responseStatus={"response": "notResponded"}, start={"dateTime": "2026-07-01T09:00:00"},
             end={"dateTime": "2026-07-01T10:00:00"}),
        dict(base, id="5", type="seriesMaster", start={"dateTime": "2026-07-01T09:00:00"},
             end={"dateTime": "2026-07-01T10:00:00"}),
    ]
    kept, skipped = filter_meetings(events)
    assert [(m.id, m.start.hour) for m in kept] == [("1", 9)]  # 07:00 UTC = 09:00 CEST
    assert [s.reason for s in skipped] == ["shown as free", "confidential", "not responded"]


# --------------------------------------------------------------------------- #
# fill / conflicts
# --------------------------------------------------------------------------- #


def test_find_conflicts_groups_chained_overlaps():
    a = meeting("a", "2026-10-28T10:00", "2026-10-28T11:30")
    b = meeting("b", "2026-10-28T11:00", "2026-10-28T12:00")
    c = meeting("c", "2026-10-28T11:45", "2026-10-28T12:30")
    d = meeting("d", "2026-10-28T13:00", "2026-10-28T14:00")
    e = meeting("e", "2026-10-28T14:00", "2026-10-28T15:00")  # touching is not overlapping
    assert [[m.id for m in g] for g in find_conflicts([e, d, c, b, a])] == [["a", "b", "c"]]


def test_fill_day_clips_into_free_time():
    booked = [wl("1", "2026-10-28T08:00", 120), wl("2", "2026-10-28T10:30", 30)]
    meetings = [
        meeting("m1", "2026-10-28T09:30", "2026-10-28T11:30"),  # -> 10:00-10:30 and 11:00-11:30
        meeting("m2", "2026-10-28T08:30", "2026-10-28T09:00"),  # fully booked
        meeting("m3", "2026-10-28T11:28", "2026-10-28T11:33"),  # 3-minute leftover is dropped
        meeting("m4", "2026-10-28T13:00", "2026-10-28T14:00"),
        meeting("m5", "2026-10-28T15:00", "2026-10-28T16:00"),
    ]
    result = fill_day(booked, meetings, already_imported={"m5"})
    assert [(f.meeting.id, f"{f.start:%H:%M}-{f.end:%H:%M}", f.clipped) for f in result.fragments] == [
        ("m1", "10:00-10:30", True),
        ("m1", "11:00-11:30", True),
        ("m4", "13:00-14:00", False),
    ]
    assert {m.id: reason for m, reason in result.skipped} == {
        "m2": "no free time left in its slot",
        "m3": "no free time left in its slot",
        "m5": "already imported",
    }


def test_fill_day_respects_queued_layout():
    # 09:00-10:00 and 09:30-10:30 are displayed as 09:00-10:00, 10:00-11:00.
    booked = [wl("1", "2026-10-28T09:00", 60), wl("2", "2026-10-28T09:30", 60)]
    result = fill_day(booked, [meeting("m", "2026-10-28T10:30", "2026-10-28T11:30")], set())
    assert [(f"{f.start:%H:%M}", f"{f.end:%H:%M}") for f in result.fragments] == [("11:00", "11:30")]


def test_fill_day_rounds_early_ends_up_to_the_half_hour():
    booked = [wl("1", "2026-10-28T13:25", 30)]
    meetings = [
        meeting("a", "2026-10-28T09:00", "2026-10-28T09:25"),  # -> 09:30
        meeting("b", "2026-10-28T10:00", "2026-10-28T10:20"),  # 10 minutes early -> 10:30
        meeting("c", "2026-10-28T11:00", "2026-10-28T11:15"),  # 15 minutes early: kept
        meeting("d", "2026-10-28T12:00", "2026-10-28T12:25"),  # next meeting starts 12:27
        meeting("e", "2026-10-28T12:27", "2026-10-28T12:50"),  # -> 13:00
        meeting("f", "2026-10-28T13:00", "2026-10-28T13:20"),  # booked from 13:25
        meeting("g", "2026-10-28T14:00", "2026-10-28T15:00"),  # already on the grid
    ]
    result = fill_day(booked, meetings, set())
    assert [(f.meeting.id, f"{f.start:%H:%M}-{f.end:%H:%M}", f.rounded, f.clipped) for f in result.fragments] == [
        ("a", "09:00-09:30", True, False),
        ("b", "10:00-10:30", True, False),
        ("c", "11:00-11:15", False, False),
        ("d", "12:00-12:27", True, False),
        ("e", "12:27-13:00", True, False),
        ("f", "13:00-13:25", True, False),
        ("g", "14:00-15:00", False, False),
    ]


def test_fill_day_rounds_only_the_fragment_that_ends_the_meeting():
    booked = [wl("1", "2026-10-28T10:00", 30)]
    result = fill_day(booked, [meeting("m", "2026-10-28T09:30", "2026-10-28T10:50")], set())
    assert [(f"{f.start:%H:%M}-{f.end:%H:%M}", f.rounded, f.clipped) for f in result.fragments] == [
        ("09:30-10:00", False, True),
        ("10:30-11:00", True, True),
    ]


# --------------------------------------------------------------------------- #
# model / upload guard
# --------------------------------------------------------------------------- #


def test_unassigned_drafts_and_series_assignment():
    ts = Timesheet()
    ref = MeetingRef("e1", "Daily", "S1", recurring=True)
    a = ts.add_draft("", datetime(2026, 10, 27, 9, 30), 900, meeting=ref)
    b = ts.add_draft("", datetime(2026, 10, 28, 9, 30), 900, meeting=MeetingRef("e2", "Daily", "S1", True))
    c = ts.add_draft("", datetime(2026, 10, 28, 13), 3600, meeting=MeetingRef("e3", "Other", "S2", False))
    assert [w.uid for w in ts.unassigned()] == [a.uid, b.uid, c.uid]
    assert ts.imported_event_ids(date(2026, 10, 28)) == {"e2", "e3"}
    with pytest.raises(ValueError, match="3 entries"):
        build_plan(ts.changes())

    assigned = ts.assign_series("S1", "DEMO-51")
    assert {w.uid for w in assigned} == {a.uid, b.uid}
    assert all(ts.current[w.uid].meeting.match == "session" for w in assigned)
    assert all(ts.current[w.uid].meeting.auto_matched for w in assigned)
    d = ts.add_draft("", datetime(2026, 10, 29, 9, 30), 900, meeting=MeetingRef("e4", "Daily", "S1", True))
    [jira_matched] = ts.assign_series("S1", "DEMO-51", match="jira")
    assert jira_matched.uid == d.uid and jira_matched.meeting.match == "jira"
    assert ts.assign_series("S2", "X-1") == []  # not recurring
    ts.remove(c.uid)
    assert [op.worklog.issue_key for op in build_plan(ts.changes())] == ["DEMO-51"] * 3


def test_copy_day_skips_unassigned_meeting_drafts():
    ts = Timesheet([wl("1", "2026-10-27T08:00", 60)])
    ts.add_draft("", datetime(2026, 10, 27, 10), 900, meeting=MeetingRef("e", "x", "S", False))
    assert len(ts.copy_day(date(2026, 10, 27), date(2026, 10, 28))) == 1


# --------------------------------------------------------------------------- #
# Jira side (demo backend)
# --------------------------------------------------------------------------- #


def test_fake_jira_meeting_story_flow():
    jira = FakeJira(today=date(2026, 10, 28), latency=0)
    [story] = jira.find_stories("DEMO", "general meetings")
    assert story.key == "DEMO-50" and jira.find_stories("ACME", "General meetings") == []
    children = jira.children_with_comments(story.key)
    matches = {sid: c.issue.key for c in children for sid in parse_ical_comments(c.comments)}
    assert matches == {series_id(goid(STANDUP_SERIES, None)): "DEMO-51"}

    created = jira.create_subtask("DEMO", story.key, "Team retro", "from Outlook")
    jira.add_comment(created.key, ical_comment(series_id(goid(RETRO_SERIES, date(2026, 10, 30)))))
    children = jira.children_with_comments(story.key)
    matches = {sid: c.issue.key for c in children for sid in parse_ical_comments(c.comments)}
    assert matches[series_id(goid(RETRO_SERIES, None))] == created.key
    assert created.key in {i.key for i in jira.assigned_issues()}


def test_fake_graph_expired_token():
    with pytest.raises(GraphAuthError):
        FakeGraph("expired", latency=0).calendar_view(date(2026, 10, 1), date(2026, 10, 2))


def test_subtask_under_other_parent_and_type_detection():
    from timecheat.app import is_subtask_type
    from timecheat.jira_client import JiraError

    assert [is_subtask_type(t) for t in ("Sub-task", "Subtask", "Részfeladat", "Story", "Task")] == [
        True, True, True, False, False]
    jira = FakeJira(today=date(2026, 10, 28), latency=0)
    created = jira.create_subtask("DEMO", "DEMO-7", "Vendor call", "desc")
    assert created.key.startswith("DEMO-") and created.issue_type == "Sub-task"
    with pytest.raises(JiraError, match="cannot be its parent"):
        jira.create_subtask("DEMO", "DEMO-51", "x", "desc")


# --------------------------------------------------------------------------- #
# HTTP clients against stubbed sessions
# --------------------------------------------------------------------------- #


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload
        self.ok = status < 400
        self.content = b"x" if payload is not None else b""
        self.headers, self.reason, self.text = {}, "r", ""

    def json(self):
        return self._payload


def test_graph_client_pages_and_errors(monkeypatch):
    calls = []
    pages = {
        "https://graph.microsoft.com/v1.0/me/calendarView": {"value": [{"id": "1"}], "@odata.nextLink": "NEXT"},
        "NEXT": {"value": [{"id": "2"}]},
    }

    def get(self, url, params=None, timeout=None):
        calls.append((url, params, dict(self.headers)))
        return Resp(200, pages[url])

    monkeypatch.setattr(requests.Session, "get", get)
    client = GraphClient("Bearer tok")
    assert [e["id"] for e in client.calendar_view(date(2026, 10, 1), date(2026, 10, 31))] == ["1", "2"]
    url, params, headers = calls[0]
    assert params["startDateTime"] == "2026-10-01T00:00:00+02:00"
    assert params["endDateTime"] == "2026-11-01T00:00:00+01:00"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Prefer"] == 'outlook.timezone="Europe/Budapest"'
    assert calls[1][1] is None

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp(401, {"error": {"code": "x"}}))
    with pytest.raises(GraphAuthError):
        client.calendar_view(date(2026, 10, 1), date(2026, 10, 31))
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp(403, {"error": {"code": "x"}}))
    with pytest.raises(GraphError, match="Calendars.Read"):
        client.calendar_view(date(2026, 10, 1), date(2026, 10, 31))


def test_jira_client_meeting_endpoints(monkeypatch):
    log = []
    story = {"key": "ACME-9", "fields": {"summary": "General meetings", "issuetype": {"name": "Story"},
                                         "status": {"name": "Open"}, "project": {"key": "ACME"}}}
    child = {"key": "ACME-10", "fields": {"summary": "Daily", "issuetype": {"name": "Sub-task"}, "updated": "u",
                                          "status": {"name": "Open"}, "project": {"key": "ACME"},
                                          "comment": {"total": 2, "comments": [{"body": "first"}]}}}

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("/rest/api/2/", 1)[1]
        log.append((method, path, kwargs))
        if path == "myself":
            return Resp(200, {"name": "me", "key": "me", "displayName": "Me"})
        if path == "search":
            jql = kwargs["json"]["jql"]
            return Resp(200, {"total": 1, "issues": [child if jql.startswith("parent") else story]})
        if path == "issue/ACME-10/comment" and method == "GET":
            return Resp(200, {"total": 2, "comments": [{"body": "first"}, {"body": "second"}]})
        if path == "project/ACME":
            return Resp(200, {"issueTypes": [{"id": "1", "name": "Story"}, {"id": "5", "name": "Sub-task",
                                                                             "subtask": True}]})
        if path == "issue" and "assignee" in kwargs["json"]["fields"]:
            return Resp(400, {"errors": {"assignee": "Field 'assignee' cannot be set."}})
        if path == "issue":
            return Resp(201, {"key": "ACME-11"})
        if path == "issue/ACME-11/comment":
            return Resp(201, {"id": "1"})
        return Resp(404, {"errorMessages": ["nope"]})

    monkeypatch.setattr(requests.Session, "request", request)
    client = JiraClient("https://jira.example", username="me", password="pw")
    assert [i.key for i in client.find_stories("ACME", "General meetings")] == ["ACME-9"]
    [c] = client.children_with_comments("ACME-9")
    assert c.comments == ["first", "second"]
    created = client.create_subtask("ACME", "ACME-9", "Daily", "desc")
    assert (created.key, created.issue_type) == ("ACME-11", "Sub-task")
    creates = [kw["json"]["fields"] for m, p, kw in log if p == "issue"]
    assert creates[0]["assignee"] == {"name": "me"} and "assignee" not in creates[1]
    assert creates[1]["parent"] == {"key": "ACME-9"} and creates[1]["issuetype"] == {"id": "5"}
    client.add_comment("ACME-11", "hello")
    assert log[-1][2]["json"] == {"body": "hello"}
