"""Unit tests for additional standalone Orchestrator methods.

These methods carry real branching logic but only touch a handful of
instance attributes, so each is bound onto a :class:`SimpleNamespace` stub
via :func:`types.MethodType` to run the genuine implementation.

Covered:

* :meth:`Orchestrator._maybe_reload_config` - hot-reload of max_agents /
  budget_usd from bernstein.yaml; no-change, parse-error, and apply paths.
* :meth:`Orchestrator._current_capacity` - slot accounting with dead agents.
* :meth:`Orchestrator._post_bulletin` - bulletin fan-out + no-op when unset.
* :meth:`Orchestrator._record_provider_health` - router update gating.
* :meth:`Orchestrator._release_file_ownership` / :meth:`_release_task_to_session`.
* :meth:`Orchestrator._maybe_poll_ci_autofix` - feature-flag / repo / throttle
  guards.
* :meth:`Orchestrator._should_auto_decompose` - config gate + delegation.
* git PR-branch helpers (:meth:`_get_current_branch`, :meth:`_has_commits_ahead`,
  :meth:`_push_branch`, :meth:`_check_existing_pr`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, Scope, Task

from bernstein.core.orchestration.orchestrator import Orchestrator


def _bind(stub: SimpleNamespace, name: str) -> None:
    setattr(stub, name, MethodType(getattr(Orchestrator, name), stub))


# ---------------------------------------------------------------------------
# _maybe_reload_config
# ---------------------------------------------------------------------------


def _reload_stub(
    config_path: Path, *, config_mtime: float, max_agents: int = 2, budget_usd: float = 5.0
) -> SimpleNamespace:
    stub = SimpleNamespace(
        _config_path=config_path,
        _config_mtime=config_mtime,
        _config=SimpleNamespace(max_agents=max_agents, budget_usd=budget_usd),
        _cost_tracker=SimpleNamespace(budget_usd=budget_usd),
    )
    _bind(stub, "_maybe_reload_config")
    return stub


def test_maybe_reload_config_missing_file_returns_false(tmp_path: Path) -> None:
    stub = _reload_stub(tmp_path / "absent.yaml", config_mtime=0.0)
    assert stub._maybe_reload_config() is False


def test_maybe_reload_config_unchanged_mtime_returns_false(tmp_path: Path) -> None:
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text("goal: x\n")
    mtime = cfg.stat().st_mtime
    # Record an mtime >= the current one so the change check short-circuits.
    stub = _reload_stub(cfg, config_mtime=mtime + 100)
    assert stub._maybe_reload_config() is False


def test_maybe_reload_config_parse_error_advances_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text("goal: x\n")

    def _boom(_p: Path) -> Any:
        raise ValueError("bad seed")

    monkeypatch.setattr("bernstein.core.seed.parse_seed", _boom)
    stub = _reload_stub(cfg, config_mtime=0.0)
    assert stub._maybe_reload_config() is False
    # mtime is advanced to the file's mtime so the parse is not retried each tick.
    assert stub._config_mtime == pytest.approx(cfg.stat().st_mtime)


def test_maybe_reload_config_applies_max_agents_and_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text("goal: x\n")

    monkeypatch.setattr(
        "bernstein.core.seed.parse_seed",
        lambda _p: SimpleNamespace(max_agents=8, budget_usd=99.0),
    )
    stub = _reload_stub(cfg, config_mtime=0.0, max_agents=2, budget_usd=5.0)
    changed = stub._maybe_reload_config()
    assert changed is True
    assert stub._config.max_agents == 8
    assert stub._config.budget_usd == pytest.approx(99.0)
    # Budget change also propagates into the live cost tracker.
    assert stub._cost_tracker.budget_usd == pytest.approx(99.0)


def test_maybe_reload_config_no_field_change_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text("goal: x\n")
    monkeypatch.setattr(
        "bernstein.core.seed.parse_seed",
        lambda _p: SimpleNamespace(max_agents=2, budget_usd=5.0),  # identical to current
    )
    stub = _reload_stub(cfg, config_mtime=0.0, max_agents=2, budget_usd=5.0)
    # mtime moved but no field actually changed -> False.
    assert stub._maybe_reload_config() is False


# ---------------------------------------------------------------------------
# _current_capacity
# ---------------------------------------------------------------------------


def _capacity_stub(agents: dict[str, AgentSession], max_agents: int) -> SimpleNamespace:
    stub = SimpleNamespace(_agents=agents, _config=SimpleNamespace(max_agents=max_agents))
    _bind(stub, "_current_capacity")
    return stub


def test_current_capacity_counts_only_live_agents() -> None:
    live = AgentSession(id="A-1", role="backend")
    live.status = "working"
    dead = AgentSession(id="A-2", role="backend")
    dead.status = "dead"
    stub = _capacity_stub({"A-1": live, "A-2": dead}, max_agents=4)
    cap = stub._current_capacity()
    assert cap.active_agents == 1
    assert cap.available_slots == 3
    assert cap.max_agents == 4


def test_current_capacity_clamps_available_slots_at_zero() -> None:
    a = AgentSession(id="A-1", role="backend")
    a.status = "working"
    b = AgentSession(id="A-2", role="backend")
    b.status = "working"
    stub = _capacity_stub({"A-1": a, "A-2": b}, max_agents=1)
    cap = stub._current_capacity()
    # Two live agents but only one slot configured: never go negative.
    assert cap.available_slots == 0
    assert cap.active_agents == 2


# ---------------------------------------------------------------------------
# _post_bulletin
# ---------------------------------------------------------------------------


def test_post_bulletin_no_board_is_noop() -> None:
    stub = SimpleNamespace(_bulletin=None)
    _bind(stub, "_post_bulletin")
    stub._post_bulletin("status", "hello")  # must not raise


def test_post_bulletin_posts_message_to_board() -> None:
    board = MagicMock()
    stub = SimpleNamespace(_bulletin=board)
    _bind(stub, "_post_bulletin")
    stub._post_bulletin("alert", "something happened")
    assert board.post.call_count == 1
    msg = board.post.call_args.args[0]
    assert msg.agent_id == "orchestrator"
    assert msg.content == "something happened"


# ---------------------------------------------------------------------------
# _record_provider_health
# ---------------------------------------------------------------------------


def _provider_stub(router: Any | None) -> SimpleNamespace:
    stub = SimpleNamespace(_router=router)
    _bind(stub, "_record_provider_health")
    return stub


def test_record_provider_health_no_router_is_noop() -> None:
    session = AgentSession(id="A-1", role="backend")
    session.provider = "anthropic"
    stub = _provider_stub(None)
    stub._record_provider_health(session, success=True)  # must not raise


def test_record_provider_health_no_provider_is_noop() -> None:
    router = MagicMock()
    session = AgentSession(id="A-1", role="backend")
    session.provider = None
    stub = _provider_stub(router)
    stub._record_provider_health(session, success=True)
    router.update_provider_health.assert_not_called()


def test_record_provider_health_updates_health_only_when_no_cost() -> None:
    router = MagicMock()
    session = AgentSession(id="A-1", role="backend")
    session.provider = "anthropic"
    stub = _provider_stub(router)
    stub._record_provider_health(session, success=False, latency_ms=120.0)
    router.update_provider_health.assert_called_once_with("anthropic", False, 120.0)
    # No cost/tokens => cost recording is skipped.
    router.record_provider_cost.assert_not_called()


def test_record_provider_health_records_cost_when_present() -> None:
    router = MagicMock()
    session = AgentSession(id="A-1", role="backend")
    session.provider = "openrouter"
    stub = _provider_stub(router)
    stub._record_provider_health(session, success=True, latency_ms=50.0, cost_usd=0.5, tokens=1000)
    router.update_provider_health.assert_called_once_with("openrouter", True, 50.0)
    router.record_provider_cost.assert_called_once_with("openrouter", 1000, 0.5)


# ---------------------------------------------------------------------------
# _release_file_ownership / _release_task_to_session
# ---------------------------------------------------------------------------


def test_release_file_ownership_clears_dict_and_lock_manager() -> None:
    from bernstein.core.persistence.file_locks import FileLock

    lock_manager = MagicMock()
    # After release, only agent-2's file remains
    lock_manager.all_locks.return_value = [
        FileLock(file_path="b.py", agent_id="agent-2", task_id="T-2", task_title="", locked_at=0.0),
    ]

    # Create a class to hold the property (SimpleNamespace can't have properties)
    class Stub:
        def __init__(self) -> None:
            self._lock_manager = lock_manager

        _release_file_ownership = Orchestrator._release_file_ownership
        _file_ownership = Orchestrator._file_ownership

    stub = Stub()
    stub._release_file_ownership("agent-1")
    lock_manager.release.assert_called_once_with("agent-1")
    # _file_ownership is now a property that reads from lock_manager.all_locks()
    assert stub._file_ownership == {"b.py": "agent-2"}


def test_release_task_to_session_pops_reverse_index() -> None:
    stub = SimpleNamespace(_task_to_session={"T-1": "A-1", "T-2": "A-2", "T-3": "A-1"})
    _bind(stub, "_release_task_to_session")
    stub._release_task_to_session(["T-1", "T-3", "T-missing"])
    assert stub._task_to_session == {"T-2": "A-2"}


# ---------------------------------------------------------------------------
# _maybe_poll_ci_autofix guards
# ---------------------------------------------------------------------------


def test_maybe_poll_ci_autofix_disabled_returns_empty() -> None:
    stub = SimpleNamespace(
        _config=SimpleNamespace(ci_autofix=SimpleNamespace(enabled=False, repo="x/y", poll_interval_s=60))
    )
    _bind(stub, "_maybe_poll_ci_autofix")
    assert stub._maybe_poll_ci_autofix() == []


def test_maybe_poll_ci_autofix_none_config_returns_empty() -> None:
    stub = SimpleNamespace(_config=SimpleNamespace(ci_autofix=None))
    _bind(stub, "_maybe_poll_ci_autofix")
    assert stub._maybe_poll_ci_autofix() == []


def test_maybe_poll_ci_autofix_no_repo_returns_empty() -> None:
    stub = SimpleNamespace(
        _config=SimpleNamespace(ci_autofix=SimpleNamespace(enabled=True, repo="", poll_interval_s=60))
    )
    _bind(stub, "_maybe_poll_ci_autofix")
    assert stub._maybe_poll_ci_autofix() == []


def test_maybe_poll_ci_autofix_throttled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("bernstein.core.orchestration.orchestrator.time.time", lambda: now)
    stub = SimpleNamespace(
        _config=SimpleNamespace(ci_autofix=SimpleNamespace(enabled=True, repo="x/y", poll_interval_s=60, token="t")),
        _last_ci_poll_ts=now - 5.0,  # only 5s since last poll, interval is 60s
    )
    _bind(stub, "_maybe_poll_ci_autofix")
    assert stub._maybe_poll_ci_autofix() == []
    # Throttle guard must not advance the timestamp.
    assert stub._last_ci_poll_ts == pytest.approx(now - 5.0)


# ---------------------------------------------------------------------------
# _should_auto_decompose
# ---------------------------------------------------------------------------


def _decompose_stub(
    *, auto_decompose: bool, force_parallel: bool = False, decomposed: set[str] | None = None
) -> SimpleNamespace:
    stub = SimpleNamespace(
        _config=SimpleNamespace(auto_decompose=auto_decompose, force_parallel=force_parallel),
        _decomposed_task_ids=decomposed or set(),
        _workdir=Path("/tmp/wd"),
    )
    _bind(stub, "_should_auto_decompose")
    return stub


def _large_task(task_id: str = "T-large") -> Task:
    return Task(id=task_id, title="Big", description="", role="backend", scope=Scope.LARGE)


def test_should_auto_decompose_disabled_returns_false() -> None:
    stub = _decompose_stub(auto_decompose=False)
    assert stub._should_auto_decompose(_large_task()) is False


def test_should_auto_decompose_large_scope_when_enabled() -> None:
    stub = _decompose_stub(auto_decompose=True)
    assert stub._should_auto_decompose(_large_task()) is True


def test_should_auto_decompose_skips_already_decomposed() -> None:
    stub = _decompose_stub(auto_decompose=True, decomposed={"T-large"})
    assert stub._should_auto_decompose(_large_task("T-large")) is False


def test_should_auto_decompose_small_task_not_decomposed() -> None:
    stub = _decompose_stub(auto_decompose=True)
    small = Task(id="T-s", title="small", description="", role="backend", scope=Scope.SMALL)
    assert stub._should_auto_decompose(small) is False


# ---------------------------------------------------------------------------
# git PR-branch helpers
# ---------------------------------------------------------------------------


def _git_stub() -> SimpleNamespace:
    stub = SimpleNamespace(_workdir=Path("/tmp/repo"))
    for name in ("_get_current_branch", "_has_commits_ahead", "_push_branch", "_check_existing_pr"):
        _bind(stub, name)
    return stub


def test_get_current_branch_returns_branch_name() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="feature/x\n", stderr="")
    with patch("subprocess.run", return_value=completed):
        assert stub._get_current_branch() == "feature/x"


def test_get_current_branch_returns_none_on_error() -> None:
    stub = _git_stub()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
        assert stub._get_current_branch() is None


def test_has_commits_ahead_true_when_log_nonempty() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="abc123 commit\n", stderr="")
    with patch("subprocess.run", return_value=completed):
        assert stub._has_commits_ahead("feature") is True


def test_has_commits_ahead_false_when_no_commits() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        assert stub._has_commits_ahead("feature") is False


def test_push_branch_true_on_success() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=completed):
        assert stub._push_branch("feature") is True


def test_push_branch_false_on_exception() -> None:
    stub = _git_stub()
    with patch("subprocess.run", side_effect=OSError("git missing")):
        assert stub._push_branch("feature") is False


def test_check_existing_pr_returns_url_when_present() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout="https://github.com/x/y/pull/9\n", stderr=""
    )
    with patch("subprocess.run", return_value=completed):
        assert stub._check_existing_pr("feature") == "https://github.com/x/y/pull/9"


def test_check_existing_pr_returns_none_when_no_pr() -> None:
    stub = _git_stub()
    completed = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="no pr")
    with patch("subprocess.run", return_value=completed):
        assert stub._check_existing_pr("feature") is None
