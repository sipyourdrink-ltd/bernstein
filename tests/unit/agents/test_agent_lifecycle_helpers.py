"""Behavioral tests for ``agent_lifecycle`` helper functions.

Exercises the deterministic / near-pure helpers that the dead-agent and
orphan-handling pipelines lean on: exit-code -> abort-reason
classification, partial-work preservation via real git, branch-commit
detection, log-path resolution, abort cascade delegation, file/task
ownership release, dead-agent purging, and orphan metric emission.

The orchestrator is duck-typed via ``SimpleNamespace`` (matching the
module's ``Any`` signatures); real ``AgentSession`` objects and a real
git repo are used so the assertions check genuine observables.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import AbortReason, AgentSession, ModelConfig

from bernstein.core.agents.agent_lifecycle import (
    _abort_siblings,
    _has_git_commits_on_branch,
    _propagate_abort_to_children,
    _release_file_ownership,
    _release_task_to_session,
    _resolve_agent_log_path,
    _save_partial_work,
    classify_agent_abort_reason,
    emit_orphan_metrics,
    purge_dead_agents,
)


def _session(
    sid: str = "A-1",
    *,
    exit_code: int | None = None,
    status: str = "dead",
    heartbeat_ts: float = 0.0,
    log_path: Path | None = None,
) -> AgentSession:
    return AgentSession(
        id=sid,
        role="backend",
        task_ids=["T-1"],
        status=status,
        spawn_ts=100.0,
        model_config=ModelConfig("sonnet", "high"),
        exit_code=exit_code,
        heartbeat_ts=heartbeat_ts,
        log_path=log_path,
    )


def _git(path: Path, *args: str) -> None:
    # Setup/mutation git steps must succeed; check=True surfaces a broken
    # fixture (e.g. failed ``git init``) instead of silently masking it.
    subprocess.run(["git", *args], cwd=str(path), capture_output=True, check=True)


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "commit", "--allow-empty", "-m", "init")


# ---------------------------------------------------------------------------
# classify_agent_abort_reason
# ---------------------------------------------------------------------------


def test_classify_no_exit_code_is_unknown() -> None:
    """A session with no exit code classifies as UNKNOWN."""
    reason, detail = classify_agent_abort_reason(_session(exit_code=None))
    assert reason == AbortReason.UNKNOWN
    assert "without exit code" in detail


def test_classify_timeout_exit_124() -> None:
    """Exit 124 maps to TIMEOUT."""
    reason, detail = classify_agent_abort_reason(_session(exit_code=124))
    assert reason == AbortReason.TIMEOUT
    assert "124" in detail


def test_classify_oom_exit_137() -> None:
    """Exit 137 maps to OOM."""
    assert classify_agent_abort_reason(_session(exit_code=137))[0] == AbortReason.OOM


def test_classify_permission_denied_exit_126() -> None:
    """Exit 126 maps to PERMISSION_DENIED."""
    assert classify_agent_abort_reason(_session(exit_code=126))[0] == AbortReason.PERMISSION_DENIED


def test_classify_other_positive_exit_is_unknown() -> None:
    """A non-special positive exit code is UNKNOWN with the code echoed."""
    reason, detail = classify_agent_abort_reason(_session(exit_code=5))
    assert reason == AbortReason.UNKNOWN
    assert "status 5" in detail


def test_classify_sigint_negative_exit() -> None:
    """A negative exit equal to -SIGINT classifies as USER_INTERRUPT."""
    assert classify_agent_abort_reason(_session(exit_code=-2))[0] == AbortReason.USER_INTERRUPT


def test_classify_sigterm_negative_exit() -> None:
    """A negative exit equal to -SIGTERM classifies as SHUTDOWN_SIGNAL."""
    assert classify_agent_abort_reason(_session(exit_code=-15))[0] == AbortReason.SHUTDOWN_SIGNAL


def test_classify_sigkill_negative_exit() -> None:
    """A negative exit equal to -SIGKILL classifies as OOM."""
    assert classify_agent_abort_reason(_session(exit_code=-9))[0] == AbortReason.OOM


def test_classify_other_signal_is_unknown() -> None:
    """An unmapped signal (e.g. SIGSEGV=-11) is UNKNOWN with the signal number."""
    reason, detail = classify_agent_abort_reason(_session(exit_code=-11))
    assert reason == AbortReason.UNKNOWN
    assert "signal 11" in detail


# ---------------------------------------------------------------------------
# _save_partial_work
# ---------------------------------------------------------------------------


def test_save_partial_work_no_worktree_returns_false() -> None:
    """No worktree path means nothing to save."""
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = None
    assert _save_partial_work(spawner, _session()) is False


def test_save_partial_work_commits_changes(tmp_path: Path) -> None:
    """Uncommitted changes are staged and committed; merge is attempted."""
    _init_repo(tmp_path)
    (tmp_path / "newfile.py").write_text("x = 1\n")
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = str(tmp_path)

    assert _save_partial_work(spawner, _session()) is True
    # A WIP commit now exists on the branch.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[WIP]" in log.stdout
    # The merge-back path is attempted.
    spawner.reap_completed_agent.assert_called_once()


def test_save_partial_work_no_changes_returns_false(tmp_path: Path) -> None:
    """A clean worktree produces no commit (git commit exits non-zero)."""
    _init_repo(tmp_path)
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = str(tmp_path)
    assert _save_partial_work(spawner, _session()) is False


# ---------------------------------------------------------------------------
# _has_git_commits_on_branch
# ---------------------------------------------------------------------------


def test_has_git_commits_true_when_ahead_of_main(tmp_path: Path) -> None:
    """A branch with commits beyond main reports True."""
    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "agent/A-1")
    (tmp_path / "work.py").write_text("y = 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "agent work")
    assert _has_git_commits_on_branch(tmp_path) is True


def test_has_git_commits_false_on_main(tmp_path: Path) -> None:
    """On main with no extra commits, reports False."""
    _init_repo(tmp_path)
    assert _has_git_commits_on_branch(tmp_path) is False


def test_has_git_commits_false_for_non_git_dir(tmp_path: Path) -> None:
    """A non-git directory yields False (exception swallowed)."""
    assert _has_git_commits_on_branch(tmp_path) is False


# ---------------------------------------------------------------------------
# _resolve_agent_log_path
# ---------------------------------------------------------------------------


def test_resolve_agent_log_path_uses_explicit_log(tmp_path: Path) -> None:
    """An explicit session.log_path that exists is returned verbatim."""
    log = tmp_path / "custom.log"
    log.write_text("x")
    resolved = _resolve_agent_log_path(tmp_path, _session(log_path=log))
    assert resolved == log


def test_resolve_agent_log_path_default_convention(tmp_path: Path) -> None:
    """Without an explicit path, the default <session>.log convention is used."""
    resolved = _resolve_agent_log_path(tmp_path, _session("A-1", log_path=None))
    assert resolved.name == "A-1.log"


# ---------------------------------------------------------------------------
# abort cascade delegation
# ---------------------------------------------------------------------------


def test_propagate_abort_no_chain_is_noop() -> None:
    """Without an abort chain, propagation is a safe no-op."""
    _propagate_abort_to_children(SimpleNamespace(), "S")


def test_propagate_abort_calls_chain_and_cleanup() -> None:
    """With a chain, propagate_abort then cleanup are both invoked."""
    chain = MagicMock()
    _propagate_abort_to_children(SimpleNamespace(_abort_chain=chain), "S1")
    chain.propagate_abort.assert_called_once_with("S1")
    chain.cleanup.assert_called_once_with("S1")


def test_propagate_abort_cleans_up_even_on_error() -> None:
    """cleanup runs even when propagate_abort raises."""
    chain = MagicMock()
    chain.propagate_abort.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        _propagate_abort_to_children(SimpleNamespace(_abort_chain=chain), "S1")
    chain.cleanup.assert_called_once_with("S1")


def test_abort_siblings_no_chain_returns_empty() -> None:
    """Without a chain no siblings are aborted."""
    assert _abort_siblings(SimpleNamespace(), "S") == []


def test_abort_siblings_delegates_to_chain() -> None:
    """The chain's abort_siblings result is surfaced."""
    chain = MagicMock()
    chain.abort_siblings.return_value = ["S2", "S3"]
    result = _abort_siblings(SimpleNamespace(_abort_chain=chain), "S1", reason="sibling_failure")
    assert result == ["S2", "S3"]


