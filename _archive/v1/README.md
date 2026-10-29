# TimeCheat ⏳

Book your Jira worklogs on a calendar: see the month at a glance, drag free slots in the
day view, pick an issue, and upload everything to Jira in one confirmed batch.

Design and implementation details: [DEVELOPMENT.md](DEVELOPMENT.md).

## Install

```bash
cd timecheat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.10+ and network access to `https://jira.example.com/`.

## Run

```bash
.venv/bin/python run.py            # opens http://127.0.0.1:8765 in the browser
.venv/bin/python run.py --demo     # try it without Jira (in-memory fake data)
.venv/bin/python run.py --port 9000 --no-browser
```

## Using it

1. **Log in** with your Jira username + password (or a personal access token on Jira 8.14+). Tick *Save the secret in the system keychain* to skip typing it next time: leave the field empty and the saved one is used.
2. **Pick a month** (defaults to the current one) and a **Jira project**. The project's issues are cached in `~/.timecheat/cache`; refresh them with the ⟳ button.
3. The **month view** shows your worklogs from all projects plus each day's total against the 8 h target:
   * light green: target met
   * light red: under target
   * light orange: over target
   * grey: weekend or Hungarian public holiday
4. **Click a day** to open it in the day view on the right.
   * **Drag over free time** (30-minute steps) to create an entry. Pick the issue (★ = assigned to you; type to search by key or text), adjust start / end / minutes and add an optional comment.
   * **Click** an entry to edit, delete or revert it.
   * **Drag** an entry to move it, or drag its bottom edge to change its length.
   * Use ⧉ to copy another day's entries into the selected day.
5. Entries that are not uploaded yet are **faded and dashed**. **Pending** lists them; you can revert single changes or discard all of them.
6. **Let's TimeCheat** shows exactly what will be created, updated or deleted in Jira. Confirm to upload; each row reports its result. Failed rows stay pending so you can fix them and retry.

Overlapping worklogs are queued one after the other in the day view; their real start time is shown as "logged at HH:MM".

## Files

* `~/.timecheat/settings.json` – server, last username, last project
* `~/.timecheat/cache/<server>/<PROJECT>.json` – issue cache
* `~/.timecheat/workdays.json` (optional) – extra working days / days off, e.g. `{"workdays": ["2027-01-09"], "days_off": ["2027-01-02"]}`

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
