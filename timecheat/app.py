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

from nicegui import app, run, ui
from nicegui.events import GenericEventArguments

from . import settings as cfg
from .cache import IssueCache
from .fullcalendar import FullCalendar
from .jira_client import JiraBackend, JiraClient, JiraError, Project
from .meetings import fill_day, find_conflicts, ical_comment, parse_ical_comments
from .model import (
    MATCH_JIRA,
    MATCH_SESSION,
    Issue,
    MeetingRef,
    Placed,
    Timesheet,
    Worklog,
    day_status,
    format_duration,
    format_hours,
    layout_day,
    snap_to_grid,
)
from .outlook import (
    GRAPH_EXPLORER_URL,
    CalendarSource,
    GraphAuthError,
    GraphClient,
    Meeting,
    SkippedEvent,
    clean_token,
    filter_meetings,
    token_expiry,
)
from .upload import OperationResult, PlanRunner, build_plan, call_jira
from .workdays import WorkCalendar

RESOLVED_WITHIN_DAYS = 90
MONTHS_BACK = 12
MONTHS_AHEAD = 2
VISIBLE_MARGIN_MINUTES = 60
DEFAULT_MINUTES = 60
TIME_RE = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")
TOKEN_WARNING = timedelta(minutes=5)
DEMO_TOKEN_LIFETIME = timedelta(minutes=60)
UNASSIGNED_COLOR = "#ffe8cc"

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
.tc-day .fc-timegrid-slot { height: max(1.5em, calc((100vh - 210px) / var(--tc-slots, 21))); }
.tc-day .fc-non-business { background: rgba(120, 120, 130, .08); }
.tc-unassigned { border: 2px dashed #ea580c !important; opacity: .9; font-style: italic; }
.tc-unassigned .fc-event-main { color: #9a3412 !important; }
.tc-meeting-note { font-size: .7rem; color: #4b5563; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tc-help li { margin-bottom: 4px; }
"""


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@dataclass
class AppState:
    demo: bool = False
    settings: cfg.Settings = field(default_factory=cfg.Settings)
    work_calendar: WorkCalendar = field(default_factory=WorkCalendar)
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
    # Outlook (Microsoft Graph) - the token lives in memory only.
    graph_token: str = ""
    graph_expires: datetime | None = None
    graph_warned: bool = False
    outlook_months: dict[tuple[int, int], tuple[list[Meeting], list[SkippedEvent]]] = field(default_factory=dict)
    # The meeting story (Settings) of the selected project and series ID -> issue key from its sub-tasks.
    meeting_context_project: str | None = None
    meeting_context_story: str = ""
    meeting_story: Issue | None = None
    story_children: set[str] = field(default_factory=set)
    # series ID -> issue key: from iCalUId comments in Jira, and chosen by hand in this session (memory only).
    series_matches: dict[str, str] = field(default_factory=dict)
    session_matches: dict[str, str] = field(default_factory=dict)

    def match_for(self, series_id: str) -> tuple[str, str]:
        if series_id in self.series_matches:
            return self.series_matches[series_id], MATCH_JIRA
        if series_id in self.session_matches:
            return self.session_matches[series_id], MATCH_SESSION
        return "", ""

    def graph(self) -> CalendarSource:
        if self.demo:
            from .demo import FakeGraph

            return FakeGraph(self.graph_token)
        return GraphClient(self.graph_token)

    def token_valid(self) -> bool:
        return bool(self.graph_token) and (self.graph_expires is None or self.graph_expires > datetime.now())

    def reset(self) -> None:
        """Forget everything of the previous login (kept: mode, settings, working calendar)."""
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


def is_subtask_type(issue_type: str) -> bool:
    """Jira cannot put a sub-task under another sub-task; type names vary by locale."""
    name = issue_type.casefold().replace("-", "").replace(" ", "")
    return "subtask" in name or name in {"részfeladat", "alfeladat"}


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
    page_root = ui.element("div").classes("w-full h-screen")
    with page_root, ui.card().classes("absolute-center w-[440px] gap-3 p-6") as card:
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("hourglass_top", size="2em", color="primary")
            ui.label("TimeCheat").classes("text-2xl font-bold")
            ui.space()
            ui.button(icon="power_settings_new", on_click=lambda: quit_app(page_root, 0)).props(
                "flat round dense color=grey-7"
            ).tooltip("Quit TimeCheat")
        ui.label("Log in to Jira").classes("text-grey-7")
        if STATE.demo:
            ui.label("Demo mode: no Jira server is contacted, any credentials work.").classes(
                "text-orange-9 text-sm"
            )
        server = ui.input("Jira server", value=settings.server,
                          placeholder="https://jira.example.com/").classes("w-full")
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
            if not server_url and not STATE.demo:
                error.text = "Enter the address of your Jira server, e.g. https://jira.example.com/."
                return
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
            STATE.reset()
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
                    visible_from, visible_to = self._visible_range()
                    self.day_cal = FullCalendar(
                        self._day_options(),
                        on_select=self._on_day_select,
                        on_event_click=self._on_day_event_click,
                        on_event_change=self._on_day_event_change,
                    ).classes("tc-cal tc-day").style(f"--tc-slots: {(visible_to - visible_from) // 30 + 1}")

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
            ui.button(icon="settings", on_click=self._settings_dialog).props("flat round dense color=white").tooltip(
                "Settings"
            )
            ui.button(icon="power_settings_new", on_click=self._quit).props(
                "flat round dense color=white"
            ).tooltip("Quit TimeCheat")

    def _build_day_header(self) -> None:
        with ui.row().classes("w-full items-center no-wrap gap-1 bg-white rounded-lg px-2 py-1"):
            ui.button(icon="chevron_left", on_click=lambda: self._shift_day(-1)).props("flat round dense")
            with ui.column().classes("gap-0"):
                self.day_label = ui.label().classes("text-base font-bold whitespace-nowrap")
                self.day_note = ui.label().classes("text-xs text-grey-7")
            ui.button(icon="chevron_right", on_click=lambda: self._shift_day(1)).props("flat round dense")
            ui.space()
            self.day_chip = ui.chip("", icon="schedule").props("dense square")
            with ui.button("Outlook", icon="event_available", on_click=self._get_outlook_events).props(
                "flat dense no-caps color=primary"
            ):
                ui.tooltip("Get Outlook events: fill the free time of this day with your meetings")
            self.token_chip = ui.chip("", icon="key", on_click=lambda: self._token_dialog(None)).props(
                "dense outline clickable"
            )
            self.token_chip.tooltip("Outlook (Microsoft Graph) token lifetime – click to paste a new token")
            self.token_chip.visible = False
            ui.button(icon="content_copy", on_click=self._copy_day_dialog).props("flat round dense").tooltip(
                "Copy the entries of another day to this day"
            )
        ui.timer(15, self._tick_token)

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

    def _visible_range(self) -> tuple[int, int]:
        """Minutes of the day shown without scrolling: the working period plus a margin on both sides."""
        settings = self.s.settings
        return (max(0, settings.work_start_minutes - VISIBLE_MARGIN_MINUTES),
                min(24 * 60, settings.work_end_minutes + VISIBLE_MARGIN_MINUTES))

    def _day_options(self) -> dict[str, Any]:
        time_format = {"hour": "2-digit", "minute": "2-digit", "hour12": False}
        settings = self.s.settings
        visible_from, _ = self._visible_range()
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
            "scrollTime": f"{hhmm(visible_from)}:00",
            "slotMinTime": "00:00:00",
            "slotMaxTime": "24:00:00",
            "businessHours": {"daysOfWeek": [0, 1, 2, 3, 4, 5, 6], "startTime": settings.work_start,
                              "endTime": settings.work_end},
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
            warning = getattr(s.backend, "projects_warning", None)
            if warning:
                ui.notify(warning, type="warning", multi_line=True, timeout=10000, close_button="OK")
            for issue in assigned:
                s.issues[issue.key] = issue
            s.assigned_keys = [i.key for i in assigned]
        self._quiet = True
        self.project_select.set_options({p.key: p.label for p in s.projects})
        project = s.project or s.settings.start_project
        if project in {p.key for p in s.projects}:
            self.project_select.value = project
        self._quiet = False
        if project and not s.project_keys and project in {p.key for p in s.projects}:
            await self._select_project(project)
        self._update_cache_tooltip()
        self._tick_token()
        await self._show_month(*s.month, day=s.day)

    # -- projects & cache ---------------------------------------------------- #

    async def _on_project_select(self, e: Any) -> None:
        if self._quiet or not e.value or e.value == self.s.project:
            return
        await self._select_project(e.value)
        if self.s.outlook_months:
            await self._ensure_meeting_context()

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
        self.day_label.text = f"{s.day:%a, %d %b %Y}"
        self.day_note.text = s.work_calendar.holiday_name(s.day) or ("Working day" if status.target_seconds else "Weekend")
        self.day_chip.text = status.label
        self.day_chip.props(f"color={STATE_COLORS[status.state]} text-color=white")

        pending = len(s.timesheet.changes())
        self.pending_badge.text = str(pending)
        self.pending_badge.visible = pending > 0
        self.upload_button.enabled = pending > 0

    def entry_label(self, worklog: Worklog) -> str:
        if worklog.is_assigned:
            return self.issue(worklog.issue_key).label
        subject = worklog.meeting.subject if worklog.meeting else ""
        return f"⚠ no issue yet · Outlook: {subject}"

    def _event(self, placed: Placed, compact: bool) -> dict[str, Any]:
        worklog = placed.worklog
        meeting = worklog.meeting
        issue = self.issue(worklog.issue_key)
        pending = self.s.timesheet.is_pending(worklog.uid)
        assigned = worklog.is_assigned
        color = issue_color(worklog.issue_key) if assigned else UNASSIGNED_COLOR
        duration = format_duration(worklog.seconds)
        key = html.escape(issue.key)
        tooltip = [
            self.entry_label(worklog),
            f"{worklog.start:%H:%M}–{worklog.end:%H:%M} ({duration})",
        ]
        if meeting is not None:
            tooltip.append(f"Outlook meeting: {meeting.subject}" + (" (recurring)" if meeting.recurring else ""))
            if meeting.match == MATCH_JIRA and assigned:
                tooltip.append("🔗 Issue matched via the iCalUId comment in Jira")
            elif meeting.match == MATCH_SESSION and assigned:
                tooltip.append("↺ Issue remembered from your choice in this session (not stored in Jira)")
            if not assigned:
                tooltip.append("Click to assign a Jira issue – required before upload")
        if placed.shifted:
            tooltip.append(f"Shown at {placed.start:%H:%M} because it overlaps an earlier entry")
        if worklog.comment:
            tooltip.append(f"Comment: {worklog.comment}")
        if pending:
            tooltip.append("Not uploaded to Jira yet")
        subject = html.escape(meeting.subject) if meeting else ""
        link = {MATCH_JIRA: " 🔗", MATCH_SESSION: " ↺"}.get(meeting.match, "") if meeting and assigned else ""
        if compact:
            if assigned:
                content = f'<div class="tc-month-ev"><b>{key}</b> {duration}{link}</div>'
            else:
                content = f'<div class="tc-month-ev">⚠ <b>{subject}</b> {duration}</div>'
        else:
            note = f'<div class="tc-ev-note">logged at {worklog.start:%H:%M}</div>' if placed.shifted else ""
            meeting_note = f'<div class="tc-meeting-note">📅 {subject}{link}</div>' if meeting is not None else ""
            if assigned:
                content = (
                    f'<div class="tc-ev-head"><b>{key}</b> · {html.escape(issue.issue_type)} · {duration}</div>'
                    f'{meeting_note}<div class="tc-ev-sum">{html.escape(issue.summary)}</div>{note}'
                )
            else:
                content = (
                    f'<div class="tc-ev-head">⚠ <b>{subject}</b> · {duration}</div>'
                    f'<div class="tc-ev-sum">Click to assign a Jira issue</div>{note}'
                )
        classes = ["tc-event"] + (["tc-pending"] if pending else []) + (["tc-shifted"] if placed.shifted else [])
        if not assigned:
            classes.append("tc-unassigned")
        return {
            "id": worklog.uid,
            "title": issue.key or (meeting.subject if meeting else ""),
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
        # FullCalendar moves by whole slots from the original time, so 10:38 would land on 11:08.
        if moved:
            new_start = snap_to_grid(new_start)
            seconds = worklog.seconds
        else:
            seconds = int((snap_to_grid(new_end) - new_start).total_seconds())
            if seconds <= 0:
                seconds = int((new_end - new_start).total_seconds())
        self.s.timesheet.put(replace(
            worklog,
            start=new_start if moved else worklog.start,
            seconds=seconds,
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
            meeting = worklog.meeting if worklog else None
            if meeting is not None:
                with ui.row().classes("w-full items-center gap-2 bg-orange-50 rounded p-2"):
                    ui.icon("event", color="orange-9")
                    ui.label(meeting.subject).classes("font-medium")
                    if meeting.recurring:
                        ui.chip("recurring", icon="repeat").props("dense outline color=grey-8")
                    ui.space()
                    matched, source = s.match_for(meeting.series_id)
                    if source == MATCH_JIRA:
                        ui.label(f"🔗 iCalUId comment in Jira: {matched}").classes("text-xs text-grey-8")
                    elif source == MATCH_SESSION:
                        ui.label(f"↺ remembered in this session: {matched} (not stored in Jira)").classes(
                            "text-xs text-grey-8"
                        )
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                issue_select = ui.select(
                    self._issue_options(worklog.issue_key if worklog else None),
                    value=(worklog.issue_key or None) if worklog else None,
                    label="Jira issue (★ = assigned to you) – type to search",
                    with_input=True,
                ).props("options-dense clearable autofocus").classes("flex-grow")
                if meeting is not None:
                    create_button = ui.button("Create sub-task", icon="add_task").props("flat dense no-caps")
                    story = s.meeting_story if s.meeting_context_project == s.project else None
                    if not self._parent_options():
                        create_button.disable()
                        create_button.tooltip("Select a Jira project in the header first.")
                    else:
                        default_parent = f"{story.key} {story.summary}" if story else "an issue you choose"
                        create_button.tooltip(f"Create a Jira sub-task for this meeting (default parent: "
                                              f"{default_parent})")

                        async def create_subtask() -> None:
                            created = await self._create_subtask_dialog(meeting)
                            if created is not None:
                                issue_select.set_options(self._issue_options(created.key), value=created.key)

                        create_button.on_click(create_subtask)
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
                meeting = worklog.meeting
                if meeting is not None:
                    matched, source = s.match_for(meeting.series_id)
                    meeting = replace(meeting, match=source if issue_key == matched else "")
                timesheet.put(replace(worklog, issue_key=issue_key, start=new_start, seconds=length * 60,
                                      comment=comment, meeting=meeting))
                if meeting is not None and meeting.recurring and issue_key != worklog.issue_key:
                    await self._remember_series_choice(meeting, issue_key, worklog.uid)
        self.refresh()

    async def _remember_series_choice(self, meeting: MeetingRef, issue_key: str, uid: str) -> None:
        """A recurring meeting was assigned by hand: reuse the choice, in Jira if the user agrees."""
        s = self.s
        already_noted = s.session_matches.get(meeting.series_id) == issue_key
        source = MATCH_SESSION
        if s.series_matches.get(meeting.series_id) == issue_key:
            source = MATCH_JIRA
        elif issue_key in s.story_children and await self._confirm_ical_comment(meeting, issue_key):
            if await self._write_ical_comment(issue_key, meeting):
                source = MATCH_JIRA
        if source == MATCH_SESSION:
            s.session_matches[meeting.series_id] = issue_key
        else:
            edited = s.timesheet.current.get(uid)
            if edited is not None and edited.meeting is not None:
                s.timesheet.put(replace(edited, meeting=replace(edited.meeting, match=MATCH_JIRA)))
        others = s.timesheet.assign_series(meeting.series_id, issue_key, match=source)
        if others:
            ui.notify(f'{issue_key} was also assigned to {len(others)} other occurrence(s) of "{meeting.subject}".',
                      type="info")
        if source == MATCH_SESSION and not already_noted:
            ui.notify(f'↺ "{meeting.subject}" → {issue_key} is remembered until TimeCheat is closed '
                      "(no iCalUId comment in Jira).", type="info")

    async def _confirm_ical_comment(self, meeting: MeetingRef, issue_key: str) -> bool:
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-full gap-3"):
            ui.label("Remember this recurring meeting in Jira?").classes("text-lg font-bold")
            ui.label(f'Add the comment "iCalUId": "…" to {issue_key}, so every future occurrence of '
                     f'"{meeting.subject}" is assigned to it automatically – also after restarting TimeCheat.'
                     ).classes("text-sm")
            ui.label("Without it, the choice is only remembered until TimeCheat is closed.").classes(
                "text-xs text-grey-7"
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Only for this session", on_click=lambda: dialog.submit(False)).props("flat no-caps")
                ui.button(f"Add comment to {issue_key}", icon="add_comment",
                          on_click=lambda: dialog.submit(True)).props("no-caps")
        confirmed = await dialog
        dialog.delete()
        return bool(confirmed)

    async def _write_ical_comment(self, issue_key: str, meeting: MeetingRef, persistent: bool = True) -> bool:
        """Add the iCalUId comment; ``persistent`` means auto-matching will find it (a story sub-task)."""
        s = self.s
        assert s.backend is not None
        try:
            async with Busy(self.root, f"Adding the iCalUId comment to {issue_key} …"):
                await run.io_bound(s.backend.add_comment, issue_key, ical_comment(meeting.series_id))
        except Exception as error:  # noqa: BLE001
            s.session_matches[meeting.series_id] = issue_key
            ui.notify(f"The iCalUId comment could not be added to {issue_key}: {error}. The choice is remembered "
                      "for this session only.", type="warning", multi_line=True, timeout=0, close_button="OK")
            return False
        if not persistent:
            s.session_matches[meeting.series_id] = issue_key
            ui.notify(f'iCalUId comment added to {issue_key}. Auto-matching only reads the sub-tasks of '
                      f'"{s.settings.meeting_story}", so this series is remembered for this session only (↺).',
                      type="info", multi_line=True)
            return True
        s.series_matches[meeting.series_id] = issue_key
        s.session_matches.pop(meeting.series_id, None)
        ui.notify(f"🔗 iCalUId comment added to {issue_key}.", type="positive")
        return True

    def _parent_options(self) -> dict[str, str]:
        """Issues a meeting sub-task can be created under: the meeting story first, then non-sub-task issues."""
        s = self.s
        options: dict[str, str] = {}
        story = s.meeting_story if s.meeting_context_project == s.project else None
        if story is not None:
            options[story.key] = f"★ {story.label}"
        candidates = [i for i in s.issues.values() if i.key not in options and not is_subtask_type(i.issue_type)]
        candidates.sort(key=lambda i: (i.project != s.project, i.issue_type != "Story", i.key))
        for issue in candidates:
            options[issue.key] = issue.label
        return options

    # -- Outlook ---------------------------------------------------------------- #

    def _tick_token(self) -> None:
        s = self.s
        chip = self.token_chip
        if not s.graph_token:
            chip.visible = False
            return
        chip.visible = True
        if s.graph_expires is None:
            chip.text, color = "token", "grey-8"
        else:
            left = s.graph_expires - datetime.now()
            minutes = int(left.total_seconds() // 60)
            if left.total_seconds() <= 0:
                chip.text, color = "expired", "negative"
            else:
                chip.text = f"{minutes} min"
                color = "warning" if left <= TOKEN_WARNING else "positive"
                if left <= TOKEN_WARNING and not s.graph_warned:
                    s.graph_warned = True
                    ui.notify(f"Your Outlook token expires in {max(minutes, 1)} min. Click the key chip in the "
                              "day view to paste a new one.", type="warning", timeout=10000)
        chip.props(f"color={color}")

    async def _token_dialog(self, reason: str | None) -> bool:
        s = self.s
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-full gap-3"):
            with ui.row().classes("w-full items-center"):
                ui.icon("key", size="1.5em", color="primary")
                ui.label("Outlook access token").classes("text-lg font-bold")
                ui.space()
                help_button = ui.button(icon="help_outline").props("flat round dense")
                help_button.tooltip("How do I get a token?")
            if reason:
                ui.label(reason).classes("text-orange-9 font-medium")
            ui.label(
                "TimeCheat reads your calendar with a Microsoft Graph token copied from Graph Explorer. "
                "It is kept in memory only and is valid for about 60–90 minutes."
            ).classes("text-sm text-grey-8")
            with ui.column().classes("tc-help w-full bg-blue-50 rounded p-3 text-sm gap-1") as help_box:
                ui.html(
                    "<ol style='padding-left:1.2em; list-style:decimal'>"
                    f"<li>Open <a href='{GRAPH_EXPLORER_URL}' target='_blank' style='color:#1d4ed8'>"
                    "Graph Explorer</a> and sign in with your work account (top right).</li>"
                    "<li>Run a calendar query once, e.g. <code>GET https://graph.microsoft.com/v1.0/me/calendarView"
                    "?startDateTime=2026-10-01T00:00:00&amp;endDateTime=2026-10-02T00:00:00</code>. If it asks, "
                    "consent to <b>Calendars.Read</b> under <i>Modify permissions</i>.</li>"
                    "<li>Open the <b>Access token</b> tab under the request box and click the copy icon.</li>"
                    "<li>Paste it below. When it expires, TimeCheat warns you and asks for a new one.</li>"
                    "</ol>"
                )
                if s.demo:
                    ui.label("Demo mode: any text works; type 'expired' to simulate an expired token.").classes(
                        "text-orange-9"
                    )
            help_box.visible = not s.graph_token
            help_button.on_click(lambda: help_box.set_visibility(not help_box.visible))
            token_input = ui.input("Access token", password=True, password_toggle_button=True).props(
                "autofocus"
            ).classes("w-full")
            error = ui.label().classes("text-negative text-sm")

            def use_token() -> None:
                token = clean_token(token_input.value or "")
                if not token:
                    error.text = "Paste the token first."
                    return
                expires = token_expiry(token)
                if expires is not None and expires <= datetime.now():
                    error.text = f"This token already expired at {expires:%H:%M}. Copy a fresh one."
                    return
                if expires is None and s.demo:
                    expires = datetime.now() + DEMO_TOKEN_LIFETIME
                dialog.submit((token, expires))

            token_input.on("keydown.enter", use_token)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Use token", icon="check", on_click=use_token)
        result = await dialog
        dialog.delete()
        if not result:
            return False
        s.graph_token, s.graph_expires = result
        s.graph_warned = False
        self._tick_token()
        return True

    async def _ensure_token(self) -> bool:
        s = self.s
        if s.token_valid():
            return True
        reason = "Your Outlook token has expired – paste a new one." if s.graph_token else None
        return await self._token_dialog(reason)

    async def _month_meetings(self, year: int, month: int, force: bool = False
                              ) -> tuple[list[Meeting], list[SkippedEvent]] | None:
        s = self.s
        if not force and (year, month) in s.outlook_months:
            return s.outlook_months[(year, month)]
        if not await self._ensure_token():
            return None
        first, last = month_bounds(year, month)
        for attempt in range(2):
            try:
                async with Busy(self.root, f"Loading your Outlook meetings for {calendar.month_name[month]} {year} …"):
                    events = await run.io_bound(s.graph().calendar_view, first, last)
            except GraphAuthError as error:
                s.graph_token, s.graph_expires = "", None
                self._tick_token()
                if attempt == 0 and await self._token_dialog(str(error)):
                    continue
                return None
            except Exception as error:  # noqa: BLE001
                notify_error(error)
                return None
            s.outlook_months[(year, month)] = filter_meetings(events)
            return s.outlook_months[(year, month)]
        return None

    async def _ensure_meeting_context(self) -> None:
        s = self.s
        assert s.backend is not None
        if not s.project:
            ui.notify("Select a Jira project to enable automatic issue matching and sub-task creation.", type="info")
            return
        story_name = s.settings.meeting_story
        if s.meeting_context_project == s.project and s.meeting_context_story == story_name:
            return
        project = s.project
        if not story_name:
            stories: list[Issue] = []
            ui.notify("No meeting story is set: automatic matching and sub-task creation are off. Set it in "
                      "⚙ Settings.", type="info", timeout=8000)
        else:
            try:
                async with Busy(self.root, f'Looking for "{story_name}" in {project} …'):
                    stories = await run.io_bound(s.backend.find_stories, project, story_name)
            except Exception as error:  # noqa: BLE001
                notify_error(error)
                return
        story = stories[0] if len(stories) == 1 else (await self._choose_story(stories) if stories else None)
        matches: dict[str, str] = {}
        if story is not None:
            s.issues[story.key] = story
            try:
                async with Busy(self.root, f"Reading the sub-tasks of {story.key} …"):
                    children = await run.io_bound(s.backend.children_with_comments, story.key)
            except Exception as error:  # noqa: BLE001
                notify_error(error)
                children = []
            # Several sub-tasks naming the same series: the most recently updated one wins.
            for child in sorted(children, key=lambda c: c.updated):
                s.issues[child.issue.key] = child.issue
                for series in parse_ical_comments(child.comments):
                    matches[series] = child.issue.key
        elif story_name:
            ui.notify(f'No "{story_name}" story in {project}: automatic matching and sub-task creation '
                      "are not available.", type="warning", timeout=8000)
        s.meeting_context_project, s.meeting_context_story = project, story_name
        s.meeting_story = story
        s.story_children = {c.issue.key for c in children} if story is not None else set()
        s.series_matches = matches

    async def _choose_story(self, stories: list[Issue]) -> Issue | None:
        with self.root, ui.dialog() as dialog, ui.card().classes("gap-3"):
            ui.label(f'Several "{self.s.settings.meeting_story}" issues').classes("text-lg font-bold")
            ui.label("Pick the one whose sub-tasks hold your meetings.").classes("text-sm text-grey-8")
            choice = ui.radio({i.key: i.label for i in stories}, value=stories[0].key)
            with ui.row().classes("w-full justify-end"):
                ui.button("Skip", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Use this", on_click=lambda: dialog.submit(choice.value))
        key = await dialog
        dialog.delete()
        return next((i for i in stories if i.key == key), None)

    async def _resolve_conflicts(self, groups: list[list[Meeting]]) -> set[str] | None:
        checks: list[tuple[Meeting, ui.checkbox]] = []
        with self.root, ui.dialog().props("persistent") as dialog, ui.card().classes("w-[600px] max-w-full gap-3"):
            ui.label("Overlapping Outlook meetings").classes("text-lg font-bold")
            ui.label("These meetings overlap each other. Tick the one(s) you attended; the rest are skipped. "
                     "If you keep several, they are booked one after the other in the free time.").classes(
                "text-sm text-grey-8"
            )
            for group in groups:
                with ui.column().classes("w-full gap-0 border rounded p-2"):
                    for index, meeting in enumerate(group):
                        label = f"{meeting.start:%H:%M}–{meeting.end:%H:%M}  {meeting.subject}"
                        checks.append((meeting, ui.checkbox(label, value=index == 0)))
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel import", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Continue", icon="arrow_forward",
                          on_click=lambda: dialog.submit({m.id for m, box in checks if box.value}))
        kept = await dialog
        dialog.delete()
        return kept

    async def _get_outlook_events(self, force: bool = False) -> None:
        s = self.s
        day = s.day
        loaded = await self._month_meetings(day.year, day.month, force=force)
        if loaded is None:
            return
        meetings, skipped = loaded
        await self._ensure_meeting_context()
        todays = [m for m in meetings if m.day == day]
        imported = s.timesheet.imported_event_ids(day)
        dropped: list[Meeting] = []
        groups = find_conflicts([m for m in todays if m.id not in imported])
        if groups:
            kept = await self._resolve_conflicts(groups)
            if kept is None:
                return
            dropped = [m for group in groups for m in group if m.id not in kept]
            todays = [m for m in todays if m not in dropped]
        result = fill_day(s.timesheet.on_day(day), todays, imported)
        created: list[Worklog] = []
        for fragment in result.fragments:
            meeting = fragment.meeting
            key, source = s.match_for(meeting.series_id)
            ref = MeetingRef(meeting.id, meeting.subject, meeting.series_id, meeting.is_recurring, source)
            created.append(s.timesheet.add_draft(key, fragment.start, fragment.seconds, "", meeting=ref))
        self.refresh()
        skipped_today = [(x.subject, x.start, x.reason) for x in skipped if x.start and x.start.date() == day]
        skipped_today += [(m.subject, m.start, reason) for m, reason in result.skipped]
        skipped_today += [(m.subject, m.start, "overlapping meeting not chosen") for m in dropped]
        clipped = sum(1 for f in result.fragments if f.clipped)
        rounded = sum(1 for f in result.fragments if f.rounded)
        await self._import_summary(day, created, clipped, rounded,
                                   sorted(skipped_today, key=lambda x: x[1] or datetime.min))

    async def _import_summary(self, day: date, created: list[Worklog], clipped: int, rounded: int,
                              skipped: list[tuple[str, datetime | None, str]]) -> None:
        s = self.s
        matched = sum(1 for w in created if w.is_assigned)
        from_jira = sum(1 for w in created if w.is_assigned and w.meeting and w.meeting.match == MATCH_JIRA)
        open_ = len(created) - matched
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-full gap-2"):
            ui.label(f"Outlook meetings · {day:%A, %d %B}").classes("text-lg font-bold")
            if created:
                ui.label(f"✅ {len(created)} entries added"
                         + (f" ({clipped} clipped to the free time)" if clipped else "")).classes("text-positive")
                if rounded:
                    ui.label(f"⏱ {rounded} ending a few minutes early rounded up to the half hour")
            else:
                ui.label("No meeting fitted into the free time of this day.").classes("text-grey-8")
            if from_jira:
                ui.label(f"🔗 {from_jira} matched via the iCalUId comment in Jira")
            if matched - from_jira:
                ui.label(f"↺ {matched - from_jira} assigned from your earlier choice in this session "
                         "(not stored in Jira)")
            if open_:
                ui.label(f"⚠ {open_} still need a Jira issue – click them in the day view. Upload is blocked "
                         "until they are assigned.").classes("text-orange-9")
            if s.project and s.meeting_story is None:
                missing = (f'No "{s.settings.meeting_story}" story in {s.project}' if s.settings.meeting_story
                           else "No meeting story is set in ⚙ Settings")
                ui.label(f"{missing}: automatic matching and sub-task creation are off.").classes(
                    "text-xs text-grey-7"
                )
            if skipped:
                ui.label("Skipped").classes("font-medium mt-2")
                rows = [{"n": i, "time": f"{start:%H:%M}" if start else "", "subject": subject, "reason": reason}
                        for i, (subject, start, reason) in enumerate(skipped)]
                ui.table(columns=[
                    {"name": "time", "label": "Time", "field": "time", "align": "left"},
                    {"name": "subject", "label": "Meeting", "field": "subject", "align": "left"},
                    {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
                ], rows=rows, row_key="n").props("dense flat").classes("w-full")
            with ui.row().classes("w-full items-center"):
                ui.button("Refetch from Outlook", icon="sync", on_click=lambda: dialog.submit("refetch")).props(
                    "flat no-caps"
                ).tooltip("Load the month from Outlook again (e.g. after adding a meeting) and retry this day")
                ui.space()
                ui.button("OK", on_click=lambda: dialog.submit(None))
        action = await dialog
        dialog.delete()
        if action == "refetch":
            await self._get_outlook_events(force=True)

    async def _create_subtask_dialog(self, meeting: MeetingRef) -> Issue | None:
        s = self.s
        assert s.backend is not None
        story = s.meeting_story if s.meeting_context_project == s.project else None
        parents = self._parent_options()
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[620px] max-w-full gap-3"):
            ui.label("Create Jira sub-task").classes("text-lg font-bold")
            parent = ui.select(parents, value=story.key if story else None, with_input=True,
                               label="Parent issue (story / task) – type to search").props(
                "options-dense"
            ).classes("w-full")
            summary = ui.input("Summary", value=meeting.subject).props("autofocus").classes("w-full")
            ui.label("The sub-task is created in Jira right away, in the parent's project, and assigned to you."
                     ).classes("text-xs text-grey-7")
            hint = ui.label().classes("text-xs text-grey-7")
            error = ui.label().classes("text-negative text-sm")

            def update_hint() -> None:
                if not meeting.recurring:
                    hint.text = ""
                elif story is not None and parent.value == story.key:
                    hint.text = ('Recurring meeting: the comment "iCalUId": "…" is added, so the next occurrences are '
                                 "matched automatically (🔗).")
                else:
                    hint.text = ('Recurring meeting: the comment "iCalUId": "…" is added, but auto-matching only reads '
                                 f'the sub-tasks of "{self.s.settings.meeting_story}" – under this parent the series is '
                                 "remembered for this session only (↺).")

            parent.on_value_change(update_hint)
            update_hint()

            def create() -> None:
                if not parent.value or parent.value not in parents:
                    error.text = "Pick an existing parent issue."
                elif not (summary.value or "").strip():
                    error.text = "The summary is required."
                else:
                    dialog.submit((parent.value, summary.value.strip()))

            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Create in Jira", icon="add_task", on_click=create)
        result = await dialog
        dialog.delete()
        if not result:
            return None
        parent_key, title = result
        parent_issue = self.issue(parent_key)
        project = parent_issue.project or parent_key.rsplit("-", 1)[0]
        under_story = story is not None and parent_key == story.key
        description = f'Created from the Outlook meeting "{meeting.subject}".'
        try:
            async with Busy(self.root, "Creating the Jira sub-task …"):
                issue = await run.io_bound(s.backend.create_subtask, project, parent_key, title, description)
        except Exception as error:  # noqa: BLE001
            notify_error(error)
            return None
        s.issues[issue.key] = issue
        if under_story:
            s.story_children.add(issue.key)
        if issue.key not in s.assigned_keys:
            s.assigned_keys.insert(0, issue.key)
        if meeting.recurring:
            await self._write_ical_comment(issue.key, meeting, persistent=under_story)
        ui.notify(f"{issue.key} created under {parent_key}.", type="positive")
        return issue

    async def _confirm_unassigned(self, unassigned: list[Worklog]) -> bool:
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-full gap-3"):
            ui.label("Some Outlook meetings have no Jira issue").classes("text-lg font-bold text-orange-9")
            ui.label("Every entry needs a Jira issue before it can be uploaded:").classes("text-sm")
            rows = [{"uid": w.uid, "date": f"{w.start:%Y-%m-%d %a}", "time": f"{w.start:%H:%M}–{w.end:%H:%M}",
                     "subject": w.meeting.subject if w.meeting else ""} for w in unassigned]
            ui.table(columns=[
                {"name": "date", "label": "Date", "field": "date", "align": "left"},
                {"name": "time", "label": "Time", "field": "time", "align": "left"},
                {"name": "subject", "label": "Meeting", "field": "subject", "align": "left"},
            ], rows=rows, row_key="uid").props("dense flat").classes("w-full max-h-[40vh]")
            with ui.row().classes("w-full justify-end"):
                ui.button("Go back", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button("Remove unassigned meetings and upload the rest", icon="delete_sweep", color="warning",
                          on_click=lambda: dialog.submit(True)).props("no-caps")
        confirmed = await dialog
        dialog.delete()
        if confirmed:
            for worklog in unassigned:
                self.s.timesheet.remove(worklog.uid)
            self.refresh()
        return bool(confirmed)

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
            rows.append({
                "uid": change.uid,
                "kind": change.kind,
                "issue": self.entry_label(worklog),
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
        unassigned = s.timesheet.unassigned()
        if unassigned and not await self._confirm_unassigned(unassigned):
            return
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

    async def _quit(self) -> None:
        await quit_app(self.root, len(self.s.timesheet.changes()))

    async def _settings_dialog(self) -> None:
        s = self.s
        settings = s.settings
        options = {"": "– none: use the last selected project –", **{p.key: p.label for p in s.projects}}
        if settings.default_project and settings.default_project not in options:
            options[settings.default_project] = settings.default_project
        with self.root, ui.dialog() as dialog, ui.card().classes("w-[540px] max-w-full gap-3"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("settings", size="1.5em", color="primary")
                ui.label("Settings").classes("text-lg font-bold")
            project = ui.select(options, value=settings.default_project, label="Default Jira project",
                                with_input=True).props("options-dense").classes("w-full")
            ui.label("Selected automatically when TimeCheat starts.").classes("text-xs text-grey-7 -mt-2")
            ui.label("Working period").classes("font-medium mt-2")
            with ui.row().classes("items-start gap-3"):
                start = ui.input("Starts", value=settings.work_start).props("mask='##:##'").classes("w-28")
                end = ui.input("Ends", value=settings.work_end).props("mask='##:##'").classes("w-28")
                target = ui.label().classes("self-center text-sm")
            ui.label("The daily target is the length of the working period (e.g. 09:00–17:00 = 8h). The day view "
                     "highlights it and opens on it.").classes("text-xs text-grey-7")
            story = ui.input("Meeting story", value=settings.meeting_story,
                             placeholder="e.g. General meetings").classes("w-full mt-2")
            ui.label("Summary of the story in the selected project whose sub-tasks hold your recurring meetings. "
                     "Outlook meetings are matched to these sub-tasks, and new ones are created under it. Leave "
                     "empty to turn this off.").classes("text-xs text-grey-7 -mt-2")
            error = ui.label().classes("text-negative text-sm")
            ui.label(f"Saved to {cfg.Settings.path()}").classes("text-xs text-grey-6 break-all")

            def update_target() -> None:
                problem = cfg.validate_work_period(start.value or "", end.value or "")
                if problem:
                    target.text = ""
                    error.text = problem
                    return
                error.text = ""
                minutes = (cfg.parse_hhmm(end.value) or 0) - (cfg.parse_hhmm(start.value) or 0)
                target.text = f"Daily target: {format_hours(minutes * 60)}h"

            start.on_value_change(update_target)
            end.on_value_change(update_target)
            update_target()

            def save() -> None:
                update_target()
                if not error.text:
                    dialog.submit((project.value or "", start.value.strip(), end.value.strip(),
                                   (story.value or "").strip()))

            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                ui.button("Save", icon="check", on_click=save)
        result = await dialog
        dialog.delete()
        if not result:
            return
        default_project, work_start, work_end, meeting_story = result
        period_changed = (work_start, work_end) != (settings.work_start, settings.work_end)
        settings.default_project, settings.work_start, settings.work_end = default_project, work_start, work_end
        settings.meeting_story = meeting_story
        if s.meeting_context_story != meeting_story:
            s.meeting_context_project = None
        try:
            settings.save()
        except OSError as error:
            notify_error(error)
            return
        s.work_calendar.target_seconds_per_day = settings.target_seconds
        ui.notify("Settings saved.", type="positive")
        if period_changed:
            # The day view's business hours and slot sizing are fixed at creation time; pending edits live in STATE.
            ui.navigate.reload()
        else:
            self.refresh()


async def quit_app(container: ui.element, pending: int) -> None:
    if pending:
        with container, ui.dialog() as dialog, ui.card().classes("gap-3"):
            ui.label("Quit TimeCheat?").classes("text-lg font-bold")
            ui.label(f"You have {pending} change(s) that are not uploaded to Jira yet. They will be lost.").classes(
                "text-orange-9"
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Stay", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button("Quit anyway", icon="power_settings_new", color="negative",
                          on_click=lambda: dialog.submit(True))
        confirmed = await dialog
        dialog.delete()
        if not confirmed:
            return
    container.clear()
    with container:
        # The server is about to go away on purpose: hide NiceGUI's "connection lost" popup.
        ui.add_css("#popup { display: none !important; }")
        with ui.card().classes("absolute-center items-center gap-2 p-8"):
            ui.icon("hourglass_bottom", size="3em", color="grey-7")
            ui.label("TimeCheat has stopped.").classes("text-xl font-bold")
            ui.label("You can close this tab.").classes("text-grey-7")
        # Give the browser a moment to render the goodbye before the server goes away.
        ui.timer(0.5, app.shutdown, once=True)


def main(demo: bool = False, port: int = 8765, show: bool = True) -> None:
    STATE.demo = demo
    cfg.configure(demo)
    STATE.settings = cfg.Settings.load()
    STATE.work_calendar = WorkCalendar(cfg.home_dir() / "workdays.json", STATE.settings.target_seconds)
    ui.run(
        host="127.0.0.1",
        port=port,
        title="TimeCheat",
        favicon="⏳",
        reload=False,
        show=show,
        show_welcome_message=True,
    )
