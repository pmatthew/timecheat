"""Which days are working days in Hungary, and how many hours they should hold.

Public holidays, bridge days ("pihenőnap") and the Saturdays they are worked off
on all come from the ``holidays`` package. ``~/.timecheat/workdays.json`` can
override single days for the cases the package does not know yet, e.g.::

    {"workdays": ["2027-01-09"], "days_off": ["2027-01-02"]}
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import holidays

TARGET_HOURS = 8


class WorkCalendar:
    def __init__(self, overrides_file: Path | None = None, target_hours: int = TARGET_HOURS) -> None:
        self.target_seconds_per_day = target_hours * 3600
        self._holidays = holidays.country_holidays("HU")
        self._extra_workdays: set[date] = set()
        self._extra_days_off: set[date] = set()
        if overrides_file is not None and overrides_file.is_file():
            data = json.loads(overrides_file.read_text(encoding="utf-8"))
            self._extra_workdays = {date.fromisoformat(d) for d in data.get("workdays", [])}
            self._extra_days_off = {date.fromisoformat(d) for d in data.get("days_off", [])}

    def is_workday(self, day: date) -> bool:
        if day in self._extra_workdays:
            return True
        if day in self._extra_days_off:
            return False
        return bool(self._holidays.is_working_day(day))

    def holiday_name(self, day: date) -> str | None:
        if day in self._extra_days_off:
            return "Day off"
        if day in self._extra_workdays or day in self._holidays.weekend_workdays:
            return "Transferred working day" if day.weekday() >= 5 else None
        return self._holidays.get(day)

    def target_seconds(self, day: date) -> int:
        return self.target_seconds_per_day if self.is_workday(day) else 0
