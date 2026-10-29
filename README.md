# TimeCheat ⏳

Book your Jira worklogs on a calendar: see the month at a glance, drag free slots in the
day view, pick an issue, and upload everything to Jira in one confirmed batch.

Design and implementation details: [DEVELOPMENT.md](DEVELOPMENT.md).

## Install

TimeCheat needs **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/); on Windows tick
*Add python.exe to PATH* in the installer) and access to your Jira Server / Data Center. The first start
also needs access to [pypi.org](https://pypi.org) to install the packages.

### From the release zip (no terminal needed)

1. Download `timecheat-<version>.zip` from the [Releases](../../releases) page (or the team zip shared
   inside your company, which already knows your Jira server).
2. Windows: right-click the zip → *Properties* → tick *Unblock* → *OK*, then *Extract All*.
   macOS: double-click the zip.
3. Start it:
   * **Windows:** double-click `start.bat`.
   * **macOS:** double-click `start.command`. If macOS refuses to open it ("unidentified developer"),
     open *System Settings → Privacy & Security* and click *Open Anyway*, or run once in Terminal:
     `xattr -dr com.apple.quarantine <the extracted folder>`.
   * **Linux:** run `./start.command`.

   The first start creates a Python environment in `.venv` inside the folder and installs the packages
   (about a minute). Later starts are quick. The launchers pass their options on to `run.py`, e.g.
   `start.bat --demo`.

**Updating:** extract the new zip, start it from there and delete the old folder. Your settings, saved
password and issue cache live in your home folder (see [Files](#files)), so nothing is lost.

### From git

```bash
git clone <this repository> timecheat
cd timecheat
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows (PowerShell or cmd):

```bat
git clone <this repository> timecheat
cd timecheat
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

If `py` is not available, use `python` instead. On Windows the venv puts its executables in
`.venv\Scripts\` (not `.venv/bin/`), so replace `.venv/bin/python` with `.venv\Scripts\python`
in all commands below. `start.bat` / `start.command` work in a clone too.

## Run

```bash
.venv/bin/python run.py            # opens http://127.0.0.1:8765 in the browser
.venv/bin/python run.py --demo     # try it without Jira (in-memory fake data)
.venv/bin/python run.py --port 9000 --no-browser
.venv/bin/python run.py --version
```

Windows: `.venv\Scripts\python run.py` (same options).

## Using it

1. **Log in** with your Jira server address (e.g. `https://jira.example.com/`; remembered for next time), your Jira username + password (or a personal access token on Jira 8.14+). Tick *Save the secret in the system keychain* to skip typing it next time: leave the field empty and the saved one is used.
2. **Pick a month** (defaults to the current one) and a **Jira project**. The project's issues are cached in `~/.timecheat/cache`; refresh them with the ⟳ button.
3. The **month view** shows your worklogs from all projects plus each day's total against the daily target (8 h by default, see *Settings*):
   * light green: target met
   * light red: under target
   * light orange: over target
   * grey: weekend or Hungarian public holiday
4. **Click a day** to open it in the day view on the right.
   * **Drag over free time** (30-minute steps) to create an entry. Pick the issue (★ = assigned to you; type to search by key or text), adjust start / end / minutes and add an optional comment.
   * **Click** an entry to edit, delete or revert it.
   * **Drag** an entry to move it, or drag its bottom edge to change its length. The start (when moving) or the end (when resizing) snaps to the nearest half hour, so a 10:38 entry dragged down lands on 11:00, not 11:08.
   * Use ⧉ to copy another day's entries into the selected day.
5. Entries that are not uploaded yet are **faded and dashed**. **Pending** lists them; you can revert single changes or discard all of them.
6. **Let's TimeCheat** shows exactly what will be created, updated or deleted in Jira. Confirm to upload; each row reports its result. Failed rows stay pending so you can fix them and retry.

Overlapping worklogs are queued one after the other in the day view; their real start time is shown as "logged at HH:MM".

## Settings and quitting

* ⚙ **Settings** (header):
  * **Default Jira project**, selected automatically when TimeCheat starts.
  * **Working period**, default 09:00–17:00. Its length is the daily target: 09:00–17:00 = 8h, 08:00–14:00 = 6h. The day view highlights the period and opens on it.
  * **Meeting story**: the summary of the story whose sub-tasks hold your recurring meetings, e.g. *General meetings* (see [Outlook meetings](#outlook-meetings)). Empty turns automatic matching and sub-task creation off.

  Settings are saved to `~/.timecheat/settings.json`.
* ⏻ **Quit** (header and login page) stops TimeCheat. If there are changes not uploaded to Jira yet, it asks first.

## Outlook meetings

1. Set the **meeting story** in ⚙ Settings (e.g. *General meetings*) and select the Jira project that has it (needed for auto-matching and for creating sub-tasks), then open a day and click **Outlook** in the day header.
2. The first time, TimeCheat asks for a Microsoft Graph token (click **?** for these steps):
   1. Open [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) and sign in with your work account.
   2. Run a calendar query once, e.g. `GET https://graph.microsoft.com/v1.0/me/calendarView?startDateTime=2026-10-01T00:00:00&endDateTime=2026-10-02T00:00:00`. If asked, consent to **Calendars.Read** under *Modify permissions*.
   3. Open the **Access token** tab and copy the token, then paste it into TimeCheat.
3. The token is valid for about 60–90 minutes and is kept in memory only. The key chip next to the Outlook button shows the minutes left; click it to paste a new one. TimeCheat warns 5 minutes before expiry and asks again when the token no longer works.
4. The month's meetings are loaded and the selected day's meetings are put into the free time:
   * Meetings that are only partly free are clipped.
   * Meetings that end at most 10 minutes before the half hour (Outlook's 10:00–10:25 or 10:00–10:20) are booked up to it (10:30), as far as that time is free and no other meeting starts in it. 10:00–10:15 stays as it is.
   * If meetings overlap each other, you pick which one(s) to keep.
   * Private, cancelled, declined, tentative / not-responded, free and all-day events are skipped.
   * A summary lists what was added and what was skipped (and why).
5. Recurring meetings whose series ID is in the comment of a sub-task under the meeting story (`"iCalUId": "…"`) get that sub-task automatically (🔗).
6. Other meetings appear with an **orange dashed border**. Click one to pick any Jira issue, or click **Create sub-task**: it creates a sub-task right away, assigned to you – under the meeting story by default, or under any other story / task you pick in the popup. For recurring meetings it also adds the iCalUId comment, so the next occurrences are matched automatically. Assigning a recurring meeting also assigns its other imported occurrences. If you pick a sub-task of the meeting story by hand, TimeCheat asks whether to add the iCalUId comment to it. Otherwise the choice is only remembered until you quit (↺).
   * 🔗 = matched via an iCalUId comment in Jira (persistent); ↺ = remembered in this session only (nothing written to Jira).
7. **Let's TimeCheat** does not upload meetings without an issue: it offers to remove them and upload the rest, or to go back.

## Files

* `~/.timecheat/settings.json` – your settings: server, last username, last / default project, working period, meeting story. Only values that differ from the defaults are stored. Demo mode uses `settings.demo.json`. (Before 1.1 this was `timecheat_settings.json` next to `run.py`; it is picked up automatically.)
* `timecheat_defaults.json` (optional, next to `run.py`) – defaults for a team, e.g. the Jira server and the meeting story; see `timecheat_defaults.example.json`. Your own settings override them.
* The password / token, if you ticked *Save the secret*, is in the system keychain (Keychain, Windows Credential Manager, Secret Service).
* `~/.timecheat/cache/<server>/<PROJECT>.json` – issue cache
* `~/.timecheat/workdays.json` (optional) – extra working days / days off, e.g. `{"workdays": ["2027-01-09"], "days_off": ["2027-01-02"]}`

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Windows: `.venv\Scripts\python -m pip install -r requirements-dev.txt`, then `.venv\Scripts\python -m pytest -q`.

## Sharing it

Releases, the team zip and the CI are described in [DEVELOPMENT.md](DEVELOPMENT.md#9-releases-and-sharing).

## Security notes

* TimeCheat runs a local web server on `127.0.0.1` only; nothing is reachable from other machines.
* TLS certificate verification towards Jira is **off**, because on-prem Jira servers often use a certificate
  from an internal CA that Python does not trust. Use it on a network you trust.
* The Graph token is kept in memory only. Passwords / tokens are stored only if you ask for it, and then
  only in the system keychain.

## License

MIT, see [LICENSE](LICENSE). Includes [FullCalendar](https://fullcalendar.io) (MIT) in `timecheat/fullcalendar/lib`.
