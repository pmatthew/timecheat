"""NiceGUI front-end: login page and the month/day booking view.

TimeCheat is a single-user tool bound to localhost, so all state lives in one
module-level ``STATE``. Unsent edits therefore survive a browser refresh but
not an app restart.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from nicegui import run, ui
from nicegui.events import GenericEventArguments

from . import settings as cfg
from .cache import IssueCache
from .fullcalendar import FullCalendar
from .jira_client import JiraBackend, JiraClient, JiraError, Project
from .model import Issue, Placed, Timesheet, Worklog, day_status, format_duration, layout_day
from .upload import OperationResult, PlanRunner, build_plan, call_jira
from .workdays import WorkCalendar

RESOLVED_WITHIN_DAYS = 90
MONTHS_BACK = 12
MONTHS_AHEAD = 2
WORKDAY_START = "08:00"
WORKDAY_END = "18:00"
DEFAULT_MINUTES = 60
TIME_RE = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")

PALETTE = ["#9ec5fe", "#a3e4c1", "#fcd29f", "#d7b4f3", "#f8b4c8", "#9fe2e8", "#e6d79c", "#b8c4d6",
           "#c5e1a5", "#ffccbc"]
STATE_COLORS = {"ok": "positive", "under": "negative", "over": "warning", "off": "grey", "future": "grey"}
ACTION_ICONS = {"create": "add_circle", "update": "edit", "delete": "delete", "move": "swap_horiz"}

CSS = """
body { background: #f7f8fa; }
.tc-header { background: #1f2a44; color: white; }
.tc-header .q-field__native, .tc-header .q-field__label, .tc-header .q-select__dropdown-icon { color: white; }
.tc-month-pane { flex: 3 1 0; min-width: 0; height: 100%; }
.tc-day-pane { flex: 2 1 0; min-width: 380px; height: 100%; }
.tc-cal { width: 100%; flex: 1 1 auto; min-height: 0; background: white; border-radius: 8px; padding: 6px; }
.fc .tc-ok { background: #e6f4e8 !important; }
.fc .tc-under { background: #fbe9e9 !important; }
.fc .tc-over { background: #fff0d6 !important; }
.fc .tc-off { background: #eef0f3 !important; }
.fc .tc-selected { box-shadow: inset 0 0 0 3px #3b82f6; }
.fc .fc-daygrid-day { cursor: pointer; }
.fc .fc-daygrid-day-top { flex-direction: row; }
.fc .fc-daygrid-day-number { width: 100%; display: flex; gap: 4px; align-items: center; color: #1f2933; }
.tc-day-num { margin-left: auto; }
.tc-day-sum { font-weight: 700; font-size: .8rem; }
.tc-day-note { font-size: .65rem; color: #6b7280; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
               max-width: 60%; }
.tc-event { cursor: pointer; }
.tc-event .fc-event-main { color: #1f2933 !important; overflow: hidden; }
.tc-pending { opacity: .55; border-style: dashed !important; border-width: 2px !important; font-style: italic; }
.tc-shifted { border-left: 4px solid #6b7280 !important; }
.tc-ev-head { font-size: .78rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tc-ev-sum { font-size: .75rem; white-space: normal; line-height: 1.15; }
.tc-ev-note { font-size: .68rem; color: #374151; }
.tc-month-ev { font-size: .74rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 0 2px; }
.tc-day .fc-timegrid-slot { height: max(1.5em, calc((100vh - 210px) / 21)); }
.tc-day .fc-non-business { background: rgba(120, 120, 130, .08); }
"""


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@dataclass
class AppState:
    demo: bool = False
    settings: cfg.Settings = field(default_factory=cfg.Settings.load)
    work_calendar: WorkCalendar = field(default_factory=lambda: WorkCalendar(cfg.home_dir() / "workdays.json"))
    backend: JiraBackend | None = None
    cache: IssueCache | None = None
    projects: list[Project] = field(default_factory=list)
    project: str = ""
    project_info: str = ""
    issues: dict[str, Issue] = field(default_factory=dict)
    project_keys: list[str] = field(default_factory=list)
    assigned_keys: list[str] = field(default_factory=list)
    timesheet: Timesheet = field(default_factory=Timesheet)
    loaded_months: set[tuple[int, int]] = field(default_factory=set)
    month: tuple[int, int] = field(default_factory=lambda: (date.today().year, date.today().month))
    day: date = field(default_factory=date.today)

    def log_out(self) -> None:
        fresh = AppState(demo=self.demo, settings=self.settings, work_calendar=self.work_calendar)
        self.__dict__.update(fresh.__dict__)


STATE = AppState()


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_options(today: date) -> dict[str, str]:
    options: dict[str, str] = {}
    for offset in range(MONTHS_AHEAD, -MONTHS_BACK - 1, -1):
        index = today.year * 12 + today.month - 1 + offset
        year, month = divmod(index, 12)
        options[month_key(year, month + 1)] = f"{calendar.month_name[month + 1]} {year}"
    return options


def parse_hhmm(value: str | None) -> int | None:
    match = TIME_RE.match((value or "").strip())
    if not match:
        return None
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    return minutes if minutes <= 24 * 60 else None


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def local_iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def issue_color(key: str) -> str:
    return PALETTE[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(PALETTE)]


def notify_error(error: Exception) -> None:
    ui.notify(str(error), type="negative", multi_line=True, close_button="OK", timeout=0)


class Busy:
    """Modal hourglass, optionally with a progress bar fed from a worker thread."""

    def __init__(self, parent: ui.element, message: str) -> None:
        self.message = message
        self.done = 0
        self.total = 0
        with parent:
            self.dialog = ui.dialog().props("persistent")
            with self.dialog, ui.card().classes("items-center gap-3 p-6 min-w-[340px]"):
                ui.spinner("hourglass", size="3em", color="primary")
                self.label = ui.label(message)
                self.bar = ui.linear_progress(value=0, show_value=False).props("instant-feedback").classes("w-64")
                self.bar.visible = False
            self.timer = ui.timer(0.2, self._tick)

    def progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total

    def _tick(self) -> None:
        if self.total:
            self.bar.visible = True
            self.bar.value = self.done / self.total
            self.label.text = f"{self.message} ({self.done}/{self.total})"

    async def __aenter__(self) -> Busy:
        self.dialog.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.timer.cancel()
        self.dialog.close()
        self.dialog.delete()


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


@ui.page("/login")
def login_page() -> None:
    ui.add_css(CSS)
    settings = STATE.settings
    with ui.card().classes("absolute-center w-[440px] gap-3 p-6") as card:
        with ui.row().classes("items-center gap-2"):
            ui.icon("hourglass_top", size="2em", color="primary")
            ui.label("TimeCheat").classes("text-2xl font-bold")
        ui.label("Log in to Jira").classes("text-grey-7")
        if STATE.demo:
            ui.label("Demo mode: no Jira server is contacted, any credentials work.").classes(
                "text-orange-9 text-sm"
            )
        server = ui.input("Jira server", value=settings.server).classes("w-full")
        mode = ui.toggle({"password": "Username + password", "token": "Personal access token"},
                         value=settings.auth_mode).classes("w-full")
        with ui.column().classes("w-full gap-1").bind_visibility_from(mode, "value", value="password"):
            username = ui.input("Username", value=settings.username).classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True).classes("w-full")
        with ui.column().classes("w-full gap-1").bind_visibility_from(mode, "value", value="token"):
            token = ui.input("Personal access token", password=True, password_toggle_button=True).classes("w-full")
        ui.label("Leave the secret empty to use the one saved in the system keychain.").classes(
            "text-xs text-grey-7"
        )
        remember = ui.checkbox("Save the secret in the system keychain")
        error = ui.label().classes("text-negative text-sm")

        async def log_in() -> None:
            error.text = ""
            server_url = (server.value or "").strip() or cfg.DEFAULT_SERVER
            auth_mode = mode.value
            user = (username.value or "").strip()
            account = user if auth_mode == "password" else cfg.PAT_ACCOUNT
            typed = password.value if auth_mode == "password" else token.value
            if auth_mode == "password" and not user:
                error.text = "The username is required."
                return
            secret = typed or (None if STATE.demo else await run.io_bound(cfg.load_secret, server_url, account))
            if not secret and not STATE.demo:
                error.text = "Enter the password / token (nothing is saved in the keychain yet)."
                return

            def connect() -> JiraBackend:
                if STATE.demo:
                    from .demo import FakeJira

                    return FakeJira()
                if auth_mode == "token":
                    return JiraClient(server_url, token=secret)
                return JiraClient(server_url, username=user, password=secret)

            try:
                async with Busy(card, f"Connecting to {server_url} …"):
                    backend = await run.io_bound(connect)
            except Exception as exc:  # noqa: BLE001 - shown to the user
                error.text = str(exc)
                return

            settings.server, settings.auth_mode = server_url, auth_mode
            if auth_mode == "password":
                settings.username = user
            settings.save()
            if remember.value and typed and not STATE.demo:
                if not await run.io_bound(cfg.save_secret, server_url, account, typed):
                    ui.notify("Could not save the secret in the keychain.", type="warning")
            STATE.log_out()
            STATE.backend = backend
            STATE.cache = IssueCache(cfg.home_dir(), "demo" if STATE.demo else server_url)
            ui.navigate.to("/")

        for element in (username, password, token):
            element.on("keydown.enter", log_in)
        ui.button("Log in", icon="login", on_click=log_in).classes("w-full")


# --------------------------------------------------------------------------- #
# main view
# --------------------------------------------------------------------------- #


@ui.page("/")
async def main_page() -> None:
    if STATE.backend is None:
        ui.navigate.to("/login")
        return
    ui.add_css(CSS)
    page = MainPage(STATE)
    await ui.context.client.connected()
    await page.start()


class MainPage:
    def __init__(self, state: AppState) -> None:
        self.s = state
        self._quiet = False
        ui.query(".nicegui-content").classes("p-0 gap-0")
        self.root = ui.column().classes("w-full h-screen no-wrap gap-0 p-0")
        with self.root:
            self._build_header()
            with ui.row().classes("w-full no-wrap gap-3 p-3 items-stretch flex-1 min-h-0"):
                with ui.column().classes("tc-month-pane gap-2"):
                    self.month_cal = FullCalendar(
                        self._month_options(),
                        on_date_click=self._on_month_date_click,
                        on_event_click=self._on_month_event_click,
                    ).classes("tc-cal tc-month")
                with ui.column().classes("tc-day-pane gap-2"):
                    self._build_day_header()
                    self.day_cal = FullCalendar(
                        self._day_options(),
                        on_select=self._on_day_select,
                        on_event_click=self._on_day_event_click,
                        on_event_change=self._on_day_event_change,
                    ).classes("tc-cal tc-day")

    # -- layout --------------------------------------------------------------- #

    def _build_header(self) -> None:
        with ui.row().classes("tc-header w-full items-center gap-3 px-4 py-2"):
            ui.icon("hourglass_top", size="1.8em")
            ui.label("TimeCheat").classes("text-xl font-bold mr-4")
            self.month_select = ui.select(
                month_options(date.today()), value=month_key(*self.s.month), label="Month",
                on_change=self._on_month_select,
            ).props("dark dense options-dense outlined").classes("w-44")
            ui.button(icon="today", on_click=self._go_today).props("flat round dense color=white").tooltip("Today")
            self.project_select = ui.select(
                {}, label="Jira project", with_input=True, on_change=self._on_project_select,
            ).props("dark dense options-dense outlined").classes("w-72")
            self.cache_button = ui.button(icon="sync", on_click=self._refresh_project_cache).props(
                "flat round dense color=white"
            )
            with self.cache_button:
                self.cache_tooltip = ui.tooltip("Refresh the issue cache of the selected project")
            ui.button(icon="refresh", on_click=self._reload_month).props("flat round dense color=white").tooltip(
                "Reload this month's worklogs from Jira"
            )
            ui.space()
            self.pending_button = ui.button("Pending", icon="pending_actions", on_click=self._pending_dialog).props(
                "flat no-caps color=white"
            )
            with self.pending_button:
                self.pending_badge = ui.badge("0", color="orange").props("floating")
            self.upload_button = ui.button("Let's TimeCheat", icon="rocket_launch", on_click=self._upload_dialog).props(
                "color=positive unelevated no-caps no-wrap"
            )
            ui.label(self.s.backend.user.display_name if self.s.backend else "").classes("ml-2 text-sm whitespace-nowrap")
            ui.button(icon="logout", on_click=self._log_out).props("flat round dense color=white").tooltip("Log out")

    def _build_day_header(self) -> None:
        with ui.row().classes("w-full items-center no-wrap gap-1 bg-white rounded-lg px-2 py-1"):
            ui.button(icon="chevron_left", on_click=lambda: self._shift_day(-1)).props("flat round dense")
            with ui.column().classes("gap-0"):
                self.day_label = ui.label().classes("text-base font-bold")
                self.day_note = ui.label().classes("text-xs text-grey-7")
            ui.button(icon="chevron_right", on_click=lambda: self._shift_day(1)).props("flat round dense")
            ui.space()
            self.day_chip = ui.chip("", icon="schedule").props("dense square")
            ui.button(icon="content_copy", on_click=self._copy_day_dialog).props("flat round dense").tooltip(
                "Copy the entries of another day to this day"
            )

    def _month_options(self) -> dict[str, Any]:
        first, _ = month_bounds(*self.s.month)
        return {
            "initialView": "dayGridMonth",
            "initialDate": first.isoformat(),
            "headerToolbar": False,
            "firstDay": 1,
            "height": "100%",
            "fixedWeekCount": False,
            "showNonCurrentDates": False,
            "eventDisplay": "block",
            "displayEventTime": False,
            "dayMaxEvents": False,
            "editable": False,
            "selectable": False,
            "eventOrder": "start",
        }

    def _day_options(self) -> dict[str, Any]:
        time_format = {"hour": "2-digit", "minute": "2-digit", "hour12": False}
        return {
            "initialView": "timeGridDay",
            "initialDate": self.s.day.isoformat(),
            "headerToolbar": False,
            "dayHeaders": False,
            "allDaySlot": False,
            "height": "100%",
            "slotDuration": "00:30:00",
            "snapDuration": "00:30:00",
            "slotLabelInterval": "01:00:00",
            "slotLabelFormat": time_format,
            "eventTimeFormat": time_format,
            "displayEventTime": False,
            "scrollTime": f"{WORKDAY_START}:00",
            "slotMinTime": "00:00:00",
            "slotMaxTime": "24:00:00",
            "businessHours": {"daysOfWeek": [0, 1, 2, 3, 4, 5, 6], "startTime": WORKDAY_START,
                              "endTime": WORKDAY_END},
            "selectable": True,
            "selectMirror": True,
            "selectOverlap": False,
            "unselectAuto": False,
            "editable": True,
            "eventOverlap": False,
            "nowIndicator": True,
            "eventOrder": "start",
        }

    # -- start-up ------------------------------------------------------------- #

    async def start(self) -> None:
        s = self.s
        assert s.backend is not None
        if not s.projects:
            try:
                async with Busy(self.root, "Loading projects and your assigned issues …"):
                    s.projects = await run.io_bound(s.backend.projects)
                    assigned = await run.io_bound(s.backend.assigned_issues)
            except Exception as error:  # noqa: BLE001
                notify_error(error)
                assigned = []
            for issue in assigned:
                s.issues[issue.key] = issue
            s.assigned_keys = [i.key for i in assigned]
        self._quiet = True
        self.project_select.set_options({p.key: p.label for p in s.projects})
        project = s.project or s.settings.last_project
        if project in {p.key for p in s.projects}:
            self.project_select.value = project
        self._quiet = False
        if project and not s.project_keys and project in {p.key for p in s.projects}:
            await self._select_project(project)
        self._update_cache_tooltip()
        await self._show_month(*s.month, day=s.day)

    # -- projects & cache ---------------------------------------------------- #

    async def _on_project_select(self, e: Any) -> None:
        if self._quiet or not e.value or e.value == self.s.project:
            return
        await self._select_project(e.value)

    async def _select_project(self, project: str) -> None:
        s = self.s
        s.project = project
        s.settings.last_project = project
        s.settings.save()
        cached = s.cache.load(project) if s.cache else None
        if cached is None:
            await self._fetch_project(project)
            return
        self._use_project_issues(project, cached.issues, cached.fetched_at)

    async def _refresh_project_cache(self) -> None:
        if not self.s.project:
            ui.notify("Select a Jira project first.", type="warning")
            return
        await self._fetch_project(self.s.project)

    async def _fetch_project(self, project: str) -> None:
        s = self.s
        assert s.backend is not None and s.cache is not None
        try:
            async with Busy(self.root, f"Caching the issues of {project} …"):
                issues = await run.io_bound(s.backend.project_issues, project, RESOLVED_WITHIN_DAYS)
                cached = await run.io_bound(s.cache.save, project, issues)
        except Exception as error:  # noqa: BLE001
            notify_error(error)
            return
        self._use_project_issues(project, cached.issues, cached.fetched_at)
        ui.notify(f"{len(issues)} issues of {project} cached.", type="positive")

    def _use_project_issues(self, project: str, issues: list[Issue], fetched_at: datetime) -> None:
        s = self.s
        for issue in issues:
            s.issues[issue.key] = issue
        s.project_keys = [i.key for i in issues]
        s.project_info = f"{project}: {len(issues)} issues cached at {fetched_at:%Y-%m-%d %H:%M}"
        self._update_cache_tooltip()

    def _update_cache_tooltip(self) -> None:
        info = self.s.project_info or "No project cached yet"
        self.cache_tooltip.text = f"Refresh the issue cache ({info})"

    # -- months & days -------------------------------------------------------- #

    async def _on_month_select(self, e: Any) -> None:
        # Async handlers run after the programmatic change in _show_month, so compare with the state.
        if self._quiet or not e.value or e.value == month_key(*self.s.month):
            return
        year, month = (int(part) for part in e.value.split("-"))
        await self._show_month(year, month)

    async def _go_today(self) -> None:
        today = date.today()
        await self._show_month(today.year, today.month, day=today)

    async def _show_month(self, year: int, month: int, day: date | None = None) -> None:
        s = self.s
        s.month = (year, month)
        first, last = month_bounds(year, month)
        if day is None or not first <= day <= last:
            today = date.today()
            day = today if first <= today <= last else first
        s.day = day
        self._quiet = True
        key = month_key(year, month)
        if key not in self.month_select.options:
            self.month_select.set_options({**self.month_select.options, key: f"{calendar.month_name[month]} {year}"})
        self.month_select.value = key
        self._quiet = False
        self.month_cal.goto_date(first.isoformat())
        self.day_cal.goto_date(day.isoformat())
        if (year, month) not in s.loaded_months:
            await self._load_month(year, month)
        self.refresh()

    async def _load_month(self, year: int, month: int) -> bool:
        s = self.s
        assert s.backend is not None
        first, last = month_bounds(year, month)
        try:
            async with Busy(self.root, f"Loading your worklogs for {calendar.month_name[month]} {year} …") as busy:
                worklogs, issues = await run.io_bound(s.backend.my_worklogs, first, last, busy.progress)
        except Exception as error:  # noqa: BLE001
            notify_error(error)
            return False
        for issue in issues:
            s.issues[issue.key] = issue
        s.timesheet.merge_remote(first, last, worklogs)
        s.loaded_months.add((year, month))
        return True

    async def _reload_month(self) -> None:
        if await self._load_month(*self.s.month):
            ui.notify("Worklogs reloaded from Jira.", type="positive")
        self.refresh()

    async def _select_day(self, day: date) -> None:
        if (day.year, day.month) != self.s.month:
            await self._show_month(day.year, day.month, day=day)
            return
        self.s.day = day
        self.day_cal.goto_date(day.isoformat())
        self.refresh()

    async def _shift_day(self, delta: int) -> None:
        await self._select_day(self.s.day + timedelta(days=delta))

    # -- rendering ------------------------------------------------------------ #

    def issue(self, key: str) -> Issue:
        return self.s.issues.get(key) or Issue(key, "", "")

    def refresh(self) -> None:
        s = self.s
        first, last = month_bounds(*s.month)
        today = date.today()
        month_events: list[dict[str, Any]] = []
        day_info: dict[str, dict[str, str]] = {}
        day = first
        while day <= last:
            placed = layout_day(s.timesheet.on_day(day))
            logged = sum(p.worklog.seconds for p in placed)
            status = day_status(day, logged, s.work_calendar.target_seconds(day), today)
            classes = f"tc-{status.state}" + (" tc-selected" if day == s.day else "")
            day_info[day.isoformat()] = {
                "cls": classes,
                "label": status.label if status.target_seconds or logged else "",
                "note": html.escape(s.work_calendar.holiday_name(day) or ""),
            }
            month_events.extend(self._event(p, compact=True) for p in placed)
            day += timedelta(days=1)
        self.month_cal.set_data(month_events, day_info)

        placed = layout_day(s.timesheet.on_day(s.day))
        self.day_cal.set_data([self._event(p, compact=False) for p in placed])
        logged = sum(p.worklog.seconds for p in placed)
        status = day_status(s.day, logged, s.work_calendar.target_seconds(s.day), today)
        self.day_label.text = f"{s.day:%A, %d %B %Y}"
        self.day_note.text = s.work_calendar.holiday_name(s.day) or ("Working day" if status.target_seconds else "Weekend")
        self.day_chip.text = status.label
        self.day_chip.props(f"color={STATE_COLORS[status.state]} text-color=white")

        pending = len(s.timesheet.changes())
        self.pending_badge.text = str(pending)
        self.pending_badge.visible = pending > 0
        self.upload_button.enabled = pending > 0

    def _event(self, placed: Placed, compact: bool) -> dict[str, Any]:
        worklog = placed.worklog
        issue = self.issue(worklog.issue_key)
        pending = self.s.timesheet.is_pending(worklog.uid)
        color = issue_color(worklog.issue_key)
        duration = format_duration(worklog.seconds)
        key = html.escape(issue.key)
        tooltip = [
            issue.label,
            f"{worklog.start:%H:%M}–{worklog.end:%H:%M} ({duration})",
        ]
        if placed.shifted:
            tooltip.append(f"Shown at {placed.start:%H:%M} because it overlaps an earlier entry")
        if worklog.comment:
            tooltip.append(f"Comment: {worklog.comment}")
        if pending:
            tooltip.append("Not uploaded to Jira yet")
        if compact:
            content = f'<div class="tc-month-ev"><b>{key}</b> {duration}</div>'
        else:
            note = f'<div class="tc-ev-note">logged at {worklog.start:%H:%M}</div>' if placed.shifted else ""
            content = (
                f'<div class="tc-ev-head"><b>{key}</b> · {html.escape(issue.issue_type)} · {duration}</div>'
                f'<div class="tc-ev-sum">{html.escape(issue.summary)}</div>{note}'
            )
        classes = ["tc-event"] + (["tc-pending"] if pending else []) + (["tc-shifted"] if placed.shifted else [])
        return {
            "id": worklog.uid,
            "title": issue.key,
            "start": local_iso(placed.start),
            "end": local_iso(placed.end),
            "backgroundColor": color,
            "borderColor": "#475569" if pending else color,
            "classNames": classes,
            "extendedProps": {"html": content, "tooltip": "\n".join(tooltip)},
        }

    # -- calendar gestures ---------------------------------------------------- #

    async def _on_month_date_click(self, e: GenericEventArguments) -> None:
        await self._select_day(date.fromisoformat(e.args["date"]))

    async def _on_month_event_click(self, e: GenericEventArguments) -> None:
        await self._select_day(date.fromisoformat(e.args["date"]))
        await self._edit_existing(e.args["id"])

    async def _on_day_event_click(self, e: GenericEventArguments) -> None:
        await self._edit_existing(e.args["id"])

    async def _on_day_select(self, e: GenericEventArguments) -> None:
        start = datetime.fromisoformat(e.args["start"])
        end = datetime.fromisoformat(e.args["end"])
        try:
            await self._entry_dialog(None, start, int((end - start).total_seconds()))
        finally:
            self.day_cal.unselect()

    def _on_day_event_change(self, e: GenericEventArguments) -> None:
        worklog = self.s.timesheet.current.get(e.args["id"])
        if worklog is None:
            self.refresh()
            return
        shown = next((p for p in layout_day(self.s.timesheet.on_day(worklog.day)) if p.worklog.uid == worklog.uid),
                     None)
        new_start = datetime.fromisoformat(e.args["start"])
        new_end = datetime.fromisoformat(e.args["end"])
        moved = shown is None or new_start != shown.start
        self.s.timesheet.put(replace(
            worklog,
            start=new_start if moved else worklog.start,
            seconds=int((new_end - new_start).total_seconds()),
        ))
        self.refresh()

    async def _edit_existing(self, uid: str) -> None:
        worklog = self.s.timesheet.current.get(uid)
        if worklog is not None:
            await self._entry_dialog(worklog, worklog.start, worklog.seconds)

    # -- dialogs -------------------------------------------------------------- #

    def _issue_options(self, extra: str | None = None) -> dict[str, str]:
        s = self.s
        options: dict[str, str] = {}
        for key in s.assigned_keys:
            options[key] = f"★ {s.issues[key].label}"
        for key in s.project_keys:
            options.setdefault(key, s.issues[key].label)
        for key in sorted(s.issues):
            options.setdefault(key, s.issues[key].label)
        if extra and extra not in options:
            options[extra] = self.issue(extra).label
        return options

    async def _entry_dialog(self, worklog: Worklog | None, start: datetime, seconds: int) -> None:
        s = self.s
        timesheet = s.timesheet
        day = start.date()
        start_minutes = start.hour * 60 + start.minute
        minutes = max(1, round(seconds / 60))
        pending = worklog is not None and timesheet.is_pending(worklog.uid)

        with self.root, ui.dialog() as dialog, ui.card().classes("w-[680px] max-w-full gap-3"):
            with ui.row().classes("w-full items-center"):
                ui.label("Edit worklog" if worklog else "New worklog").classes("text-lg font-bold")
                ui.space()
                if pending:
                    ui.chip("not uploaded yet", icon="cloud_off", color="orange").props("dense outline")
                elif worklog is not None:
                    ui.chip("in Jira", icon="cloud_done", color="positive").props("dense outline")
            ui.label(f"{day:%A, %d %B %Y}").classes("text-grey-8")
            issue_select = ui.select(
                self._issue_options(worklog.issue_key if worklog else None),
                value=worklog.issue_key if worklog else None,
                label="Jira issue (★ = assigned to you) – type to search",
                with_input=True,
            ).props("options-dense clearable autofocus").classes("w-full")
            if not s.project_keys:
                ui.label(
                    "Only your assigned issues and this month's issues are searchable – select a Jira project "
                    "in the header to search all of its issues."
                ).classes("text-xs text-orange-9 -mt-2")
            with ui.row().classes("items-start gap-3"):
                start_input = ui.input("Start", value=hhmm(start_minutes)).props("mask='##:##'").classes("w-24")
                end_input = ui.input("End", value=hhmm(start_minutes + minutes)).props("mask='##:##'").classes("w-24")
                minutes_input = ui.number("Duration (min)", value=minutes, min=1, step=15, format="%d").classes("w-36")
            comment_input = ui.textarea("Comment (optional)", value=worklog.comment if worklog else "").props(
                "autogrow"
            ).classes("w-full")
            error = ui.label().classes("text-negative text-sm")

            syncing = False

            def sync(source: str) -> None:
                nonlocal syncing
                if syncing:
                    return
                syncing = True
                try:
                    begin = parse_hhmm(start_input.value)
                    if source in ("start", "minutes"):
                        length = int(minutes_input.value or 0)
                        if begin is not None and length > 0:
                            end_input.value = hhmm(min(begin + length, 24 * 60))
                    else:
                        finish = parse_hhmm(end_input.value)
                        if begin is not None and finish is not None and finish > begin:
                            minutes_input.value = finish - begin
                finally:
                    syncing = False

            start_input.on_value_change(lambda: sync("start"))
            minutes_input.on_value_change(lambda: sync("minutes"))
            end_input.on_value_change(lambda: sync("end"))

            def save() -> None:
                begin = parse_hhmm(start_input.value)
                length = int(minutes_input.value or 0)
                if not issue_select.value or issue_select.value not in issue_select.options:
                    error.text = "Pick an existing Jira issue from the list."
                elif begin is None or begin >= 24 * 60:
                    error.text = "The start time must look like 09:30."
                elif length < 1 or begin + length > 24 * 60:
                    error.text = "The duration must be at least 1 minute and end by 24:00."
                else:
                    dialog.submit(("save", issue_select.value, begin, length, comment_input.value or ""))

            with ui.row().classes("w-full items-center"):
                if worklog is not None:
                    ui.button("Delete", icon="delete", color="negative", on_click=lambda: dialog.submit(("delete",))
                              ).props("flat")
                    if pending and worklog.uid in timesheet.remote:
                        ui.button("Revert", icon="undo", on_click=lambda: dialog.submit(("revert",))).props("flat")
                ui.space()
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Save", icon="check", on_click=save)

        result = await dialog
        dialog.delete()
        if not result:
            return
        action = result[0]
        if action == "delete" and worklog is not None:
            timesheet.remove(worklog.uid)
        elif action == "revert" and worklog is not None:
            timesheet.revert(worklog.uid)
        elif action == "save":
            _, issue_key, begin, length, comment = result
            new_start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=begin)
            if worklog is None:
                timesheet.add_draft(issue_key, new_start, length * 60, comment)
            else:
                timesheet.put(replace(worklog, issue_key=issue_key, start=new_start, seconds=length * 60,
                                      comment=comment))
        self.refresh()

    async def _copy_day_dialog(self) -> None:
        s = self.s
        target = s.day
        source = target - timedelta(days=1)
        for back in range(1, 32):
            candidate = target - timedelta(days=back)
            if s.timesheet.on_day(candidate):
                source = candidate
                break

        with self.root, ui.dialog() as dialog, ui.card().classes("gap-3"):
            ui.label(f"Copy entries to {target:%A, %d %B}").classes("text-lg font-bold")
            ui.label("Copied entries become drafts at the same times of day.").classes("text-sm text-grey-7")
            picker = ui.date(value=source.isoformat()).props("first-day-of-week=1")
            preview = ui.label()

            def update_preview() -> None:
                chosen = date.fromisoformat(picker.value) if picker.value else None
                if chosen is None:
                    preview.text = "Pick a day."
                elif (chosen.year, chosen.month) not in s.loaded_months:
                    preview.text = "That month is not loaded yet; it is loaded when you copy."
                else:
                    entries = s.timesheet.on_day(chosen)
                    total = format_duration(sum(w.seconds for w in entries))
                    preview.text = f"{len(entries)} entries, {total}"

            picker.on_value_change(update_preview)
            update_preview()
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Copy", icon="content_copy", on_click=lambda: dialog.submit(picker.value))

        chosen = await dialog
        dialog.delete()
        if not chosen:
            return
        source = date.fromisoformat(chosen)
        if source == target:
            ui.notify("Pick a different day.", type="warning")
            return
        if (source.year, source.month) not in s.loaded_months and not await self._load_month(source.year, source.month):
            return
        copied = s.timesheet.copy_day(source, target)
        ui.notify(f"{len(copied)} entries copied as drafts.", type="positive" if copied else "warning")
        self.refresh()

    def _change_rows(self) -> list[dict[str, Any]]:
        rows = []
        for change in self.s.timesheet.changes():
            worklog = change.after or change.before
            assert worklog is not None
            issue = self.issue(worklog.issue_key)
            rows.append({
                "uid": change.uid,
                "kind": change.kind,
                "issue": issue.label,
                "date": f"{worklog.start:%Y-%m-%d %a}",
                "time": f"{worklog.start:%H:%M}–{worklog.end:%H:%M}",
                "duration": format_duration(worklog.seconds),
            })
        return rows

    async def _pending_dialog(self) -> None:
        s = self.s
        columns = [
            {"name": "kind", "label": "Change", "field": "kind", "align": "left"},
            {"name": "issue", "label": "Issue", "field": "issue", "align": "left"},
            {"name": "date", "label": "Date", "field": "date", "align": "left"},
            {"name": "time", "label": "Time", "field": "time", "align": "left"},
            {"name": "duration", "label": "Duration", "field": "duration", "align": "right"},
            {"name": "revert", "label": "", "field": "uid"},
        ]
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[960px] max-w-full"):
            ui.label("Pending changes (not uploaded yet)").classes("text-lg font-bold")
            table = ui.table(columns=columns, rows=self._change_rows(), row_key="uid").props("dense flat").classes(
                "w-full"
            )
            table.add_slot("body-cell-revert", """
                <q-td :props="props">
                  <q-btn flat dense round icon="undo" @click="$parent.$emit('revert', props.row.uid)">
                    <q-tooltip>Revert this change</q-tooltip>
                  </q-btn>
                </q-td>
            """)

            def revert(e: GenericEventArguments) -> None:
                s.timesheet.revert(e.args)
                table.rows = self._change_rows()
                table.update()
                self.refresh()

            def discard_all() -> None:
                s.timesheet.discard_all()
                table.rows = []
                table.update()
                self.refresh()

            table.on("revert", revert)
            with ui.row().classes("w-full"):
                ui.button("Discard all", icon="delete_sweep", color="negative", on_click=discard_all).props("flat")
                ui.space()
                ui.button("Close", on_click=dialog.close)
        await dialog
        dialog.delete()

    async def _upload_dialog(self) -> None:
        s = self.s
        assert s.backend is not None
        plan = build_plan(s.timesheet.changes())
        if not plan:
            ui.notify("Nothing to upload.")
            return
        rows = []
        for index, operation in enumerate(plan):
            worklog = operation.worklog
            rows.append({
                "n": index + 1,
                "action": operation.action,
                "issue": self.issue(worklog.issue_key).label,
                "date": f"{worklog.start:%Y-%m-%d %a}",
                "time": f"{worklog.start:%H:%M}–{worklog.end:%H:%M}",
                "duration": format_duration(worklog.seconds),
                "comment": worklog.comment,
                "note": operation.note,
                "status": "",
            })
        counts = {a: sum(1 for o in plan if o.action == a) for a in ("create", "update", "delete")}
        total = sum(o.worklog.seconds for o in plan if o.action != "delete")
        columns = [
            {"name": "n", "label": "#", "field": "n"},
            {"name": "action", "label": "Action", "field": "action", "align": "left"},
            {"name": "issue", "label": "Issue", "field": "issue", "align": "left",
             "style": "max-width: 360px; white-space: normal"},
            {"name": "date", "label": "Date", "field": "date", "align": "left"},
            {"name": "time", "label": "Time", "field": "time", "align": "left"},
            {"name": "duration", "label": "Duration", "field": "duration", "align": "right"},
            {"name": "comment", "label": "Comment", "field": "comment", "align": "left",
             "style": "max-width: 200px; white-space: normal"},
            {"name": "note", "label": "Note", "field": "note", "align": "left"},
            {"name": "status", "label": "Result", "field": "status", "align": "left",
             "style": "max-width: 260px; white-space: normal"},
        ]
        with self.root, ui.dialog().props("persistent") as dialog, ui.card().classes("w-[1200px] max-w-full gap-3"):
            ui.label("Let's TimeCheat – review before upload").classes("text-lg font-bold")
            ui.label(
                f"{len(plan)} Jira operations: {counts['create']} create, {counts['update']} update, "
                f"{counts['delete']} delete · {format_duration(total)} booked by creates/updates"
            ).classes("text-grey-8")
            table = ui.table(columns=columns, rows=rows, row_key="n").props("dense flat wrap-cells").classes(
                "w-full max-h-[60vh]"
            )
            progress = ui.linear_progress(value=0, show_value=False).props("instant-feedback").classes("w-full")
            progress.visible = False
            status = ui.label()
            with ui.row().classes("w-full justify-end"):
                cancel = ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                confirm = ui.button("Upload to Jira", icon="cloud_upload", color="positive")
                close = ui.button("Close", on_click=lambda: dialog.submit(True))
                close.visible = False

            async def upload() -> None:
                cancel.visible = False
                confirm.visible = False
                progress.visible = True
                runner = PlanRunner(s.backend, s.timesheet)
                results: list[OperationResult] = []
                for index, operation in enumerate(plan):
                    status.text = f"Uploading {index + 1}/{len(plan)} …"
                    reason = runner.skip_reason(operation)
                    if reason:
                        result = OperationResult(operation, False, reason)
                    else:
                        try:
                            returned = await run.io_bound(call_jira, s.backend, operation)
                            result = runner.finish(operation, returned, None)
                        except Exception as error:  # noqa: BLE001
                            result = runner.finish(operation, None, error)
                    results.append(result)
                    table.rows[index]["status"] = "✅ done" if result.ok else f"❌ {result.error}"
                    table.update()
                    progress.value = (index + 1) / len(plan)
                failed = sum(1 for r in results if not r.ok)
                status.text = (
                    "All operations succeeded. 🎉" if not failed
                    else f"{failed} of {len(plan)} operations failed; they stay pending so you can fix and retry."
                )
                status.classes(replace="text-positive" if not failed else "text-negative")
                close.visible = True
                self.refresh()

            confirm.on_click(upload)

        uploaded = await dialog
        dialog.delete()
        if uploaded:
            await self._reload_month()

    async def _log_out(self) -> None:
        pending = len(self.s.timesheet.changes())
        if pending:
            with self.root, ui.dialog() as dialog, ui.card():
                ui.label(f"You have {pending} pending changes that will be lost. Log out anyway?")
                with ui.row().classes("w-full justify-end"):
                    ui.button("Stay", on_click=lambda: dialog.submit(False)).props("flat")
                    ui.button("Log out", color="negative", on_click=lambda: dialog.submit(True))
            confirmed = await dialog
            dialog.delete()
            if not confirmed:
                return
        self.s.log_out()
        ui.navigate.to("/login")


def main(demo: bool = False, port: int = 8765, show: bool = True) -> None:
    STATE.demo = demo
    ui.run(
        host="127.0.0.1",
        port=port,
        title="TimeCheat",
        favicon="⏳",
        reload=False,
        show=show,
        show_welcome_message=True,
    )
