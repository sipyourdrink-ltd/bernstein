"""TEST-014: Mock adapter for deterministic integration testing.

Tests that the mock adapter always succeeds/fails as configured
and can be used for integration-style tests without real API calls.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.lifecycle import transition_task
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore

# ---------------------------------------------------------------------------
# Deterministic mock adapter (test-level, not the production MockAgentAdapter)
# ---------------------------------------------------------------------------


class DeterministicMockAdapter:
    """A mock adapter that always produces a deterministic result.

    Configurable to succeed, fail, or return specific output.
    Useful for integration testing without real subprocess spawning.
    """

    def __init__(
        self,
        *,
        should_succeed: bool = True,
        exit_code: int = 0,
        output: str = "mock completed",
        delay_ms: int = 0,
        fail_message: str = "mock failure",
    ) -> None:
        self.should_succeed = should_succeed
        self.exit_code = exit_code if not should_succeed else 0
        self.output = output
        self.delay_ms = delay_ms
        self.fail_message = fail_message
        self.spawn_count = 0
        self.spawned_prompts: list[str] = []

    def name(self) -> str:
        return "deterministic-mock"

    async def spawn(self, *, prompt: str, role: str = "backend") -> dict[str, Any]:
        """Simulate spawning an agent.

        Args:
            prompt: The task prompt.
            role: Agent role.

        Returns:
            Dict with result details.
        """
        self.spawn_count += 1
        self.spawned_prompts.append(prompt)

        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000)

        if self.should_succeed:
            return {
                "status": "success",
                "exit_code": 0,
                "output": self.output,
                "role": role,
            }
        return {
            "status": "failed",
            "exit_code": self.exit_code,
            "output": self.fail_message,
            "role": role,
        }


class DeterministicMockOrchestrator:
    """Minimal orchestrator-like loop using DeterministicMockAdapter.

    Processes tasks from a TaskStore, spawns mock agents, and updates status.
    """

    def __init__(self, store: TaskStore, adapter: DeterministicMockAdapter) -> None:
        self.store = store
        self.adapter = adapter
        self.processed: list[str] = []

    async def process_one(self, role: str) -> Task | None:
        """Claim and process one task for the given role.

        Args:
            role: Agent role to claim for.

        Returns:
            The processed task, or None if no tasks available.
        """
        task = await self.store.claim_next(role)
        if task is None:
            return None

        result = await self.adapter.spawn(prompt=task.description, role=task.role)
        self.processed.append(task.id)

        if result["status"] == "success":
            transition_task(task, TaskStatus.DONE, reason="mock-complete")
            task.result_summary = result["output"]
        else:
            transition_task(task, TaskStatus.FAILED, reason=result["output"])

        return task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCreateRequest:
    """Minimal object satisfying the TaskCreateRequest protocol."""

    def __init__(self, title: str = "mock-task", role: str = "backend") -> None:
        self.title = title
        self.description = f"Do {title}"
        self.role = role
        self.priority = 2
        self.scope = "medium"
        self.complexity = "medium"
        self.estimated_minutes: int | None = None
        self.depends_on: list[str] = []
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


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    return TaskStore(jsonl)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeterministicMockAdapter:
    """Tests for the mock adapter itself."""

    def test_success_mode(self) -> None:
        adapter = DeterministicMockAdapter(should_succeed=True, output="done!")
        result = _run(adapter.spawn(prompt="test task"))
        assert result["status"] == "success"
        assert result["exit_code"] == 0
        assert result["output"] == "done!"
        assert adapter.spawn_count == 1

    def test_failure_mode(self) -> None:
        adapter = DeterministicMockAdapter(should_succeed=False, exit_code=1, fail_message="boom")
        result = _run(adapter.spawn(prompt="test task"))
        assert result["status"] == "failed"
        assert result["exit_code"] == 1
        assert result["output"] == "boom"

    def test_tracks_prompts(self) -> None:
        adapter = DeterministicMockAdapter()
        _run(adapter.spawn(prompt="first"))
        _run(adapter.spawn(prompt="second"))
        assert adapter.spawned_prompts == ["first", "second"]
        assert adapter.spawn_count == 2

    def test_name(self) -> None:
        adapter = DeterministicMockAdapter()
        assert adapter.name() == "deterministic-mock"


class TestMockOrchestrator:
    """Integration-style tests using the mock orchestrator."""

    def test_process_success(self, store: TaskStore) -> None:
        _run(store.create(_FakeCreateRequest("implement login", "backend")))
        adapter = DeterministicMockAdapter(should_succeed=True, output="login done")
        orch = DeterministicMockOrchestrator(store, adapter)

        task = _run(orch.process_one("backend"))
        assert task is not None
        assert task.status == TaskStatus.DONE
        assert task.result_summary == "login done"

    def test_process_failure(self, store: TaskStore) -> None:
        _run(store.create(_FakeCreateRequest("implement auth", "backend")))
        adapter = DeterministicMockAdapter(should_succeed=False, fail_message="compile error")
        orch = DeterministicMockOrchestrator(store, adapter)

        task = _run(orch.process_one("backend"))
        assert task is not None
        assert task.status == TaskStatus.FAILED

    def test_no_tasks_available(self, store: TaskStore) -> None:
        adapter = DeterministicMockAdapter()
        orch = DeterministicMockOrchestrator(store, adapter)

        task = _run(orch.process_one("backend"))
        assert task is None
        assert adapter.spawn_count == 0

    def test_multiple_tasks(self, store: TaskStore) -> None:
        for i in range(3):
            _run(store.create(_FakeCreateRequest(f"task-{i}", "backend")))

        adapter = DeterministicMockAdapter(should_succeed=True)
        orch = DeterministicMockOrchestrator(store, adapter)

        for _ in range(3):
            task = _run(orch.process_one("backend"))
            assert task is not None
            assert task.status == TaskStatus.DONE

        # No more tasks
        task = _run(orch.process_one("backend"))
        assert task is None
        assert len(orch.processed) == 3

    def test_role_isolation(self, store: TaskStore) -> None:
        _run(store.create(_FakeCreateRequest("backend task", "backend")))
        _run(store.create(_FakeCreateRequest("qa task", "qa")))

        adapter = DeterministicMockAdapter(should_succeed=True)
        orch = DeterministicMockOrchestrator(store, adapter)

        task = _run(orch.process_one("qa"))
        assert task is not None
        assert task.role == "qa"

        task = _run(orch.process_one("qa"))
        assert task is None  # Only 1 QA task
