# TimeCheat – Development Document (Phase 1)

TimeCheat is a local web app for booking Jira worklogs on a calendar. It shows a
large month view and a detailed day view (similar to Outlook's day view). You select
free time slots with the mouse and assign them to a Jira issue. Nothing is written to
Jira until you review the full list of changes and confirm it with **Let's TimeCheat**.

This document captures the agreed requirements, the decisions taken, the
architecture and the Jira integration details. For installation and day-to-day
usage see [README.md](README.md).

---

## 1. Requirements (Phase 1)

| # | Requirement | Status |
|---|---|---|
| R1 | Ask for Jira credentials on start-up and authenticate | ✅ |
| R2 | Large month calendar showing the booked issues and time spent per day, in chronological order; overlapping worklogs are shown one after the other (the daily total is what matters) | ✅ |
| R3 | Month drop-down, defaulting to the current month | ✅ |
| R4 | Project drop-down (projects the user can browse); selecting one caches the project's issues for later use | ✅ |
| R5 | Each day shows coverage of the 8 h target (e.g. `6/8h`); full days are light green, partly booked days light red | ✅ |
| R6 | Clicking a day shows a detailed, time-proportional day view next to the month view; overlaps are queued, not drawn on top of each other | ✅ |
| R7 | Select free time in the day view by dragging the mouse, with 30-minute granularity | ✅ |
| R8 | After selecting, a dialog offers the issues assigned to the user, plus typeahead search over the cached issues. Only existing issues can be chosen. Each issue is shown as key + type + summary. Start and end can be edited by hand. Duration is shown in minutes; editing it moves the **end**, not the start. | ✅ |
| R9 | New entries appear in both the day and month views; entries not uploaded yet are visibly different (faded, dashed) | ✅ |
| R10 | Both new entries and existing Jira worklogs can be modified | ✅ |
| R11 | A **Let's TimeCheat** button lists exactly what will be written to Jira; the upload starts only after the user confirms | ✅ |
| R12 | Every wait is shown with an hourglass and/or progress bar | ✅ |
| R13 | Phase 2: Outlook integration (to be specified later) | ⏳ |

## 2. Decisions

| Topic | Decision |
|---|---|
| UI framework | **NiceGUI + FullCalendar 6** (vendored, thin Vue wrapper). See §3. |
| Jira | On-prem **Jira Server 8.13.5** at `https://jira.example.com/`, REST API v2, native worklogs (no Tempo). TLS verification off, as in the other Jira tools in this workspace. |
| Authentication | Username + password **or** personal access token (Bearer), selectable on the login page; the secret can optionally be stored in the system keychain (`keyring`). ⚠️ The current server (8.13.5) does **not** support PATs (introduced in 8.14); TimeCheat detects this and tells the user to use the password. |
| Month view scope | The user's own worklogs from **all** projects. The project selector only drives the issue cache and the search. |
| Project selector | Every project the user can browse (`GET /project`). |
| Issue cache | Open issues plus issues resolved in the last **90 days** (≈3 months). Stored on disk, refreshed with a button. |
| Suggested issues | Issues assigned to the user (not Done), marked with ★ and listed first. |
| Overlapping worklogs | Queued in the day view: an entry that would overlap the previous one is drawn right after it. The real start appears as "logged at HH:MM" and in the tooltip. |
| Visible part of the day | 08:00–18:00 (business hours); the rest of the day is reachable by scrolling. |
| Day colours | Light green = target met, light red = under target, light orange = over target (e.g. 9/8h, or any work on a day off), grey = weekend / public holiday (target 0). Future working days with nothing booked stay neutral (see §6). |
| Working days | Hungarian calendar from the `holidays` package, **including bridge days and the Saturdays they are worked off on**. `~/.timecheat/workdays.json` can override single days. |
| Editing | Popup edit, drag to move, drag the bottom edge to resize, delete, copy another day into this day. |
| Worklog comment | Optional. |
| Remaining estimate | `adjustEstimate=auto` (Jira's default behaviour). |
| Pending changes | Kept in memory only (they survive a browser refresh, not an app restart). Logging out with pending changes asks for confirmation. |
| UI language | English. |

## 3. Why NiceGUI (framework comparison)

| | Streamlit + streamlit-calendar | **NiceGUI + FullCalendar** (chosen) | FastAPI + JS frontend |
|---|---|---|---|
| Language | Python only | Python + ~150 lines of JS | Python + full JS app |
| Interaction model | Whole script re-runs on every click or drag. Two calendars plus a dialog need a lot of `session_state` handling. | Event-driven: handlers update state and push data, no re-runs | Event-driven |
| FullCalendar customisation | JSON options only (no JS callbacks) | JS callbacks in the wrapper (cell colours, custom event content) | Full |
| Dialogs / live field binding | `st.dialog` after a re-run | Real modal dialogs, minutes ↔ end synced live | Full |
| Effort | Lowest at first, highest to make smooth | Moderate | Highest |

## 4. Architecture

```
timecheat/
├── run.py                       # entry point: python run.py [--demo] [--port N] [--no-browser]
├── requirements.txt             # runtime dependencies
├── requirements-dev.txt         # pytest
├── timecheat/
│   ├── app.py                   # NiceGUI pages (login, main view), dialogs, rendering
│   ├── fullcalendar/            # NiceGUI element wrapping FullCalendar
│   │   ├── __init__.py          #   Python side: props, events, run_method helpers
│   │   ├── fullcalendar.js      #   Vue component: renders events/dayInfo, emits tc_* events
│   │   └── lib/index.global.min.js   # vendored FullCalendar 6.1.19 standard bundle (no CDN needed)
│   ├── model.py                 # Issue, Worklog, Timesheet (working copy), day layout, day status
│   ├── jira_client.py           # Jira REST v2 client (requests), JiraBackend protocol
│   ├── upload.py                # pending changes -> Jira operations, PlanRunner
│   ├── cache.py                 # on-disk issue cache per project
│   ├── workdays.py              # Hungarian working-day calendar + overrides
│   ├── settings.py              # ~/.timecheat/settings.json, keychain helpers
│   └── demo.py                  # in-memory fake Jira for --demo and tests
└── tests/                       # pytest suite (model, client, upload, cache, workdays)
```

### 4.1 Layers

1. **Model (`model.py`)** – pure Python, no I/O. Every time is a *naive wall-clock time in Europe/Budapest*; time zones exist only at the Jira boundary.
2. **Jira access (`jira_client.py`, `demo.py`)** – both implement the `JiraBackend` protocol, so the UI cannot tell the real server from the demo one.
3. **Upload (`upload.py`)** – turns the difference between Jira and the working copy into an ordered list of operations.
4. **UI (`app.py`, `fullcalendar/`)** – NiceGUI page plus the FullCalendar component. Blocking calls run in worker threads (`run.io_bound`) while a modal hourglass/progress dialog (`Busy`) is shown.

### 4.2 Working copy (`Timesheet`)

`Timesheet` holds two dicts keyed by a local `uid` (`jira-<worklogId>` for existing worklogs, `draft-<random>` for new ones):

* `remote` – worklogs exactly as last read from / written to Jira,
* `current` – what the user wants Jira to look like.

**Pending changes are simply the difference between the two.** For each uid:

| `remote` | `current` | Change |
|---|---|---|
| – | present | `create` |
| present | – | `delete` |
| present | differs, same issue | `update` |
| present | differs, other issue | `move` (Jira cannot move a worklog → create + delete) |

As a result, revert, discard all, the pending badge, the faded styling and the upload plan all come from the same comparison. Editing an entry back to its original value automatically makes it "not pending" again.

`merge_remote(first, last, fresh)` replaces the Jira baseline for a date range (when a month is loaded or reloaded) and keeps local edits:

* Unchanged entries are refreshed.
* Pending updates and deletes stay pending.
* An entry edited locally but deleted in Jira meanwhile becomes a new draft.
* Months already loaded keep their drafts when you switch months.

### 4.3 Day layout (overlap handling)

`layout_day()` sorts the day's worklogs by start time. Each entry starts at its real start, unless the previous entry has not ended yet; then it starts when that one ends:

```
real:      09:00–10:00, 09:30–10:30, 12:00–12:30
displayed: 09:00–10:00, 10:00–11:00, 12:00–12:30   (second one: "logged at 09:30")
```

The same layout is used in the month view (for order) and in the day view (for position). FullCalendar runs with `selectOverlap: false` and `eventOverlap: false`, so only genuinely free slots (in the displayed layout) can be selected, and drag/resize cannot drop onto another entry.

Gesture semantics:

* **Select a free slot** → new draft starting at the selected (displayed) time.
* **Drag an entry** → its real start becomes the drop position.
* **Resize an entry** → its duration changes; the real start stays the same.
* **Edit in the dialog** → start, end and duration apply to the real start. Changing the duration or the end keeps the start; changing the start keeps the duration.

### 4.4 Day status

`day_status(day, logged, target, today)`:

| Condition | State | Colour |
|---|---|---|
| logged > target | `over` | light orange |
| target = 0 (weekend/holiday), nothing logged | `off` | grey |
| logged = target | `ok` | light green |
| future day, nothing logged | `future` | neutral |
| otherwise | `under` | light red |

The month cell shows `logged/target h` (e.g. `6.5/8h`) plus the holiday name. The day header shows the same as a coloured chip.

### 4.5 FullCalendar component

The Vue component (`fullcalendar.js`) holds no state of its own.

**Python → JS**
* Props: `options` (plain JSON), `events` (FullCalendar event objects with `extendedProps.html` / `extendedProps.tooltip`) and `dayInfo` (`{"YYYY-MM-DD": {cls, label, note}}`).
* Methods: `gotoDate`, `unselect`, `scrollToTime`, `updateSize`.

**JS → Python** – these events:

| Event | Payload |
|---|---|
| `tc_select` | `{start, end}` |
| `tc_date_click` | `{date}` |
| `tc_event_click` | `{id, date}` |
| `tc_event_change` | `{id, start, end}` |

All times are local wall-clock ISO strings without an offset.

**Supporting behaviour**
* Event changes trigger `refetchEvents()` (a function event source), so the calendar never keeps a stale copy.
* `dayInfo` changes re-register the cell callbacks, which makes FullCalendar re-render the day cells.
* A `ResizeObserver` keeps the calendar size correct and scrolls the day view to 08:00 once the real height is known.

## 5. Jira integration

All calls go to REST API v2 with TLS verification off.

| Purpose | Call |
|---|---|
| Login check / current user | `GET /rest/api/2/myself` |
| Projects for the selector | `GET /rest/api/2/project` |
| Issue cache | `POST /rest/api/2/search` – `project = "X" AND (statusCategory != Done OR resolved >= -90d)`, paged by 100 |
| Assigned issues | `POST /rest/api/2/search` – `assignee = currentUser() AND statusCategory != Done` |
| Issues with my worklogs in a month | `POST /rest/api/2/search` – `worklogAuthor = currentUser() AND worklogDate >= "<first-1d>" AND worklogDate <= "<last+1d>"` |
| Worklogs of those issues | `GET /rest/api/2/issue/{key}/worklog` (paged; 8 parallel threads), filtered to the current user's worklogs that fall in the month on the Budapest wall-clock date |
| New entry | `POST /rest/api/2/issue/{key}/worklog?adjustEstimate=auto` – `{started, timeSpentSeconds, comment}` |
| Modified entry | `PUT /rest/api/2/issue/{key}/worklog/{id}?adjustEstimate=auto` – an in-place update, **no delete + re-create needed** |
| Deleted entry | `DELETE /rest/api/2/issue/{key}/worklog/{id}?adjustEstimate=auto` |
| Entry moved to another issue | `POST` on the new issue, **then** `DELETE` on the old one. If the create fails, the delete is skipped, so no time is lost. |
| PAT support check (on PAT login failure) | `GET /rest/api/2/serverInfo` (anonymous) |

* `started` is sent as `yyyy-MM-ddTHH:mm:ss.000±hhmm` in Europe/Budapest, with daylight saving time handled correctly.
* The month query is widened by a day on each side because `worklogDate` is evaluated in the user's Jira time zone. The precise filtering happens client-side.
* Error responses are reported to the user:
  * Jira's `errorMessages` / `errors` text is shown as-is.
  * A 401 is shown as an authentication error.
  * A 403 caused by a CAPTCHA asks the user to log in once in the browser.

### 5.1 Upload ("Let's TimeCheat")

1. `build_plan(timesheet.changes())` creates the ordered operation list, sorted by date.
2. The review dialog shows every operation. Columns: action, issue (key · type · summary), date, time, duration, comment and a note (e.g. "was 15:00, 60 min", "moved from DEMO-33").
3. After confirmation, the operations run one by one. The network part runs in a worker thread; the result is recorded on the event loop (`PlanRunner.finish`). A progress bar and a per-row ✅/❌ result are shown.
4. Successful operations update `remote` immediately. Failed ones stay pending, so the user can fix them and retry. The month is then reloaded from Jira.

## 6. Notes, limitations and deviations

* **Future days:** future working days with nothing booked are neutral instead of red, so the rest of the current month isn't all red. Once something is booked, they follow the normal colours.
* **PAT on the current server:** Jira 8.13.5 has no personal access tokens; use username + password. The PAT option works automatically once the server is upgraded to 8.14 or newer.
* **Single user, localhost:** the app binds to `127.0.0.1` and keeps one global state. Opening it in two tabs shows the same state.
* **Issue search:** it covers the selected project's cache, your assigned issues and every issue already seen in your worklogs. Choose a project to make all of its issues searchable. A hint is shown in the dialog until you do.
* **Issue list size:** the dropdown filters on the client side. This works smoothly up to a few thousand issues per project.
* **Not covered (yet):** worklog visibility restrictions (group/role), Tempo attributes, and keeping drafts across app restarts.

## 7. Development

```bash
cd timecheat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q          # unit tests (no network)
.venv/bin/python run.py --demo         # UI against the in-memory fake Jira
```

* `TIMECHEAT_HOME` overrides `~/.timecheat` (settings, cache, workday overrides). This is useful for demos and tests.
* The demo backend seeds the previous and current month (including overlapping entries), adds a small delay to every call so the busy indicators are visible, and always fails on `DEMO-13` to show upload errors.
* FullCalendar is vendored (`timecheat/fullcalendar/lib`). To upgrade, replace `index.global.min.js` with a newer `fullcalendar@6` standard bundle.
* The code changes from this session were tested manually in the browser against the demo backend:
  * login and the busy indicators,
  * month and project switching,
  * selection, the dialog and minutes ↔ end sync,
  * drag, resize, copy day, the Pending dialog (revert / discard all), upload with partial failure, and the logout guard.

## 8. Phase 2 – Outlook integration (outline)

To be specified. Points to clarify then:

* **Access route:** on macOS there is no COM automation. The realistic options are:
  * the Microsoft Graph API (needs an Azure AD app registration and consent, which may be restricted in a corporate tenant),
  * reading an ICS export or subscription,
  * EWS, if it is still enabled on the tenant.
* **UX:** show meetings as a read-only background layer in the day view, then turn a meeting into a draft worklog with one click, with the issue suggested from the meeting subject.
* **Fit:** this slots into the current design as another event source for the day view. The `Timesheet` and upload code need no changes.
