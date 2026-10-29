"""JiraClient against a stubbed HTTP session: endpoints, params and auth."""

from datetime import date, datetime
from typing import Any

import pytest
import requests

from timecheat.jira_client import JiraClient, JiraError
from timecheat.model import Worklog


class FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload
        self.ok = status < 400
        self.content = b"x" if payload is not None else b""
        self.headers: dict[str, str] = {}
        self.reason = "reason"
        self.text = ""

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    log: list[tuple[str, str, dict[str, Any]]] = []
    worklog = {
        "id": "501", "started": "2026-10-28T09:00:00.000+0100", "timeSpentSeconds": 3600, "comment": "c",
        "author": {"name": "me", "key": "me"},
    }
    other = dict(worklog, id="502", author={"name": "someone", "key": "someone"})
    routes = {
        ("GET", "myself"): {"name": "me", "key": "me", "displayName": "Me"},
        ("POST", "search"): {"total": 1, "issues": [{"key": "ACME-1", "fields": {
            "summary": "S", "issuetype": {"name": "Story"}, "status": {"name": "Open"}, "project": {"key": "ACME"}}}]},
        ("GET", "issue/ACME-1/worklog"): {"total": 2, "worklogs": [worklog, other]},
        ("POST", "issue/ACME-1/worklog"): dict(worklog, id="600"),
        ("PUT", "issue/ACME-1/worklog/501"): worklog,
        ("DELETE", "issue/ACME-1/worklog/501"): None,
    }

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("/rest/api/2/", 1)[1]
        log.append((method, path, kwargs))
        if (method, path) not in routes:
            return FakeResponse(404, {"errorMessages": ["nope"]})
        payload = routes[(method, path)]
        return FakeResponse(204 if payload is None else 200, payload)

    monkeypatch.setattr(requests.Session, "request", request)
    return log


def test_token_auth_and_my_worklogs(calls):
    client = JiraClient("https://jira.example/", token="secret")
    assert client.session.headers["Authorization"] == "Bearer secret"
    assert client.session.verify is False
    worklogs, issues = client.my_worklogs(date(2026, 10, 1), date(2026, 10, 31))
    assert [w.worklog_id for w in worklogs] == ["501"]
    assert worklogs[0].start == datetime(2026, 10, 28, 9)
    assert issues[0].issue_type == "Story"
    jql = next(kw["json"]["jql"] for m, p, kw in calls if p == "search")
    assert 'worklogDate >= "2026-09-30"' in jql and "worklogAuthor = currentUser()" in jql


def test_write_operations_use_adjust_estimate(calls):
    client = JiraClient("https://jira.example", username="me", password="pw")
    assert client.session.auth == ("me", "pw")
    draft = Worklog("draft-1", "ACME-1", datetime(2026, 10, 28, 9), 3600, "c")
    created = client.create_worklog(draft)
    assert created.worklog_id == "600"
    client.update_worklog(Worklog("jira-501", "ACME-1", datetime(2026, 10, 28, 9), 3600, "c", "501"))
    client.delete_worklog("ACME-1", "501")
    writes = [(m, kw) for m, p, kw in calls if m in ("POST", "PUT", "DELETE") and "worklog" in p]
    assert all(kw["params"] == {"adjustEstimate": "auto"} for _, kw in writes)
    assert writes[0][1]["json"] == {"started": "2026-10-28T09:00:00.000+0100", "timeSpentSeconds": 3600,
                                    "comment": "c"}


def test_errors_are_readable(calls, monkeypatch):
    client = JiraClient("https://jira.example", token="t")
    with pytest.raises(JiraError, match="nope"):
        client.delete_worklog("XX-1", "1")
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: FakeResponse(401))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"versionNumbers": [9, 4, 0]}))
    with pytest.raises(JiraError, match="Authentication failed"):
        JiraClient("https://jira.example", token="bad")


def test_token_on_jira_older_than_8_14_is_explained(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: FakeResponse(401))
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"versionNumbers": [8, 13, 5]}))
    with pytest.raises(JiraError, match=r"8\.13\.5\) does not support personal access tokens"):
        JiraClient("https://jira.example", token="tok")
    with pytest.raises(JiraError, match="Authentication failed"):
        JiraClient("https://jira.example", username="u", password="p")


# --------------------------------------------------------------------------- #
# project list from the "Browse projects" page
# --------------------------------------------------------------------------- #


def browse_page(projects: list[dict[str, Any]]) -> str:
    """Mimic BrowseProjects.jspa: a JSON string embedded in a JavaScript string literal."""
    import json

    literal = json.dumps(json.dumps(projects, ensure_ascii=False), ensure_ascii=False)
    return (
        "<html><script>WRM._unparsedData[\"com.atlassian.jira.project.browse:categories\"]=\"[]\";\n"
        f"WRM._unparsedData[\"com.atlassian.jira.project.browse:projects\"]={literal};\n"
        "WRM._unparsedData[\"com.atlassian.jira.project.browse:selectedCategory\"]=\"\\\"all\\\"\";</script></html>"
    )


def stub_jira(monkeypatch, page_status: int, page: str, all_projects: list[dict[str, Any]]):
    log: list[tuple[str, dict[str, Any]]] = []

    def request(self, method, url, timeout=None, **kwargs):
        log.append((url, kwargs))
        if url.endswith("/secure/project/BrowseProjects.jspa"):
            response = FakeResponse(page_status)
            response.text = page
            return response
        path = url.split("/rest/api/2/", 1)[1]
        if path == "myself":
            return FakeResponse(200, {"name": "me", "key": "me", "displayName": "Me"})
        if path == "project":
            return FakeResponse(200, all_projects)
        return FakeResponse(404, {"errorMessages": ["nope"]})

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error")

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(FakeResponse, "raise_for_status", raise_for_status, raising=False)
    return log


ALL = [{"key": k, "name": k.lower()} for k in ("AAA", "ALPHA", "BETA", "GAMMA", "OMEGA", "ZZZ")]


def test_projects_come_from_the_browse_page(monkeypatch):
    mine = [
        {"key": "GAMMA", "name": "Gamma \"Factory\" – Ünnep", "id": 3, "projectCategoryId": 10},
        {"key": "BETA", "name": "Beta Squad", "id": 2},
        {"key": "ALPHA", "name": "Alpha / Ops", "id": 1},
    ]
    log = stub_jira(monkeypatch, 200, browse_page(mine), ALL)
    client = JiraClient("https://jira.example", username="me", password="pw")
    projects = client.projects()
    assert [p.key for p in projects] == ["ALPHA", "BETA", "GAMMA"]
    assert projects[2].name == "Gamma \"Factory\" – Ünnep"
    assert client.projects_warning is None
    url, kwargs = next((u, k) for u, k in log if "BrowseProjects" in u)
    assert kwargs["params"] == {"s": "view_projects", "os_authType": "basic"}
    assert kwargs["headers"]["Accept"] == "text/html"
    assert not any(u.endswith("/rest/api/2/project") for u, _ in log)


@pytest.mark.parametrize("status, page", [
    (500, "error"),
    (200, "<html>login form, no project data</html>"),
    (200, browse_page([])),
])
def test_projects_fall_back_to_all_browsable(monkeypatch, status, page):
    stub_jira(monkeypatch, status, page, ALL)
    client = JiraClient("https://jira.example", username="me", password="pw")
    assert [p.key for p in client.projects()] == [p["key"] for p in ALL]
    assert client.projects_warning and "Browse projects" in client.projects_warning
