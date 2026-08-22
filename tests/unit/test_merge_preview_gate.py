"""Quality gate is evaluated against the merged tree, not the run checkout.

An agent commits inside its own worktree on ``agent/<session-id>``.  The run
checkout only receives those commits when the merge-back runs, and the
merge-back only runs after the quality gate has already returned a verdict.
Grading the run checkout therefore grades a tree that is missing exactly the
work under review: a task whose acceptance signal names a file it produced
could never pass, and the reopen budget was spent re-failing the same way
(issue #4367).

These tests use real git repositories in ``tmp_path`` because the fix shells
out for ``worktree add``, ``merge``, and ``worktree remove``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import CompletionSignal, Task, TaskType

from bernstein.core.git.merge_preview import (
    MergePreviewConflict,
    MergePreviewError,
    merge_preview,
    preview_worktree_path,
)
from bernstein.core.tasks import task_lifecycle
from bernstein.core.tasks.task_lifecycle import (
    _enqueue_alive_exit_janitor_pass,
    _evaluate_approval_gate,
    _verify_against_merge_preview,
)

_LOGGER = "bernstein.core.tasks.task_lifecycle"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _py(snippet: str) -> str:
    """A shell command that runs *snippet* with the running interpreter."""
    return f'"{sys.executable}" -c "{snippet}"'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A run checkout on ``main`` with one seed commit."""
    root = tmp_path / "run"
    root.mkdir()
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "run@example.com"], root)
    _run(["git", "config", "user.name", "Run Checkout"], root)
    _run(["git", "config", "commit.gpgsign", "false"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], root)
    _run(["git", "commit", "-m", "seed"], root)
    return root


def _agent_worktree(repo: Path, session_id: str) -> Path:
    """Create ``agent/<session_id>`` in its own worktree, as a spawn does."""
    wt = repo / ".sdd" / "worktrees" / session_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", f"agent/{session_id}", str(wt)], repo)
    return wt


def _commit(wt: Path, relpath: str, body: str, message: str) -> None:
    target = wt / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _run(["git", "add", relpath], wt)
    _run(["git", "commit", "-m", message], wt)


def _task(task_id: str, command: str) -> Task:
    task = Task(
        id=task_id,
        title="Add the goal-persistence test module",
        description="Create tests/unit/test_goal_persistence.py",
        role="qa",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.completion_signals = [CompletionSignal(type="test_passes", value=command)]
    return task


def _orch(repo: Path, session_id: str, worktree: Path | None) -> Any:
    return SimpleNamespace(
        _executor=None,
        _workdir=repo,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url=None, judge_model=None, judge_provider=None),
        _task_to_session={},
        _approval_gate=None,
        _find_session_for_task=lambda _tid: SimpleNamespace(id=session_id, status="running"),
        _spawner=SimpleNamespace(get_worktree_path=lambda _sid: worktree),
    )


def _refs(repo: Path) -> str:
    return _run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], repo).stdout


def _log(repo: Path) -> str:
    return _run(["git", "log", "--oneline", "main"], repo).stdout


# ---------------------------------------------------------------------------
# Acceptance 1 -- a file that exists only on the agent branch passes the gate
# ---------------------------------------------------------------------------


def test_signal_naming_a_file_created_by_the_agent_passes_and_merge_proceeds(
    repo: Path,
) -> None:
    """The verdict sees the agent's new file, so the gate passes and merge runs.

    Before the fix the signal ran in the run checkout, where the file does
    not exist yet, so the gate held the merge that would have created it.
    """
    session_id = "qa-ef212c35"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "tests/unit/test_goal_persistence.py", "def test_x():\n    pass\n", "add test")

    task = _task(
        "0c6c6f3004cb",
        _py("import pathlib,sys; sys.exit(0 if pathlib.Path('tests/unit/test_goal_persistence.py').exists() else 1)"),
    )
    orch = _orch(repo, session_id, wt)

    future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")
    assert future is not None
    passed, failed = future.result()

    assert passed is True, failed
    assert failed == []
    # A positive verdict must let the real merge run.
    session = SimpleNamespace(id=session_id)
    assert _evaluate_approval_gate(orch, task, session, None, passed) is False


# ---------------------------------------------------------------------------
# Acceptance 2 -- genuinely failing tests are still held, run branch untouched
# ---------------------------------------------------------------------------