# ---------------------------------------------------------------------------
# ownership / task release
# ---------------------------------------------------------------------------


def test_release_file_ownership_clears_lock_and_dict() -> None:
    """The lock manager is released and the legacy dict entries removed."""
    lock_manager = MagicMock()
    orch = SimpleNamespace(_lock_manager=lock_manager, _file_ownership={"a.py": "A-1", "b.py": "A-2"})
    _release_file_ownership(orch, "A-1")
    lock_manager.release.assert_called_once_with("A-1")
    assert orch._file_ownership == {"b.py": "A-2"}


def test_release_file_ownership_without_lock_manager() -> None:
    """With no lock manager only the legacy dict is cleaned."""
    orch = SimpleNamespace(_lock_manager=None, _file_ownership={"a.py": "A-1"})
    _release_file_ownership(orch, "A-1")
    assert orch._file_ownership == {}


def test_release_task_to_session_drops_only_named_tasks() -> None:
    """Only the named task ids are removed from the reverse index."""
    orch = SimpleNamespace(_task_to_session={"T-1": "A-1", "T-2": "A-2"})
    _release_task_to_session(orch, ["T-1", "T-missing"])
    assert orch._task_to_session == {"T-2": "A-2"}


# ---------------------------------------------------------------------------
# purge_dead_agents
# ---------------------------------------------------------------------------


