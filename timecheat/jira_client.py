"""Thin Jira Server / Data Center REST v2 client.

Only the handful of endpoints TimeCheat needs. Plain ``requests`` instead of the
``jira`` package keeps pagination, ``adjustEstimate`` and both auth schemes
explicit. TLS verification is off: on-prem Jira servers often use a certificate
from an internal CA that the trust store of a corporate laptop (and ``certifi``)
does not include.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests
import urllib3

from .model import Issue, Worklog, remote_uid

BUDAPEST = ZoneInfo("Europe/Budapest")
ISSUE_FIELDS = "summary,issuetype,status,project"
ADJUST_ESTIMATE = "auto"
PAGE_SIZE = 100
WORKLOG_FETCH_THREADS = 8
PAT_MIN_VERSION = (8, 14)
BROWSE_PROJECTS_DATA = re.compile(
    r'WRM\._unparsedData\["com\.atlassian\.jira\.project\.browse:projects"\]\s*=\s*"((?:[^"\\]|\\.)*)"'
)

ProgressCallback = Callable[[int, int], None]


class JiraError(RuntimeError):
    pass


@dataclass(frozen=True)
class Project:
    key: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.key} – {self.name}"


@dataclass(frozen=True)
class ChildIssue:
    issue: Issue
    updated: str
    comments: list[str]


@dataclass(frozen=True)
class User:
    name: str
    key: str
    display_name: str

    def owns(self, author: dict[str, Any]) -> bool:
        return any(
            author.get(field) and author.get(field) == value
            for field, value in (("key", self.key), ("name", self.name), ("accountId", self.key))
        )


class JiraBackend(Protocol):
    """What the UI needs from Jira; implemented by ``JiraClient`` and ``demo.FakeJira``."""

    user: User

    def projects(self) -> list[Project]: ...

    def project_issues(self, project: str, resolved_within_days: int) -> list[Issue]: ...

    def assigned_issues(self) -> list[Issue]: ...

    def my_worklogs(
        self, first: date, last: date, progress: ProgressCallback | None = None
    ) -> tuple[list[Worklog], list[Issue]]: ...

    def create_worklog(self, worklog: Worklog) -> Worklog: ...

    def update_worklog(self, worklog: Worklog) -> Worklog: ...

    def delete_worklog(self, issue_key: str, worklog_id: str) -> None: ...

    def find_stories(self, project: str, summary: str) -> list[Issue]: ...

    def children_with_comments(self, parent_key: str) -> list[ChildIssue]: ...

    def create_subtask(self, project: str, parent_key: str, summary: str, description: str) -> Issue: ...

    def add_comment(self, issue_key: str, body: str) -> None: ...


# --------------------------------------------------------------------------- #
# conversions
# --------------------------------------------------------------------------- #


def format_started(start: datetime) -> str:
    return start.replace(tzinfo=BUDAPEST).strftime("%Y-%m-%dT%H:%M:%S.000%z")


def parse_started(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    return parsed.astimezone(BUDAPEST).replace(tzinfo=None)


def issue_from_json(data: dict[str, Any]) -> Issue:
    fields = data.get("fields") or {}
    return Issue(
        key=data["key"],
        summary=fields.get("summary") or "",
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        status=(fields.get("status") or {}).get("name", ""),
        project=(fields.get("project") or {}).get("key", data["key"].split("-")[0]),
    )


def worklog_from_json(issue_key: str, data: dict[str, Any]) -> Worklog:
    worklog_id = str(data["id"])
    return Worklog(
        uid=remote_uid(worklog_id),
        issue_key=issue_key,
        start=parse_started(data["started"]),
        seconds=int(data["timeSpentSeconds"]),
        comment=data.get("comment") or "",
        worklog_id=worklog_id,
    )


def worklog_body(worklog: Worklog) -> dict[str, Any]:
    return {
        "started": format_started(worklog.start),
        "timeSpentSeconds": worklog.seconds,
        "comment": worklog.comment,
    }


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


class JiraClient:
    def __init__(
        self,
        server: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        verify: bool = False,
        timeout: float = 60,
    ) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout
        # Set by projects() when the "Browse projects" list could not be read and all projects are returned.
        self.projects_warning: str | None = None
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            self.session.auth = (username, password)
        else:
            raise JiraError("Either a username and password or a personal access token is required.")
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            self.user = self._myself()
        except JiraError:
            if token:
                self._explain_token_support()
            raise

    def _explain_token_support(self) -> None:
        """Personal access tokens only exist since Jira (Server/DC) 8.14."""
        try:
            response = requests.get(f"{self.server}/rest/api/2/serverInfo", verify=self.session.verify,
                                    timeout=self.timeout)
            numbers = tuple(response.json().get("versionNumbers") or ())
        except (requests.RequestException, ValueError):
            return
        if numbers and numbers < PAT_MIN_VERSION:
            version = ".".join(str(n) for n in numbers)
            raise JiraError(
                f"This Jira ({version}) does not support personal access tokens (they need 8.14 or newer). "
                "Log in with username and password instead."
            )

    # -- plumbing ----------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.server}/rest/api/2/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as error:
            raise JiraError(f"Cannot reach {self.server}: {error}") from error
        if response.status_code == 401:
            raise JiraError("Authentication failed: check the username/password or the token.")
        if response.status_code == 403 and "CAPTCHA" in response.headers.get("X-Authentication-Denied-Reason", ""):
            raise JiraError("Jira requires a CAPTCHA: log in once in the browser, then try again.")
        if not response.ok:
            raise JiraError(f"{method} {path} failed ({response.status_code}): {_error_text(response)}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _myself(self) -> User:
        data = self._request("GET", "myself")
        return User(
            name=data.get("name") or data.get("accountId", ""),
            key=data.get("key") or data.get("accountId", ""),
            display_name=data.get("displayName") or data.get("name", ""),
        )

    def search(self, jql: str, fields: str = ISSUE_FIELDS) -> Iterator[dict[str, Any]]:
        start = 0
        while True:
            page = self._request(
                "POST",
                "search",
                json={"jql": jql, "startAt": start, "maxResults": PAGE_SIZE, "fields": fields.split(",")},
            )
            issues = page.get("issues", [])
            yield from issues
            start += len(issues)
            if not issues or start >= page.get("total", 0):
                return

    # -- reading ------------------------------------------------------------ #

    def projects(self) -> list[Project]:
        """The projects on the user's "Browse projects" page; all browsable ones if that page cannot be read.

        The page's list is computed server-side and matches no single REST permission, so it is read from the
        JSON the page embeds for its own JavaScript.
        """
        self.projects_warning = None
        try:
            projects = self._browse_page_projects()
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            projects, reason = [], str(error)
        else:
            reason = "the page listed no projects"
        if projects:
            return projects
        self.projects_warning = (
            f'Could not read your "Browse projects" list ({reason}); showing every project you can browse instead.'
        )
        return self.all_projects()

    def all_projects(self) -> list[Project]:
        data = self._request("GET", "project")
        return sorted((Project(p["key"], p.get("name", "")) for p in data), key=lambda p: p.key)

    def _browse_page_projects(self) -> list[Project]:
        response = self.session.get(
            f"{self.server}/secure/project/BrowseProjects.jspa",
            params={"s": "view_projects", "os_authType": "basic"},
            headers={"Accept": "text/html"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return parse_browse_projects(response.text)

    def project_issues(self, project: str, resolved_within_days: int) -> list[Issue]:
        jql = (
            f'project = "{project}" AND (statusCategory != Done OR resolved >= -{resolved_within_days}d) '
            "ORDER BY key DESC"
        )
        return [issue_from_json(i) for i in self.search(jql)]

    def assigned_issues(self) -> list[Issue]:
        jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
        return [issue_from_json(i) for i in self.search(jql)]

    def _issue_worklogs(self, issue_key: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self._request(
                "GET", f"issue/{issue_key}/worklog", params={"startAt": start, "maxResults": 1000}
            )
            worklogs = page.get("worklogs", [])
            result.extend(worklogs)
            start += len(worklogs)
            if not worklogs or start >= page.get("total", 0):
                return result

    def my_worklogs(
        self, first: date, last: date, progress: ProgressCallback | None = None
    ) -> tuple[list[Worklog], list[Issue]]:
        # worklogDate is evaluated in the user's Jira time zone; widen by a day each
        # side and filter precisely on the Budapest wall-clock date below.
        jql = (
            "worklogAuthor = currentUser() "
            f'AND worklogDate >= "{first - timedelta(days=1)}" AND worklogDate <= "{last + timedelta(days=1)}"'
        )
        issues = [issue_from_json(i) for i in self.search(jql)]
        done = 0
        if progress:
            progress(0, len(issues))

        def fetch(issue: Issue) -> list[Worklog]:
            return [
                worklog_from_json(issue.key, w)
                for w in self._issue_worklogs(issue.key)
                if self.user.owns(w.get("author") or {})
            ]

        worklogs: list[Worklog] = []
        with ThreadPoolExecutor(max_workers=WORKLOG_FETCH_THREADS) as pool:
            for batch in pool.map(fetch, issues):
                worklogs.extend(w for w in batch if first <= w.day <= last)
                done += 1
                if progress:
                    progress(done, len(issues))
        return worklogs, issues

    # -- writing ------------------------------------------------------------ #

    def create_worklog(self, worklog: Worklog) -> Worklog:
        data = self._request(
            "POST",
            f"issue/{worklog.issue_key}/worklog",
            params={"adjustEstimate": ADJUST_ESTIMATE},
            json=worklog_body(worklog),
        )
        return worklog_from_json(worklog.issue_key, data)

    def update_worklog(self, worklog: Worklog) -> Worklog:
        if worklog.worklog_id is None:
            raise JiraError("Only an existing worklog can be updated.")
        data = self._request(
            "PUT",
            f"issue/{worklog.issue_key}/worklog/{worklog.worklog_id}",
            params={"adjustEstimate": ADJUST_ESTIMATE},
            json=worklog_body(worklog),
        )
        return worklog_from_json(worklog.issue_key, data)

    def delete_worklog(self, issue_key: str, worklog_id: str) -> None:
        self._request(
            "DELETE",
            f"issue/{issue_key}/worklog/{worklog_id}",
            params={"adjustEstimate": ADJUST_ESTIMATE},
        )

    # -- meeting story ------------------------------------------------------ #

    def find_stories(self, project: str, summary: str) -> list[Issue]:
        """Issues of the project whose summary is exactly ``summary`` (case-insensitive), stories first."""
        wanted = summary.strip().casefold()
        escaped = summary.replace('"', '\\"')
        candidates: dict[str, Issue] = {}
        for jql in (
            f'project = "{project}" AND summary ~ "\\"{escaped}\\""',
            f'project = "{project}" AND summary ~ "{escaped.split()[-1]}"',
        ):
            for data in self.search(jql):
                issue = issue_from_json(data)
                if issue.summary.strip().casefold() == wanted:
                    candidates[issue.key] = issue
            if candidates:
                break
        return sorted(candidates.values(), key=lambda i: (i.issue_type != "Story", i.key))

    def children_with_comments(self, parent_key: str) -> list[ChildIssue]:
        children: list[ChildIssue] = []
        for data in self.search(f"parent = {parent_key}", fields=f"{ISSUE_FIELDS},updated,comment"):
            fields = data.get("fields") or {}
            block = fields.get("comment") or {}
            comments = [c.get("body") or "" for c in block.get("comments", [])]
            if block.get("total", 0) > len(comments):
                comments = self._all_comments(data["key"])
            children.append(ChildIssue(issue_from_json(data), fields.get("updated") or "", comments))
        return children

    def _all_comments(self, issue_key: str) -> list[str]:
        comments: list[str] = []
        start = 0
        while True:
            page = self._request("GET", f"issue/{issue_key}/comment", params={"startAt": start, "maxResults": 100})
            batch = page.get("comments", [])
            comments.extend(c.get("body") or "" for c in batch)
            start += len(batch)
            if not batch or start >= page.get("total", 0):
                return comments

    def _subtask_type(self, project: str) -> tuple[str, str]:
        data = self._request("GET", f"project/{project}")
        for issue_type in data.get("issueTypes", []):
            if issue_type.get("subtask"):
                return issue_type["id"], issue_type.get("name", "Sub-task")
        raise JiraError(f"Project {project} has no sub-task issue type.")

    def create_subtask(self, project: str, parent_key: str, summary: str, description: str) -> Issue:
        type_id, type_name = self._subtask_type(project)
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"id": type_id},
            "parent": {"key": parent_key},
            "summary": summary,
            "description": description,
            "assignee": {"name": self.user.name},
        }
        try:
            data = self._request("POST", "issue", json={"fields": fields})
        except JiraError as error:
            if "assignee" not in str(error):
                raise
            # Some create screens do not offer the assignee; create unassigned instead.
            fields = {k: v for k, v in fields.items() if k != "assignee"}
            data = self._request("POST", "issue", json={"fields": fields})
        return Issue(data["key"], summary, type_name, "Open", project)

    def add_comment(self, issue_key: str, body: str) -> None:
        self._request("POST", f"issue/{issue_key}/comment", json={"body": body})


def parse_browse_projects(page: str) -> list[Project]:
    """Projects embedded in BrowseProjects.jspa as a JSON string inside a JavaScript string literal."""
    match = BROWSE_PROJECTS_DATA.search(page)
    if match is None:
        raise ValueError("no project data on the page (not logged in, or the page has changed)")
    data = json.loads(json.loads(f'"{match.group(1)}"'))
    projects = {item["key"]: Project(item["key"], item.get("name", "")) for item in data if item.get("key")}
    return sorted(projects.values(), key=lambda p: p.key)


def _error_text(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] or response.reason
    messages = list(data.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return "; ".join(messages) or response.reason
