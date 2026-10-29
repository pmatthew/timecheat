"""Thin Jira Server / Data Center REST v2 client.

Only the handful of endpoints TimeCheat needs. Plain ``requests`` instead of the
``jira`` package keeps pagination, ``adjustEstimate`` and both auth schemes
explicit. TLS verification is off: on-prem Jira servers often use a certificate
from an internal CA that the trust store of a corporate laptop (and ``certifi``)
does not include.
"""

from __future__ import annotations

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
        data = self._request("GET", "project")
        return sorted((Project(p["key"], p.get("name", "")) for p in data), key=lambda p: p.key)

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


def _error_text(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] or response.reason
    messages = list(data.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return "; ".join(messages) or response.reason
