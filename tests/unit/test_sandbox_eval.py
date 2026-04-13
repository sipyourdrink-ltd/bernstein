"""Tests for the sandbox evaluation session manager."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.sandbox_eval import (
    MAX_BUDGET_USD,
    MAX_CONCURRENT_SESSIONS,
    SandboxManager,
    SandboxSession,
    SessionStatus,
    SolutionPack,
    validate_repo_url,
)


@pytest.fixture()
def mgr(tmp_path: Path) -> SandboxManager:
    return SandboxManager(workspace_base=tmp_path)


class TestValidateRepoUrl:
    def test_valid_url(self) -> None:
        assert validate_repo_url("https://github.com/owner/repo") is None

    def test_valid_url_trailing_slash(self) -> None:
        assert validate_repo_url("https://github.com/owner/repo/") is None

    def test_empty(self) -> None:
        assert validate_repo_url("") is not None

    def test_non_github(self) -> None:
        assert validate_repo_url("https://gitlab.com/owner/repo") is not None

    def test_private_path(self) -> None:
        assert validate_repo_url("https://github.com/owner/repo/../../etc") is not None

    def test_no_repo_name(self) -> None:
        assert validate_repo_url("https://github.com/owner") is not None

    def test_ssh_url(self) -> None:
        assert validate_repo_url("git@github.com:owner/repo.git") is not None


class TestSandboxSession:
    def test_defaults(self) -> None:
        s = SandboxSession()
        assert s.status == SessionStatus.QUEUED
        assert not s.is_terminal
        assert s.elapsed_s == pytest.approx(0.0)

    def test_terminal_states(self) -> None:
        for status in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.TIMED_OUT, SessionStatus.CANCELLED):
            s = SandboxSession(status=status)
            assert s.is_terminal

    def test_to_dict(self) -> None:
        s = SandboxSession(repo_url="https://github.com/a/b")
        d = s.to_dict()
        assert d["repo_url"] == "https://github.com/a/b"
        assert d["budget_limit_usd"] == MAX_BUDGET_USD
        assert d["max_agents"] == 3


class TestSandboxManager:
    def test_create_session(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/owner/repo", "code-quality")
        assert s.status == SessionStatus.QUEUED
        assert s.repo_url == "https://github.com/owner/repo"
        assert s.solution_pack == SolutionPack.CODE_QUALITY

    def test_invalid_url_raises(self, mgr: SandboxManager) -> None:
        with pytest.raises(ValueError, match="public GitHub"):
            mgr.create_session("https://gitlab.com/x/y", "code-quality")

    def test_invalid_pack_raises(self, mgr: SandboxManager) -> None:
        with pytest.raises(ValueError, match="Unknown solution pack"):
            mgr.create_session("https://github.com/a/b", "nonexistent")

    def test_get_session(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "security-audit")
        found = mgr.get_session(s.id)
        assert found is not None
        assert found.id == s.id

    def test_get_session_missing(self, mgr: SandboxManager) -> None:
        assert mgr.get_session("nonexistent") is None

    def test_cancel(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "code-quality")
        assert mgr.cancel(s.id)
        assert s.status == SessionStatus.CANCELLED
        assert not mgr.cancel(s.id)  # already terminal

    def test_ip_rate_limit(self, mgr: SandboxManager) -> None:
        for i in range(3):
            mgr.create_session(f"https://github.com/a/repo{i}", "code-quality", "1.2.3.4")
        with pytest.raises(RuntimeError, match="concurrent sessions per IP"):
            mgr.create_session("https://github.com/a/repo4", "code-quality", "1.2.3.4")

    def test_different_ip_ok(self, mgr: SandboxManager) -> None:
        for i in range(3):
            mgr.create_session(f"https://github.com/a/repo{i}", "code-quality", "1.2.3.4")
        s = mgr.create_session("https://github.com/a/x", "code-quality", "5.6.7.8")
        assert s.status == SessionStatus.QUEUED

    def test_max_concurrent_sessions(self, mgr: SandboxManager) -> None:
        for i in range(MAX_CONCURRENT_SESSIONS):
            mgr.create_session(f"https://github.com/a/repo{i}", "code-quality", f"10.0.0.{i}")
        with pytest.raises(RuntimeError, match="Too many active"):
            mgr.create_session("https://github.com/a/overflow", "code-quality", "99.0.0.1")

    def test_record_cost_triggers_completion(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "code-quality")
        mgr.mark_cloning(s.id)
        mgr.mark_started(s.id)
        mgr.record_cost(s.id, MAX_BUDGET_USD + 0.01)
        assert s.status == SessionStatus.COMPLETED
        assert s.error == "Budget exhausted"

    def test_lifecycle_transitions(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "code-quality")
        assert s.status == SessionStatus.QUEUED
        mgr.mark_cloning(s.id)
        assert s.status == SessionStatus.CLONING
        mgr.mark_started(s.id)
        assert s.status == SessionStatus.RUNNING
        assert s.started_at > 0

    def test_get_orchestrator_config(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "test-coverage")
        cfg = mgr.get_orchestrator_config(s)
        assert cfg["cli"] == "auto"
        assert cfg["budget"] == f"${MAX_BUDGET_USD}"
        assert cfg["max_agents"] <= 3

    def test_list_solution_packs(self, mgr: SandboxManager) -> None:
        packs = mgr.get_solution_packs()
        assert len(packs) == 8
        assert all("id" in p and "goal" in p for p in packs)

    def test_list_sessions_excludes_finished(self, mgr: SandboxManager) -> None:
        s1 = mgr.create_session("https://github.com/a/b", "code-quality")
        s2 = mgr.create_session("https://github.com/a/c", "code-quality")
        mgr.cancel(s1.id)
        active = mgr.list_sessions(include_finished=False)
        assert len(active) == 1
        assert active[0]["id"] == s2.id
        all_sessions = mgr.list_sessions(include_finished=True)
        assert len(all_sessions) == 2

    def test_check_timeouts(self, mgr: SandboxManager) -> None:
        s = mgr.create_session("https://github.com/a/b", "code-quality")
        s.created_at = 0  # far in the past
        timed_out = mgr.check_timeouts()
        assert s.id in timed_out
        assert s.status == SessionStatus.TIMED_OUT
