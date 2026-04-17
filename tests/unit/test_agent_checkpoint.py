"""Unit tests for agent-level WAL crash recovery (agent_checkpoint)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bernstein.core.persistence.agent_checkpoint import (
    AgentCheckpoint,
    build_resume_prompt,
    is_checkpoint_recoverable,
    load_checkpoint,
    save_checkpoint,
    scan_orphaned_checkpoints,
)


def _make_checkpoint(
    *,
    agent_id: str = "agent-1",
    task_id: str = "task-1",
    worktree_path: str = "/tmp/worktree",
    files_modified: list[str] | None = None,
    last_output: str = "",
    step_count: int = 0,
    elapsed_seconds: float = 0.0,
) -> AgentCheckpoint:
    return AgentCheckpoint(
        agent_id=agent_id,
        task_id=task_id,
        worktree_path=worktree_path,
        files_modified=files_modified or [],
        last_output=last_output,
        step_count=step_count,
        elapsed_seconds=elapsed_seconds,
    )


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo for tests."""
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# save / load roundtrip
# ---------------------------------------------------------------------------


def test_save_checkpoint_creates_file(tmp_path: Path) -> None:
    checkpoint = _make_checkpoint(agent_id="a", task_id="t")
    written = save_checkpoint(checkpoint, tmp_path)
    assert written.exists()
    assert written == tmp_path / "agents" / "a" / "checkpoint.json"


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    checkpoint = _make_checkpoint(
        agent_id="ag-7",
        task_id="task-42",
        worktree_path="/tmp/wt",
        files_modified=["a.py", "b.py"],
        last_output="hello world",
        step_count=3,
        elapsed_seconds=12.5,
    )
    save_checkpoint(checkpoint, tmp_path)
    loaded = load_checkpoint("ag-7", tmp_path)
    assert loaded is not None
    assert loaded.agent_id == "ag-7"
    assert loaded.task_id == "task-42"
    assert loaded.files_modified == ["a.py", "b.py"]
    assert loaded.last_output == "hello world"
    assert loaded.step_count == 3
    assert loaded.elapsed_seconds == pytest.approx(12.5)


def test_load_checkpoint_missing_returns_none(tmp_path: Path) -> None:
    assert load_checkpoint("does-not-exist", tmp_path) is None


def test_save_checkpoint_overwrites_existing(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="x", step_count=1), tmp_path)
    save_checkpoint(_make_checkpoint(agent_id="x", step_count=2), tmp_path)
    loaded = load_checkpoint("x", tmp_path)
    assert loaded is not None
    assert loaded.step_count == 2


def test_save_checkpoint_writes_sorted_json(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="z"), tmp_path)
    raw = (tmp_path / "agents" / "z" / "checkpoint.json").read_text()
    data = json.loads(raw)
    # Keys should be alphabetical
    assert list(data.keys()) == sorted(data.keys())


# ---------------------------------------------------------------------------
# scan_orphaned_checkpoints
# ---------------------------------------------------------------------------


def test_scan_orphans_no_agents_dir(tmp_path: Path) -> None:
    assert scan_orphaned_checkpoints(tmp_path) == []


def test_scan_orphans_empty_agents_dir(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    assert scan_orphaned_checkpoints(tmp_path) == []


def test_scan_orphans_finds_missing_pid(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="orphan"), tmp_path)
    # No pid file at all → orphaned
    orphans = scan_orphaned_checkpoints(tmp_path)
    assert len(orphans) == 1
    assert orphans[0].agent_id == "orphan"


def test_scan_orphans_finds_dead_pid(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="dead"), tmp_path)
    pid_path = tmp_path / "agents" / "dead" / "pid"
    # Use a PID very likely to not exist
    pid_path.write_text("999999\n")
    orphans = scan_orphaned_checkpoints(tmp_path)
    assert [c.agent_id for c in orphans] == ["dead"]


def test_scan_orphans_skips_live_process(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="live"), tmp_path)
    pid_path = tmp_path / "agents" / "live" / "pid"
    pid_path.write_text(f"{os.getpid()}\n")
    orphans = scan_orphaned_checkpoints(tmp_path)
    assert orphans == []


def test_scan_orphans_skips_non_dir_entries(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "stray-file").write_text("not a dir")
    assert scan_orphaned_checkpoints(tmp_path) == []


