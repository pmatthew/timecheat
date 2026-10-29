import json
import os
from datetime import date

import pytest

from timecheat import settings as cfg
from timecheat.workdays import WorkCalendar


@pytest.fixture
def paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TIMECHEAT_HOME", str(home))
    monkeypatch.delenv("TIMECHEAT_SETTINGS", raising=False)
    monkeypatch.setattr(cfg, "APP_DIR", tmp_path / "app")
    yield tmp_path
    cfg._settings_path = None
    cfg._demo = False


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_settings_file_lives_in_the_home_dir(paths, monkeypatch):
    cfg.configure(demo=False)
    assert cfg.Settings.path() == paths / "home" / "settings.json"
    cfg.configure(demo=True)
    assert cfg.Settings.path() == paths / "home" / "settings.demo.json"
    monkeypatch.setenv("TIMECHEAT_SETTINGS", str(paths / "x.json"))
    cfg.configure(demo=False)
    assert cfg.Settings.path() == paths / "x.json"


def test_defaults_round_trip_and_target(paths):
    cfg.configure(demo=False)
    settings = cfg.Settings.load()
    assert (settings.work_start, settings.work_end, settings.target_seconds) == ("09:00", "17:00", 8 * 3600)
    settings.default_project, settings.work_start, settings.work_end = "ACME", "08:30", "14:30"
    settings.save()
    loaded = cfg.Settings.load()
    assert loaded.default_project == "ACME" and loaded.target_seconds == 6 * 3600
    assert json.loads(cfg.Settings.path().read_text()) == {
        "default_project": "ACME", "work_start": "08:30", "work_end": "14:30",
    }


def test_start_project_prefers_the_default(paths):
    cfg.configure(demo=False)
    settings = cfg.Settings(last_project="DEMO")
    assert settings.start_project == "DEMO"
    settings.default_project = "ACME"
    assert settings.start_project == "ACME"


def test_settings_next_to_the_script_are_migrated_but_not_into_demo(paths):
    write(paths / "app" / "timecheat_settings.json", {"username": "jdoe", "last_project": "ACME", "unknown": 1})
    cfg.configure(demo=False)
    settings = cfg.Settings.load()
    assert (settings.username, settings.last_project) == ("jdoe", "ACME")
    settings.save()
    assert json.loads((paths / "home" / "settings.json").read_text())["username"] == "jdoe"
    cfg.configure(demo=True)
    assert cfg.Settings.load().username == ""


def test_newer_of_home_and_script_settings_wins(paths):
    home_file = paths / "home" / "settings.json"
    legacy = paths / "app" / "timecheat_settings.json"
    write(home_file, {"username": "old"})
    write(legacy, {"username": "new", "default_project": "ACME"})
    os.utime(home_file, (1_000, 1_000))
    cfg.configure(demo=False)
    assert cfg.Settings.load().default_project == "ACME"
    cfg.Settings.load().save()
    write(legacy, {"username": "stale"})
    os.utime(legacy, (2_000, 2_000))
    assert cfg.Settings.load().username == "new"


def test_team_defaults_sit_between_built_in_and_user_settings(paths):
    write(paths / "app" / "timecheat_defaults.json",
          {"server": "https://jira.example.com/", "meeting_story": "General meetings", "work_end": "16:00",
           "bogus": "x", "username": 5})
    cfg.configure(demo=False)
    settings = cfg.Settings.load()
    assert (settings.server, settings.meeting_story, settings.work_end) == (
        "https://jira.example.com/", "General meetings", "16:00")
    assert settings.username == ""
    settings.username, settings.meeting_story = "jdoe", "Meetings"
    settings.save()
    # Only the user's own choices are stored, so later team default changes still apply.
    assert json.loads(cfg.Settings.path().read_text()) == {"username": "jdoe", "meeting_story": "Meetings"}
    write(paths / "app" / "timecheat_defaults.json", {"server": "https://jira2.example.com/"})
    settings = cfg.Settings.load()
    assert (settings.server, settings.meeting_story, settings.work_end) == (
        "https://jira2.example.com/", "Meetings", "17:00")


def test_demo_ignores_team_defaults_and_has_a_meeting_story(paths):
    write(paths / "app" / "timecheat_defaults.json", {"server": "https://jira.example.com/"})
    cfg.configure(demo=True)
    settings = cfg.Settings.load()
    assert (settings.server, settings.meeting_story) == ("", cfg.DEMO_MEETING_STORY)


def test_invalid_work_period_in_file_falls_back_to_defaults(paths):
    cfg.configure(demo=False)
    path = cfg.Settings.path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"work_start": "17:00", "work_end": "09:00"}))
    assert (cfg.Settings.load().work_start, cfg.Settings.load().work_end) == ("09:00", "17:00")


def test_validate_work_period():
    assert cfg.validate_work_period("09:00", "17:00") is None
    assert cfg.validate_work_period("07:30", "24:00") is None
    assert "after" in cfg.validate_work_period("10:00", "10:00")
    assert "HH:MM" in cfg.validate_work_period("9", "17:00")
    assert cfg.validate_work_period("25:00", "26:00") is not None


def test_work_calendar_uses_the_configured_target():
    calendar = WorkCalendar(target_seconds=6 * 3600)
    assert calendar.target_seconds(date(2026, 10, 28)) == 6 * 3600
    assert calendar.target_seconds(date(2026, 10, 31)) == 0
