"""Tests for CheckpointState and WrapUpBrief session persistence models."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.session import (
    CheckpointState,
    WrapUpBrief,
    load_checkpoint,
    load_wrapup,
    save_checkpoint,
    save_wrapup,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# CheckpointState — serialization
# ---------------------------------------------------------------------------


def test_checkpoint_state_roundtrip() -> None:
    original = CheckpointState(
        timestamp=12345.0,
        completed_task_ids=["T-1", "T-2"],
        in_flight_task_ids=["T-3"],
        next_steps=["do X", "do Y"],
        goal="Build auth",
        cost_spent=1.25,
        git_sha="abc123def456",
    )
    restored = CheckpointState.from_dict(original.to_dict())
    assert restored.timestamp == pytest.approx(12345.0)
    assert restored.completed_task_ids == ["T-1", "T-2"]
    assert restored.in_flight_task_ids == ["T-3"]
    assert restored.next_steps == ["do X", "do Y"]
    assert restored.goal == "Build auth"
    assert restored.cost_spent == pytest.approx(1.25)
    assert restored.git_sha == "abc123def456"


def test_checkpoint_state_defaults() -> None:
    state = CheckpointState(timestamp=0.0, goal="minimal")
    assert state.completed_task_ids == []
    assert state.in_flight_task_ids == []
    assert state.next_steps == []
    assert state.cost_spent == pytest.approx(0.0)
    assert state.git_sha == ""


def test_checkpoint_state_from_dict_missing_optional_fields() -> None:
    data = {"timestamp": 99.0, "goal": "sparse"}
    state = CheckpointState.from_dict(data)
    assert state.timestamp == pytest.approx(99.0)
    assert state.goal == "sparse"
    assert state.completed_task_ids == []
    assert state.in_flight_task_ids == []
    assert state.next_steps == []
    assert state.cost_spent == pytest.approx(0.0)
    assert state.git_sha == ""


def test_checkpoint_state_from_dict_missing_timestamp_raises() -> None:
    with pytest.raises(KeyError):
        CheckpointState.from_dict({"goal": "no timestamp"})


# ---------------------------------------------------------------------------
# CheckpointState — staleness
# ---------------------------------------------------------------------------

CHECKPOINT_STALE_MINUTES = 30


def test_checkpoint_state_not_stale_when_fresh() -> None:
    state = CheckpointState(timestamp=time.time(), goal="fresh")
    assert not state.is_stale()


def test_checkpoint_state_stale_after_default_threshold() -> None:
    old_ts = time.time() - (CHECKPOINT_STALE_MINUTES * 60 + 1)
    state = CheckpointState(timestamp=old_ts, goal="old")
    assert state.is_stale()


def test_checkpoint_state_custom_stale_minutes() -> None:
    ten_min_ago = time.time() - (10 * 60 + 1)
    state = CheckpointState(timestamp=ten_min_ago, goal="ten min ago")
    assert state.is_stale(stale_minutes=10)
    assert not state.is_stale(stale_minutes=60)


# ---------------------------------------------------------------------------
# CheckpointState — file I/O
# ---------------------------------------------------------------------------


def test_save_checkpoint_writes_json_file(tmp_path: Path) -> None:
    state = CheckpointState(
        timestamp=12345.0,
        completed_task_ids=["T-1"],
        in_flight_task_ids=["T-2"],
        next_steps=["next"],
        goal="test goal",
        cost_spent=0.5,
        git_sha="deadbeef",
    )
    saved_path = save_checkpoint(tmp_path, state)
    assert saved_path.exists()
    assert saved_path.suffix == ".json"
    assert "checkpoint" in saved_path.name
    data = json.loads(saved_path.read_text())
    assert data["goal"] == "test goal"
    assert data["completed_task_ids"] == ["T-1"]
    assert data["in_flight_task_ids"] == ["T-2"]
    assert data["git_sha"] == "deadbeef"


def test_save_checkpoint_path_under_sdd_sessions(tmp_path: Path) -> None:
    state = CheckpointState(timestamp=time.time(), goal="path test")
    saved_path = save_checkpoint(tmp_path, state)
    assert saved_path.parent == tmp_path / ".sdd" / "sessions"


def test_load_checkpoint_returns_state(tmp_path: Path) -> None:
    state = CheckpointState(
        timestamp=time.time(),
        goal="load test",
        completed_task_ids=["T-A"],
        git_sha="cafebabe",
    )
    saved_path = save_checkpoint(tmp_path, state)
    loaded = load_checkpoint(saved_path)
    assert loaded is not None
    assert loaded.goal == "load test"
    assert loaded.completed_task_ids == ["T-A"]
    assert loaded.git_sha == "cafebabe"


def test_load_checkpoint_returns_none_for_missing_file(tmp_path: Path) -> None:
    result = load_checkpoint(tmp_path / "nonexistent.json")
    assert result is None


def test_load_checkpoint_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad-checkpoint.json"
    bad_file.write_text("{not valid json")
    result = load_checkpoint(bad_file)
    assert result is None


def test_save_checkpoint_creates_parent_dirs(tmp_path: Path) -> None:
    state = CheckpointState(timestamp=time.time(), goal="dir creation")
    save_checkpoint(tmp_path, state)
    assert (tmp_path / ".sdd" / "sessions").is_dir()


# ---------------------------------------------------------------------------
# WrapUpBrief — serialization
# ---------------------------------------------------------------------------


def test_wrapup_brief_roundtrip() -> None:
    original = WrapUpBrief(
        timestamp=99999.0,
        session_id="sess-abc",
        changes_summary="Added X, fixed Y",
        learnings=["prefer async", "use dataclasses"],
        next_session_brief="Continue with auth module",
        git_diff_stat="3 files changed, 42 insertions",
    )
    restored = WrapUpBrief.from_dict(original.to_dict())
    assert restored.timestamp == pytest.approx(99999.0)
    assert restored.session_id == "sess-abc"
    assert restored.changes_summary == "Added X, fixed Y"
    assert restored.learnings == ["prefer async", "use dataclasses"]
    assert restored.next_session_brief == "Continue with auth module"
    assert restored.git_diff_stat == "3 files changed, 42 insertions"


def test_wrapup_brief_defaults() -> None:
    brief = WrapUpBrief(timestamp=0.0, session_id="s1")
    assert brief.changes_summary == ""
    assert brief.learnings == []
    assert brief.next_session_brief == ""
    assert brief.git_diff_stat == ""


def test_wrapup_brief_from_dict_missing_optional_fields() -> None:
    data = {"timestamp": 1.0, "session_id": "s1"}
    brief = WrapUpBrief.from_dict(data)
    assert brief.session_id == "s1"
    assert brief.changes_summary == ""
    assert brief.learnings == []


def test_wrapup_brief_from_dict_missing_timestamp_raises() -> None:
    with pytest.raises(KeyError):
        WrapUpBrief.from_dict({"session_id": "no-ts"})


# ---------------------------------------------------------------------------
# WrapUpBrief — file I/O
# ---------------------------------------------------------------------------


def test_save_wrapup_writes_json_file(tmp_path: Path) -> None:
    brief = WrapUpBrief(
        timestamp=12345.0,
        session_id="sess-1",
        changes_summary="Did stuff",
        learnings=["learned A"],
        next_session_brief="Do more",
        git_diff_stat="1 file changed",
    )
    saved_path = save_wrapup(tmp_path, brief)
    assert saved_path.exists()
    assert saved_path.suffix == ".json"
    assert "wrapup" in saved_path.name
    data = json.loads(saved_path.read_text())
    assert data["session_id"] == "sess-1"
    assert data["changes_summary"] == "Did stuff"
    assert data["learnings"] == ["learned A"]


def test_save_wrapup_path_under_sdd_sessions(tmp_path: Path) -> None:
    brief = WrapUpBrief(timestamp=time.time(), session_id="s2")
    saved_path = save_wrapup(tmp_path, brief)
    assert saved_path.parent == tmp_path / ".sdd" / "sessions"


def test_load_wrapup_returns_brief(tmp_path: Path) -> None:
    brief = WrapUpBrief(
        timestamp=time.time(),
        session_id="s3",
        changes_summary="changes",
        git_diff_stat="2 files",
    )
    saved_path = save_wrapup(tmp_path, brief)
    loaded = load_wrapup(saved_path)
    assert loaded is not None
    assert loaded.session_id == "s3"
    assert loaded.changes_summary == "changes"
    assert loaded.git_diff_stat == "2 files"


def test_load_wrapup_returns_none_for_missing_file(tmp_path: Path) -> None:
    result = load_wrapup(tmp_path / "nonexistent.json")
    assert result is None


def test_load_wrapup_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad-wrapup.json"
    bad_file.write_text("not json at all")
    result = load_wrapup(bad_file)
    assert result is None


def test_save_wrapup_creates_parent_dirs(tmp_path: Path) -> None:
    brief = WrapUpBrief(timestamp=time.time(), session_id="dir-test")
    save_wrapup(tmp_path, brief)
    assert (tmp_path / ".sdd" / "sessions").is_dir()
