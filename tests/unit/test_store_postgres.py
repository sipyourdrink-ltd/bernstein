"""Unit tests for selected PostgreSQL task-store behaviors."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import Any, cast

import bernstein.core.store_postgres as store_postgres
import pytest
from bernstein.core.models import TaskStatus, TaskType


class _AcquireContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeTransaction:
    def __init__(self, conn: _TxAware | None = None) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeTransaction:
        if self._conn is not None:
            self._conn.transaction_entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._conn is not None:
            self._conn.transaction_exited = True
        return None


class _TxAware:
    """Protocol-style marker for test connections that track transactions."""

    transaction_entered: bool = False
    transaction_exited: bool = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


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
        await asyncio.sleep(0)  # Async interface requirement
        self.acquired.append(task_id)
        return "lock-token"

    async def release(self, task_id: str, token: str) -> bool:
        await asyncio.sleep(0)  # Async interface requirement
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
            await asyncio.sleep(0)  # Async interface requirement
            if "AND    version = $2" in query:
                return None
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            await asyncio.sleep(0)  # Async interface requirement
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


def test_claim_next_filters_unmet_dependencies_in_single_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dependency filtering is embedded in ``_CLAIM_NEXT_SQL`` - when the
    subquery returns no eligible row, ``claim_next`` returns ``None`` without
    executing any follow-up re-open statement.  The whole call must run
    inside a single acquired connection + transaction.
    """
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    # Verify the SQL embeds the dependency subquery so re-open is unreachable.
    assert "NOT EXISTS" in store_postgres._CLAIM_NEXT_SQL
    assert "unnest(c.depends_on)" in store_postgres._CLAIM_NEXT_SQL

    class _Conn(_TxAware):
        def __init__(self) -> None:
            self.fetchrow_calls: list[str] = []
            self.execute_calls: list[str] = []

        async def fetchrow(self, query: str, *args: object) -> object | None:
            await asyncio.sleep(0)  # Async interface requirement
            self.fetchrow_calls.append(query)
            # Dependency not met → subquery returns no id → UPDATE matches no
            # row → RETURNING yields nothing.
            return None

        async def execute(self, query: str, *args: object) -> None:  # pragma: no cover
            await asyncio.sleep(0)  # Async interface requirement
            self.execute_calls.append(query)

    conn = _Conn()
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    claimed = asyncio.run(store.claim_next("backend"))

    assert claimed is None
    assert len(conn.fetchrow_calls) == 1
    assert "FOR    UPDATE SKIP LOCKED" in conn.fetchrow_calls[0]
    # No re-open - the race-prone second connection is gone.
    assert conn.execute_calls == []
    # Transaction was entered (and exited) exactly once.
    assert conn.transaction_entered is True
    assert conn.transaction_exited is True


