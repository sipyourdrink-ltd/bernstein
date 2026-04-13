"""PostgreSQL task store backend.

Drop-in replacement for the in-memory :class:`~bernstein.core.server.TaskStore`
that stores all state in PostgreSQL.  Optionally integrates with
:class:`~bernstein.core.store_redis.RedisCoordinator` for distributed locking
when multiple Bernstein instances share the same database.

Usage::

    store = PostgresTaskStore(
        dsn="postgresql://user:pass@localhost/bernstein",
        redis_url="redis://localhost:6379/0",  # optional
    )
    app = create_app(store=store)

Environment variables::

    BERNSTEIN_DATABASE_URL   — PostgreSQL DSN (enables postgres backend)
    BERNSTEIN_REDIS_URL      — Redis URL for distributed locking (optional)
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal, cast

from bernstein.core.models import (
    CompletionSignal,
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)
from bernstein.core.persistence.store import BaseTaskStore, RoleSummary, StatusSummary

if TYPE_CHECKING:
    from bernstein.core.persistence.store_redis import RedisCoordinator
    from bernstein.core.server import ArchiveRecord, TaskCreate

_ARCHIVE_INSERT_SQL = """
                INSERT INTO task_archive
                    (task_id, title, role, status, created_at, completed_at,
                     duration_seconds, result_summary, cost_usd)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """

logger = logging.getLogger(__name__)

# ``asyncpg`` is an optional dependency — only required for postgres mode.
try:
    import asyncpg  # type: ignore[import-untyped]

    _has_asyncpg = True
except ModuleNotFoundError:
    asyncpg = None  # type: ignore[assignment]
    _has_asyncpg = False

_ASYNCPG_AVAILABLE: bool = _has_asyncpg


# ---------------------------------------------------------------------------
# DDL — created on startup if not present
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT        PRIMARY KEY,
    title             TEXT        NOT NULL,
    description       TEXT        NOT NULL,
    role              TEXT        NOT NULL,
    priority          INTEGER     NOT NULL DEFAULT 2,
    scope             TEXT        NOT NULL DEFAULT 'medium',
    complexity        TEXT        NOT NULL DEFAULT 'medium',
    estimated_minutes INTEGER     NOT NULL DEFAULT 30,
    status            TEXT        NOT NULL DEFAULT 'open',
    task_type         TEXT        NOT NULL DEFAULT 'standard',
    upgrade_details   JSONB,
    depends_on        TEXT[]      NOT NULL DEFAULT '{}',
    owned_files       TEXT[]      NOT NULL DEFAULT '{}',
    assigned_agent    TEXT,
    result_summary    TEXT,
    cell_id           TEXT,
    model             TEXT,
    effort            TEXT,
    completion_signals JSONB      NOT NULL DEFAULT '[]',
    created_at        FLOAT8      NOT NULL,
    progress_log      JSONB       NOT NULL DEFAULT '[]',
    version           INTEGER     NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tasks_status        ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_role_status   ON tasks (role, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority      ON tasks (priority, created_at);

CREATE TABLE IF NOT EXISTS agents (
    id             TEXT    PRIMARY KEY,
    role           TEXT    NOT NULL,
    pid            INTEGER,
    heartbeat_ts   FLOAT8  NOT NULL DEFAULT 0,
    spawn_ts       FLOAT8  NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'starting',
    cell_id        TEXT
);

CREATE TABLE IF NOT EXISTS task_archive (
    id               BIGSERIAL  PRIMARY KEY,
    task_id          TEXT       NOT NULL,
    title            TEXT       NOT NULL,
    role             TEXT       NOT NULL,
    status           TEXT       NOT NULL,
    created_at       FLOAT8     NOT NULL,
    completed_at     FLOAT8     NOT NULL,
    duration_seconds FLOAT8     NOT NULL,
    result_summary   TEXT,
    cost_usd         FLOAT8
);

CREATE INDEX IF NOT EXISTS idx_archive_completed ON task_archive (completed_at DESC);
"""

