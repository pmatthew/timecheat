from datetime import date, datetime

from timecheat.model import (
    Timesheet,
    Worklog,
    day_status,
    format_duration,
    format_hours,
    layout_day,
    remote_uid,
)


def wl(worklog_id: str, key: str, start: str, minutes: int, remote: bool = True) -> Worklog:
    uid = remote_uid(worklog_id) if remote else f"draft-{worklog_id}"
    return Worklog(uid, key, datetime.fromisoformat(start), minutes * 60, "", worklog_id if remote else None)


def test_layout_queues_overlapping_entries():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    b = wl("2", "A-2", "2026-10-28T09:30", 60)
    c = wl("3", "A-3", "2026-10-28T12:00", 30)
    placed = layout_day([c, b, a])
    assert [(p.worklog.uid, p.start.strftime("%H:%M"), p.end.strftime("%H:%M")) for p in placed] == [
        (a.uid, "09:00", "10:00"),
        (b.uid, "10:00", "11:00"),
        (c.uid, "12:00", "12:30"),
    ]
    assert [p.shifted for p in placed] == [False, True, False]


def test_layout_identical_start_times_are_stacked():
    placed = layout_day([wl("1", "A-1", "2026-10-28T09:00", 60), wl("2", "A-1", "2026-10-28T09:00", 60)])
    assert placed[1].start == datetime(2026, 10, 28, 10, 0)


def test_changes_classify_create_update_move_delete():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    b = wl("2", "A-2", "2026-10-28T10:00", 60)
    c = wl("3", "A-3", "2026-10-28T11:00", 60)
    ts = Timesheet([a, b, c])
    assert ts.changes() == []
    ts.put(Worklog(a.uid, "A-1", a.start, 7200, "x", a.worklog_id))
    ts.put(Worklog(b.uid, "B-9", b.start, b.seconds, "", b.worklog_id))
    ts.remove(c.uid)
    draft = ts.add_draft("A-4", datetime(2026, 10, 28, 13), 1800)
    kinds = {ch.uid: ch.kind for ch in ts.changes()}
    assert kinds == {a.uid: "update", b.uid: "move", c.uid: "delete", draft.uid: "create"}
    assert ts.is_pending(draft.uid) and ts.is_pending(a.uid)


def test_revert_and_discard():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    ts = Timesheet([a])
    ts.remove(a.uid)
    draft = ts.add_draft("A-4", datetime(2026, 10, 28, 13), 1800)
    ts.revert(a.uid)
    ts.revert(draft.uid)
    assert ts.changes() == []
    ts.remove(a.uid)
    ts.discard_all()
    assert ts.current == ts.remote


def test_editing_back_to_original_is_not_pending():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    ts = Timesheet([a])
    ts.put(Worklog(a.uid, "A-1", a.start, 600, "", "1"))
    ts.put(a)
    assert not ts.is_pending(a.uid)


def test_merge_remote_keeps_local_edits_and_other_months():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    b = wl("2", "A-2", "2026-10-29T09:00", 60)
    gone = wl("3", "A-3", "2026-10-30T09:00", 60)
    september = wl("9", "A-9", "2026-09-30T09:00", 60)
    ts = Timesheet([a, b, gone, september])
    ts.remove(a.uid)  # pending delete
    ts.put(Worklog(gone.uid, "A-3", gone.start, 120, "", "3"))  # edited, then deleted in Jira
    draft = ts.add_draft("A-4", datetime(2026, 10, 28, 13), 1800)
    b_changed_remotely = Worklog(b.uid, "A-2", b.start, 7200, "", "2")
    new_remote = wl("4", "A-5", "2026-10-27T09:00", 30)

    ts.merge_remote(date(2026, 10, 1), date(2026, 10, 31), [a, b_changed_remotely, new_remote])

    assert a.uid in ts.remote and a.uid not in ts.current  # still a pending delete
    assert ts.current[b.uid] == b_changed_remotely  # unchanged locally -> refreshed
    assert ts.current[new_remote.uid] == new_remote
    assert draft.uid in ts.current
    assert september.uid in ts.current
    recreated = [w for w in ts.current.values() if w.issue_key == "A-3"]
    assert len(recreated) == 1 and recreated[0].worklog_id is None and recreated[0].seconds == 120


def test_copy_day_creates_drafts_at_same_time():
    ts = Timesheet([wl("1", "A-1", "2026-10-27T09:00", 60)])
    copied = ts.copy_day(date(2026, 10, 27), date(2026, 10, 28))
    assert len(copied) == 1
    assert copied[0].start == datetime(2026, 10, 28, 9) and copied[0].worklog_id is None


def test_confirm_create_for_move_leaves_pending_delete():
    a = wl("1", "A-1", "2026-10-28T09:00", 60)
    ts = Timesheet([a])
    ts.put(Worklog(a.uid, "B-1", a.start, a.seconds, "", "1"))
    created = wl("77", "B-1", "2026-10-28T09:00", 60)
    ts.confirm_create(a.uid, created)
    assert [c.kind for c in ts.changes()] == ["delete"]
    ts.confirm_delete(a.uid)
    assert ts.changes() == []


def test_day_status():
    today = date(2026, 10, 28)
    assert day_status(today, 8 * 3600, 8 * 3600, today).state == "ok"
    assert day_status(today, 6 * 3600, 8 * 3600, today).label == "6/8h"
    assert day_status(today, 6 * 3600, 8 * 3600, today).state == "under"
    assert day_status(today, 9 * 3600, 8 * 3600, today).state == "over"
    assert day_status(today, 0, 0, today).state == "off"
    assert day_status(today, 3600, 0, today).state == "over"
    assert day_status(date(2026, 10, 30), 0, 8 * 3600, today).state == "future"
    assert day_status(date(2026, 10, 30), 3600, 8 * 3600, today).state == "under"


def test_formatting():
    assert format_hours(int(6.5 * 3600)) == "6.5"
    assert format_hours(8 * 3600) == "8"
    assert format_duration(5400) == "1h 30m"
    assert format_duration(3600) == "1h"
    assert format_duration(900) == "15m"