def test_claim_next_returns_task_when_subquery_filters_return_eligible_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SQL selects and claims a row, ``claim_next`` returns the
    mapped Task without any extra dependency round-trip.
    """
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn(_TxAware):
        def __init__(self) -> None:
            self.fetch_calls = 0
            self.execute_calls = 0

        async def fetchrow(self, query: str, *args: object) -> object | None:
            await asyncio.sleep(0)  # Async interface requirement
            self.fetch_calls += 1
            assert "FOR    UPDATE SKIP LOCKED" in query
            return _task_row(status="claimed", depends_on=["dep-1"])

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:  # pragma: no cover
            await asyncio.sleep(0)  # Async interface requirement
            raise AssertionError("claim_next should not run a second dependency fetch")

        async def execute(self, query: str, *args: object) -> None:  # pragma: no cover
            await asyncio.sleep(0)  # Async interface requirement
            self.execute_calls += 1

    conn = _Conn()
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    task = asyncio.run(store.claim_next("backend"))

    assert task is not None
    assert task.status is TaskStatus.CLAIMED
    assert conn.fetch_calls == 1
    assert conn.execute_calls == 0
    assert conn.transaction_entered is True
    assert conn.transaction_exited is True


def test_claim_by_id_raises_key_error_when_task_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        async def fetchrow(self, query: str, *args: object) -> object | None:
            await asyncio.sleep(0)  # Async interface requirement
            if "AND    status = 'open'" in query:
                return None
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            await asyncio.sleep(0)  # Async interface requirement
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
            await asyncio.sleep(0)  # Async interface requirement
            self.calls += 1
            if self.calls == 1 and "AND    status = 'open'" in query:
                return None
            if self.calls == 2 and "SELECT * FROM tasks" in query:
                return _task_row(status="claimed")
            raise AssertionError(f"unexpected fetchrow query: {query}")

        async def fetchval(self, query: str, *args: object) -> object:
            await asyncio.sleep(0)  # Async interface requirement
            if "SELECT 1 FROM tasks" in query:
                return 1
            raise AssertionError(f"unexpected fetchval query: {query}")

    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(_Conn())

    task = asyncio.run(store.claim_by_id("task-1"))

    assert task.status is TaskStatus.CLAIMED


def test_list_tasks_limit_and_offset_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []
            self.all_rows = [_task_row(id=f"task-{i}", title=f"Task {i}") for i in range(10)]

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            await asyncio.sleep(0)
            self.queries.append((query, args))
            if "FROM tasks" in query:
                # Emulate PostgreSQL parameterized LIMIT and OFFSET
                offset_val = 0
                limit_val = len(self.all_rows)
                for i, arg in enumerate(args, start=1):
                    if f"LIMIT ${i}" in query:
                        limit_val = int(arg)  # type: ignore[arg-type]
                    elif f"OFFSET ${i}" in query:
                        offset_val = int(arg)  # type: ignore[arg-type]
                return self.all_rows[offset_val : offset_val + limit_val]
            return []

    conn = _Conn()
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    # 1. Limit bound: inserting 10 rows, list_tasks(limit=3) returns exactly 3 rows
    tasks = asyncio.run(store.list_tasks(limit=3))
    assert len(tasks) == 3
    assert [t.id for t in tasks] == ["task-0", "task-1", "task-2"]
    assert "LIMIT $1" in conn.queries[-1][0]
    assert conn.queries[-1][1] == (3,)

    # 2. Offset bound: list_tasks(limit=3, offset=3) skips appropriately
    tasks_page2 = asyncio.run(store.list_tasks(limit=3, offset=3))
    assert len(tasks_page2) == 3
    assert [t.id for t in tasks_page2] == ["task-3", "task-4", "task-5"]
    assert "LIMIT $1" in conn.queries[-1][0]
    assert "OFFSET $2" in conn.queries[-1][0]
    assert conn.queries[-1][1] == (3, 3)

    # 3. Filtering with status + cell_id + limit + offset
    asyncio.run(store.list_tasks(status="claimed", cell_id="cell-1", limit=2, offset=4))
    assert "WHERE status = $1 AND cell_id = $2" in conn.queries[-1][0]
    assert "LIMIT $3" in conn.queries[-1][0]
    assert "OFFSET $4" in conn.queries[-1][0]
    assert conn.queries[-1][1] == ("claimed", "cell-1", 2, 4)

    # 4. Status="open" with limit
    tasks_open = asyncio.run(store.list_tasks(status="open", limit=2))
    assert len(tasks_open) == 2
    assert "WHERE status = $1" in conn.queries[-2][0]
    assert "LIMIT $2" in conn.queries[-2][0]
    assert conn.queries[-2][1] == ("open", 2)
    assert conn.queries[-1][0] == "SELECT id FROM tasks WHERE status='done'"


def test_list_tasks_bounded_by_default_without_explicit_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_tasks() must not fetch the entire table when the caller omits limit."""
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)
    bound = store_postgres._LIST_TASKS_DEFAULT_LIMIT

    class _Conn:
        def __init__(self, row_count: int) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []
            self.all_rows = [_task_row(id=f"task-{i}", title=f"Task {i}") for i in range(row_count)]

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            await asyncio.sleep(0)
            self.queries.append((query, args))
            if "FROM tasks" in query:
                # Emulate PostgreSQL: only ever return up to the requested LIMIT.
                limit_val = len(self.all_rows)
                for i, arg in enumerate(args, start=1):
                    if f"LIMIT ${i}" in query:
                        limit_val = int(arg)  # type: ignore[arg-type]
                return self.all_rows[:limit_val]
            return []

    conn = _Conn(row_count=bound + 1)
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    tasks = asyncio.run(store.list_tasks())

    assert len(tasks) == bound
    assert f"LIMIT ${1}" in conn.queries[-1][0]
    assert conn.queries[-1][1] == (bound,)


def test_read_archive_bound_keeps_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []
            self.all_rows = [
                {
                    "task_id": f"task-{i}",
                    "title": f"Archived {i}",
                    "role": "backend",
                    "status": "done",
                    "created_at": 100.0 + i,
                    "completed_at": 200.0 + i,
                    "duration_seconds": 100.0,
                    "result_summary": "ok",
                    "cost_usd": 0.01,
                }
                for i in range(10)
            ]

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            await asyncio.sleep(0)
            self.queries.append((query, args))
            if "FROM   task_archive" in query:
                limit = int(args[0]) if args else len(self.all_rows)  # type: ignore[arg-type]
                desc_rows = sorted(self.all_rows, key=lambda r: float(r["completed_at"]), reverse=True)[:limit]
                return desc_rows
            return []

    conn = _Conn()
    store = store_postgres.PostgresTaskStore("postgresql://example")
    cast("Any", store)._pool = _FakePool(conn)

    records = asyncio.run(store.read_archive(limit=5))
    assert len(records) == 5
    assert "LIMIT  $1" in conn.queries[0][0]
    assert conn.queries[0][1] == (5,)


def test_postgres_count_tasks_executes_select_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_postgres, "_ASYNCPG_AVAILABLE", True)

    class _Conn:
        def __init__(self) -> None:
            self.executed_query: str | None = None
            self.executed_params: tuple[object, ...] = ()

        async def fetchval(self, query: str, *args: object) -> object:
            await asyncio.sleep(0)
            self.executed_query = query
            self.executed_params = args
            return 42

    store = store_postgres.PostgresTaskStore("postgresql://example")
    conn = _Conn()
    cast("Any", store)._pool = _FakePool(conn)

    count = asyncio.run(store.count_tasks(status="done", cell_id="cell-1", tenant_id="tenant-a"))

    assert count == 42
    assert conn.executed_query == "SELECT COUNT(*) FROM tasks WHERE status = $1 AND cell_id = $2 AND tenant_id = $3"
    assert conn.executed_params == ("done", "cell-1", "tenant-a")
