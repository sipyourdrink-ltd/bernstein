"""Administrative containers projected from the existing authenticated audit chain."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from bernstein.core.security.audit_chain import AuditChainStore


class AdministrativeRunStatus(BaseModel):
    """Authenticated task aggregate returned by administrative run surfaces."""

    runId: str
    template: Literal["administrative"] = "administrative"
    goal: str
    tenant_id: str
    status: Literal["working", "completed"]
    tasks: list[dict[str, str]]
    chainHead: str
    chainVerified: Literal[True]
    receiptHash: str


class AdministrativeRuns:
    """Durable run membership; every accepted state is covered by an HMAC event."""

    def __init__(
        self, chain: AuditChainStore, get_task: Callable[[str], Any], list_tasks: Callable[[], Iterable[Any]]
    ) -> None:
        self.chain = chain
        self.get_task = get_task
        self.list_tasks = list_tasks

    def _events(self) -> list[Any]:
        ok, errors, events = self.chain.verify_and_query(include_archived=True)
        if not ok:
            raise ValueError(f"Administrative audit verification failed: {errors}")
        return events

    def _fold(self, run_id: str, tenant: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        for event in self._events():
            if event.resource_id != run_id or event.resource_type != "administrative_run":
                continue
            data = event.details
            if event.event_type == "administrative.run_created":
                result = {
                    "runId": run_id,
                    "template": "administrative",
                    "goal": data["goal"],
                    "tenant_id": data["tenant_id"],
                    "status": "working",
                    "tasks": {},
                }
            elif result is not None and event.event_type in (
                "administrative.task_added",
                "administrative.task_updated",
            ):
                result["tasks"][data["task"]["id"]] = data["task"]
            elif result is not None and event.event_type == "administrative.run_closed":
                result["status"] = "completed"
        if result is None or result["tenant_id"] != tenant:
            raise KeyError("Administrative run not found")
        return result

    def _record(self, run_id: str, kind: str, details: dict[str, Any]) -> None:
        self.chain.log_with_prev_digest(
            event_type=f"administrative.{kind}",
            actor="administrative-runs",
            resource_type="administrative_run",
            resource_id=run_id,
            details=details,
        )

    @staticmethod
    def _snapshot(task: Any) -> dict[str, str]:
        return {"id": task.id, "title": task.title, "status": task.status.value, "tenant_id": task.tenant_id}

    def create(self, goal: str, tenant: str) -> dict[str, Any]:
        with self.chain.chain_transaction():
            self._events()
            run_id = f"admin-{uuid.uuid4().hex}"
            self._record(run_id, "run_created", {"goal": goal, "tenant_id": tenant})
            return self.status(run_id, tenant)

    def require_open(self, run_id: str, tenant: str) -> None:
        if self._fold(run_id, tenant)["status"] == "completed":
            raise ValueError("Administrative run is closed")

    def add(self, run_id: str, task: Any, tenant: str) -> None:
        with self.chain.chain_transaction():
            self.require_open(run_id, tenant)
            if task.tenant_id != tenant:
                raise ValueError("Task tenant differs from run tenant")
            state = self._fold(run_id, tenant)
            if task.id in state["tasks"]:
                raise ValueError("Task already belongs to run")
            self._record(run_id, "task_added", {"task": self._snapshot(task)})
            self._close_if_complete(run_id, tenant)

    def _close_if_complete(self, run_id: str, tenant: str) -> None:
        state = self._fold(run_id, tenant)
        if (
            state["status"] != "completed"
            and state["tasks"]
            and all(row["status"] in {"done", "cancelled", "closed"} for row in state["tasks"].values())
        ):
            self._record(run_id, "run_closed", {"task_ids": sorted(state["tasks"])})

    def persist_task(self, run_id: str, task: Any) -> None:
        """Called by the store after each durable administrative task write."""
        with self.chain.chain_transaction():
            state = self._fold(run_id, task.tenant_id)
            if task.id not in state["tasks"]:
                self.add(run_id, task, task.tenant_id)
            else:
                self.task_updated(task)

    def task_updated(self, task: Any) -> None:
        with self.chain.chain_transaction():
            # Membership is authenticated independently of mutable task metadata.
            run_ids = {
                e.resource_id
                for e in self._events()
                if e.event_type == "administrative.task_added" and e.details["task"]["id"] == task.id
            }
            for run_id in run_ids:
                state = self._fold(run_id, task.tenant_id)
                if state["tasks"][task.id] == self._snapshot(task):
                    continue
                if state["status"] == "completed" and task.status.value not in {"done", "cancelled", "closed"}:
                    raise ValueError("Administrative run is closed; task snapshot changed")
                self._record(run_id, "task_updated", {"task": self._snapshot(task)})
                self._close_if_complete(run_id, task.tenant_id)

    def status(self, run_id: str, tenant: str) -> dict[str, Any]:
        with self.chain.chain_transaction():
            state = self._fold(run_id, tenant)
            actual_ids = {
                task.id
                for task in self.list_tasks()
                if getattr(task, "metadata", {}).get("administrative_run_id") == run_id
            }
            if actual_ids - set(state["tasks"]):
                raise ValueError("Administrative membership snapshot differs from live task store")
            for row in state["tasks"].values():
                task = self.get_task(row["id"])
                if task is None or self._snapshot(task) != row:
                    raise ValueError("Administrative task snapshot differs from live task store")
            state["tasks"] = sorted(state["tasks"].values(), key=lambda row: row["id"])
            state["chainHead"] = self.chain.resync_head()
            state["chainVerified"] = True
            state["receiptHash"] = hashlib.sha256(
                json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return state