# Atomic claim: find the best open task for a role and claim it in one
# UPDATE statement.  FOR UPDATE SKIP LOCKED prevents two concurrent callers
# from claiming the same row.
_CLAIM_NEXT_SQL = """
UPDATE tasks
SET    status  = 'claimed',
       version = version + 1
WHERE  id = (
    SELECT id
    FROM   tasks
    WHERE  role   = $1
    AND    status = 'open'
    ORDER  BY priority, created_at
    LIMIT  1
    FOR    UPDATE SKIP LOCKED
)
RETURNING *
"""

_CLAIM_BY_ID_SQL = """
UPDATE tasks
SET    status  = 'claimed',
       version = version + 1
WHERE  id = $1
AND    status = 'open'
RETURNING *
"""

_CLAIM_BY_ID_CAS_SQL = """
UPDATE tasks
SET    status  = 'claimed',
       version = version + 1
WHERE  id      = $1
AND    version = $2
AND    status  = 'open'
RETURNING *
"""


# ---------------------------------------------------------------------------
# Row → Task conversion
# ---------------------------------------------------------------------------


def _row_to_task(row: Any) -> Task:
    """Convert an asyncpg ``Record`` to a domain :class:`Task`."""
    raw: dict[str, Any] = dict(row)

    signals: list[CompletionSignal] = []
    raw_signals: list[dict[str, Any]] = raw.get("completion_signals") or []
    for sig in raw_signals:
        with contextlib.suppress(KeyError, TypeError):
            signals.append(CompletionSignal(type=sig["type"], value=sig["value"]))

    upgrade_details: UpgradeProposalDetails | None = None
    if raw.get("upgrade_details"):
        raw_upgrade = cast("dict[str, Any]", raw["upgrade_details"])
        upgrade_details = UpgradeProposalDetails(
            current_state=raw_upgrade.get("current_state", ""),
            proposed_change=raw_upgrade.get("proposed_change", ""),
            benefits=list(raw_upgrade.get("benefits", [])),
            risk_assessment=RiskAssessment(**raw_upgrade.get("risk_assessment", {})),
            rollback_plan=RollbackPlan(**raw_upgrade.get("rollback_plan", {})),
            cost_estimate_usd=float(raw_upgrade.get("cost_estimate_usd", 0.0)),
            performance_impact=raw_upgrade.get("performance_impact", ""),
        )

    return Task(
        id=raw["id"],
        title=raw["title"],
        description=raw["description"],
        role=raw["role"],
        priority=raw["priority"],
        scope=Scope(raw["scope"]),
        complexity=Complexity(raw["complexity"]),
        estimated_minutes=raw["estimated_minutes"],
        status=TaskStatus(raw["status"]),
        task_type=TaskType(raw["task_type"]),
        upgrade_details=upgrade_details,
        depends_on=list(raw.get("depends_on") or []),
        owned_files=list(raw.get("owned_files") or []),
        assigned_agent=raw.get("assigned_agent"),
        result_summary=raw.get("result_summary"),
        cell_id=raw.get("cell_id"),
        model=raw.get("model"),
        effort=raw.get("effort"),
        completion_signals=signals,
        created_at=raw.get("created_at") or time.time(),
        progress_log=cast("list[dict[str, Any]]", list(raw.get("progress_log") or [])),
        version=raw.get("version", 1),
    )


# ---------------------------------------------------------------------------
# PostgresTaskStore
# ---------------------------------------------------------------------------


