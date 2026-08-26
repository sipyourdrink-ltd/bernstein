"""Tests for ``bernstein.cli.commands.wrap_up_cmd`` utilities and CLI.

The goal is to exercise the helper functions that build the wrap‑up brief
and to ensure the ``wrap-up`` command runs without raising unexpected
exceptions (e.g. the ``base`` variable typo).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

# Import the module under test
from bernstein.cli.commands import wrap_up_cmd

# ---------------------------------------------------------------------------
# Helper function monkey‑patching utilities
# ---------------------------------------------------------------------------


def _mock_subprocess_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return a callable that mimics ``subprocess.run``.

    The returned function accepts the same arguments as ``subprocess.run``
    and returns an object with ``returncode``, ``stdout`` and ``stderr`` attributes.
    """

    class Result:
        def __init__(self, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(*_args, **_kwargs):
        return Result(returncode, stdout, stderr)

    return _run


# ---------------------------------------------------------------------------
# Tests for the pure‑Python helper functions
# ---------------------------------------------------------------------------


def test_build_changes_summary_empty():
    assert wrap_up_cmd._build_changes_summary([]) == "No tasks completed this session."


def test_build_changes_summary_with_tasks():
    tasks = [
        {"title": "Task A", "result_summary": "All good"},
        {"title": "Task B", "result_summary": ""},
        {"title": "Task C"},
    ]
    out = wrap_up_cmd._build_changes_summary(tasks)
    # Order must be preserved
    assert out.splitlines() == [
        "- Task A: All good",
        "- Task B",
        "- Task C",
    ]


def test_extract_learnings():
    failed = [
        {"title": "Bad 1", "result_summary": "Missing field"},
        {"title": "Bad 2"},
    ]
    learn = wrap_up_cmd._extract_learnings(failed)
    assert learn == [
        "Task 'Bad 1' failed: Missing field",
        "Task 'Bad 2' failed without a recorded reason.",
    ]


def test_build_next_session_brief_no_open():
    assert wrap_up_cmd._build_next_session_brief([]) == (
        "No open tasks remaining. Consider running `bernstein evolve` to generate new work."
    )


def test_build_next_session_brief_with_tasks():
    open_tasks = [
        {"title": "Alpha", "priority": 1, "role": "backend"},
        {"title": "Beta", "priority": 3},
        {"title": "Gamma", "priority": 2, "role": "frontend"},
    ]
    brief = wrap_up_cmd._build_next_session_brief(open_tasks)
    lines = brief.splitlines()
    # First line is the header
    assert lines[0] == "Remaining open tasks (by priority):"
    # Tasks must be sorted by priority (1,2,3)
    assert "[1] Alpha (backend)" in lines[1]
    assert "[2] Gamma (frontend)" in lines[2]
    assert "[3] Beta" in lines[3]


# ---------------------------------------------------------------------------
# Tests that exercise the ``wrap-up`` CLI command
# ---------------------------------------------------------------------------


def _stub_wrap_up(monkeypatch: pytest.MonkeyPatch, tasks: list[dict[str, Any]]) -> list[Any]:
    """Stub every external call `wrap-up` makes and capture the brief it saves.

    Returns the list the `save_wrapup` stub appends to, so a test can assert on
    the brief itself rather than on the command merely not raising.
    """
    monkeypatch.setattr(wrap_up_cmd, "require_server_reachable", lambda: None)
    monkeypatch.setattr(wrap_up_cmd, "server_get", lambda _path: tasks)
    monkeypatch.setattr(wrap_up_cmd, "_get_git_diff_stat", lambda start_sha="": "")
    monkeypatch.setattr(wrap_up_cmd, "_find_session_start_sha", lambda saved_at: "")
    monkeypatch.setattr(wrap_up_cmd, "_load_session_saved_at", lambda: 0.0)
    monkeypatch.setattr(wrap_up_cmd, "_render_wrapup_brief", lambda *_, **__: None)

    saved: list[Any] = []

    def fake_save(workdir: Path, brief: Any) -> Path:
        saved.append(brief)
        return Path("/tmp/wrapup.json")

    monkeypatch.setattr(wrap_up_cmd, "save_wrapup", fake_save)
    return saved


def test_wrap_up_falls_back_to_the_session_summary_when_no_task_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that finished no task still reports what changed.

    This is the whole point of the command on a run whose tasks all failed:
    the commits exist, so answering "No tasks completed this session." throws
    away the only description of them there is.
    """
    saved = _stub_wrap_up(monkeypatch, tasks=[])

    class Summary:
        changes_summary = "rewrote the publish gate"

    monkeypatch.setattr(wrap_up_cmd, "load_session_summary", lambda *_, **__: Summary())

    result = CliRunner().invoke(wrap_up_cmd.wrap_up, [])
    assert result.exit_code == 0, result.output
    assert saved, "wrap-up saved no brief"
    assert saved[0].changes_summary == "rewrote the publish gate"


def test_wrap_up_uses_the_completed_tasks_when_there_are_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback must not displace the real answer.

    A summary loaded from disk is older than the tasks that just completed, so
    reaching for it when tasks exist would report stale work as this session's.
    """
    saved = _stub_wrap_up(
        monkeypatch,
        tasks=[{"status": "done", "title": "add the receipt vectors", "role": "backend"}],
    )

    def _fail(*_: object, **__: object) -> None:
        raise AssertionError("the session summary must not be consulted when a task completed")

    monkeypatch.setattr(wrap_up_cmd, "load_session_summary", _fail)

    result = CliRunner().invoke(wrap_up_cmd.wrap_up, [])
    assert result.exit_code == 0, result.output
    assert saved, "wrap-up saved no brief"
    assert "add the receipt vectors" in saved[0].changes_summary


# ---------------------------------------------------------------------------
# Tests for the git‑diff helper – using monkey‑patched ``subprocess.run``
# ---------------------------------------------------------------------------


def test_get_git_diff_stat_with_start_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a successful ``git diff --stat <sha>..HEAD`` call.
    fake_output = " file1 | 2 +\n file2 | 5 -\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        _mock_subprocess_run(returncode=0, stdout=fake_output),
    )
    out = wrap_up_cmd._get_git_diff_stat("deadbeef")
    assert out == fake_output.strip()