def test_scan_orphans_skips_missing_checkpoint(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agents" / "no-cp"
    agent_dir.mkdir(parents=True)
    # No checkpoint.json present
    assert scan_orphaned_checkpoints(tmp_path) == []


def test_scan_orphans_tolerates_corrupt_json(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agents" / "bad"
    agent_dir.mkdir(parents=True)
    (agent_dir / "checkpoint.json").write_text("{not valid json")
    # Corrupt entries are skipped, not raised
    assert scan_orphaned_checkpoints(tmp_path) == []


def test_scan_orphans_tolerates_bad_pid_file(tmp_path: Path) -> None:
    save_checkpoint(_make_checkpoint(agent_id="bad-pid"), tmp_path)
    (tmp_path / "agents" / "bad-pid" / "pid").write_text("not-a-number")
    # Unparseable pid → treated as dead → orphan
    orphans = scan_orphaned_checkpoints(tmp_path)
    assert [c.agent_id for c in orphans] == ["bad-pid"]


# ---------------------------------------------------------------------------
# is_checkpoint_recoverable
# ---------------------------------------------------------------------------


def test_is_recoverable_missing_worktree(tmp_path: Path) -> None:
    checkpoint = _make_checkpoint(worktree_path=str(tmp_path / "does-not-exist"))
    recoverable, reason = is_checkpoint_recoverable(checkpoint)
    assert recoverable is False
    assert "missing" in reason


def test_is_recoverable_not_git(tmp_path: Path) -> None:
    # Plain directory, no git
    checkpoint = _make_checkpoint(worktree_path=str(tmp_path))
    recoverable, reason = is_checkpoint_recoverable(checkpoint)
    assert recoverable is False
    assert "git" in reason


def test_is_recoverable_clean_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    checkpoint = _make_checkpoint(worktree_path=str(tmp_path))
    recoverable, reason = is_checkpoint_recoverable(checkpoint)
    assert recoverable is False
    assert "clean" in reason


def test_is_recoverable_with_uncommitted_changes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "new_file.py").write_text("print('hi')\n")
    checkpoint = _make_checkpoint(worktree_path=str(tmp_path))
    recoverable, reason = is_checkpoint_recoverable(checkpoint)
    assert recoverable is True
    assert "uncommitted" in reason


# ---------------------------------------------------------------------------
# build_resume_prompt
# ---------------------------------------------------------------------------


def test_build_resume_prompt_includes_goal_and_steps() -> None:
    checkpoint = _make_checkpoint(
        files_modified=["foo.py", "bar.py"],
        step_count=5,
        elapsed_seconds=37.0,
        last_output="all good",
    )
    prompt = build_resume_prompt(checkpoint, "Implement widget")
    assert "Implement widget" in prompt
    assert "5" in prompt
    assert "37" in prompt
    assert "foo.py" in prompt
    assert "bar.py" in prompt
    assert "all good" in prompt


def test_build_resume_prompt_handles_no_files() -> None:
    checkpoint = _make_checkpoint()
    prompt = build_resume_prompt(checkpoint, "Do the thing")
    assert "(none yet)" in prompt


def test_build_resume_prompt_truncates_long_output() -> None:
    big = "x" * 5000
    checkpoint = _make_checkpoint(last_output=big)
    prompt = build_resume_prompt(checkpoint, "Goal")
    # The last_output slice must be capped at 2000 chars
    assert "x" * 2000 in prompt
    assert "x" * 2001 not in prompt


def test_build_resume_prompt_limits_files_list() -> None:
    many_files = [f"file_{i:03d}.py" for i in range(25)]
    checkpoint = _make_checkpoint(files_modified=many_files)
    prompt = build_resume_prompt(checkpoint, "Goal")
    # Only first 10 file names should appear
    assert "file_000.py" in prompt
    assert "file_009.py" in prompt
    assert "file_010.py" not in prompt
    assert "file_024.py" not in prompt


def test_build_resume_prompt_contains_instructions() -> None:
    checkpoint = _make_checkpoint()
    prompt = build_resume_prompt(checkpoint, "Goal")
    # Must tell the agent NOT to restart from scratch
    assert "Do NOT restart" in prompt
    assert "Resuming from checkpoint" in prompt
