"""Tests for the runtime agent kill switch in bernstein.core.circuit_breaker.

Covers:
- enforce_kill_signal / log_kill_event / write_quarantine_metadata
- check_scope_violations
- check_budget_violations
- check_guardrail_violations
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.circuit_breaker import (
    _files_outside_scope,
    _get_worktree_diff,
    check_budget_violations,
    check_guardrail_violations,
    check_scope_violations,
    enforce_kill_signal,
)
from bernstein.core.models import AgentSession, KillReason

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(session_id: str, tokens_used: int = 0) -> AgentSession:
    s = AgentSession(id=session_id, role="backend")
    s.tokens_used = tokens_used
    return s


def _make_orch(
    tmp_path: Path,
    sessions: list[AgentSession],
    token_budget_per_session: int = 0,
) -> SimpleNamespace:
    """Build a minimal orchestrator-like namespace for unit tests."""
    config = SimpleNamespace(
        server_url="http://localhost:8052",
        token_budget_per_session=token_budget_per_session,
    )

    spawner = MagicMock()
    spawner.get_worktree_path.return_value = None  # no worktree by default

    orch = SimpleNamespace(
        _agents={s.id: s for s in sessions},
        _workdir=tmp_path,
        _config=config,
        _spawner=spawner,
        _client=MagicMock(),
    )
    return orch


def _make_result() -> SimpleNamespace:
    return SimpleNamespace(reaped=[])


# ---------------------------------------------------------------------------
# enforce_kill_signal
# ---------------------------------------------------------------------------


class TestEnforceKillSignal:
    def test_writes_kill_file(self, tmp_path: Path) -> None:
        enforce_kill_signal(tmp_path, "sess-1", KillReason.MANUAL, "test kill")
        kill_file = tmp_path / ".sdd" / "runtime" / "sess-1.kill"
        assert kill_file.exists()
        payload = json.loads(kill_file.read_text())
        assert payload["reason"] == "manual"
        assert payload["detail"] == "test kill"

    def test_appends_audit_log(self, tmp_path: Path) -> None:
        enforce_kill_signal(tmp_path, "sess-2", KillReason.SCOPE_VIOLATION, "out of scope", files=["bad.py"])
        audit = tmp_path / ".sdd" / "metrics" / "kill_audit.jsonl"
        assert audit.exists()
        event = json.loads(audit.read_text().strip())
        assert event["session_id"] == "sess-2"
        assert event["reason"] == "scope_violation"
        assert event["files"] == ["bad.py"]

    def test_writes_quarantine_for_violation(self, tmp_path: Path) -> None:
        enforce_kill_signal(
            tmp_path,
            "sess-3",
            KillReason.GUARDRAIL_VIOLATION,
            "secret detected",
            branch="agent/sess-3",
        )
        q = tmp_path / ".sdd" / "quarantine" / "sess-3.json"
        assert q.exists()
        meta = json.loads(q.read_text())
        assert meta["reason"] == "guardrail_violation"
        assert meta["branch"] == "agent/sess-3"

    def test_no_quarantine_for_manual(self, tmp_path: Path) -> None:
        enforce_kill_signal(tmp_path, "sess-4", KillReason.MANUAL, "human stop")
        q = tmp_path / ".sdd" / "quarantine" / "sess-4.json"
        assert not q.exists()


# ---------------------------------------------------------------------------
# _files_outside_scope
# ---------------------------------------------------------------------------


class TestFilesOutsideScope:
    def test_all_in_scope(self) -> None:
        assert _files_outside_scope(["src/foo.py", "src/bar.py"], ["src/"]) == []

    def test_some_outside(self) -> None:
        out = _files_outside_scope(["src/foo.py", "tests/test_foo.py"], ["src/"])
        assert out == ["tests/test_foo.py"]

    def test_exact_match(self) -> None:
        assert _files_outside_scope(["pyproject.toml"], ["pyproject.toml"]) == []

    def test_empty_owned(self) -> None:
        # No scope defined → everything is out of scope
        assert _files_outside_scope(["src/foo.py"], []) == ["src/foo.py"]


# ---------------------------------------------------------------------------
# check_scope_violations
# ---------------------------------------------------------------------------


class TestCheckScopeViolations:
    def test_no_worktree_skipped(self, tmp_path: Path) -> None:
        session = _make_session("s1")
        orch = _make_orch(tmp_path, [session])
        # Patch _lookup_tasks to return a task with owned_files
        task = MagicMock()
        task.owned_files = ["src/"]
        with patch("bernstein.core.circuit_breaker._lookup_tasks", return_value=[task]):
            result = _make_result()
            check_scope_violations(orch, result)
        # No worktree → nothing killed
        assert result.reaped == []

    def test_dead_session_skipped(self, tmp_path: Path) -> None:
        session = _make_session("s2")
        session.status = "dead"
        orch = _make_orch(tmp_path, [session])
        result = _make_result()
        check_scope_violations(orch, result)
        assert result.reaped == []

    def test_violation_kills_session(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        session = _make_session("s3")
        session.task_ids = ["task-1"]
        orch = _make_orch(tmp_path, [session])
        orch._spawner.get_worktree_path.return_value = str(wt_path)

        task = MagicMock()
        task.owned_files = ["src/"]

        with (
            patch("bernstein.core.circuit_breaker._lookup_tasks", return_value=[task]),
            patch(
                "bernstein.core.circuit_breaker._get_worktree_changed_files",
                return_value=["outside/file.py"],
            ),
            patch("bernstein.core.circuit_breaker._get_worktree_branch", return_value=None),
        ):
            result = _make_result()
            check_scope_violations(orch, result)

        assert "s3" in result.reaped
        assert session.status == "dead"
        kill_file = tmp_path / ".sdd" / "runtime" / "s3.kill"
        assert kill_file.exists()


# ---------------------------------------------------------------------------
# check_budget_violations
# ---------------------------------------------------------------------------


class TestCheckBudgetViolations:
    def test_disabled_when_budget_zero(self, tmp_path: Path) -> None:
        session = _make_session("b1", tokens_used=999_999)
        orch = _make_orch(tmp_path, [session], token_budget_per_session=0)
        result = _make_result()
        check_budget_violations(orch, result)
        assert result.reaped == []
        assert session.status != "dead"

    def test_under_budget_not_killed(self, tmp_path: Path) -> None:
        session = _make_session("b2", tokens_used=10_000)
        orch = _make_orch(tmp_path, [session], token_budget_per_session=50_000)
        result = _make_result()
        check_budget_violations(orch, result)
        assert result.reaped == []

    def test_over_budget_killed(self, tmp_path: Path) -> None:
        session = _make_session("b3", tokens_used=60_001)
        orch = _make_orch(tmp_path, [session], token_budget_per_session=60_000)
        result = _make_result()
        with patch("bernstein.core.circuit_breaker._get_worktree_branch", return_value=None):
            check_budget_violations(orch, result)
        assert "b3" in result.reaped
        assert session.status == "dead"
        kill_file = tmp_path / ".sdd" / "runtime" / "b3.kill"
        assert kill_file.exists()
        payload = json.loads(kill_file.read_text())
        assert payload["reason"] == "budget_exceeded"

    def test_dead_session_skipped(self, tmp_path: Path) -> None:
        session = _make_session("b4", tokens_used=999_999)
        session.status = "dead"
        orch = _make_orch(tmp_path, [session], token_budget_per_session=1)
        result = _make_result()
        check_budget_violations(orch, result)
        assert result.reaped == []

    def test_quarantine_written_on_budget_exceed(self, tmp_path: Path) -> None:
        session = _make_session("b5", tokens_used=100_001)
        orch = _make_orch(tmp_path, [session], token_budget_per_session=100_000)
        result = _make_result()
        with patch("bernstein.core.circuit_breaker._get_worktree_branch", return_value=None):
            check_budget_violations(orch, result)
        q = tmp_path / ".sdd" / "quarantine" / "b5.json"
        assert q.exists()
        meta = json.loads(q.read_text())
        assert meta["reason"] == "budget_exceeded"


# ---------------------------------------------------------------------------
# check_guardrail_violations
# ---------------------------------------------------------------------------


class TestCheckGuardrailViolations:
    def test_no_worktree_skipped(self, tmp_path: Path) -> None:
        session = _make_session("g1")
        orch = _make_orch(tmp_path, [session])
        result = _make_result()
        check_guardrail_violations(orch, result)
        assert result.reaped == []

    def test_clean_diff_not_killed(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        session = _make_session("g2")
        orch = _make_orch(tmp_path, [session])
        orch._spawner.get_worktree_path.return_value = str(wt_path)

        with patch(
            "bernstein.core.circuit_breaker._get_worktree_diff",
            return_value="diff --git a/src/foo.py b/src/foo.py\n+def hello(): pass",
        ):
            result = _make_result()
            check_guardrail_violations(orch, result)
        assert result.reaped == []

    def test_secret_in_diff_kills_agent(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        session = _make_session("g3")
        orch = _make_orch(tmp_path, [session])
        orch._spawner.get_worktree_path.return_value = str(wt_path)

        # Embed a fake AWS access key in the diff
        dirty_diff = "diff --git a/config.py b/config.py\n+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE123'\n"
        with (
            patch("bernstein.core.circuit_breaker._get_worktree_diff", return_value=dirty_diff),
            patch("bernstein.core.circuit_breaker._get_worktree_branch", return_value="agent/g3"),
        ):
            result = _make_result()
            check_guardrail_violations(orch, result)

        assert "g3" in result.reaped
        assert session.status == "dead"
        kill_file = tmp_path / ".sdd" / "runtime" / "g3.kill"
        assert kill_file.exists()
        payload = json.loads(kill_file.read_text())
        assert payload["reason"] == "guardrail_violation"

        q = tmp_path / ".sdd" / "quarantine" / "g3.json"
        assert q.exists()
        assert json.loads(q.read_text())["branch"] == "agent/g3"

    def test_dead_session_skipped(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        session = _make_session("g4")
        session.status = "dead"
        orch = _make_orch(tmp_path, [session])
        orch._spawner.get_worktree_path.return_value = str(wt_path)

        result = _make_result()
        check_guardrail_violations(orch, result)
        assert result.reaped == []

    def test_empty_diff_not_killed(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()

        session = _make_session("g5")
        orch = _make_orch(tmp_path, [session])
        orch._spawner.get_worktree_path.return_value = str(wt_path)

        with patch("bernstein.core.circuit_breaker._get_worktree_diff", return_value=""):
            result = _make_result()
            check_guardrail_violations(orch, result)
        assert result.reaped == []


# ---------------------------------------------------------------------------
# _get_worktree_diff
# ---------------------------------------------------------------------------


class TestGetWorktreeDiff:
    def test_returns_none_on_error(self, tmp_path: Path) -> None:
        # Non-git dir → returns None
        result = _get_worktree_diff(tmp_path / "not_a_repo")
        assert result is None

    def test_returns_diff_string(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="diff output here")
            diff = _get_worktree_diff(tmp_path)
        assert diff == "diff output here"

    def test_returns_none_on_nonzero_exit(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            diff = _get_worktree_diff(tmp_path)
        assert diff is None
