"""Mutation-kill tests for ``core/security/sandbox_eval.py``.

``scripts/mutmut_critical.py`` scored the ``sandbox_eval`` target at 19/60:
``test_sandbox_eval.py`` drives the happy paths, but the published limits,
the solution-pack table every prospect sees, the elapsed-time clock and the
guards on each state transition could be changed without a test failing.
Each test below pins one of those decisions to an exact value, following the
other ``*_mutation_kill.py`` files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security import sandbox_eval
from bernstein.core.security.sandbox_eval import (
    SOLUTION_PACK_CONFIGS,
    SandboxManager,
    SandboxSession,
    SessionStatus,
    SolutionPack,
    validate_repo_url,
)

_URL = "https://github.com/owner/repo"


@pytest.fixture
def mgr(tmp_path: Path) -> SandboxManager:
    return SandboxManager(tmp_path)


# ---------------------------------------------------------------------------
# Published limits
# ---------------------------------------------------------------------------


def test_the_published_limits() -> None:
    """The module docstring promises "up to 3 agents ... within a $2 budget"."""
    assert sandbox_eval.MAX_AGENTS == 3
    assert sandbox_eval.MAX_BUDGET_USD == 2.0
    assert sandbox_eval.SESSION_TIMEOUT_S == 1800
    assert sandbox_eval.MAX_CONCURRENT_SESSIONS == 20
    assert sandbox_eval.MAX_REPO_SIZE_MB == 200
    assert sandbox_eval.CLEANUP_GRACE_S == 300


# ---------------------------------------------------------------------------
# Solution packs: the catalogue a prospect picks from
# ---------------------------------------------------------------------------

_PACKS = {
    "code-quality": (
        ["backend", "qa"],
        "Run linters, fix code style issues, and improve code quality across the repository",
        2,
        10,
    ),
    "test-coverage": (
        ["qa", "backend"],
        "Analyze test coverage gaps and write missing unit and integration tests",
        3,
        15,
    ),
    "security-audit": (
        ["security", "backend"],
        "Scan for OWASP Top 10 vulnerabilities, dependency CVEs, and hardcoded secrets",
        2,
        10,
    ),
    "documentation": (
        ["docs", "architect"],
        "Generate README, API docs, architecture overview, and inline documentation",
        2,
        10,
    ),
    "performance": (
        ["backend", "architect"],
        "Profile hot paths, identify N+1 queries, and optimize critical bottlenecks",
        2,
        15,
    ),
    "bug-fix": (
        ["backend", "qa"],
        "Identify and fix open bugs from issue tracker or failing tests",
        3,
        15,
    ),
    "dependency-update": (
        ["backend", "qa"],
        "Update outdated dependencies, fix breaking changes, and verify tests pass",
        2,
        10,
    ),
    "refactor": (
        ["architect", "backend"],
        "Identify code smells, reduce duplication, and improve module structure",
        2,
        15,
    ),
}


def test_every_solution_pack_is_listed_exactly(mgr: SandboxManager) -> None:
    listed = {p["id"]: p for p in mgr.get_solution_packs()}
    assert list(listed) == list(_PACKS)
    for pack_id, (team, goal, max_agents, minutes) in _PACKS.items():
        assert listed[pack_id] == {
            "id": pack_id,
            "name": pack_id.replace("-", " ").title(),
            "goal": goal,
            "team": team,
            "max_agents": max_agents,
            "estimated_minutes": minutes,
        }


def test_no_pack_asks_for_more_agents_than_the_sandbox_allows() -> None:
    assert all(cfg["max_agents"] <= sandbox_eval.MAX_AGENTS for cfg in SOLUTION_PACK_CONFIGS.values())
    assert set(SOLUTION_PACK_CONFIGS) == set(SolutionPack)


@pytest.mark.parametrize("pack", list(SolutionPack))
def test_the_orchestrator_config_for_each_pack(mgr: SandboxManager, pack: SolutionPack) -> None:
    session = mgr.create_session(_URL, pack.value)
    team, goal, max_agents, _ = _PACKS[pack.value]

    assert mgr.get_orchestrator_config(session) == {
        "goal": goal,
        "cli": "auto",
        "team": team,
        "budget": "$2.0",
        "max_agents": max_agents,
        "constraints": [
            "Public repo only - do not push, create PRs, or modify remote",
            "Read-only access to external services",
            "Hard budget cap: $2.0",
        ],
    }


# ---------------------------------------------------------------------------
# SandboxSession
# ---------------------------------------------------------------------------


def test_a_new_session_has_used_nothing() -> None:
    s = SandboxSession()
    assert (s.started_at, s.finished_at, s.budget_used_usd, s.agents_spawned) == (0.0, 0.0, 0.0, 0)
    assert s.status is SessionStatus.QUEUED
    assert s.elapsed_s == 0.0


def test_elapsed_is_finished_minus_started() -> None:
    s = SandboxSession(started_at=100.0, finished_at=142.5)
    assert s.elapsed_s == 42.5


def test_a_sub_second_finish_time_is_still_a_finish_time() -> None:
    """``finished_at > 0`` means "finished"; the clock is read only when it is 0."""
    assert SandboxSession(started_at=0.25, finished_at=0.75).elapsed_s == 0.5


def test_elapsed_runs_against_the_clock_until_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 130.0)
    assert SandboxSession(started_at=100.0).elapsed_s == 30.0


def test_a_session_started_at_one_second_is_still_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """``started_at == 0`` means "not started"; any other value is a start time."""
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 5.0)
    assert SandboxSession(started_at=1.0).elapsed_s == 4.0


def test_to_dict_rounds_elapsed_to_one_place() -> None:
    d = SandboxSession(started_at=100.0, finished_at=101.26, budget_used_usd=0.123456).to_dict()
    assert d["elapsed_s"] == 1.3
    assert d["budget_used_usd"] == 0.1235
    assert d["budget_limit_usd"] == 2.0
    assert d["max_agents"] == 3


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (SessionStatus.QUEUED, False),
        (SessionStatus.CLONING, False),
        (SessionStatus.RUNNING, False),
        (SessionStatus.COMPLETED, True),
        (SessionStatus.FAILED, True),
        (SessionStatus.TIMED_OUT, True),
        (SessionStatus.CANCELLED, True),
    ],
)
def test_terminal_states(status: SessionStatus, terminal: bool) -> None:
    assert SandboxSession(status=status).is_terminal is terminal


# ---------------------------------------------------------------------------
# validate_repo_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["", "   ", "\t\n"])
def test_an_empty_or_blank_url_is_required(url: str) -> None:
    assert validate_repo_url(url) == "Repository URL is required"


def test_a_non_github_url_names_the_supported_form() -> None:
    assert validate_repo_url("https://gitlab.com/o/r") == (
        "Only public GitHub repository URLs are supported (https://github.com/owner/repo)"
    )


def test_a_good_url_with_surrounding_space_and_slash_is_valid() -> None:
    assert validate_repo_url(f"  {_URL}/  ") is None


def test_a_dot_dot_segment_is_refused() -> None:
    assert validate_repo_url("https://github.com/owner/..") == "Invalid repository URL"


# ---------------------------------------------------------------------------
# SandboxManager.create_session
# ---------------------------------------------------------------------------


def test_the_stored_url_is_normalised(mgr: SandboxManager) -> None:
    session = mgr.create_session(f" {_URL}/ ", "code-quality")
    assert session.repo_url == _URL
    assert session.workspace_path == str(mgr._base / "sandbox" / session.id)


def test_the_unknown_pack_message_lists_every_pack(mgr: SandboxManager) -> None:
    with pytest.raises(ValueError) as exc:
        mgr.create_session(_URL, "nope")
    assert str(exc.value) == "Unknown solution pack. Valid packs: " + ", ".join(_PACKS)


def test_the_global_cap_admits_the_twentieth_and_refuses_the_twenty_first(mgr: SandboxManager) -> None:
    for _ in range(19):
        mgr.create_session(_URL, "code-quality")
    assert mgr.active_count == 19
    mgr.create_session(_URL, "code-quality")
    assert mgr.active_count == 20
    with pytest.raises(RuntimeError, match="Too many active sessions"):
        mgr.create_session(_URL, "code-quality")


def test_a_terminal_session_frees_a_global_slot(mgr: SandboxManager) -> None:
    sessions = [mgr.create_session(_URL, "code-quality") for _ in range(20)]
    mgr.cancel(sessions[0].id)
    assert mgr.active_count == 19
    mgr.create_session(_URL, "code-quality")


@pytest.mark.parametrize(
    "status", [SessionStatus.QUEUED, SessionStatus.CLONING, SessionStatus.RUNNING], ids=lambda s: s.value
)
def test_every_live_status_counts_as_active(mgr: SandboxManager, status: SessionStatus) -> None:
    mgr.create_session(_URL, "code-quality").status = status
    assert mgr.active_count == 1


def test_the_per_ip_cap_admits_the_third_and_refuses_the_fourth(mgr: SandboxManager) -> None:
    for _ in range(3):
        mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4")
    with pytest.raises(RuntimeError, match=r"^Maximum 3 concurrent sessions per IP$"):
        mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4")


def test_a_finished_session_frees_its_ips_slot(mgr: SandboxManager) -> None:
    sessions = [mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4") for _ in range(3)]
    mgr.cancel(sessions[0].id)
    mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4")


def test_a_cleaned_up_session_frees_its_ips_slot(mgr: SandboxManager) -> None:
    """A session id the IP index still lists, but the manager has dropped, is not active."""
    sessions = [mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4") for _ in range(3)]
    del mgr._sessions[sessions[0].id]
    mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4")


def test_the_ip_is_stored_hashed_and_indexed(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality", client_ip="1.2.3.4")
    expected = sandbox_eval.hashlib.sha256(b"1.2.3.4").hexdigest()[:16]
    assert session.client_ip_hash == expected
    assert mgr._ip_sessions == {expected: [session.id]}


def test_no_ip_means_no_ip_index_and_no_cap(mgr: SandboxManager) -> None:
    for _ in range(5):
        session = mgr.create_session(_URL, "code-quality")
        assert session.client_ip_hash == ""
    assert mgr._ip_sessions == {}


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_record_cost_on_an_unknown_session_is_a_no_op(mgr: SandboxManager) -> None:
    mgr.record_cost("missing", 5.0)
    assert mgr.get_session("missing") is None


def test_a_cost_just_under_the_cap_keeps_running(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality")
    mgr.record_cost(session.id, 1.99)
    assert session.status is SessionStatus.QUEUED
    assert session.error == ""


def test_reaching_the_cap_completes_with_a_reason(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality")
    mgr.record_cost(session.id, 1.0)
    mgr.record_cost(session.id, 1.0)
    assert session.status is SessionStatus.COMPLETED
    assert session.error == "Budget exhausted"


def test_mark_started_only_moves_a_cloning_session(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 777.0)
    session = mgr.create_session(_URL, "code-quality")

    mgr.mark_started(session.id)
    assert session.status is SessionStatus.QUEUED
    assert session.started_at == 0.0

    mgr.mark_cloning(session.id)
    mgr.mark_started(session.id)
    assert session.status is SessionStatus.RUNNING
    assert session.started_at == 777.0


def test_mark_cloning_only_moves_a_queued_session(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality")
    session.status = SessionStatus.RUNNING
    mgr.mark_cloning(session.id)
    assert session.status is SessionStatus.RUNNING


@pytest.mark.parametrize("call", ["mark_started", "mark_cloning", "finish", "cancel"])
def test_transitions_on_an_unknown_session_do_nothing(mgr: SandboxManager, call: str) -> None:
    fn = getattr(mgr, call)
    result = fn("missing", SessionStatus.FAILED) if call == "finish" else fn("missing")
    assert result in (None, False)


def test_cancel_reports_whether_it_cancelled(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality")
    assert mgr.cancel("missing") is False
    assert mgr.cancel(session.id) is True
    assert session.status is SessionStatus.CANCELLED
    assert mgr.cancel(session.id) is False


def test_finish_records_status_time_and_error_once(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 900.0)
    session = mgr.create_session(_URL, "code-quality")
    mgr.finish(session.id, SessionStatus.FAILED, error="clone failed")
    assert (session.status, session.finished_at, session.error) == (SessionStatus.FAILED, 900.0, "clone failed")

    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 999.0)
    mgr.finish(session.id, SessionStatus.COMPLETED, error="later")
    assert (session.status, session.finished_at, session.error) == (SessionStatus.FAILED, 900.0, "clone failed")


def test_finish_without_an_error_keeps_the_error_empty(mgr: SandboxManager) -> None:
    session = mgr.create_session(_URL, "code-quality")
    mgr.finish(session.id, SessionStatus.COMPLETED)
    assert session.error == ""


# ---------------------------------------------------------------------------
# Timeouts, cleanup and listing
# ---------------------------------------------------------------------------


def test_a_session_times_out_only_after_the_full_window(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    session = mgr.create_session(_URL, "code-quality")
    session.created_at = 1000.0

    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 1000.0 + 1800)
    assert mgr.check_timeouts() == []
    assert session.status is SessionStatus.QUEUED

    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 1000.0 + 1801)
    assert mgr.check_timeouts() == [session.id]
    assert (session.status, session.error) == (SessionStatus.TIMED_OUT, "Session timed out")


def test_a_finished_session_is_not_timed_out(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    session = mgr.create_session(_URL, "code-quality")
    session.created_at = 0.0
    mgr.cancel(session.id)
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 10_000.0)
    assert mgr.check_timeouts() == []
    assert session.status is SessionStatus.CANCELLED


def test_cleanup_waits_for_the_grace_period(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    session = mgr.create_session(_URL, "code-quality")
    Path(session.workspace_path).mkdir(parents=True)
    mgr.cancel(session.id)
    session.finished_at = 5000.0

    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 5000.0 + 300)
    assert mgr.cleanup_finished() == 0
    assert mgr.get_session(session.id) is session

    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 5000.0 + 301)
    assert mgr.cleanup_finished() == 1
    assert not Path(session.workspace_path).exists()
    assert mgr.get_session(session.id) is None


def test_cleanup_drops_a_session_with_no_workspace_without_counting_it(
    mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = mgr.create_session(_URL, "code-quality")
    mgr.cancel(session.id)
    session.finished_at = 0.0
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 10_000.0)
    assert mgr.cleanup_finished() == 0
    assert mgr.get_session(session.id) is None


def test_cleanup_leaves_live_sessions(mgr: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> None:
    session = mgr.create_session(_URL, "code-quality")
    monkeypatch.setattr(sandbox_eval.time, "time", lambda: 10_000_000.0)
    assert mgr.cleanup_finished() == 0
    assert mgr.get_session(session.id) is session


def test_list_sessions_newest_first_and_filters_finished(mgr: SandboxManager) -> None:
    old = mgr.create_session(_URL, "code-quality")
    new = mgr.create_session(_URL, "code-quality")
    old.created_at, new.created_at = 1.0, 2.0
    mgr.cancel(old.id)

    assert [d["id"] for d in mgr.list_sessions()] == [new.id]
    assert [d["id"] for d in mgr.list_sessions(include_finished=True)] == [new.id, old.id]
