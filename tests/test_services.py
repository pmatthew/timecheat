from dataclasses import replace
from datetime import date, datetime

from timecheat.cache import IssueCache
from timecheat.demo import LOCKED_ISSUE, FakeJira
from timecheat.jira_client import format_started, parse_started
from timecheat.model import Issue, Timesheet
from timecheat.upload import PlanRunner, build_plan
from timecheat.workdays import WorkCalendar

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def test_started_round_trip_handles_dst():
    winter = datetime(2026, 11, 2, 9, 0)
    summer = datetime(2026, 7, 2, 9, 0)
    assert format_started(winter) == "2026-11-02T09:00:00.000+0100"
    assert format_started(summer) == "2026-07-02T09:00:00.000+0200"
    assert parse_started("2026-07-02T07:00:00.000+0000") == summer
    assert parse_started(format_started(winter)) == winter


def test_hungarian_workdays(tmp_path):
    cal = WorkCalendar()
    assert not cal.is_workday(date(2026, 10, 23))  # national holiday
    assert cal.is_workday(date(2026, 8, 8))  # Saturday worked off for 21 Aug
    assert not cal.is_workday(date(2026, 8, 21))  # bridge day
    assert not cal.is_workday(date(2026, 10, 31))  # plain Saturday
    assert cal.target_seconds(date(2026, 10, 28)) == 8 * 3600
    assert cal.holiday_name(date(2026, 8, 8)) == "Transferred working day"

    overrides = tmp_path / "workdays.json"
    overrides.write_text('{"workdays": ["2026-10-31"], "days_off": ["2026-10-28"]}')
    cal = WorkCalendar(overrides)
    assert cal.is_workday(date(2026, 10, 31)) and not cal.is_workday(date(2026, 10, 28))


def test_issue_cache_round_trip(tmp_path):
    cache = IssueCache(tmp_path, "https://jira.example:8443/")
    assert cache.load("ACME") is None
    issues = [Issue("ACME-1", "Ünnep ✓", "Story", "Open", "ACME")]
    cache.save("ACME", issues)
    loaded = cache.load("ACME")
    assert loaded is not None and loaded.issues == issues


def test_upload_plan_against_fake_jira():
    jira = FakeJira(today=date(2026, 10, 28), latency=0)
    worklogs, _ = jira.my_worklogs(*OCTOBER)
    ts = Timesheet(worklogs)
    first, second, third = sorted(worklogs, key=lambda w: w.start)[:3]

    ts.add_draft("DEMO-2", datetime(2026, 10, 30, 9), 3600, "new")
    ts.put(replace(first, seconds=1800, comment="shorter"))
    ts.put(replace(second, issue_key="ACME-3"))
    ts.remove(third.uid)
    ts.add_draft(LOCKED_ISSUE, datetime(2026, 10, 30, 11), 3600)

    plan = build_plan(ts.changes())
    assert sorted(o.action for o in plan) == ["create", "create", "create", "delete", "delete", "update"]
    runner = PlanRunner(jira, ts)
    results = [runner.run(o) for o in plan]

    failed = [r for r in results if not r.ok]
    assert len(failed) == 1 and LOCKED_ISSUE in failed[0].error
    assert [c.after.issue_key for c in ts.changes()] == [LOCKED_ISSUE]

    fresh, _ = jira.my_worklogs(*OCTOBER)
    ts.merge_remote(*OCTOBER, fresh)
    assert {w.uid for w in fresh} | {c.uid for c in ts.changes()} == set(ts.current)
    assert any(w.issue_key == "ACME-3" and w.start == second.start for w in fresh)
    assert third.worklog_id not in {w.worklog_id for w in fresh}


def test_failed_move_create_skips_delete():
    jira = FakeJira(today=date(2026, 10, 28), latency=0)
    worklogs, _ = jira.my_worklogs(*OCTOBER)
    ts = Timesheet(worklogs)
    target = worklogs[0]
    ts.put(replace(target, issue_key=LOCKED_ISSUE))
    runner = PlanRunner(jira, ts)
    results = [runner.run(o) for o in build_plan(ts.changes())]
    assert [r.ok for r in results] == [False, False]
    assert "skipped" in results[1].error
    fresh, _ = jira.my_worklogs(*OCTOBER)
    assert target.worklog_id in {w.worklog_id for w in fresh}