def test_purge_dead_agents_removes_oldest_and_cleans_index() -> None:
    """Excess dead agents (oldest heartbeat first) are removed with their index entries."""
    orch = SimpleNamespace(
        _agents={
            "D1": _session("D1", status="dead", heartbeat_ts=10.0),
            "D2": _session("D2", status="dead", heartbeat_ts=20.0),
            "D3": _session("D3", status="dead", heartbeat_ts=30.0),
            "A": _session("A", status="working", heartbeat_ts=5.0),
        },
        _task_to_session={"T-1": "D1", "T-2": "A"},
        _MAX_DEAD_AGENTS_KEPT=2,
    )
    purge_dead_agents(orch)
    # D1 is the oldest dead agent and gets purged; the live agent stays.
    assert sorted(orch._agents.keys()) == ["A", "D2", "D3"]
    # The reverse-index entry pointing at the purged agent is cleared.
    assert orch._task_to_session == {"T-2": "A"}


def test_purge_dead_agents_under_limit_is_noop() -> None:
    """Below the dead-agent cap nothing is removed."""
    orch = SimpleNamespace(
        _agents={"D1": _session("D1", status="dead", heartbeat_ts=10.0)},
        _task_to_session={},
        _MAX_DEAD_AGENTS_KEPT=5,
    )
    purge_dead_agents(orch)
    assert list(orch._agents.keys()) == ["D1"]


# ---------------------------------------------------------------------------
# emit_orphan_metrics
# ---------------------------------------------------------------------------


def test_emit_orphan_metrics_writes_success_record(tmp_path: Path) -> None:
    """A success record is written with test_pass_rate 1.0 and no error type."""
    session = _session("A-1")
    emit_orphan_metrics(tmp_path, "T-1", session, start_ts=0.0, success=True, error_type=None)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    record = json.loads((tmp_path / ".sdd" / "metrics" / f"{today}.jsonl").read_text().strip())
    assert record["task_id"] == "T-1"
    assert record["agent_id"] == "A-1"
    assert record["success"] is True
    assert record["test_pass_rate"] == 1.0
    assert record["error_type"] is None


def test_emit_orphan_metrics_appends_failure_record(tmp_path: Path) -> None:
    """A second emission appends; a failure record has test_pass_rate 0.0."""
    session = _session("A-1")
    emit_orphan_metrics(tmp_path, "T-1", session, start_ts=0.0, success=True, error_type=None)
    emit_orphan_metrics(tmp_path, "T-2", session, start_ts=0.0, success=False, error_type="timeout")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = (tmp_path / ".sdd" / "metrics" / f"{today}.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    failure = json.loads(lines[1])
    assert failure["success"] is False
    assert failure["test_pass_rate"] == 0.0
    assert failure["error_type"] == "timeout"


# ---------------------------------------------------------------------------
# _preserve_runner_logs
# ---------------------------------------------------------------------------


def test_preserve_runner_logs_copies_logs_and_manifest(tmp_path: Path) -> None:
    """Runner logs + manifest are copied out of the worktree before cleanup."""
    from bernstein.core.agents.agent_lifecycle import _preserve_runner_logs

    worktree = tmp_path / "wt"
    runtime = worktree / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "A-1.log").write_text("runner transcript")
    (runtime / "A-1.events.log").write_text("events")
    (runtime / "A-1.manifest.json").write_text("{}")
    (runtime / "OTHER-SESSION.log").write_text("not ours")
    orch_dir = tmp_path / "orch"
    orch_dir.mkdir()
    orch = SimpleNamespace(
        _spawner=SimpleNamespace(get_worktree_path=lambda sid: worktree),
        _workdir=orch_dir,
    )

    dest = _preserve_runner_logs(orch, _session())

    assert dest == orch_dir / ".sdd" / "runtime" / "agent_logs" / "A-1"
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["A-1.events.log", "A-1.log", "A-1.manifest.json"]
    assert (dest / "A-1.log").read_text() == "runner transcript"


