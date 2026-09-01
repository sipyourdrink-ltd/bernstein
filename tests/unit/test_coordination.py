"""Tests for team coordination: bulletin summaries, prompt injection, conflict detection."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Test task",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description="Description.",
        role=role,
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        owned_files=owned_files or [],
    )


def _make_session(*, id: str = "backend-abc123", status: str = "working") -> AgentSession:
    return AgentSession(
        id=id,
        role="backend",
        task_ids=[],
        model_config=ModelConfig(model="sonnet", effort="normal"),
        status=status,
    )


# ---------------------------------------------------------------------------
# BulletinBoard.summary()
# ---------------------------------------------------------------------------


def test_bulletin_summary_empty_board():
    """Empty board returns empty string."""
    board = BulletinBoard()
    assert board.summary() == ""


def test_bulletin_summary_single_message():
    """A single message is included in the summary."""
    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="backend-abc", type="status", content="created src/auth.py"))
    summary = board.summary()
    assert "backend-abc" in summary
    assert "created src/auth.py" in summary


def test_bulletin_summary_respects_limit():
    """summary(limit=N) returns at most N messages."""
    board = BulletinBoard()
    for i in range(15):
        board.post(BulletinMessage(agent_id=f"agent-{i}", type="status", content=f"message {i}"))
    summary = board.summary(limit=5)
    # Should contain last 5 messages (10-14)
    assert "message 14" in summary
    assert "message 10" in summary
    # Should NOT contain early messages
    assert "message 4" not in summary


def test_bulletin_summary_default_limit_is_10():
    """Default limit is 10 messages."""
    board = BulletinBoard()
    for i in range(20):
        board.post(BulletinMessage(agent_id=f"agent-{i}", type="status", content=f"msg {i}"))
    summary = board.summary()
    # Last 10: messages 10-19
    assert "msg 19" in summary
    assert "msg 10" in summary
    assert "msg 9" not in summary


# ---------------------------------------------------------------------------
# BulletinBoard.post_file_created()
# ---------------------------------------------------------------------------


def test_post_file_created_adds_message():
    """post_file_created posts a status message with file path and classes."""
    board = BulletinBoard()
    board.post_file_created("backend-xyz", "src/auth.py", ["AuthMiddleware", "TokenValidator"])
    msgs = board.read_by_type("status")
    assert len(msgs) == 1
    assert "src/auth.py" in msgs[0].content
    assert "AuthMiddleware" in msgs[0].content
    assert msgs[0].agent_id == "backend-xyz"


def test_post_file_created_no_classes():
    """post_file_created works with empty classes list."""
    board = BulletinBoard()
    board.post_file_created("qa-abc", "tests/test_auth.py", [])
    msgs = board.read_by_type("status")
    assert len(msgs) == 1
    assert "tests/test_auth.py" in msgs[0].content


# ---------------------------------------------------------------------------
# BulletinBoard.post_api_endpoint()
# ---------------------------------------------------------------------------


def test_post_api_endpoint_adds_message():
    """post_api_endpoint posts a finding message with method, route."""
    board = BulletinBoard()
    board.post_api_endpoint("backend-xyz", "POST", "/auth/login", "{token, refresh_token}")
    msgs = board.read_by_type("finding")
    assert len(msgs) == 1
    assert "POST" in msgs[0].content
    assert "/auth/login" in msgs[0].content
    assert msgs[0].agent_id == "backend-xyz"


def test_post_api_endpoint_without_response():
    """post_api_endpoint works without response description."""
    board = BulletinBoard()
    board.post_api_endpoint("backend-xyz", "GET", "/health")
    msgs = board.read_by_type("finding")
    assert len(msgs) == 1
    assert "GET" in msgs[0].content
    assert "/health" in msgs[0].content


# ---------------------------------------------------------------------------
# Spawner bulletin injection
# ---------------------------------------------------------------------------


def test_spawn_prompt_includes_bulletin_summary(tmp_path: Path):
    """When spawner has a bulletin with messages, the agent prompt includes a team awareness section."""
    from bernstein.core.spawner import AgentSpawner

    from bernstein.adapters.base import CLIAdapter, SpawnResult

    board = BulletinBoard()
    board.post(BulletinMessage(agent_id="backend-abc", type="status", content="created src/auth.py"))

    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=12345, log_path=tmp_path / "agent.log")
    adapter.is_rate_limited.return_value = False

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=templates_dir,
        workdir=tmp_path,
        bulletin=board,
        default_model="mock-model",
    )

    task = _make_task()
    spawner.spawn_for_tasks([task])

    captured_prompt = adapter.spawn.call_args[1]["prompt"]
    assert "Team awareness" in captured_prompt or "team awareness" in captured_prompt.lower()
    assert "backend-abc" in captured_prompt
    assert "created src/auth.py" in captured_prompt


def test_spawn_prompt_no_bulletin_section_without_board(tmp_path: Path):
    """When no bulletin is provided, the prompt has no team awareness section."""
    from bernstein.core.spawner import AgentSpawner

    from bernstein.adapters.base import CLIAdapter, SpawnResult

    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=12345, log_path=tmp_path / "agent.log")
    adapter.is_rate_limited.return_value = False

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=templates_dir,
        workdir=tmp_path,
        default_model="mock-model",
    )

    task = _make_task()
    spawner.spawn_for_tasks([task])

    captured_prompt = adapter.spawn.call_args[1]["prompt"]
    assert "Team awareness" not in captured_prompt


def test_spawn_prompt_no_bulletin_section_with_empty_board(tmp_path: Path):
    """Empty bulletin board produces no team awareness section (nothing to show)."""
    from bernstein.core.spawner import AgentSpawner

    from bernstein.adapters.base import CLIAdapter, SpawnResult

    board = BulletinBoard()  # empty

    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=12345, log_path=tmp_path / "agent.log")
    adapter.is_rate_limited.return_value = False

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=templates_dir,
        workdir=tmp_path,
        bulletin=board,
        default_model="mock-model",
    )

    task = _make_task()
    spawner.spawn_for_tasks([task])

    captured_prompt = adapter.spawn.call_args[1]["prompt"]
    assert "Team awareness" not in captured_prompt


# ---------------------------------------------------------------------------
# Orchestrator file conflict detection
# ---------------------------------------------------------------------------


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    """Create a minimal Orchestrator for testing conflict detection."""
    from bernstein.core.spawner import AgentSpawner

    from bernstein.adapters.base import CLIAdapter, SpawnResult

    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=999, log_path=tmp_path / "agent.log")
    adapter.is_rate_limited.return_value = False

    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=templates_dir,
        workdir=tmp_path,
    )
    config = OrchestratorConfig(max_agents=4)
    client = MagicMock(spec=__import__("httpx").Client)
    # Patch manifest to avoid JSON-serializing mock adapter
    with (
        patch("bernstein.core.orchestration.orchestrator.build_manifest", return_value=MagicMock()),
        patch("bernstein.core.orchestration.orchestrator.save_manifest"),
    ):
        orch = Orchestrator(config=config, spawner=spawner, workdir=tmp_path, client=client)
    return orch


def test_check_file_overlap_no_conflict(tmp_path: Path):
    """Tasks with no owned_files never trigger a conflict."""
    orch = _make_orchestrator(tmp_path)
    batch = [_make_task(owned_files=[])]
    assert orch._check_file_overlap(batch) is False


def _claim(orch: Orchestrator, agent_id: str, *files: str) -> None:
    """Take ownership of *files* for *agent_id* the way production does.

    ``FileLockManager`` is the only writer of ownership; ``_file_ownership``
    is a read-only projection of it. These tests used to seed ownership by
    assigning into that projection, which mutated a throwaway dict and left
    the orchestrator owning nothing - the assertion three lines later failed
    with no hint about where the claim went.
    """
    conflicts = orch._lock_manager.acquire(
        list(files),
        agent_id=agent_id,
        task_id="T-001",
        task_title="Test task",
    )
    assert conflicts == [], f"fixture could not claim {files}: held by {conflicts}"


def test_check_file_overlap_detects_active_agent(tmp_path: Path):
    """Conflict detected when an active agent owns a file in the batch."""
    orch = _make_orchestrator(tmp_path)

    # Simulate agent owning src/utils.py
    session = _make_session(id="backend-abc", status="working")
    orch._agents["backend-abc"] = session
    _claim(orch, "backend-abc", "src/utils.py")

    batch = [_make_task(owned_files=["src/utils.py"])]
    assert orch._check_file_overlap(batch) is True


def test_reaping_a_dead_agent_frees_its_files_for_the_next_batch(tmp_path: Path):
    """A dead agent does not block a new batch, because reaping releases its locks.

    The guarantee is unchanged; the mechanism that provides it moved. Ownership
    used to be filtered against ``AgentSession.status`` on every overlap check,
    so a lock left behind by a dead agent was ignored in place. It is now
    released when the agent is reaped, which means the release is the thing
    worth asserting - checking overlap against a lock nobody released would
    pass only while the TTL happened to be short.
    """
    orch = _make_orchestrator(tmp_path)

    session = _make_session(id="backend-dead", status="dead")
    orch._agents["backend-dead"] = session
    _claim(orch, "backend-dead", "src/utils.py")

    batch = [_make_task(owned_files=["src/utils.py"])]
    assert orch._check_file_overlap(batch) is True, "an unreleased lock still holds the batch"

    orch._release_file_ownership("backend-dead")
    assert orch._check_file_overlap(batch) is False


def test_check_file_overlap_different_files_no_conflict(tmp_path: Path):
    """No conflict when tasks own different files from the active agent."""
    orch = _make_orchestrator(tmp_path)

    session = _make_session(id="backend-abc", status="working")
    orch._agents["backend-abc"] = session
    _claim(orch, "backend-abc", "src/auth.py")

    batch = [_make_task(owned_files=["src/users.py"])]
    assert orch._check_file_overlap(batch) is False


def test_writing_to_the_ownership_projection_raises_instead_of_vanishing(tmp_path: Path):
    """``_file_ownership`` is a projection, and a write to it must fail loudly.

    A plain dict rebuilt on each access accepts the assignment and throws it
    away, so the caller keeps running with a claim that was never made. That
    is a silent no-op in the one place a missed claim lets two agents edit the
    same file. The proxy turns it into a ``TypeError`` on the offending line.
    """
    orch = _make_orchestrator(tmp_path)

    with pytest.raises(TypeError):
        orch._file_ownership["src/utils.py"] = "backend-abc"  # type: ignore[index]

    _claim(orch, "backend-abc", "src/utils.py")
    assert orch._file_ownership == {"src/utils.py": "backend-abc"}
