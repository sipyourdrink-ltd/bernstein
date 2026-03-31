"""Unit tests for selected PostgreSQL task-store behaviors."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

import bernstein.core.store_postgres as store_postgres
from bernstein.core.models import TaskStatus, TaskType


class _AcquireContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakePool:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


class _FakeRedis:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[tuple[str, str]] = []

    async def acquire(self, task_id: str) -> str:
        self.acquired.append(task_id)
        return "lock-token"

    async def release(self, task_id: str, token: str) -> bool:
        self.released.append((task_id, token))
        return True


def _task_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "task-1",
        "title": "Review change",
        "description": "desc",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "status": "open",
        "task_type": "upgrade_proposal",
        "upgrade_details": {
            "current_state": "old",
            "proposed_change": "new",
            "risk_assessment": {"level": "high"},
            "rollback_plan": {"steps": ["revert"]},
        },
        "depends_on": [],
        "owned_files": ["src/demo.py"],
        "assigned_agent": None,
        "result_summary": None,
        "cell_id": None,
        "model": "sonnet",
        "effort": "high",
        "completion_signals": [{"type": "path_exists", "value": "src/demo.py"}],
        "created_at": 1.0,
        "progress_log": [{"message": "started"}],
        "version": 3,
    }
    row.update(overrides)
    return row


def test_row_to_task_parses_upgrade_details_and_completion_signals() -> None:
    task = store_postgres._row_to_task(_task_row())

    assert task.status is TaskStatus.OPEN
    assert task.task_type is TaskType.UPGRADE_PROPOSAL
    assert task.upgrade_details is not None
    assert task.upgrade_details.risk_assessment.level == "high"
    assert [(signal.type, signal.value) for signal in task.completion_signals] == [("path_exists", "src/demo.py")]


def test_claim_by_id_releases_distributed_lock_on_version_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        async def fetchrow(self, query: str, *args: object) -> object | None:
            if "AND    version = $2" in query:
                return None
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            if "SELECT 1 FROM tasks" in query:
                return 1
            if "SELECT version FROM tasks" in query:
                return 7
            raise AssertionError(f"unexpected fetchval query: {query}")

    redis = _FakeRedis()
    store = store_postgres.PostgresTaskStore("postgresql://example", redis_coordinator=cast("Any", redis))
    cast("Any", store)._pool = _FakePool(_Conn())

    with pytest.raises(ValueError, match="Version conflict"):
        asyncio.run(store.claim_by_id("task-1", expected_version=3))

    assert redis.acquired == ["task-1"]
    assert redis.released == [("task-1", "lock-token")]


def test_claim_next_reopens_task_when_dependencies_are_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        def __init__(self) -> None:
            self.reopened = False

        async def fetchrow(self, query: str, *args: object) -> object | None:
            if "UPDATE tasks" in query and "FOR    UPDATE SKIP LOCKED" in query:
                return _task_row(status="claimed", depends_on=["dep-1"])
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            if "status = 'done'" in query:
                return []
            raise AssertionError(f"unexpected fetch query: {query}")

        async def execute(self, query: str, *args: object) -> None:
            assert "SET status='open'" in query
            self.reopened = True

    conn = _Conn()
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    claimed = asyncio.run(store.claim_next("backend"))

    assert claimed is None
    assert conn.reopened is True


def test_claim_by_id_raises_key_error_when_task_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        async def fetchrow(self, query: str, *args: object) -> object | None:
            if "AND    status = 'open'" in query:
                return None
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            if "SELECT 1 FROM tasks" in query:
                return None
            raise AssertionError(f"unexpected fetchval query: {query}")

    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(_Conn())

    with pytest.raises(KeyError):
        asyncio.run(store.claim_by_id("missing-task"))


def test_claim_by_id_without_version_returns_existing_non_open_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        def __init__(self) -> None:
            self.calls = 0

        async def fetchrow(self, query: str, *args: object) -> object | None:
            self.calls += 1
            if self.calls == 1 and "AND    status = 'open'" in query:
                return None
            if self.calls == 2 and "SELECT * FROM tasks" in query:
                return _task_row(status="claimed")
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            if "SELECT 1 FROM tasks" in query:
                return 1
            raise AssertionError(f"unexpected fetchval query: {query}")

    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(_Conn())

    task = asyncio.run(store.claim_by_id("task-1"))

    assert task.status is TaskStatus.CLAIMED
