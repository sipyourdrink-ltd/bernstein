"""TEST-013: Snapshot tests for CLI output formatting.

Captures expected output from key CLI-facing formatters and compares
against stored snapshots.  If output changes, the test fails so you
can review and update the snapshot.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.models import Task, TaskStatus, TaskType

# ---------------------------------------------------------------------------
# Snapshot directory
# ---------------------------------------------------------------------------

_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "golden" / "cli_snapshots"


def _read_snapshot(name: str) -> str | None:
    path = _SNAPSHOT_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_snapshot(name: str, content: str) -> None:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _SNAPSHOT_DIR / name
    path.write_text(content, encoding="utf-8")


def _assert_snapshot(name: str, actual: str) -> None:
    """Compare actual output against stored snapshot.

    On first run (no snapshot file), writes the snapshot.
    On subsequent runs, asserts equality.
    """
    existing = _read_snapshot(name)
    if existing is None:
        _write_snapshot(name, actual)
        return
    assert actual == existing, (
        f"Snapshot mismatch for {name!r}. If the change is intentional, delete the snapshot file and re-run."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_task(
    *,
    status: TaskStatus = TaskStatus.OPEN,
    role: str = "backend",
    priority: int = 2,
) -> Task:
    return Task(
        id="task-001",
        title="Implement login endpoint",
        description="Add POST /auth/login with JWT",
        role=role,
        priority=priority,
        status=status,
        task_type=TaskType.STANDARD,
        tenant_id="default",
        created_at=1700000000.0,
    )


# ---------------------------------------------------------------------------
# Snapshot: TaskResponse JSON serialization
# ---------------------------------------------------------------------------


class TestTaskResponseSnapshot:
    """Snapshot the JSON shape returned by the task API."""

    def test_task_response_shape(self) -> None:
        """The JSON serialization of TaskResponse must be stable."""
        from bernstein.core.server import TaskResponse

        task = _sample_task()
        resp = TaskResponse(
            id=task.id,
            title=task.title,
            description=task.description,
            role=task.role,
            tenant_id=task.tenant_id,
            priority=task.priority,
            scope=task.scope.value,
            complexity=task.complexity.value,
            eu_ai_act_risk="minimal",
            approval_required=False,
            risk_level="low",
            estimated_minutes=30,
            status=task.status.value,
            depends_on=[],
            parent_task_id=None,
            depends_on_repo=None,
            owned_files=[],
            assigned_agent=None,
            result_summary=None,
            cell_id=None,
            repo=None,
            task_type="standard",
            upgrade_details=None,
            model=None,
            effort=None,
            created_at=task.created_at,
        )
        actual = json.dumps(resp.model_dump(), indent=2, sort_keys=True)
        _assert_snapshot("task_response.json", actual)


class TestStatusResponseSnapshot:
    """Snapshot the status endpoint response shape."""

    def test_status_response_shape(self) -> None:
        from bernstein.core.server import RoleCounts, StatusResponse

        resp = StatusResponse(
            total=5,
            open=2,
            claimed=1,
            done=1,
            failed=1,
            per_role=[
                RoleCounts(role="backend", open=1, claimed=1, done=0, failed=1),
                RoleCounts(role="qa", open=1, claimed=0, done=1, failed=0),
            ],
            total_cost_usd=1.23,
        )
        actual = json.dumps(resp.model_dump(), indent=2, sort_keys=True)
        _assert_snapshot("status_response.json", actual)


class TestHealthResponseSnapshot:
    """Snapshot the health endpoint response shape."""

    def test_health_response_shape(self) -> None:
        from bernstein.core.server import HealthResponse

        resp = HealthResponse(
            status="ok",
            uptime_s=3600.0,
            task_count=10,
            agent_count=3,
            task_queue_depth=5,
            memory_mb=128.0,
        )
        actual = json.dumps(resp.model_dump(), indent=2, sort_keys=True)
        _assert_snapshot("health_response.json", actual)