def test_preserve_runner_logs_none_without_worktree(tmp_path: Path) -> None:
    from bernstein.core.agents.agent_lifecycle import _preserve_runner_logs

    orch = SimpleNamespace(
        _spawner=SimpleNamespace(get_worktree_path=lambda sid: None),
        _workdir=tmp_path,
    )
    assert _preserve_runner_logs(orch, _session()) is None


def test_preserve_runner_logs_none_when_no_matching_files(tmp_path: Path) -> None:
    from bernstein.core.agents.agent_lifecycle import _preserve_runner_logs

    worktree = tmp_path / "wt"
    (worktree / ".sdd" / "runtime").mkdir(parents=True)
    orch = SimpleNamespace(
        _spawner=SimpleNamespace(get_worktree_path=lambda sid: worktree),
        _workdir=tmp_path / "orch",
    )
    assert _preserve_runner_logs(orch, _session()) is None
    assert not (tmp_path / "orch" / ".sdd" / "runtime" / "agent_logs" / "A-1").exists()


# ---------------------------------------------------------------------------
# suspicious short-lived clean-exit auto-complete warning
# ---------------------------------------------------------------------------


def _orphan_orch(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _workdir=tmp_path,
        _client=MagicMock(),
        _spawner=SimpleNamespace(get_worktree_path=lambda sid: None),
        _config=SimpleNamespace(max_task_retries=3),
        _retried_task_ids=set(),
    )


def test_suspicious_clean_exit_is_failed_not_auto_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A <60s clean exit with an empty diff and no signals must NOT be marked done.

    Regression for #2810 / #2806: a fast clean exit was auto-completed to
    ``done`` even though no deliverable was produced (and the run then
    self-declared healthy). Such a task must route to fail/unverified.
    """
    import logging
    import time as _time

    import bernstein.core.agents.agent_lifecycle as al

    monkeypatch.setattr(al, "collect_completion_data", lambda workdir, session: {"files_modified": []})
    completed: list[str] = []
    monkeypatch.setattr(al, "complete_task", lambda client, base, task_id, summary: completed.append(task_id))
    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(al, "retry_or_fail_task", lambda task_id, reason, **kw: failed.append((task_id, reason)))

    session = _session(exit_code=0)
    session.spawn_ts = _time.time() - 3.0
    with caplog.at_level(logging.WARNING):
        success, error_type = al._handle_orphan_no_signals(
            _orphan_orch(tmp_path), MagicMock(), "T-1", session, "http://srv", _time.time()
        )
    # Task is NOT completed; it is failed/unverified instead.
    assert success is False
    assert error_type == "clean_exit_unverified"
    assert completed == []
    assert failed and failed[0][0] == "T-1"
    warning = next(r for r in caplog.records if "SUSPICIOUS clean exit" in r.message)
    assert "A-1" in warning.message
    assert "agent_logs" in warning.message


def test_long_lived_clean_exit_auto_complete_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import time as _time

    import bernstein.core.agents.agent_lifecycle as al

    monkeypatch.setattr(al, "collect_completion_data", lambda workdir, session: {"files_modified": []})
    monkeypatch.setattr(al, "complete_task", lambda client, base, task_id, summary: None)
    session = _session(exit_code=0)
    session.spawn_ts = _time.time() - 300.0
    with caplog.at_level(logging.WARNING):
        success, _ = al._handle_orphan_no_signals(
            _orphan_orch(tmp_path), MagicMock(), "T-1", session, "http://srv", _time.time()
        )
    assert success is True
    assert not any("SUSPICIOUS auto-complete" in r.message for r in caplog.records)
