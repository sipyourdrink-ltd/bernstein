"""No-change exits must be judged against the agent's declared gates (#6606)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, CompletionSignal, ModelConfig, Task, TaskStatus

from bernstein.core.agents.agent_lifecycle import handle_orphaned_task
from bernstein.core.persistence.wal import WALWriter
from bernstein.core.tasks.artifact_completion import verify_task_completion

_TEST_COMMAND = "./.venv/bin/python -m pytest tests/unit/test_deliverable.py -q"


@pytest.fixture
def no_change_attempt(tmp_path: Path):
    """Use the orphan-recovery fixtures' shape, with all git probes stubbed."""
    worktree = tmp_path / "agent-worktree"
    worktree.mkdir()
    task = Task(
        id="T-satisfied-base",
        title="Implement existing feature",
        description="The deliverable may already exist on base",
        role="backend",
        status=TaskStatus.CLAIMED,
        completion_signals=[
            CompletionSignal(type="path_exists", value="deliverable.txt"),
            CompletionSignal(type="file_contains", value="feature.txt :: implemented"),
            CompletionSignal(type="test_passes", value=_TEST_COMMAND),
        ],
    )
    session = AgentSession(
        id="sess-no-change",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,
        spawn_ts=time.time(),  # Even a fast exit is backed by passing gates.
    )
    client = MagicMock()
    client.get.return_value.json.return_value = task.to_dict()
    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server", recovery="restart", max_crash_retries=3, max_task_retries=3
        ),
        _client=client,
        _workdir=tmp_path,
        _rate_limit_tracker=None,
        _crash_counts={},
        _retried_task_ids=set(),
        _record_provider_health=MagicMock(),
        _evolution=None,
        _wal_writer=WALWriter("satisfied-base", tmp_path / ".sdd"),
        _spawner=MagicMock(),
    )
    orch._spawner.get_worktree_path.return_value = worktree
    # Both trees start satisfied; each test makes one tree stale independently.
    for root in (tmp_path, worktree):
        (root / "deliverable.txt").write_text("already landed\n")
        (root / "feature.txt").write_text("implemented\n")
        (root / "test-result.txt").write_text("pass\n")

    def run_gate(command, **kwargs):
        # Only the test_passes subprocess is allowed; never execute git/network.
        assert command == _TEST_COMMAND
        root = Path(kwargs["cwd"])
        passed = (root / "test-result.txt").read_text().strip() == "pass"
        return subprocess.CompletedProcess(command, 0 if passed else 1, stdout=b"", stderr=b"")

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle._uncommitted_work_paths", return_value=[]),
        patch("bernstein.core.agents.agent_lifecycle._satisfied_on_base_is_safe", return_value=True),
        patch("bernstein.core.quality.janitor._gitignore_exclusion", return_value=None),
        patch("bernstein.core.quality.janitor.subprocess.run", side_effect=run_gate) as gate_command,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry,
        patch("bernstein.core.agents.agent_lifecycle.emit_orphan_metrics"),
    ):
        yield orch, task, session, worktree, retry, gate_command


def _journal(orch: SimpleNamespace) -> dict:
    path = orch._workdir / ".sdd/runtime/wal/satisfied-base.wal.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    return rows[0]