def test_failing_tests_on_the_merged_tree_are_still_held_and_run_branch_is_clean(
    repo: Path,
) -> None:
    """A real failure stays a failure, and the preview never lands anything."""
    session_id = "backend-deadbeef"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "src/feature.py", "broken = True\n", "add feature")

    before_head = _run(["git", "rev-parse", "main"], repo).stdout.strip()
    task = _task("task-fails", _py("import sys; sys.exit(1)"))
    orch = _orch(repo, session_id, wt)

    future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")
    assert future is not None
    passed, failed = future.result()

    assert passed is False
    assert failed, "a failing test must report its signal"
    assert not any("merge_preview_conflict" in desc for desc in failed)

    session = SimpleNamespace(id=session_id)
    assert _evaluate_approval_gate(orch, task, session, None, passed) is True

    assert _run(["git", "rev-parse", "main"], repo).stdout.strip() == before_head
    assert "add feature" not in _log(repo)
    assert not (repo / "src" / "feature.py").exists()


# ---------------------------------------------------------------------------
# Acceptance 3 -- a conflict is a conflict, not a failing test
# ---------------------------------------------------------------------------


def test_conflicting_preview_is_reported_as_a_conflict(
    repo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A preview that cannot be built reports the conflict, not a test failure."""
    session_id = "backend-c0nfl1ct"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "shared.txt", "agent side\n", "agent edits shared.txt")

    # The run branch moves on with an incompatible edit to the same file.
    (repo / "shared.txt").write_text("run side\n", encoding="utf-8")
    _run(["git", "add", "shared.txt"], repo)
    _run(["git", "commit", "-m", "run branch edits shared.txt"], repo)

    task = _task("task-conflict", _py("import sys; sys.exit(0)"))
    orch = _orch(repo, session_id, wt)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")
        assert future is not None
        passed, failed = future.result()

    assert passed is False
    assert any(desc.startswith("merge_preview_conflict") for desc in failed), failed
    assert not any(desc.startswith("test_passes") for desc in failed), failed
    assert any("merge_preview" in rec.message and "verdict=conflict" in rec.message for rec in caplog.records), (
        caplog.text
    )


# ---------------------------------------------------------------------------
# Acceptance 4 -- the preview worktree is removed on every path
# ---------------------------------------------------------------------------


def _preview_dirs(repo: Path) -> list[Path]:
    base = repo / ".sdd" / "runtime" / "merge-preview"
    return [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []


def _worktree_list(repo: Path) -> str:
    return _run(["git", "worktree", "list"], repo).stdout


def test_preview_worktree_is_removed_after_a_passing_verdict(repo: Path) -> None:
    session_id = "qa-pass"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "produced.txt", "made by the agent\n", "produce file")

    seen: list[Path] = []

    def _verify(_task: Task, workdir: Path) -> tuple[bool, list[str]]:
        seen.append(workdir)
        assert (workdir / "produced.txt").exists()
        return True, []

    passed, failed = _verify_against_merge_preview(
        _verify,
        _task("t-pass", "unused"),
        repo,
        f"agent/{session_id}",
        session_id,
        None,
    )

    assert (passed, failed) == (True, [])
    assert seen and seen[0] != repo
    assert _preview_dirs(repo) == []
    assert str(seen[0]) not in _worktree_list(repo)


def test_preview_worktree_is_removed_when_verification_raises(repo: Path) -> None:
    session_id = "qa-raise"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "produced.txt", "made by the agent\n", "produce file")

    def _boom(_task: Task, _workdir: Path) -> tuple[bool, list[str]]:
        raise RuntimeError("verification blew up")

    with pytest.raises(RuntimeError, match="verification blew up"):
        _verify_against_merge_preview(
            _boom,
            _task("t-raise", "unused"),
            repo,
            f"agent/{session_id}",
            session_id,
            None,
        )

    assert _preview_dirs(repo) == []
    assert "merge-preview" not in _worktree_list(repo)


def test_preview_worktree_is_removed_after_a_conflict(repo: Path) -> None:
    session_id = "qa-conflict"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "shared.txt", "agent side\n", "agent edits shared.txt")
    (repo / "shared.txt").write_text("run side\n", encoding="utf-8")
    _run(["git", "add", "shared.txt"], repo)
    _run(["git", "commit", "-m", "run branch edits shared.txt"], repo)

    with pytest.raises(MergePreviewConflict) as excinfo:
        with merge_preview(
            repo,
            f"agent/{session_id}",
            session_id=session_id,
            task_id="t-conflict",
        ):
            pytest.fail("the preview must not be entered when the merge conflicts")

    assert "shared.txt" in excinfo.value.conflicting_files
    assert _preview_dirs(repo) == []
    assert "merge-preview" not in _worktree_list(repo)


def test_missing_agent_branch_is_a_preview_error_not_a_silent_pass(repo: Path) -> None:
    with pytest.raises(MergePreviewError):
        with merge_preview(repo, "agent/never-existed", session_id="s", task_id="t"):
            pytest.fail("the preview must not be entered for a missing branch")
    assert _preview_dirs(repo) == []


# ---------------------------------------------------------------------------
# Preview must not mutate the run branch nor leave refs behind
# ---------------------------------------------------------------------------


def test_preview_leaves_the_run_branch_and_refs_untouched(repo: Path) -> None:
    session_id = "qa-isolated"
    wt = _agent_worktree(repo, session_id)
    _commit(wt, "produced.txt", "made by the agent\n", "produce file")

    refs_before = _refs(repo)
    head_before = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    with merge_preview(repo, f"agent/{session_id}", session_id=session_id, task_id="t") as preview:
        assert (preview / "produced.txt").exists()

    assert _refs(repo) == refs_before
    assert _run(["git", "rev-parse", "HEAD"], repo).stdout.strip() == head_before
    assert not (repo / "produced.txt").exists()


def test_preview_paths_are_unique_per_task_and_session(tmp_path: Path) -> None:
    """Parallel verification through the executor must not share a preview path."""
    a = preview_worktree_path(tmp_path, session_id="qa-1", task_id="task-1")
    b = preview_worktree_path(tmp_path, session_id="qa-2", task_id="task-1")
    c = preview_worktree_path(tmp_path, session_id="qa-1", task_id="task-1")
    assert a != b
    assert a != c, "two previews for the same task must still get distinct paths"


# ---------------------------------------------------------------------------
# Non-regression -- a run without agent worktrees keeps using the run checkout
# ---------------------------------------------------------------------------


class _FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple[Any, ...]]] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.submitted.append((fn, args))
        return SimpleNamespace(_fn=fn, _args=args)


def test_task_without_agent_worktree_is_verified_in_the_run_checkout() -> None:
    """Single-agent runs work in the run checkout: no preview, no change."""
    from bernstein.core.tasks.artifact_completion import verify_task_completion

    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _workdir=Path("/tmp"),
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url=None, judge_model=None, judge_provider=None),
        _task_to_session={},
        _find_session_for_task=lambda _tid: SimpleNamespace(id="solo-1", status="running"),
        _spawner=SimpleNamespace(get_worktree_path=lambda _sid: None),
    )
    task = _task("solo-task", "true")

    assert _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick") is not None
    fn, args = executor.submitted[0]
    assert fn is verify_task_completion
    assert args == (task, Path("/tmp"))


def test_task_with_agent_worktree_is_never_verified_in_the_run_checkout() -> None:
    """A task with a produced-file signal must not be graded on the unmerged tree."""
    executor = _FakeExecutor()
    orch: Any = SimpleNamespace(
        _executor=executor,
        _workdir=Path("/tmp"),
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url=None, judge_model=None, judge_provider=None),
        _task_to_session={},
        _find_session_for_task=lambda _tid: SimpleNamespace(id="qa-1", status="running"),
        _spawner=SimpleNamespace(get_worktree_path=lambda _sid: Path("/tmp/wt")),
    )
    task = _task("preview-task", "true")

    assert _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick") is not None
    fn, args = executor.submitted[0]
    assert fn is _verify_against_merge_preview
    assert "agent/qa-1" in args


def test_preview_receives_the_shared_dirs_an_agent_worktree_gets(repo: Path) -> None:
    """Checks that shell out to the toolchain need the same shared dirs.

    Without them the preview has no ``.venv`` and every command fails for a
    reason unrelated to the work under review.
    """
    session_id = "qa-provisioned"
    _agent_worktree(repo, session_id)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".env").write_text("PORT=1234\n", encoding="utf-8")

    with merge_preview(
        repo,
        f"agent/{session_id}",
        session_id=session_id,
        task_id="t",
        symlink_dirs=(".venv",),
        copy_files=(".env",),
    ) as preview:
        assert (preview / ".venv" / "bin").is_dir()
        assert (preview / ".venv").is_symlink()
        assert (preview / ".env").read_text(encoding="utf-8") == "PORT=1234\n"
        assert not (preview / ".env").is_symlink()

    assert _preview_dirs(repo) == []
