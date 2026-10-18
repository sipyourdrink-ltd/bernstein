"""Issue #5112: a worktree whose task is confirmed terminal, live PID or not.

``classify_worktrees`` decides ``ORPHAN``/``STALE``/``CORRUPT``/``ACTIVE`` from
process liveness and trace-file age -- never from the task record's actual
status. A PID can outlive the task it served: reparented, wedged in cleanup,
or slow to exit after its last write. ``sweep_leaked_worktrees`` asks the
authoritative question instead -- what does the task log say -- and lists a
worktree whenever the task log says done, independent of what the liveness
heuristic concluded.

Fixture idiom borrowed from ``tests/unit/test_worktrees_cmd.py``: fake (no
real git) worktree directories are enough here since this sweep never probes
git state, only the classifier's own PID/task-id fields and the task log.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bernstein.core.worktrees.leak_sweep import sweep_leaked_worktrees
from bernstein.core.worktrees.task_status import read_task_statuses, status_is_terminal, task_is_terminal


def _init_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_root)], check=True)
    (repo_root / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "user.email=test@bernstein",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
    )


def _make_worktree_dir(repo_root: Path, session_id: str) -> Path:
    base = repo_root / ".sdd" / "runtime" / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    wt = base / session_id
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nowhere\n")
    return wt


def _write_pid_record(repo_root: Path, session_id: str, *, pid: int, task_id: str) -> None:
    pid_dir = repo_root / ".sdd" / "runtime" / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / f"{session_id}.json").write_text(json.dumps({"worker_pid": pid, "task_id": task_id}))


def _write_task_record(repo_root: Path, task_id: str, status: str, *, archive: bool = False) -> None:
    subdir = "archive" if archive else "runtime"
    log_path = repo_root / ".sdd" / subdir / "tasks.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": task_id, "status": status}) + "\n")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


class TestTaskStatusReader:
    def test_no_log_at_all_is_undecidable(self, repo_root: Path) -> None:
        assert task_is_terminal(repo_root / ".sdd", "task-x") is None
        assert read_task_statuses(repo_root / ".sdd") == {}

    def test_reads_the_latest_status_for_a_task(self, repo_root: Path) -> None:
        _write_task_record(repo_root, "task-1", "open")
        _write_task_record(repo_root, "task-1", "in_progress")
        _write_task_record(repo_root, "task-1", "done")

        assert read_task_statuses(repo_root / ".sdd") == {"task-1": "done"}
        assert task_is_terminal(repo_root / ".sdd", "task-1") is True

    def test_a_non_terminal_status_is_false_not_none(self, repo_root: Path) -> None:
        _write_task_record(repo_root, "task-2", "open")
        assert task_is_terminal(repo_root / ".sdd", "task-2") is False

    def test_a_torn_line_is_skipped_not_fatal(self, repo_root: Path) -> None:
        _write_task_record(repo_root, "task-3", "done")
        log_path = repo_root / ".sdd" / "runtime" / "tasks.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write('{"id": "task-4", "status": "open"' + "\n")  # torn: missing closing brace

        statuses = read_task_statuses(repo_root / ".sdd")
        assert statuses == {"task-3": "done"}

    def test_a_task_present_in_both_logs_takes_the_archive_value(self, repo_root: Path) -> None:
        """Archival happens once a task already reached its final status.

        The live log's own record for the same id can predate that --
        whatever it last said before the task was filed away -- so the
        archive is the newer, authoritative one where the two disagree.
        """
        _write_task_record(repo_root, "task-5", "in_progress")  # live: stale, pre-archival
        _write_task_record(repo_root, "task-5", "done", archive=True)  # archive: the real ending

        assert read_task_statuses(repo_root / ".sdd") == {"task-5": "done"}
        assert task_is_terminal(repo_root / ".sdd", "task-5") is True


class TestStatusIsTerminal:
    """The pure predicate leak_sweep applies against an already-read mapping."""

    def test_a_terminal_status_is_true(self) -> None:
        assert status_is_terminal("done") is True

    def test_a_non_terminal_status_is_false(self) -> None:
        assert status_is_terminal("open") is False

    def test_no_status_at_all_is_none(self) -> None:
        assert status_is_terminal(None) is None


class TestSweepLeakedWorktrees:
    def test_sweep_lists_worktree_whose_task_is_terminal(self, repo_root: Path) -> None:
        """The load-bearing case: PID alive, task log says done."""
        sid = "leaked"
        _make_worktree_dir(repo_root, sid)
        _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="task-leaked")
        _write_task_record(repo_root, "task-leaked", "done")

        leaked = sweep_leaked_worktrees(repo_root)

        assert [entry.worktree.session_id for entry in leaked] == [sid]
        assert leaked[0].task_status == "done"

    def test_a_worktree_with_a_live_task_is_not_listed(self, repo_root: Path) -> None:
        sid = "still-running"
        _make_worktree_dir(repo_root, sid)
        _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="task-running")
        _write_task_record(repo_root, "task-running", "in_progress")

        assert sweep_leaked_worktrees(repo_root) == []

    def test_a_worktree_with_no_task_record_at_all_is_not_listed(self, repo_root: Path) -> None:
        """Undecidable is not the same as leaked -- an orphan with no record is left to the existing heuristic."""
        sid = "no-record"
        _make_worktree_dir(repo_root, sid)
        _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="task-unknown")

        assert sweep_leaked_worktrees(repo_root) == []

    def test_an_orphan_worktree_with_no_pid_record_is_not_listed(self, repo_root: Path) -> None:
        """No PID record means no task id to look up -- the sweep has nothing to check."""
        _make_worktree_dir(repo_root, "orphan")
        assert sweep_leaked_worktrees(repo_root) == []

    def test_multiple_leaked_worktrees_are_listed_sorted_by_session_id(self, repo_root: Path) -> None:
        """Written out of order on purpose: the sort is this function's own guarantee, not inherited."""
        for sid, task_id, status in (("zebra", "task-z", "failed"), ("apple", "task-a", "done")):
            _make_worktree_dir(repo_root, sid)
            _write_pid_record(repo_root, sid, pid=os.getpid(), task_id=task_id)
            _write_task_record(repo_root, task_id, status)

        leaked = sweep_leaked_worktrees(repo_root)
        assert [entry.worktree.session_id for entry in leaked] == ["apple", "zebra"]

    def test_an_archived_task_record_is_found_too(self, repo_root: Path) -> None:
        sid = "archived"
        _make_worktree_dir(repo_root, sid)
        _write_pid_record(repo_root, sid, pid=os.getpid(), task_id="task-archived")
        _write_task_record(repo_root, "task-archived", "closed", archive=True)

        leaked = sweep_leaked_worktrees(repo_root)
        assert [entry.worktree.session_id for entry in leaked] == [sid]
        assert leaked[0].task_status == "closed"
