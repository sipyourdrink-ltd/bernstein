"""TEST-014: Mock adapter for deterministic integration testing.

Tests that the mock adapter always succeeds/fails as configured
and can be used for integration-style tests without real API calls.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore

from bernstein.core.lifecycle import transition_task

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


# ---------------------------------------------------------------------------
# Production MockAgentAdapter default_model (issue #2799)
# ---------------------------------------------------------------------------


def test_mock_adapter_declares_non_claude_default_model() -> None:
    """MockAgentAdapter must declare a non-Claude-tier default_model.

    Regression for issue #2799: the spawn-time model gate refuses to spawn when
    a Claude cascade tier (opus/sonnet/haiku) reaches a non-Claude adapter that
    has no default_model, so ``bernstein demo`` failed every task. A declared
    default lets the gate coerce the tier name instead of refusing.
    """
    from bernstein.adapters.mock import MockAgentAdapter
    from bernstein.core.agents.spawner_warm_pool import _CLAUDE_TIER_MODELS

    assert MockAgentAdapter.default_model
    assert MockAgentAdapter.default_model not in _CLAUDE_TIER_MODELS


def test_mock_default_model_coerces_claude_tier_instead_of_refusing() -> None:
    """The model gate coerces a Claude tier to the mock default rather than raising."""
    from bernstein.core.models import ModelConfig

    from bernstein.adapters.mock import MockAgentAdapter
    from bernstein.core.agents.spawner_warm_pool import _coerce_model_for_non_claude_adapter

    selected = ModelConfig(model="sonnet", effort="high")
    coerced = _coerce_model_for_non_claude_adapter(
        selected,
        adapter_name="mock",
        adapter_default_model=MockAgentAdapter.default_model,
    )
    assert coerced.model == MockAgentAdapter.default_model


# ---------------------------------------------------------------------------
# Completion evidence - the mock must present what real agents present
# (issue #3431)
# ---------------------------------------------------------------------------


def _make_demo_workdir(tmp_path: Path) -> Path:
    """Seed the real demo fixture project (Flask app + git repo with HEAD)."""
    from bernstein.cli.run_confirm import setup_demo_project

    project = tmp_path / "demo-project"
    project.mkdir()
    setup_demo_project(project, "mock")
    return project


def _run_mock_script(workdir: Path, task_title: str, tmp_path: Path) -> Path:
    """Execute the embedded mock-agent script exactly as ``spawn()`` does."""
    from bernstein.adapters.mock import MockAgentAdapter

    script = tmp_path / "mock_script.py"
    script.write_text(MockAgentAdapter._build_mock_script())
    log_path = workdir / ".sdd" / "runtime" / "agent-mock-test.log"
    task_info = json.dumps({
        "workdir": str(workdir),
        "log_path": str(log_path),
        "task_id": "test-task-id",
        "task_title": task_title,
    })
    subprocess.run(
        [sys.executable, str(script), task_info],
        check=True,
        timeout=60,
        capture_output=True,
    )
    return log_path


def test_mock_fix_evidence_parses_into_files_modified(tmp_path: Path) -> None:
    """The dead-agent reap path auto-completes only when the log aggregator
    extracts a non-empty ``files_modified``. Prose-only, timestamp-prefixed
    lines never match its ^-anchored ``file_modified`` pattern - which is how
    every demo task ended up failed as unverified (issue #3431). The assertion
    runs through the real consumer, not a string grep of the log.
    """
    from bernstein.core.agents.agent_log_aggregator import AgentLogAggregator

    project = _make_demo_workdir(tmp_path)
    log_path = _run_mock_script(project, "Fix off-by-one in get_item route", tmp_path)

    summary = AgentLogAggregator(project).parse_log("agent-mock-test", log_path=log_path)
    assert "app.py" in summary.files_modified


def test_mock_fix_commits_on_the_worktree_branch(tmp_path: Path) -> None:
    """The reap path's second evidence check and the merge path both key off
    commits on the branch; an edit left uncommitted can never merge back into
    the demo project (issue #3431).
    """

    def _count(project: Path) -> int:
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return int(out.strip())

    project = _make_demo_workdir(tmp_path)
    before = _count(project)
    _run_mock_script(project, "Fix health endpoint returns 201 instead of 200", tmp_path)

    assert _count(project) == before + 1
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert porcelain.strip() == "", porcelain


def test_mock_without_git_repo_degrades_to_log_evidence(tmp_path: Path) -> None:
    """A workdir that is not a git repository must not crash the agent: the
    fix is still applied, the ``Modified:`` evidence line still parses, and
    the skipped commit is recorded instead of raised.
    """
    import shutil

    project = _make_demo_workdir(tmp_path)
    shutil.rmtree(project / ".git")

    log_path = _run_mock_script(project, "Fix off-by-one in get_item route", tmp_path)
    text = log_path.read_text(encoding="utf-8")
    assert any(line.startswith("Modified: app.py") for line in text.splitlines())
    assert "commit skipped" in text


def test_noop_task_neither_commits_nor_claims_evidence(tmp_path: Path) -> None:
    """A task whose fix pattern is absent (already fixed) must not commit
    anything - especially not unrelated worktree edits swept up by broad
    staging - and must not emit a ``Modified:`` evidence line for work it
    did not do (finding 3722894413).
    """

    def _count(project: Path) -> int:
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return int(out.strip())

    project = _make_demo_workdir(tmp_path)
    # First run applies the fix and commits it; the rerun sees no pattern.
    _run_mock_script(project, "Fix off-by-one in get_item route", tmp_path)
    after_first = _count(project)
    # An unrelated edit a resumed worktree might carry.
    unrelated = project / "requirements.txt"
    unrelated.write_text(unrelated.read_text() + "\n# unrelated local edit\n")

    log_path = project / ".sdd" / "runtime" / "agent-rerun.log"
    task_info = json.dumps({"workdir": str(project), "task_id": "rerun", "task_title": "Fix off-by-one in get_item route", "log_path": str(log_path)})
    script = tmp_path / "mock_script.py"
    subprocess.run([sys.executable, str(script), task_info], check=True, timeout=60, capture_output=True)

    assert _count(project) == after_first
    assert "Modified:" not in log_path.read_text(encoding="utf-8")
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert " M requirements.txt" in porcelain


def test_commit_contains_only_the_fixed_file(tmp_path: Path) -> None:
    """The task's commit must carry exactly the file the fix mutated; an
    unrelated dirty file in the worktree stays uncommitted (finding
    3722894413), and the evidence line lands only after the commit
    (finding 3722894421 - ordering pinned via the log).
    """
    project = _make_demo_workdir(tmp_path)
    unrelated = project / "requirements.txt"
    unrelated.write_text(unrelated.read_text() + "\n# unrelated local edit\n")

    log_path = _run_mock_script(project, "Fix health endpoint returns 201 instead of 200", tmp_path)

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert committed == ["app.py"]
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert " M requirements.txt" in porcelain

    lines = log_path.read_text(encoding="utf-8").splitlines()
    committed_at = next(i for i, ln in enumerate(lines) if "Committed fix:" in ln)
    evidence_at = next(i for i, ln in enumerate(lines) if ln.startswith("Modified: app.py"))
    assert committed_at < evidence_at

def test_mock_agent_attributes_evidence_to_correct_task_id(tmp_path: Path) -> None:
    """Two tasks with identical prompts must resolve to distinct task identities.

    Regression for issue #3629: prompt-based identity matching caused collisions
    when two tasks shared enough text. The mock adapter now receives a real
    task_id and writes it to the log, ensuring evidence lands against exactly
    one task regardless of prompt wording.
    """
    project = _make_demo_workdir(tmp_path)

    # Spawn session for Task A
    log_path_a = project / ".sdd" / "runtime" / "agent-task-a.log"
    task_info_a = json.dumps({
        "workdir": str(project),
        "log_path": str(log_path_a),
        "task_id": "task-a-123",
        "task_title": "Task A",
    })
    script_path = tmp_path / "mock_script.py"
    from bernstein.adapters.mock import MockAgentAdapter
    script_path.write_text(MockAgentAdapter._build_mock_script())

    subprocess.run(
        [sys.executable, str(script_path), task_info_a],
        check=True,
        timeout=60,
        capture_output=True,
    )

    # Spawn session for Task B
    log_path_b = project / ".sdd" / "runtime" / "agent-task-b.log"
    task_info_b = json.dumps({
        "workdir": str(project),
        "log_path": str(log_path_b),
        "task_id": "task-b-456",
        "task_title": "Task B",
    })
    subprocess.run(
        [sys.executable, str(script_path), task_info_b],
        check=True,
        timeout=60,
        capture_output=True,
    )

    # Assert that the logs contain the correct, distinct task_ids
    log_a = log_path_a.read_text(encoding="utf-8")
    log_b = log_path_b.read_text(encoding="utf-8")

    assert "TaskID: task-a-123" in log_a
    assert "TaskID: task-b-456" in log_b
    assert "task-a-123" not in log_b
    assert "task-b-456" not in log_a
