"""Dependency-aware claiming invariants for the task server (#2357).

AC5 (property test): no claim path - ``claim_next``, ``claim_by_id``,
``claim_batch`` - ever offers a task whose declared dependencies are not
all in a terminal-success state (``done`` or ``closed``).

AC3 (claim half): rebuilding the store from the same JSONL journal
reproduces the identical claim-eligibility projection - the drain order
of ``claim_next`` is a deterministic function of the journal.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING, Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.tasks.task_store_core import TaskStore

if TYPE_CHECKING:
    from pathlib import Path

_ROLE = "backend"


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeCreateRequest:
    """Minimal object satisfying the TaskCreateRequest protocol."""

    def __init__(self, title: str, depends_on: list[str]) -> None:
        self.title = title
        self.description = f"{title} description"
        self.role = _ROLE
        self.priority = 2
        self.scope = "medium"
        self.complexity = "medium"
        self.estimated_minutes: int | None = None
        self.depends_on = depends_on
        self.parent_task_id: str | None = None
        self.depends_on_repo: str | None = None
        self.owned_files: list[str] = []
        self.tenant_id = "default"
        self.cell_id: str | None = None
        self.repo: str | None = None
        self.task_type = "standard"
        self.upgrade_details: dict[str, Any] | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.batch_eligible = False
        self.approval_required = False
        self.eu_ai_act_risk = "minimal"
        self.risk_level = "low"
        self.completion_signals: list[Any] = []
        self.slack_context: dict[str, Any] | None = None
        self.parent_session_id: str | None = None


def _make_store(base: Path, name: str = "runtime") -> TaskStore:
    jsonl = base / name / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    return TaskStore(jsonl, archive_path=base / name / "archive.jsonl")


# ---------------------------------------------------------------------------
# Hypothesis strategy: a random DAG plus a random completion plan
# ---------------------------------------------------------------------------


@st.composite
def dag_scenarios(draw: st.DrawFn) -> tuple[list[list[int]], list[bool], list[bool]]:
    """Return ``(deps_by_index, complete_flags, close_flags)``.

    Task *i* may only depend on tasks with a smaller index, which keeps the
    graph acyclic by construction. ``complete_flags[i]`` asks the scenario to
    finish task *i* when its own dependencies allow it; ``close_flags[i]``
    additionally archives a completed task to ``closed``.
    """
    n = draw(st.integers(min_value=2, max_value=8))
    deps: list[list[int]] = []
    for i in range(n):
        pool = list(range(i))
        subset = draw(st.lists(st.sampled_from(pool), unique=True, max_size=min(3, len(pool)))) if pool else []
        deps.append(sorted(subset))
    complete_flags = [draw(st.booleans()) for _ in range(n)]
    close_flags = [draw(st.booleans()) for _ in range(n)]
    return deps, complete_flags, close_flags


async def _build_scenario(
    store: TaskStore,
    deps_by_index: list[list[int]],
    complete_flags: list[bool],
    close_flags: list[bool],
) -> tuple[list[str], set[str]]:
    """Create the DAG and drive the completion plan.

    Returns ``(task_ids, terminal_ok_ids)`` where ``terminal_ok_ids`` are
    the tasks that reached ``done`` or ``closed``.
    """
    ids: list[str] = []
    for i, dep_indexes in enumerate(deps_by_index):
        req = _FakeCreateRequest(f"task-{i}", [ids[j] for j in dep_indexes])
        task = await store.create(req)
        ids.append(task.id)

    terminal_ok: set[str] = set()
    for i, task_id in enumerate(ids):
        if not complete_flags[i]:
            continue
        if any(ids[j] not in terminal_ok for j in deps_by_index[i]):
            continue  # the plan respects the same gating the server enforces
        await store.claim_by_id(task_id)
        await store.complete(task_id, result_summary="done")
        terminal_ok.add(task_id)
        if close_flags[i]:
            await store.close(task_id)
    return ids, terminal_ok


# ---------------------------------------------------------------------------
# AC5 - the claim API never offers a task with incomplete dependencies
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scenario=dag_scenarios())
def test_claim_next_never_offers_incomplete_dependencies(
    scenario: tuple[list[list[int]], list[bool], list[bool]],
    tmp_path: Path,
) -> None:
    deps_by_index, complete_flags, close_flags = scenario

    async def _run() -> None:
        store = _make_store(tmp_path)
        ids, terminal_ok = await _build_scenario(store, deps_by_index, complete_flags, close_flags)
        index_of = {task_id: i for i, task_id in enumerate(ids)}

        offered: list[str] = []
        while True:
            task = await store.claim_next(_ROLE)
            if task is None:
                break
            offered.append(task.id)
            for j in deps_by_index[index_of[task.id]]:
                assert ids[j] in terminal_ok, f"claim_next offered {task.id} while dependency {ids[j]} is not terminal"

        # Completeness: every open task whose dependencies were all terminal
        # must have been offered during the drain.
        eligible = {
            task_id
            for i, task_id in enumerate(ids)
            if task_id not in terminal_ok
            and store.get_task(task_id) is not None
            and all(ids[j] in terminal_ok for j in deps_by_index[i])
        }
        assert eligible == set(offered)

    _run_async(_run())


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scenario=dag_scenarios())
def test_claim_by_id_and_batch_gate_incomplete_dependencies(
    scenario: tuple[list[list[int]], list[bool], list[bool]],
    tmp_path: Path,
) -> None:
    deps_by_index, complete_flags, close_flags = scenario

    async def _run() -> None:
        store = _make_store(tmp_path)
        ids, terminal_ok = await _build_scenario(store, deps_by_index, complete_flags, close_flags)
        index_of = {task_id: i for i, task_id in enumerate(ids)}

        gated = [
            task_id
            for i, task_id in enumerate(ids)
            if task_id not in terminal_ok and any(ids[j] not in terminal_ok for j in deps_by_index[i])
        ]
        for task_id in gated:
            try:
                await store.claim_by_id(task_id)
            except ValueError:
                pass
            else:  # pragma: no cover - the assertion message is the point
                raise AssertionError(f"claim_by_id claimed {task_id} despite incomplete dependencies")

        claimed, _failed = await store.claim_batch(list(ids), agent_id="fleet-worker")
        for task_id in claimed:
            for j in deps_by_index[index_of[task_id]]:
                assert ids[j] in terminal_ok, f"claim_batch claimed {task_id} while dependency {ids[j]} is not terminal"

    _run_async(_run())


# ---------------------------------------------------------------------------
# AC3 (claim half) - replay reproduces identical claim eligibility
# ---------------------------------------------------------------------------


def test_replayed_journal_reproduces_identical_claim_eligibility(tmp_path: Path) -> None:
    async def _build() -> None:
        store = _make_store(tmp_path, name="origin")
        dep = await store.create(_FakeCreateRequest("dep", []))
        await store.create(_FakeCreateRequest("child-ready", [dep.id]))
        blocker = await store.create(_FakeCreateRequest("blocker", []))
        await store.create(_FakeCreateRequest("child-gated", [blocker.id]))
        await store.create(_FakeCreateRequest("standalone", []))
        await store.claim_by_id(dep.id)
        await store.complete(dep.id, result_summary="done")
        await store.flush_buffer()

    _run_async(_build())

    origin = tmp_path / "origin" / "tasks.jsonl"

    def _drain(copy_name: str) -> list[str]:
        replica_dir = tmp_path / copy_name
        replica_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, replica_dir / "tasks.jsonl")
        store = TaskStore(replica_dir / "tasks.jsonl", archive_path=replica_dir / "archive.jsonl")
        store.replay_jsonl()

        async def _run() -> list[str]:
            drained: list[str] = []
            while True:
                task = await store.claim_next(_ROLE)
                if task is None:
                    break
                drained.append(task.id)
            return drained

        return _run_async(_run())

    first = _drain("replica-a")
    second = _drain("replica-b")
    assert first == second
    assert len(first) == 3  # child-ready, blocker, standalone - never child-gated

    def _titles(drained: list[str]) -> list[str]:
        store = TaskStore(tmp_path / "replica-a" / "tasks.jsonl")
        store.replay_jsonl()
        return [t.title for task_id in drained for t in [store.get_task(task_id)] if t is not None]

    assert "child-gated" not in _titles(first)


def test_replayed_journal_keeps_dependency_gate_closed(tmp_path: Path) -> None:
    async def _build() -> tuple[str, str]:
        store = _make_store(tmp_path, name="origin")
        blocker = await store.create(_FakeCreateRequest("blocker", []))
        child = await store.create(_FakeCreateRequest("child", [blocker.id]))
        await store.flush_buffer()
        return blocker.id, child.id

    _blocker_id, child_id = _run_async(_build())

    replica = tmp_path / "replica"
    replica.mkdir()
    shutil.copy2(tmp_path / "origin" / "tasks.jsonl", replica / "tasks.jsonl")
    store = TaskStore(replica / "tasks.jsonl", archive_path=replica / "archive.jsonl")
    store.replay_jsonl()

    async def _try_claim() -> None:
        try:
            await store.claim_by_id(child_id)
        except ValueError:
            return
        raise AssertionError("replayed store claimed a dependency-gated task")

    _run_async(_try_claim())