def test_clean_no_change_completes_when_all_worktree_gates_pass(no_change_attempt) -> None:
    orch, task, session, worktree, retry, gate_command = no_change_attempt
    # The run checkout is stale; all three gates pass in the agent worktree.
    (orch._workdir / "deliverable.txt").unlink()
    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    retry.assert_not_called()
    orch._client.post.assert_called_once()
    assert orch._client.post.call_args.args == (f"http://server/tasks/{task.id}/complete",)
    summary = orch._client.post.call_args.kwargs["json"]["result_summary"]
    assert "already satisfied on base" in summary
    assert "janitor" in summary
    gate_command.assert_called_once()
    assert gate_command.call_args.kwargs["cwd"] == worktree
    row = _journal(orch)
    assert row["decision_type"] == "task_completed"
    assert row["output"]["success"] is True
    assert row["output"]["completion_signals_checked"] is True
    assert row["output"]["janitor_passed"] is True
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.mark.parametrize("failed_gate", ["path_exists", "file_contains", "test_passes"])
def test_clean_no_change_retries_when_any_worktree_gate_fails(no_change_attempt, failed_gate: str) -> None:
    orch, task, session, worktree, retry, gate_command = no_change_attempt
    # The run checkout passes, but it must not mask a failing worktree gate.
    if failed_gate == "path_exists":
        (worktree / "deliverable.txt").unlink()
    elif failed_gate == "file_contains":
        (worktree / "feature.txt").write_text("unfinished\n")
    else:
        (worktree / "test-result.txt").write_text("fail\n")
    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert retry.call_args.args[0] == task.id
    assert "janitor failed" in retry.call_args.args[1]
    assert failed_gate in retry.call_args.args[1]
    assert retry.call_args.kwargs["max_task_retries"] == 3
    gate_command.assert_called_once()
    assert gate_command.call_args.kwargs["cwd"] == worktree
    row = _journal(orch)
    assert row["decision_type"] == "task_failed"
    assert row["output"]["success"] is False
    assert row["output"]["completion_signals_checked"] is True
    assert row["output"]["janitor_passed"] is False
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.fixture
def real_worktree_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use a linked worktree with real completion-data, git and janitor probes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
    ):
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

    git("init", "-b", "main")
    (repo / ".gitignore").write_text("deliverable.txt\n.sdd/\n")
    (repo / "feature.txt").write_text("already on base\n")
    git("add", ".gitignore", "feature.txt")
    git("commit", "-m", "base")
    worktree = tmp_path / "agent-worktree"
    git("worktree", "add", "-b", "agent/no-change", str(worktree))

    task = Task(
        id="T-satisfied-base",
        title="Verify existing deliverable",
        description="Only deliverables that survive a merge count",
        role="backend",
        status=TaskStatus.CLAIMED,
        completion_signals=[CompletionSignal(type="path_exists", value="feature.txt")],
    )
    session = AgentSession(
        id="sess-no-change",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,
        spawn_ts=time.time(),
    )
    client = MagicMock()
    client.get.return_value.json.side_effect = lambda: task.to_dict()
    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server", recovery="restart", max_crash_retries=3, max_task_retries=3
        ),
        _client=client,
        _workdir=repo,
        _rate_limit_tracker=None,
        _crash_counts={},
        _retried_task_ids=set(),
        _record_provider_health=MagicMock(),
        _evolution=None,
        _wal_writer=WALWriter("satisfied-base", repo / ".sdd"),
        _spawner=MagicMock(),
    )
    orch._spawner.get_worktree_path.return_value = worktree
    with (
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry,
        patch("bernstein.core.agents.agent_lifecycle.emit_orphan_metrics"),
    ):
        yield orch, task, session, worktree, retry, git


@pytest.mark.parametrize(
    ("path", "ignored"),
    [("deliverable.txt", True), ("untracked.txt", False), (".sdd/deliverable.txt", True)],
)
def test_worktree_only_file_cannot_complete_as_satisfied_base(real_worktree_attempt, path: str, ignored: bool) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    task.completion_signals = [CompletionSignal(type="path_exists", value=path)]
    deliverable = worktree / path
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("exists only in the agent worktree\n")

    assert not (orch._workdir / path).exists()
    assert git("diff", "HEAD", cwd=worktree) == ""
    assert git("log", "--oneline", "main..HEAD", cwd=worktree) == ""
    expected_status = "!!" if ignored else "??"
    assert git("status", "--porcelain=v1", "--ignored", "--untracked-files=all", cwd=worktree) == (
        f"{expected_status} {path}\n"
    )
    # The gate really passes on this ephemeral file; only delivery is missing.
    assert verify_task_completion(task, worktree) == (True, [])

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert "janitor failed" in retry.call_args.args[1]
    assert "path_exists" in retry.call_args.args[1]
    assert _journal(orch)["decision_type"] == "task_failed"