def test_get_git_diff_stat_fallback_when_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The range diff fails, so the helper falls back to the working-tree diff.

    The revision spec is ``args[3]`` - ``args`` is
    ``["git", "diff", "--stat", "<rev>"]``. Branching on ``args[2]`` matches
    ``"--stat"`` on every call, so the failing branch would never be taken and
    the fallback this test exists for would never run.
    """
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        rev = args[3] if len(args) > 3 else ""

        if rev.endswith("..HEAD"):

            class Failed:
                returncode = 1
                stdout = ""
                stderr = "error"

            return Failed()

        class Succeeded:
            returncode = 0
            stdout = "fileX | 1 +\n"
            stderr = ""

        return Succeeded()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = wrap_up_cmd._get_git_diff_stat("deadbeef")
    assert out == "fileX | 1 +"
    # Both commands were attempted, in order: the range first, then the fallback.
    revs = [c[3] for c in calls if len(c) > 3]
    assert revs == ["deadbeef..HEAD", "HEAD"]


def test_get_git_diff_stat_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate ``FileNotFoundError`` (git not installed).
    def raise_fn(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", raise_fn)
    out = wrap_up_cmd._get_git_diff_stat("deadbeef")
    assert out == ""


# ---------------------------------------------------------------------------
# Tests for session‑metadata loader
# ---------------------------------------------------------------------------


def test_load_session_saved_at_reads_file(tmp_path: Path) -> None:
    # Create the expected JSON file inside the temporary worktree.
    session_dir = tmp_path / ".sdd" / "runtime"
    session_dir.mkdir(parents=True)
    data = {"saved_at": 12345.678}
    (session_dir / "session.json").write_text(json.dumps(data), encoding="utf-8")

    # Change cwd to the temporary directory for the duration of the test.
    cwd = Path.cwd()
    try:
        import os

        os.chdir(tmp_path)
        assert wrap_up_cmd._load_session_saved_at() == 12345.678
    finally:
        os.chdir(cwd)


def test_load_session_saved_at_missing_returns_zero(tmp_path: Path) -> None:
    cwd = Path.cwd()
    try:
        import os

        os.chdir(tmp_path)
        assert wrap_up_cmd._load_session_saved_at() == 0.0
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Tests for ``_find_session_start_sha`` – using a fake git log output
# ---------------------------------------------------------------------------


def test_find_session_start_sha_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate ``git log`` returning two commits after the given timestamp.
    commits = "a1b2c3d4\n e5f6g7h8\n"
    parent = "deadbeef"

    def fake_run(args, **_kwargs):
        if "log" in args:

            class Res:
                returncode = 0
                stdout = commits
                stderr = ""

            return Res()
        if "rev-parse" in args:

            class Res:
                returncode = 0
                stdout = parent + "\n"
                stderr = ""

            return Res()
        raise AssertionError("Unexpected subprocess call: " + str(args))

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Use a fixed timestamp – the function converts it to ISO internally.
    sha = wrap_up_cmd._find_session_start_sha(0.0)
    assert sha == parent


def test_find_session_start_sha_no_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **_kwargs):
        if "log" in args:

            class Res:
                returncode = 0
                stdout = ""
                stderr = ""

            return Res()
        raise AssertionError("Unexpected call")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = wrap_up_cmd._find_session_start_sha(0.0)
    assert sha == ""


# ---------------------------------------------------------------------------
# End of file
# ---------------------------------------------------------------------------
