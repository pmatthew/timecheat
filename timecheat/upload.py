"""Turn pending changes into Jira operations and run them one by one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .jira_client import JiraBackend
from .model import Change, Timesheet, Worklog

Action = Literal["create", "update", "delete"]


@dataclass(frozen=True)
class Operation:
    action: Action
    worklog: Worklog
    change: Change

    @property
    def note(self) -> str:
        if self.change.kind == "move":
            before = self.change.before
            after = self.change.after
            assert before is not None and after is not None
            return f"moved from {before.issue_key}" if self.action == "create" else f"moved to {after.issue_key}"
        if self.action == "update" and self.change.before is not None:
            before = self.change.before
            return f"was {before.start:%H:%M}, {before.seconds // 60} min"
        return ""


@dataclass(frozen=True)
class OperationResult:
    operation: Operation
    ok: bool
    error: str = ""


def build_plan(changes: list[Change]) -> list[Operation]:
    unassigned = [c for c in changes if c.after is not None and not c.after.is_assigned]
    if unassigned:
        raise ValueError(f"{len(unassigned)} entries have no Jira issue assigned yet.")
    plan: list[Operation] = []
    for change in changes:
        if change.kind == "create":
            assert change.after is not None
            plan.append(Operation("create", change.after, change))
        elif change.kind == "update":
            assert change.after is not None
            plan.append(Operation("update", change.after, change))
        elif change.kind == "delete":
            assert change.before is not None
            plan.append(Operation("delete", change.before, change))
        else:
            # Create first: if it fails, the original worklog is still there.
            assert change.before is not None and change.after is not None
            plan.append(Operation("create", change.after, change))
            plan.append(Operation("delete", change.before, change))
    return plan


def call_jira(client: JiraBackend, operation: Operation) -> Worklog | None:
    """The network half of an operation; safe to run in a worker thread."""
    if operation.action == "create":
        return client.create_worklog(operation.worklog)
    if operation.action == "update":
        return client.update_worklog(operation.worklog)
    assert operation.worklog.worklog_id is not None
    client.delete_worklog(operation.worklog.issue_key, operation.worklog.worklog_id)
    return None


def record(timesheet: Timesheet, operation: Operation, returned: Worklog | None) -> None:
    """The bookkeeping half: make the working copy reflect what Jira now holds."""
    if operation.action == "create":
        assert returned is not None
        timesheet.confirm_create(operation.change.uid, returned)
    elif operation.action == "update":
        assert returned is not None
        timesheet.confirm_update(returned)
    else:
        timesheet.confirm_delete(operation.worklog.uid)


class PlanRunner:
    """Skips the rest of a change once one of its steps failed (a move's delete after a failed create)."""

    def __init__(self, client: JiraBackend, timesheet: Timesheet) -> None:
        self.client = client
        self.timesheet = timesheet
        self.failed: set[str] = set()

    def skip_reason(self, operation: Operation) -> str | None:
        if operation.change.uid in self.failed:
            return "skipped: the previous step of this change failed"
        return None

    def finish(self, operation: Operation, returned: Worklog | None, error: Exception | None) -> OperationResult:
        if error is not None:
            self.failed.add(operation.change.uid)
            return OperationResult(operation, False, str(error))
        record(self.timesheet, operation, returned)
        return OperationResult(operation, True)

    def run(self, operation: Operation) -> OperationResult:
        """Synchronous variant, used by tests and scripts."""
        reason = self.skip_reason(operation)
        if reason:
            return OperationResult(operation, False, reason)
        try:
            returned = call_jira(self.client, operation)
        except Exception as error:  # noqa: BLE001 - every failure is reported to the user
            return self.finish(operation, None, error)
        return self.finish(operation, returned, None)