def test_real_clean_worktree_completes_when_base_gate_passes(real_worktree_attempt) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    assert git("status", "--porcelain=v1", "--ignored", "--untracked-files=all", cwd=worktree) == ""
    # A stale run checkout must not hide the committed deliverable in the worktree.
    (orch._workdir / "feature.txt").unlink()

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    retry.assert_not_called()
    orch._client.post.assert_called_once()
    assert "already satisfied on base" in orch._client.post.call_args.kwargs["json"]["result_summary"]
    row = _journal(orch)
    assert row["decision_type"] == "task_completed"
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.mark.parametrize("pattern", ["out/*.json", "out/**/*.json"])
@pytest.mark.parametrize("signal_type", ["path_exists", "glob_exists"])
def test_glob_tracked_match_on_base_completes(real_worktree_attempt, pattern: str, signal_type: str) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    paths = ["out/sub/a.json"]
    if pattern == "out/*.json":
        # Python's * cannot match the nested file; commit a direct match too.
        paths.append("out/result.json")
    for path in paths:
        target = orch._workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
    with (orch._workdir / ".gitignore").open("a") as ignore:
        ignore.write(".venv/\n")
    git("add", ".gitignore", "out")
    git("commit", "-m", "glob deliverables on base")
    git("reset", "--hard", "main", cwd=worktree)
    # Ordinary ignored build state must not disable the shortcut.
    (worktree / ".venv").mkdir()
    (worktree / ".venv/pyvenv.cfg").write_text("synthetic build state\n")
    task.completion_signals = [CompletionSignal(type=signal_type, value=pattern)]
    assert git("diff", "HEAD", cwd=worktree) == ""
    assert git("log", "--oneline", "main..HEAD", cwd=worktree) == ""
    assert verify_task_completion(task, worktree) == (True, [])
    # Verify the shortcut actually selects the worktree over a stale checkout.
    for path in paths:
        (orch._workdir / path).unlink()

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    retry.assert_not_called()
    orch._client.post.assert_called_once()
    assert "already satisfied on base" in orch._client.post.call_args.kwargs["json"]["result_summary"]
    row = _journal(orch)
    assert row["decision_type"] == "task_completed"
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("tracked_nested", [False, True])
@pytest.mark.parametrize("signal_type", ["path_exists", "glob_exists"])
def test_glob_worktree_only_match_retries(
    real_worktree_attempt, ignored: bool, tracked_nested: bool, signal_type: str
) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    if ignored:
        with (orch._workdir / ".gitignore").open("a") as ignore:
            ignore.write("out/result.json\n")
    if tracked_nested:
        nested = orch._workdir / "out/sub/a.json"
        nested.parent.mkdir(parents=True)
        nested.write_text("{}\n")
        git("add", "out/sub/a.json")
    if ignored or tracked_nested:
        git("add", ".gitignore")
        git("commit", "-m", "base with no direct glob match")
        git("reset", "--hard", "main", cwd=worktree)
    target = worktree / "out/result.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n")
    task.completion_signals = [CompletionSignal(type=signal_type, value="out/*.json")]
    assert git("diff", "HEAD", cwd=worktree) == ""
    assert git("log", "--oneline", "main..HEAD", cwd=worktree) == ""
    assert verify_task_completion(task, worktree) == (True, [])
    assert verify_task_completion(task, orch._workdir)[0] is False

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert signal_type in retry.call_args.args[1]
    assert _journal(orch)["decision_type"] == "task_failed"


