import json
import time
from pathlib import Path

import pytest
from bernstein.core.session import SessionState, discard_session, load_session, save_session


def test_session_state_to_from_dict() -> None:
    """Test SessionState serialization and deserialization."""
    state = SessionState(
        saved_at=123456789.0,
        goal="test goal",
        completed_task_ids=["task-1", "task-2"],
        pending_task_ids=["task-3"],
        cost_spent=1.5,
    )
    data = state.to_dict()
    assert data["saved_at"] == pytest.approx(123456789.0)
    assert data["goal"] == "test goal"

    new_state = SessionState.from_dict(data)
    assert new_state == state


def test_session_state_from_dict_invalid() -> None:
    """Test SessionState.from_dict with invalid data."""
    with pytest.raises(KeyError):
        SessionState.from_dict({})

    with pytest.raises(ValueError):
        SessionState.from_dict({"saved_at": "not-a-float"})


def test_session_state_is_stale() -> None:
    """Test session staleness logic."""
    now = time.time()
    fresh_state = SessionState(saved_at=now - 60)  # 1 minute ago
    assert not fresh_state.is_stale(stale_minutes=30)

    stale_state = SessionState(saved_at=now - 3600)  # 60 minutes ago
    assert stale_state.is_stale(stale_minutes=30)


def test_save_and_load_session_round_trip(tmp_path: Path) -> None:
    """Test saving and then loading a session from disk."""
    workdir = tmp_path
    state = SessionState(
        saved_at=time.time(), goal="persistent goal", completed_task_ids=["A"], pending_task_ids=["B"], cost_spent=2.0
    )

    save_session(workdir, state)

    loaded = load_session(workdir)
    assert loaded is not None
    assert abs(loaded.saved_at - state.saved_at) < 0.001
    assert loaded.goal == "persistent goal"
    assert loaded.completed_task_ids == ["A"]
    assert loaded.pending_task_ids == ["B"]
    assert loaded.cost_spent == pytest.approx(2.0)


def test_load_session_missing_file(tmp_path: Path) -> None:
    """Test loading a session when no file exists."""
    assert load_session(tmp_path) is None


def test_load_session_stale_file(tmp_path: Path) -> None:
    """Test loading a session that is older than the stale threshold."""
    workdir = tmp_path
    stale_time = time.time() - 3600  # 60 minutes ago
    state = SessionState(saved_at=stale_time)
    save_session(workdir, state)

    # Should be None with default 30 minute threshold
    assert load_session(workdir, stale_minutes=30) is None

    # Should load with 120 minute threshold
    assert load_session(workdir, stale_minutes=120) is not None


def test_load_session_corrupt_json(tmp_path: Path) -> None:
    """Test loading a session from a file with invalid JSON."""
    workdir = tmp_path
    session_path = workdir / ".sdd" / "runtime" / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("this is not {json}")

    assert load_session(workdir) is None


def test_load_session_invalid_schema(tmp_path: Path) -> None:
    """Test loading a session from a JSON file with missing required fields."""
    workdir = tmp_path
    session_path = workdir / ".sdd" / "runtime" / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({"goal": "no saved_at field"}))

    assert load_session(workdir) is None


def test_discard_session(tmp_path: Path) -> None:
    """Test removing the session file."""
    workdir = tmp_path
    state = SessionState(saved_at=time.time())
    save_session(workdir, state)

    session_path = workdir / ".sdd" / "runtime" / "session.json"
    assert session_path.exists()

    discard_session(workdir)
    assert not session_path.exists()

    # Calling discard on missing file should not raise
    discard_session(workdir)
