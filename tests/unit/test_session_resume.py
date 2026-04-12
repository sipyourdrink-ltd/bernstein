"""Tests for fast session resume (skip re-planning after brief stop)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.session import (
    DEFAULT_STALE_MINUTES,
    SessionState,
    discard_session,
    load_session,
    save_session,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# SessionState unit tests
# ---------------------------------------------------------------------------


def test_session_state_not_stale_when_fresh() -> None:
    state = SessionState(saved_at=time.time(), goal="test", completed_task_ids=[], cost_spent=0.0)
    assert not state.is_stale()


def test_session_state_stale_after_threshold() -> None:
    old_ts = time.time() - (DEFAULT_STALE_MINUTES * 60 + 1)
    state = SessionState(saved_at=old_ts, goal="test", completed_task_ids=[], cost_spent=0.0)
    assert state.is_stale()


def test_session_state_custom_stale_minutes() -> None:
    ten_min_ago = time.time() - (10 * 60 + 1)
    state = SessionState(saved_at=ten_min_ago, goal="test", completed_task_ids=[], cost_spent=0.0)
    assert state.is_stale(stale_minutes=10)
    assert not state.is_stale(stale_minutes=60)


def test_session_state_roundtrip() -> None:
    original = SessionState(
        saved_at=12345.0,
        goal="Build auth",
        completed_task_ids=["T-001", "T-002"],
        pending_task_ids=["T-003"],
        cost_spent=1.25,
    )
    restored = SessionState.from_dict(original.to_dict())
    assert restored.saved_at == pytest.approx(12345.0)
    assert restored.goal == "Build auth"
    assert restored.completed_task_ids == ["T-001", "T-002"]
    assert restored.pending_task_ids == ["T-003"]
    assert restored.cost_spent == pytest.approx(1.25)


def test_session_state_roundtrip_without_pending() -> None:
    """Backward compat: sessions saved without pending_task_ids still load."""
    data = {
        "saved_at": 12345.0,
        "goal": "old format",
        "completed_task_ids": ["T-001"],
        "cost_spent": 0.5,
    }
    restored = SessionState.from_dict(data)
    assert restored.pending_task_ids == []


# ---------------------------------------------------------------------------
# save_session / load_session / discard_session
# ---------------------------------------------------------------------------


def test_save_writes_json_to_sdd_runtime(tmp_path: Path) -> None:
    state = SessionState(
        saved_at=time.time(),
        goal="test goal",
        completed_task_ids=["T-1"],
        pending_task_ids=["T-2"],
        cost_spent=0.5,
    )
    save_session(tmp_path, state)
    session_file = tmp_path / ".sdd" / "runtime" / "session.json"
    assert session_file.exists()
    data = json.loads(session_file.read_text())
    assert data["goal"] == "test goal"
    assert data["completed_task_ids"] == ["T-1"]
    assert data["pending_task_ids"] == ["T-2"]
    assert data["cost_spent"] == pytest.approx(0.5)


def test_load_returns_state_for_fresh_session(tmp_path: Path) -> None:
    state = SessionState(
        saved_at=time.time(),
        goal="load test",
        completed_task_ids=["T-A"],
        pending_task_ids=["T-B"],
        cost_spent=0.1,
    )
    save_session(tmp_path, state)
    loaded = load_session(tmp_path)
    assert loaded is not None
    assert loaded.goal == "load test"
    assert loaded.completed_task_ids == ["T-A"]
    assert loaded.pending_task_ids == ["T-B"]


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    result = load_session(tmp_path)
    assert result is None


def test_load_returns_none_when_stale(tmp_path: Path) -> None:
    old_ts = time.time() - (DEFAULT_STALE_MINUTES * 60 + 60)
    state = SessionState(saved_at=old_ts, goal="old", completed_task_ids=[], cost_spent=0.0)
    save_session(tmp_path, state)
    result = load_session(tmp_path)
    assert result is None


def test_load_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session.json").write_text("{not valid json")
    result = load_session(tmp_path)
    assert result is None


def test_load_returns_none_on_missing_saved_at(tmp_path: Path) -> None:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session.json").write_text(json.dumps({"goal": "test"}))
    result = load_session(tmp_path)
    assert result is None


def test_discard_removes_session_file(tmp_path: Path) -> None:
    state = SessionState(saved_at=time.time(), goal="discard me", completed_task_ids=[], cost_spent=0.0)
    save_session(tmp_path, state)
    assert (tmp_path / ".sdd" / "runtime" / "session.json").exists()
    discard_session(tmp_path)
    assert not (tmp_path / ".sdd" / "runtime" / "session.json").exists()


def test_discard_is_noop_when_no_file(tmp_path: Path) -> None:
    # Should not raise when file doesn't exist
    discard_session(tmp_path)


def test_load_respects_custom_stale_minutes(tmp_path: Path) -> None:
    # 5 minutes old — stale at 4 min, fresh at 10 min
    ts = time.time() - 5 * 60
    state = SessionState(saved_at=ts, goal="custom stale", completed_task_ids=[], cost_spent=0.0)
    save_session(tmp_path, state)
    assert load_session(tmp_path, stale_minutes=4) is None
    assert load_session(tmp_path, stale_minutes=10) is not None


# ---------------------------------------------------------------------------
# Bootstrap integration: check_resume_session
# ---------------------------------------------------------------------------


def test_check_resume_returns_session_when_valid(tmp_path: Path) -> None:
    from bernstein.core.session import check_resume_session

    state = SessionState(
        saved_at=time.time(),
        goal="resume me",
        completed_task_ids=["T-1", "T-2"],
        pending_task_ids=["T-3"],
        cost_spent=0.8,
    )
    save_session(tmp_path, state)
    result = check_resume_session(tmp_path)
    assert result is not None
    assert result.goal == "resume me"
    assert result.pending_task_ids == ["T-3"]


def test_check_resume_returns_none_when_no_session(tmp_path: Path) -> None:
    from bernstein.core.session import check_resume_session

    result = check_resume_session(tmp_path)
    assert result is None


def test_check_resume_returns_none_when_fresh_flag(tmp_path: Path) -> None:
    from bernstein.core.session import check_resume_session

    state = SessionState(saved_at=time.time(), goal="ignore me", completed_task_ids=[], cost_spent=0.0)
    save_session(tmp_path, state)
    result = check_resume_session(tmp_path, force_fresh=True)
    assert result is None


# ---------------------------------------------------------------------------
# Orchestrator._save_session_state integration
# ---------------------------------------------------------------------------


def test_orchestrator_save_session_state(tmp_path: Path) -> None:
    """Orchestrator._save_session_state writes session.json with task statuses."""
    from bernstein.core.models import OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator

    mock_spawner = MagicMock()
    config = OrchestratorConfig(server_url="http://localhost:19999")

    # Mock the httpx client to return task data
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"id": "T-done-1", "status": "done"},
        {"id": "T-done-2", "status": "done"},
        {"id": "T-wip", "status": "in_progress"},
        {"id": "T-claimed", "status": "claimed"},
        {"id": "T-open", "status": "open"},
    ]
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    # Patch evolution/metrics to avoid side effects
    with patch("bernstein.core.orchestration.orchestrator.EvolutionCoordinator"), patch("bernstein.core.orchestration.orchestrator.get_collector"):
        orch = Orchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            client=mock_client,
        )

    orch._save_session_state()

    session_file = tmp_path / ".sdd" / "runtime" / "session.json"
    assert session_file.exists()
    data = json.loads(session_file.read_text())
    assert sorted(data["completed_task_ids"]) == ["T-done-1", "T-done-2"]
    assert sorted(data["pending_task_ids"]) == ["T-claimed", "T-wip"]


def test_orchestrator_save_session_state_server_down(tmp_path: Path) -> None:
    """_save_session_state is best-effort — doesn't raise if server is down."""
    from bernstein.core.models import OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator

    mock_spawner = MagicMock()
    config = OrchestratorConfig(server_url="http://localhost:19999")

    mock_client = MagicMock()
    mock_client.get.side_effect = Exception("Connection refused")

    with patch("bernstein.core.orchestration.orchestrator.EvolutionCoordinator"), patch("bernstein.core.orchestration.orchestrator.get_collector"):
        orch = Orchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            client=mock_client,
        )

    # Should not raise
    orch._save_session_state()

    # No session file should be written
    session_file = tmp_path / ".sdd" / "runtime" / "session.json"
    assert not session_file.exists()