def test_test_passes_with_untracked_input_retries(real_worktree_attempt) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    (worktree / "generated-input.txt").write_text("uncommitted test fixture\n")
    task.completion_signals = [CompletionSignal(type="test_passes", value="test -f generated-input.txt")]
    assert git("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree) == "?? generated-input.txt\n"
    assert git("diff", "HEAD", cwd=worktree) == ""
    assert git("log", "--oneline", "main..HEAD", cwd=worktree) == ""
    assert verify_task_completion(task, worktree) == (True, [])
    assert verify_task_completion(task, orch._workdir)[0] is False

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert "test_passes" in retry.call_args.args[1]
    assert _journal(orch)["decision_type"] == "task_failed"


@pytest.mark.parametrize("failure", ["nonzero", "oserror", "timeout"])
def test_test_passes_status_failure_blocks_satisfied_base(real_worktree_attempt, failure: str) -> None:
    orch, task, session, worktree, retry, _git = real_worktree_attempt
    (worktree / "deliverable.txt").write_text("ignored test input\n")
    task.completion_signals = [CompletionSignal(type="test_passes", value="test -f deliverable.txt")]
    assert verify_task_completion(task, worktree) == (True, [])
    assert verify_task_completion(task, orch._workdir)[0] is False
    run = subprocess.run
    status_calls = []

    def fail_status(command, *args, **kwargs):
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            status_calls.append(command)
            if failure == "oserror":
                raise OSError("simulated git status failure")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 5)
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="simulated git status failure")
        return run(command, *args, **kwargs)

    with patch("bernstein.core.agents.agent_lifecycle.subprocess.run", side_effect=fail_status):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    assert status_calls == [["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]]
    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert "test_passes" in retry.call_args.args[1]
    assert _journal(orch)["decision_type"] == "task_failed"


def test_file_contains_symlink_blocks_satisfied_base(real_worktree_attempt) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    (orch._workdir / "feature-link.txt").symlink_to("deliverable.txt")
    git("add", "feature-link.txt")
    git("commit", "-m", "symlink on base with ignored target")
    git("reset", "--hard", "main", cwd=worktree)
    (worktree / "deliverable.txt").write_text("implemented\n")
    task.completion_signals = [CompletionSignal(type="file_contains", value="feature-link.txt :: implemented")]
    assert (worktree / "feature-link.txt").is_symlink()
    assert git("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree) == ""
    assert git("diff", "HEAD", cwd=worktree) == ""
    assert verify_task_completion(task, worktree) == (True, [])
    assert verify_task_completion(task, orch._workdir)[0] is False

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    orch._client.post.assert_not_called()
    retry.assert_called_once()
    assert "file_contains" in retry.call_args.args[1]
    assert _journal(orch)["decision_type"] == "task_failed"


@pytest.mark.parametrize("signal_type", ["path_exists", "glob_exists"])
def test_incidental_artifacts_do_not_block_satisfied_base(real_worktree_attempt, signal_type: str) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    target = "feature.txt" if signal_type == "path_exists" else "feature.*"
    task.completion_signals = [CompletionSignal(type=signal_type, value=target)]
    for path in (".claude/scheduled_tasks.json", ".sdd/attestations/spawn.json"):
        artifact = worktree / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}\n")
    status = git("status", "--porcelain=v1", "--ignored", "--untracked-files=all", cwd=worktree)
    assert "?? .claude/scheduled_tasks.json" in status
    assert "!! .sdd/attestations/spawn.json" in status
    (orch._workdir / "feature.txt").unlink()

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    retry.assert_not_called()
    orch._client.post.assert_called_once()
    assert "already satisfied on base" in orch._client.post.call_args.kwargs["json"]["result_summary"]
    row = _journal(orch)
    assert row["decision_type"] == "task_completed"
    assert row["output"]["completion_signals_checked"] is True
    assert row["output"]["janitor_passed"] is True
    assert row["output"]["verification_workdir"] == str(worktree)


@pytest.mark.parametrize("uncommitted_edit", [False, True])
def test_file_contains_requires_committed_content(real_worktree_attempt, uncommitted_edit: bool) -> None:
    orch, task, session, worktree, retry, git = real_worktree_attempt
    needle = "implemented" if uncommitted_edit else "already on base"
    task.completion_signals = [CompletionSignal(type="file_contains", value=f"feature.txt :: {needle}")]
    if uncommitted_edit:
        (worktree / "feature.txt").write_text("already on base\nimplemented\n")
        assert needle not in git("show", "HEAD:feature.txt", cwd=worktree)
    else:
        (orch._workdir / "feature.txt").unlink()
    assert verify_task_completion(task, worktree) == (True, [])

    handle_orphaned_task(orch, task.id, session, {"claimed": [task]})

    if uncommitted_edit:
        orch._client.post.assert_not_called()
        retry.assert_called_once()
        assert "file_contains" in retry.call_args.args[1]
        assert _journal(orch)["decision_type"] == "task_failed"
    else:
        retry.assert_not_called()
        orch._client.post.assert_called_once()
        assert "already satisfied on base" in orch._client.post.call_args.kwargs["json"]["result_summary"]
        row = _journal(orch)
        assert row["decision_type"] == "task_completed"
        assert row["output"]["verification_workdir"] == str(worktree)
