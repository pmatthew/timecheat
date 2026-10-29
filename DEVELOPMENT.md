# TimeCheat – Development Document (Phase 1 + 2, releases)

TimeCheat is a local web app for booking Jira worklogs on a calendar. It shows a
large month view and a detailed day view (similar to Outlook's day view). You select
free time slots with the mouse and assign them to a Jira issue. Nothing is written to
Jira until you review the full list of changes and confirm it with **Let's TimeCheat**.
Phase 2 adds Outlook: your meetings can be pulled in and turned into worklog drafts (see §8).

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
| R5 | Each day shows coverage of the daily target (e.g. `6/8h`); full days are light green, partly booked days light red | ✅ |
| R6 | Clicking a day shows a detailed, time-proportional day view next to the month view; overlaps are queued, not drawn on top of each other | ✅ |
| R7 | Select free time in the day view by dragging the mouse, with 30-minute granularity | ✅ |
| R8 | After selecting, a dialog offers the issues assigned to the user, plus typeahead search over the cached issues. Only existing issues can be chosen. Each issue is shown as key + type + summary. Start and end can be edited by hand. Duration is shown in minutes; editing it moves the **end**, not the start. | ✅ |
| R9 | New entries appear in both the day and month views; entries not uploaded yet are visibly different (faded, dashed) | ✅ |
| R10 | Both new entries and existing Jira worklogs can be modified | ✅ |
| R11 | A **Let's TimeCheat** button lists exactly what will be written to Jira; the upload starts only after the user confirms | ✅ |
| R12 | Every wait is shown with an hourglass and/or progress bar | ✅ |
| R13 | Phase 2: Outlook meetings → worklog drafts (see §8) | ✅ |

## 2. Decisions

| Topic | Decision |
|---|---|
| UI framework | **NiceGUI + FullCalendar 6** (vendored, thin Vue wrapper). See §3. |
| Jira | On-prem **Jira Server / Data Center** (developed against 8.13.5), REST API v2, native worklogs (no Tempo). The server address is entered on the login page or comes from the team defaults (§9). TLS verification off, because such servers often use a certificate from an internal CA. |
| Authentication | Username + password **or** personal access token (Bearer), selectable on the login page; the secret can optionally be stored in the system keychain (`keyring`). ⚠️ The current server (8.13.5) does **not** support PATs (introduced in 8.14); TimeCheat detects this and tells the user to use the password. |
| Month view scope | The user's own worklogs from **all** projects. The project selector only drives the issue cache and the search. |
| Project selector | Every project the user can browse (`GET /project`). |
| Issue cache | Open issues plus issues resolved in the last **90 days** (≈3 months). Stored on disk, refreshed with a button. |
| Suggested issues | Issues assigned to the user (not Done), marked with ★ and listed first. |
| Overlapping worklogs | Queued in the day view: an entry that would overlap the previous one is drawn right after it. The real start appears as "logged at HH:MM" and in the tooltip. |
| Working period / daily target | Set in **Settings** (gear icon), default **09:00–17:00**. The daily target is the length of the period (09:00–17:00 = 8h, 08:00–14:00 = 6h; minutes allowed, e.g. 7.5h). |
| Visible part of the day | The working period is highlighted as business hours; it is shown with 1 hour of margin on each side (default 08:00–18:00). The rest of the day is reachable by scrolling. |
| Settings file | `~/.timecheat/settings.json` (since 1.1; a fresh zip must not overwrite them). It holds the default project, the working period, the server, the auth mode, the username, the last project and the meeting story. Layers: built-in defaults < `timecheat_defaults.json` next to `run.py` (team defaults, git-ignored, not used in demo mode) < the user's file. Only values that differ from the defaults are saved, so changed team defaults still reach everyone. Demo mode uses `settings.demo.json`. `TIMECHEAT_SETTINGS` overrides the path. The 1.0 file `timecheat_settings.json` next to `run.py` is read while it is newer than the home file. Secrets stay in the keychain. |
| Default project | Selected automatically at start-up; if none is set, the last selected project is used. |
| Quit | Power button in the header and on the login page. It stops the server process; with pending changes it asks first. |
| Day colours | Light green = target met, light red = under target, light orange = over target (e.g. 9/8h, or any work on a day off), grey = weekend / public holiday (target 0). Future working days with nothing booked stay neutral (see §6). |
| Working days | Hungarian calendar from the `holidays` package, **including bridge days and the Saturdays they are worked off on**. `~/.timecheat/workdays.json` can override single days. |
| Editing | Popup edit, drag to move, drag the bottom edge to resize, delete, copy another day into this day. |
| Worklog comment | Optional. |
| Remaining estimate | `adjustEstimate=auto` (Jira's default behaviour). |
| Pending changes | Kept in memory only (they survive a browser refresh, not an app restart). Logging out or quitting with pending changes asks for confirmation. |
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
├── run.py                       # entry point: python run.py [--demo] [--port N] [--no-browser] [--version]
├── start.bat / start.command    # double-click launchers: create .venv, install requirements, run
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
│   ├── settings.py              # ~/.timecheat/settings.json + team defaults (Settings dialog), keychain helpers
│   ├── outlook.py               # Phase 2: Microsoft Graph client, Meeting, event filter, iCalUId series ID
│   ├── meetings.py              # Phase 2: meeting conflicts, clipping into free time, iCalUId comments
│   └── demo.py                  # in-memory fake Jira + fake Graph for --demo and tests
├── tests/                       # pytest suite (model, client, upload, cache, workdays, outlook, release zip)
├── tools/build_release.py       # builds dist/timecheat-<version>[-team].zip (§9)
├── .github/workflows/ci.yml     # tests on Windows/macOS/Linux, launcher smoke test, GitHub release on v* tags
├── timecheat_defaults.example.json   # template for the git-ignored team defaults
└── pytest.ini                   # limits test discovery to tests/
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
* **Drag an entry** → its real start becomes the drop position, snapped to the nearest half hour (FullCalendar moves by whole slots from the original time, so 10:38 would otherwise land on 11:08).
* **Resize an entry** → its duration changes so that the end snaps to the nearest half hour; the real start stays the same.
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
* **PAT on older servers:** Jira before 8.14 has no personal access tokens; use username + password there. The PAT option works on 8.14 or newer.
* **Single user, localhost:** the app binds to `127.0.0.1` and keeps one global state. Opening it in two tabs shows the same state.
* **Issue search:** it covers the selected project's cache, your assigned issues and every issue already seen in your worklogs. Choose a project to make all of its issues searchable. A hint is shown in the dialog until you do.
* **Issue list size:** the dropdown filters on the client side. This works smoothly up to a few thousand issues per project.
* **Not covered (yet):** worklog visibility restrictions (group/role), Tempo attributes, and keeping drafts across app restarts.

## 7. Development

```bash
cd timecheat
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q          # unit tests (no network)
.venv/bin/python run.py --demo         # UI against the in-memory fake Jira
```

On Windows use `py -m venv .venv` and `.venv\Scripts\python` instead of `.venv/bin/python`.

* `TIMECHEAT_HOME` overrides `~/.timecheat` (settings, issue cache, workday overrides), and `TIMECHEAT_SETTINGS` overrides the settings file. Both are useful for demos and tests.
* The demo backend seeds the previous and current month (including overlapping entries), adds a small delay to every call so the busy indicators are visible, and always fails on `DEMO-13` to show upload errors.
* FullCalendar is vendored (`timecheat/fullcalendar/lib`). To upgrade, replace `index.global.min.js` with a newer `fullcalendar@6` standard bundle.
* The code changes from this session were tested manually in the browser against the demo backend:
  * login and the busy indicators,
  * month and project switching,
  * selection, the dialog and minutes ↔ end sync,
  * drag, resize, copy day, the Pending dialog (revert / discard all), upload with partial failure, and the logout guard.
* Phase 2 was tested the same way:
  * token dialog, expired-token re-prompt, token chip;
  * import summary with skip reasons, auto-match via iCalUId, conflict dialog, clipping;
  * sub-task creation (with and without the iCalUId comment), series propagation;
  * the unassigned guard and the upload.

## 8. Phase 2 – Outlook meetings

The day view has an **Outlook** button. It pulls your calendar for the month from Microsoft Graph, fits the selected day's meetings into the free time, and turns them into worklog drafts. Every meeting draft must be linked to a Jira issue before upload. For recurring meetings this is automated through the iCalUId.

### 8.1 Decisions

| Topic | Decision |
|---|---|
| Authentication | A Microsoft Graph access token copied from **Graph Explorer** and pasted into TimeCheat, so no app registration is needed. The token is kept **in memory only** (never on disk or in logs), because it carries every scope consented to in Graph Explorer. |
| Token lifetime | Read from the JWT `exp` claim and shown as a chip in the day header (`47 min`). A warning appears when 5 minutes are left. Once expired, or when Graph answers 401, the token dialog reopens and the call is retried. |
| Help | A "?" button in the token dialog shows step-by-step instructions. They are open by default until the first token is entered. |
| Fetch scope | The whole selected month in one call (paged), cached per month for the session. **Refetch from Outlook** in the summary dialog loads it again. |
| Fill scope | Only the selected day. |
| Skipped events | Skipped when any of these apply: sensitivity *private / personal / confidential* (the subject is hidden too), cancelled, all-day, declined, not responded, tentative (response or `showAs`), `showAs = free`, and series masters. Response `none` / `organizer` / `accepted` is kept. |
| Partial fit | **Clip:** only the free parts of a meeting are inserted. "Free" means the day view's displayed (queued) layout, including drafts. Pieces shorter than 5 minutes are dropped. |
| Early meeting ends | **Round up:** an end at most 10 minutes before the half hour (10:20, 10:25) is extended to it (10:30), but only into free time and never past the start of another imported meeting. Only the fragment that ends the meeting is extended. The summary counts these. |
| Meetings overlapping each other | **Ask:** a dialog lists each conflict group with checkboxes (the earliest one is pre-ticked). Unticked meetings are skipped. Ticked ones are booked one after the other in the free time. |
| Draft comment | Empty. The subject is shown on the card, in the tooltip and in the dialog. |
| Re-import | Meetings already imported on that day (same Graph event id) are skipped. Uploaded meetings occupy their slot, so they are not inserted again. |
| Meeting story | Its summary is the **Meeting story** setting (e.g. "General meetings"; team defaults can preset it, the demo uses "General meetings"). Looked up in the **selected project** by exact summary (case-insensitive), stories first. If there are several, you pick one. If there are none, or the setting is empty, auto-match and sub-task creation are off and a hint is shown. |
| Auto-match | The comments of **all sub-tasks of that story** (any status) are read once per project per session. Any `"iCalUId": "<hex>"` in a comment links that series to the sub-task. If several sub-tasks name the same series, the most recently updated one wins. Matched drafts show 🔗 and can still be changed in the dialog. |
| Manual assignment | Assigning a recurring meeting by hand also assigns the other unassigned occurrences already imported. If the chosen issue is a sub-task of the story, TimeCheat **asks** whether to add the `"iCalUId": "<series ID>"` comment to it (so it survives restarts). Otherwise, or if you decline, the choice is remembered **for this session only** (in memory, nothing in Jira). |
| Where the match lives | 🔗 = found in an iCalUId comment in Jira (persistent). ↺ = remembered from a manual choice in this session (lost on quit). Card, tooltip, entry dialog and import summary show which one applies. Comments are only written by **Create sub-task** (recurring meetings) or after confirming the prompt above. |
| Create sub-task | In the meeting's dialog. A confirmation popup is prefilled with the meeting subject. The **parent defaults to the meeting story** (★); any other known non-sub-task issue (story, task, bug, epic…) can be chosen instead. The sub-task is **created immediately** in the parent's project and assigned to you; if the create screen rejects the assignee, it is created unassigned. For recurring meetings, the comment `"iCalUId": "<series ID>"` is added. Auto-matching only reads the story's sub-tasks, so under another parent the series is remembered for the session only (↺); the dialog says so before creating. The button is enabled whenever there is a possible parent (also when the project has no story). |
| Unassigned drafts on upload | **Let's TimeCheat** lists them and offers **Remove unassigned meetings and upload the rest** or **Go back**. `build_plan` also refuses unassigned entries as a safety net. |

### 8.2 iCalUId and recurring meetings

Microsoft Graph documents `iCalUId` as *"different for each occurrence in a recurring series"*, so the raw value is **not** suitable for matching recurring meetings. For Exchange meetings it is a *global object ID*:

```
040000008200E00074C5B7101A82E008  07EA 0A 1B  C465B82C7551DD01  0000000000000000  10000000  22E2912D…3D0E
└──── class ID (fixed) ─────────┘  └ occurrence ┘ └ creation time ┘                   └ size ┘  └ unique data ┘
                                   2026-10-27
```

Bytes 17–20 hold the occurrence date. Setting them to `00000000` gives the *clean global object ID*, which is identical for every occurrence of the series. `outlook.series_id()` does this (values without the Exchange prefix are only upper-cased).

* Comments written by TimeCheat contain the series ID.
* When comments are read, every ID is normalized the same way. A comment that holds an occurrence's iCalUId (e.g. pasted by hand from Graph Explorer) therefore still matches every occurrence.
* Single meetings work too, but are only matched when that exact meeting is named in a comment.

### 8.3 Microsoft Graph call

```
GET https://graph.microsoft.com/v1.0/me/calendarView
    ?startDateTime=<first>T00:00:00+01:00&endDateTime=<last+1>T00:00:00+01:00
    &$select=id,subject,start,end,isAllDay,isCancelled,showAs,responseStatus,sensitivity,type,iCalUId,seriesMasterId
    &$top=100&$orderby=start/dateTime
Authorization: Bearer <token>
Prefer: outlook.timezone="Europe/Budapest"
```

* Times come back as Budapest wall-clock time; a `UTC` time zone is still converted for safety.
* Paging follows `@odata.nextLink`.
* Errors: 401 means the token is invalid or expired, and the user is asked for a new one. 403 means missing consent: the message tells you to consent to `Calendars.Read` in Graph Explorer.
* The token must allow `Calendars.Read`. `requests` uses its own CA bundle (certifi), so the laptop's `SSL_CERT_FILE` with the internal CA does not get in the way.

### 8.4 Jira calls added in Phase 2

| Purpose | Call |
|---|---|
| Find the story | `POST /search` – `project = "X" AND summary ~ "\"<meeting story>\""`, then an exact match on the client side; falls back to a broader search on the last word of the summary |
| Sub-tasks and comments | `POST /search` – `parent = STORY` with fields `summary,issuetype,status,project,updated,comment`; `GET /issue/{key}/comment` if the embedded comment list is truncated |
| Sub-task type | `GET /project/{key}` → the first issue type with `subtask: true` |
| Create sub-task | `POST /issue` – parent's project, sub-task type id, `parent` (default: the story, or the chosen issue), summary, description, assignee = me |
| iCalUId comment | `POST /issue/{key}/comment` – `{"body": "\"iCalUId\": \"<series ID>\""}` |

### 8.5 Code

* `outlook.py`:
  * `GraphClient` (`calendar_view`);
  * `Meeting`, `filter_meetings` (returns kept meetings and `SkippedEvent`s with a reason);
  * `series_id`, `token_expiry` (JWT `exp`, not verified), `clean_token` (strips quotes / `Bearer`).
* `meetings.py`:
  * `find_conflicts` (chained overlaps form one group);
  * `fill_day` (subtracts the displayed busy intervals, clips, drops pieces under 5 minutes, rounds early ends up to the half hour, skips already imported ones);
  * `parse_ical_comments`, `ical_comment`.
* `model.py`:
  * `MeetingRef` on `Worklog.meeting`; an **empty `issue_key` means "not assigned yet"**;
  * `Timesheet.unassigned()`, `assign_series()`, `imported_event_ids()`;
  * `copy_day` skips unassigned meeting drafts.
* `jira_client.py` / `demo.py`: `find_stories`, `children_with_comments`, `create_subtask`, `add_comment`.
* `demo.py`:
  * `FakeGraph` serves a weekly pattern: a daily stand-up (already linked to `DEMO-51` through a comment), a Tuesday sprint sync, a Wednesday overlap, a Friday retro, and private / declined / tentative / cancelled / all-day examples;
  * any token works except `expired`, which simulates a 401.
* `app.py`: the Outlook button and token chip, plus these dialogs:
  * token and help, conflict resolution, import summary;
  * the meeting section of the entry dialog with **Create sub-task**, and the sub-task confirmation;
  * the unassigned-entries guard on upload.

### 8.6 Notes

* Tenant policy may block Graph Explorer's consent for `Calendars.Read`; it worked when tested by the user.
* Sub-task creation can fail on required custom fields or a closed parent; Jira's message is shown and nothing changes locally.
* A matched sub-task may be closed. Booking on it then fails at upload with a per-row error, as for any closed issue.
* If an imported meeting draft is moved to another time and the day is imported again, the meeting is recognised by its event id and not inserted twice. If the draft was deleted, it is inserted again.
* Possible next step: MSAL sign-in with an own Entra ID app registration instead of pasting tokens.

## 9. Releases and sharing

The repository is public and holds no company data: the Jira server and the meeting story are
settings. A team ships them in a git-ignored `timecheat_defaults.json` next to `run.py`.

### 9.1 What users get

`tools/build_release.py` builds `dist/timecheat-<version>.zip` with one `TimeCheat-<version>/` folder:
the `timecheat` package, `run.py`, `requirements.txt`, `README.md`, `LICENSE`,
`timecheat_defaults.example.json` and the launchers. Tests, `.venv` and settings are left out.

* `start.bat` (Windows) and `start.command` (macOS / Linux) find Python 3.10+ (`py -3`, then `python` /
  `python3`), create `.venv` on the first start, and reinstall `requirements.txt` whenever it differs
  from the copy saved in `.venv/requirements.installed`. On errors they keep the window open.
* The script writes `start.bat` with CRLF line endings (cmd.exe misreads labels otherwise) and
  `start.command` with LF and the executable bit (honoured by macOS Archive Utility and `unzip`).
* `--team` also packs `timecheat_defaults.json` into `timecheat-<version>-team.zip`. Share that one
  inside the company only (Teams / SharePoint); it is never committed.

Settings, keychain secrets and the issue cache live in the user's home folder, so updating means
extracting the new zip and deleting the old folder.

### 9.2 Making a release

1. Raise `__version__` in `timecheat/__init__.py` (semantic versioning) and commit.
2. Tag and push: `git tag v1.2.0 && git push origin main v1.2.0`.
3. CI (`.github/workflows/ci.yml`) runs the tests on Windows and Linux with Python 3.10 and 3.13 and
   on macOS with 3.13 (macOS runners are scarce), builds the zip, extracts it and runs `start.bat --version` / `start.command --version`
   (this installs the requirements from scratch, so a missing dependency such as `tzdata` on
   Windows fails here). For a `v*` tag that matches `__version__` it then creates the GitHub
   release with the generic zip attached.
   Runs for the same commit share a concurrency group, so pushing `main` and the tag together
   tests it only once (the older run is cancelled).
4. For the team zip, run locally with your `timecheat_defaults.json` in place:
   `.venv/bin/python tools/build_release.py --team`, and share `dist/timecheat-<version>-team.zip`.

### 9.3 Not done (on purpose)

* **Stand-alone executables** (PyInstaller / `nicegui-pack`): no Python needed, but large, and
  unsigned executables are often blocked by corporate antivirus. Python 3.10+ is assumed instead.
* **Signing / notarisation** of the launchers: users unblock the zip once (see README).
* **Update check:** users download new versions themselves.
* **Offline install:** the first start needs pypi.org. If a proxy blocks it, a wheelhouse
  (`pip download -r requirements.txt -d wheels` per platform) could be added to the team zip.