class PostgresTaskStore(BaseTaskStore):
    """PostgreSQL-backed task store with optional Redis distributed locking.

    Args:
        dsn: asyncpg-compatible PostgreSQL DSN.
        redis_coordinator: Optional :class:`~bernstein.core.store_redis.RedisCoordinator`
            for distributed task-claim locking.  When ``None``, locking is
            handled by PostgreSQL's ``FOR UPDATE SKIP LOCKED`` (correct but
            slightly less contention-resistant at very high concurrency).
        pool_min: Minimum connection pool size.
        pool_max: Maximum connection pool size.
    """

    def __init__(
        self,
        dsn: str,
        redis_coordinator: RedisCoordinator | None = None,
        pool_min: int = 2,
        pool_max: int = 10,
    ) -> None:
        super().__init__()
        if not _ASYNCPG_AVAILABLE:
            raise RuntimeError(
                "asyncpg package is required for postgres mode. Install it with: pip install bernstein[postgres]"
            )
        self._dsn = dsn
        self._redis = redis_coordinator
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Any = None
        # Local cache for fast property reads
        self._agent_count: int = 0

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        """Create the connection pool and run DDL migrations."""
        self._pool = cast(
            "Any",
            await asyncpg.create_pool(  # type: ignore[union-attr]
                self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
            ),
        )
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)
        if self._redis is not None:
            await self._redis.connect()
        logger.info("PostgresTaskStore ready (pool %d-%d)", self._pool_min, self._pool_max)

    async def shutdown(self) -> None:
        """Close pool and Redis connections."""
        if self._pool is not None:
            await self._pool.close()
        if self._redis is not None:
            await self._redis.close()

    # -- helpers -------------------------------------------------------------

    async def _pool_acquire(self) -> asyncpg.Connection:  # type: ignore[name-defined]
        if self._pool is None:
            raise RuntimeError("PostgresTaskStore.startup() has not been called")
        return await self._pool.acquire()  # type: ignore[union-attr]

    def _task_params(self, task: Task) -> tuple[Any, ...]:
        """Return an asyncpg parameter tuple for full task insertion."""
        return (
            task.id,
            task.title,
            task.description,
            task.role,
            task.priority,
            task.scope.value,
            task.complexity.value,
            task.estimated_minutes,
            task.status.value,
            task.task_type.value,
            json.dumps(asdict(task.upgrade_details)) if task.upgrade_details else None,
            task.depends_on,
            task.owned_files,
            task.assigned_agent,
            task.result_summary,
            task.cell_id,
            task.model,
            task.effort,
            json.dumps([{"type": s.type, "value": s.value} for s in task.completion_signals]),
            task.created_at,
            json.dumps(task.progress_log),  # type: ignore[reportUnknownMemberType]
            task.version,
        )

    # -- task mutations ------------------------------------------------------

    async def create(self, req: TaskCreate) -> Task:
        """Create a new task and persist it to PostgreSQL.

        Args:
            req: Validated HTTP creation payload.

        Returns:
            The newly created :class:`~bernstein.core.models.Task`.

        Raises:
            fastapi.HTTPException: 422 if dependencies reference non-existent
                tasks (checked via DB query).
        """
        from fastapi import HTTPException

        from bernstein.core.server import _parse_upgrade_dict  # type: ignore[attr-defined]

        task = Task(
            id=uuid.uuid4().hex[:12],
            title=req.title,
            description=req.description,
            role=req.role,
            priority=req.priority,
            scope=Scope(req.scope),
            complexity=Complexity(req.complexity),
            estimated_minutes=req.estimated_minutes,
            depends_on=req.depends_on,
            owned_files=req.owned_files,
            cell_id=req.cell_id,
            task_type=TaskType(req.task_type),
            upgrade_details=_parse_upgrade_dict(req.upgrade_details),
            model=req.model,
            effort=req.effort,
            completion_signals=[CompletionSignal(type=s.type, value=s.value) for s in req.completion_signals],
        )

        assert self._pool is not None
        async with self._pool.acquire() as conn:
            if task.depends_on:
                rows = await conn.fetch(
                    "SELECT id FROM tasks WHERE id = ANY($1::text[])",
                    task.depends_on,
                )
                found = {r["id"] for r in rows}
                missing = [dep for dep in task.depends_on if dep not in found]
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail=f"depends_on references non-existent task(s): {', '.join(missing)}",
                    )
            await conn.execute(
                """
                INSERT INTO tasks (
                    id, title, description, role, priority, scope, complexity,
                    estimated_minutes, status, task_type, upgrade_details,
                    depends_on, owned_files, assigned_agent, result_summary,
                    cell_id, model, effort, completion_signals,
                    created_at, progress_log, version
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                    $16,$17,$18,$19,$20,$21,$22
                )
                """,
                *self._task_params(task),
            )
        return task

    async def claim_next(self, role: str) -> Task | None:
        """Claim the highest-priority open task for *role*, atomically.

        Uses ``UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`` so
        concurrent callers on different nodes cannot double-claim.
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_CLAIM_NEXT_SQL, role)
        if row is None:
            return None
        task = _row_to_task(row)
        # Filter: skip tasks with unmet dependencies.
        # (done at query time via a subquery in production-scale deployments;
        #  here we do a post-filter to keep the SQL readable)
        if task.depends_on:
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                done_rows = await conn.fetch(
                    """SELECT id FROM tasks
                       WHERE id = ANY($1::text[]) AND status = 'done'""",
                    task.depends_on,
                )
            done_ids = {r["id"] for r in done_rows}
            if not all(dep in done_ids for dep in task.depends_on):
                # Put back — re-open it (no other slot; caller gets None)
                assert self._pool is not None
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE tasks SET status='open', version=version-1 WHERE id=$1",
                        task.id,
                    )
                return None
        return task

    async def claim_by_id(
        self,
        task_id: str,
        expected_version: int | None = None,
        agent_role: str | None = None,
    ) -> Task:
        """Claim a specific task, optionally with CAS.

        When *expected_version* is provided, a Redis lock is acquired first
        (if a coordinator is configured) to prevent a race between the version
        check and the UPDATE.
        """
        lock_token: str | None = None
        try:
            if self._redis is not None and expected_version is not None:
                lock_token = await self._redis.acquire(task_id)
                if lock_token is None:
                    raise ValueError(f"Could not acquire distributed lock for task {task_id}")

            assert self._pool is not None
            async with self._pool.acquire() as conn:
                row = await self._claim_row(conn, task_id, expected_version)
            return _row_to_task(row)
        finally:
            if self._redis is not None and lock_token is not None:
                await self._redis.release(task_id, lock_token)

    @staticmethod
    async def _claim_row(conn: Any, task_id: str, expected_version: int | None) -> Any:
        """Execute the claim query and handle missing/conflicting tasks."""
        if expected_version is not None:
            row = await conn.fetchrow(_CLAIM_BY_ID_CAS_SQL, task_id, expected_version)
            if row is None:
                exists = await conn.fetchval("SELECT 1 FROM tasks WHERE id=$1", task_id)
                if not exists:
                    raise KeyError(task_id)
                ver = await conn.fetchval("SELECT version FROM tasks WHERE id=$1", task_id)
                raise ValueError(f"Version conflict: task {task_id} is at version {ver}, expected {expected_version}")
            return row
        row = await conn.fetchrow(_CLAIM_BY_ID_SQL, task_id)
        if row is None:
            exists = await conn.fetchval("SELECT 1 FROM tasks WHERE id=$1", task_id)
            if not exists:
                raise KeyError(task_id)
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id)
            assert row is not None
        return row

    async def claim_batch(
        self,
        task_ids: list[str],
        agent_id: str,
        agent_role: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Atomically claim multiple tasks.  Uses a single transaction."""
        claimed: list[str] = []
        failed: list[str] = []
        assert self._pool is not None
        async with self._pool.acquire() as conn, conn.transaction():
            for task_id in task_ids:
                row = await conn.fetchrow(
                    """
                        UPDATE tasks
                        SET    status         = 'claimed',
                               assigned_agent = $2,
                               version        = version + 1
                        WHERE  id = $1 AND status = 'open'
                        RETURNING id
                        """,
                    task_id,
                    agent_id,
                )
                if row is not None:
                    claimed.append(task_id)
                else:
                    failed.append(task_id)
        return claimed, failed

    async def complete(self, task_id: str, result_summary: str) -> Task:
        """Mark a task done and write an archive record."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET    status         = 'done',
                       result_summary = $2,
                       version        = version + 1
                WHERE  id = $1
                RETURNING *
                """,
                task_id,
                result_summary,
            )
            if row is None:
                raise KeyError(task_id)
            task = _row_to_task(row)
            completed_at = time.time()
            await conn.execute(
                _ARCHIVE_INSERT_SQL,
                task.id,
                task.title,
                task.role,
                task.status.value,
                task.created_at,
                completed_at,
                round(completed_at - task.created_at, 3),
                result_summary,
                None,
            )
        return task

    async def fail(self, task_id: str, reason: str) -> Task:
        """Mark a task failed and write an archive record."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET    status         = 'failed',
                       result_summary = $2,
                       version        = version + 1
                WHERE  id = $1
                RETURNING *
                """,
                task_id,
                reason,
            )
            if row is None:
                raise KeyError(task_id)
            task = _row_to_task(row)
            completed_at = time.time()
            await conn.execute(
                _ARCHIVE_INSERT_SQL,
                task.id,
                task.title,
                task.role,
                task.status.value,
                task.created_at,
                completed_at,
                round(completed_at - task.created_at, 3),
                reason,
                None,
            )
        return task

    async def add_progress(self, task_id: str, message: str, percent: int) -> Task:
        """Append a progress entry to the task's JSONB progress_log."""
        entry = {"timestamp": time.time(), "message": message, "percent": percent}
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET    progress_log = progress_log || $2::jsonb
                WHERE  id = $1
                RETURNING *
                """,
                task_id,
                json.dumps([entry]),
            )
            if row is None:
                raise KeyError(task_id)
        return _row_to_task(row)

    async def update(self, task_id: str, role: str | None, priority: int | None) -> Task:
        """Update mutable task fields (role, priority) — manager corrections."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            current = await conn.fetchrow("SELECT id FROM tasks WHERE id=$1", task_id)
            if current is None:
                raise KeyError(task_id)
            updates: list[str] = ["version = version + 1"]
            params: list[object] = []
            if role is not None:
                params.append(role)
                updates.append(f"role = ${len(params)}")
            if priority is not None:
                params.append(priority)
                updates.append(f"priority = ${len(params)}")
            params.append(task_id)
            row = await conn.fetchrow(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ${len(params)} RETURNING *",
                *params,
            )
            if row is None:  # pragma: no cover
                raise KeyError(task_id)
            return _row_to_task(row)

    async def cancel(self, task_id: str, reason: str) -> Task:
        """Cancel a non-terminal task."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            current = await conn.fetchrow("SELECT status FROM tasks WHERE id=$1", task_id)
            if current is None:
                raise KeyError(task_id)
            if current["status"] in ("done", "failed", "cancelled"):
                raise ValueError(f"Task '{task_id}' cannot be cancelled from status '{current['status']}'")
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET    status         = 'cancelled',
                       result_summary = $2,
                       version        = version + 1
                WHERE  id = $1
                RETURNING *
                """,
                task_id,
                reason,
            )
            if row is None:  # pragma: no cover
                raise KeyError(task_id)
            task = _row_to_task(row)
            completed_at = time.time()
            await conn.execute(
                _ARCHIVE_INSERT_SQL,
                task.id,
                task.title,
                task.role,
                task.status.value,
                task.created_at,
                completed_at,
                round(completed_at - task.created_at, 3),
                reason,
                None,
            )
        return task

    # -- queries -------------------------------------------------------------

    async def list_tasks(
        self,
        status: str | None = None,
        cell_id: str | None = None,
    ) -> list[Task]:
        """Return tasks filtered by status and/or cell_id."""
        assert self._pool is not None

        conditions: list[str] = []
        params: list[Any] = []
        param_n = 1

        if status is not None:
            conditions.append(f"status = ${param_n}")
            params.append(status)
            param_n += 1

        if cell_id is not None:
            conditions.append(f"cell_id = ${param_n}")
            params.append(cell_id)
            param_n += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM tasks {where} ORDER BY priority, created_at"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            tasks = [_row_to_task(r) for r in rows]

            if status == "open":
                done_row = await conn.fetch("SELECT id FROM tasks WHERE status='done'")
                done_ids = {r["id"] for r in done_row}
                tasks = [t for t in tasks if all(dep in done_ids for dep in t.depends_on)]
        return tasks

    async def get_task(self, task_id: str) -> Task | None:
        """Return a single task by ID."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1", task_id)
        return _row_to_task(row) if row else None

    async def status_summary(self) -> StatusSummary:
        """Return aggregated task counts via SQL GROUP BY."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, role, COUNT(*) AS cnt FROM tasks GROUP BY status, role")
        counts: dict[str, dict[str, int]] = {}
        status_totals: dict[str, int] = {}
        for row in rows:
            s, r, cnt = row["status"], row["role"], int(row["cnt"])
            counts.setdefault(r, {}).setdefault(s, 0)
            counts[r][s] += cnt
            status_totals[s] = status_totals.get(s, 0) + cnt

        per_role = [
            RoleSummary(
                role=role,
                open=d.get("open", 0),
                claimed=d.get("claimed", 0),
                done=d.get("done", 0),
                failed=d.get("failed", 0),
            )
            for role, d in sorted(counts.items())
        ]
        return StatusSummary(
            total=sum(status_totals.values()),
            open=status_totals.get("open", 0),
            claimed=status_totals.get("claimed", 0),
            done=status_totals.get("done", 0),
            failed=status_totals.get("failed", 0),
            per_role=per_role,
        )

    async def read_archive(self, limit: int = 50) -> list[ArchiveRecord]:
        """Return the last *limit* archive records, oldest-first."""
        from bernstein.core.server import ArchiveRecord as AR

        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id, title, role, status, created_at,
                       completed_at, duration_seconds, result_summary, cost_usd
                FROM   task_archive
                ORDER  BY completed_at DESC
                LIMIT  $1
                """,
                limit,
            )
        records: list[ArchiveRecord] = []
        for row in reversed(rows):  # oldest-first
            records.append(
                AR(
                    task_id=row["task_id"],
                    title=row["title"],
                    role=row["role"],
                    status=row["status"],
                    created_at=row["created_at"],
                    completed_at=row["completed_at"],
                    duration_seconds=row["duration_seconds"],
                    result_summary=row["result_summary"],
                    cost_usd=row["cost_usd"],
                )
            )
        return records

    # -- agent heartbeats ----------------------------------------------------

    async def heartbeat(
        self,
        agent_id: str,
        role: str,
        status: Literal["starting", "working", "idle", "dead"],
    ) -> float:
        """Upsert an agent heartbeat row."""
        now = time.time()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agents (id, role, heartbeat_ts, spawn_ts, status)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (id) DO UPDATE
                    SET heartbeat_ts = EXCLUDED.heartbeat_ts,
                        status       = EXCLUDED.status
                """,
                agent_id,
                role,
                now,
                now,
                status,
            )
            self._agent_count = await conn.fetchval("SELECT COUNT(*) FROM agents") or 0
        return now

    async def mark_stale_dead(self, threshold_s: float = 60.0) -> int:
        """Mark agents with stale heartbeats as dead."""
        cutoff = time.time() - threshold_s
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agents SET status='dead' WHERE heartbeat_ts < $1 AND status != 'dead'",
                cutoff,
            )
        # asyncpg returns "UPDATE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0

    # -- read-only properties ------------------------------------------------

    @property
    def agent_count(self) -> int:
        """Cached agent count (updated on each heartbeat call)."""
        return self._agent_count
