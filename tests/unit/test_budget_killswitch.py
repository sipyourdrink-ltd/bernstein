"""Regression tests for audit-056 — budget kill-switch terminates in-flight agents.

Covers :meth:`Orchestrator._enforce_budget_killswitch`:

* First transition: SHUTDOWN signal is sent to every live agent and a single
  ``budget.exhaust`` event is emitted with the final spend.
* During the configured grace window, no SIGKILL is issued — agents are
  allowed to commit WIP.
* After the grace window expires, remaining live sessions are SIGKILLed via
  ``spawner.kill``; each session is only killed once.
* When spend falls back under the threshold the switch re-arms so a future
  exhaustion triggers a fresh SHUTDOWN wave.

Only the kill-switch logic is exercised; the rest of the tick pipeline is
not relevant here.
"""

from __future__ import annotations

import time
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from bernstein.core.cost_tracker import CostTracker
from bernstein.core.models import AgentSession

from bernstein.core.orchestration.orchestrator import Orchestrator

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(sid: str) -> AgentSession:
    return AgentSession(id=sid, role="backend")


def _build_orch_stub(
    tmp_path: Path,
    sessions: list[AgentSession],
    *,
    budget_usd: float = 1.0,
    kill_grace_period_s: int = 30,
) -> SimpleNamespace:
    """Build an orchestrator-shaped stub with only the fields
    ``_enforce_budget_killswitch`` touches.

    The real :meth:`Orchestrator._enforce_budget_killswitch` is bound onto
    the stub via :func:`types.MethodType` so test coverage reaches the
    actual implementation rather than a reimplementation.
    """
    tracker = CostTracker(
        run_id="test-budget-killswitch",
        budget_usd=budget_usd,
        kill_grace_period_s=kill_grace_period_s,
    )
    spawner = MagicMock()
    spawner.kill = MagicMock()
    stub = SimpleNamespace(
        _workdir=tmp_path,
        _cost_tracker=tracker,
        _agents={s.id: s for s in sessions},
        _spawner=spawner,
        _budget_stop_fired_at=None,
        _budget_stop_killed_agents=set(),
        # Capture calls to these helpers instead of reaching further into
        # the real orchestrator graph.
        _send_shutdown_signals=MagicMock(),
        _post_bulletin=MagicMock(),
        _notify=MagicMock(),
    )
    stub._enforce_budget_killswitch = MethodType(
        Orchestrator._enforce_budget_killswitch,  # type: ignore[arg-type]
        stub,
    )
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBudgetKillSwitchTransition:
    """First tick where should_stop=True must SHUTDOWN every live agent."""

    def test_shutdown_signal_sent_to_all_live_agents(self, tmp_path: Path) -> None:
        sessions = [_session("A-1"), _session("A-2"), _session("A-3")]
        stub = _build_orch_stub(tmp_path, sessions, budget_usd=1.0)
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)
        assert stub._cost_tracker.status().should_stop is True

        stub._enforce_budget_killswitch()

        # SHUTDOWN sent exactly once, with a reason containing final spend.
        assert stub._send_shutdown_signals.call_count == 1
        reason = stub._send_shutdown_signals.call_args.args[0]
        assert "budget exhausted" in reason
        assert "$1.5000" in reason or "1.5" in reason

        # budget.exhaust event emitted via notifier + bulletin.
        notify_events = [c.args[0] for c in stub._notify.call_args_list]
        assert "budget.exhaust" in notify_events
        bulletin_msgs = [c.args[1] for c in stub._post_bulletin.call_args_list]
        assert any("budget.exhaust" in msg for msg in bulletin_msgs)

        # Kill-switch state is now armed.
        assert stub._budget_stop_fired_at is not None
        # No SIGKILL yet — grace period still in effect.
        stub._spawner.kill.assert_not_called()

    def test_dead_sessions_skipped(self, tmp_path: Path) -> None:
        alive = _session("A-live")
        dead = _session("A-dead")
        dead.status = "dead"
        stub = _build_orch_stub(tmp_path, [alive, dead], budget_usd=1.0)
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)

        stub._enforce_budget_killswitch()

        # Notification metadata reports only the live count.
        notify_call = next(c for c in stub._notify.call_args_list if c.args[0] == "budget.exhaust")
        assert notify_call.kwargs["live_agents"] == 1

    def test_notify_not_fired_when_within_budget(self, tmp_path: Path) -> None:
        stub = _build_orch_stub(tmp_path, [_session("A-1")], budget_usd=10.0)
        stub._cost_tracker.record("A-0", "T-0", "sonnet", 0, 0, cost_usd=0.01)
        assert stub._cost_tracker.status().should_stop is False

        stub._enforce_budget_killswitch()

        stub._send_shutdown_signals.assert_not_called()
        stub._spawner.kill.assert_not_called()
        assert stub._budget_stop_fired_at is None


class TestBudgetKillSwitchGracePeriod:
    """SIGKILL only fires after the grace window elapses."""

    def test_no_sigkill_during_grace_window(self, tmp_path: Path) -> None:
        sessions = [_session("A-1"), _session("A-2")]
        stub = _build_orch_stub(
            tmp_path,
            sessions,
            budget_usd=1.0,
            kill_grace_period_s=30,
        )
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)

        # Tick 1: SHUTDOWN fires, timestamp recorded.
        stub._enforce_budget_killswitch()
        assert stub._send_shutdown_signals.call_count == 1

        # Tick 2 (immediately after): still within grace window — no kill.
        stub._enforce_budget_killswitch()
        stub._spawner.kill.assert_not_called()
        # SHUTDOWN is not re-sent on later ticks.
        assert stub._send_shutdown_signals.call_count == 1

    def test_sigkill_after_grace_window(self, tmp_path: Path) -> None:
        sessions = [_session("A-1"), _session("A-2")]
        stub = _build_orch_stub(
            tmp_path,
            sessions,
            budget_usd=1.0,
            kill_grace_period_s=30,
        )
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)

        stub._enforce_budget_killswitch()
        # Simulate 31 seconds elapsed by rewinding the stored timestamp.
        stub._budget_stop_fired_at = time.time() - 31

        stub._enforce_budget_killswitch()

        # Both live agents SIGKILLed exactly once.
        assert stub._spawner.kill.call_count == 2
        killed_ids = {c.args[0].id for c in stub._spawner.kill.call_args_list}
        assert killed_ids == {"A-1", "A-2"}

    def test_sigkill_only_once_per_agent(self, tmp_path: Path) -> None:
        stub = _build_orch_stub(
            tmp_path,
            [_session("A-1")],
            budget_usd=1.0,
            kill_grace_period_s=30,
        )
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)

        stub._enforce_budget_killswitch()
        stub._budget_stop_fired_at = time.time() - 31

        stub._enforce_budget_killswitch()
        stub._enforce_budget_killswitch()  # second pass after kill

        assert stub._spawner.kill.call_count == 1


class TestBudgetKillSwitchRearm:
    """Lowering spend below the threshold re-arms the switch."""

    def test_rearms_when_spend_drops_back(self, tmp_path: Path) -> None:
        stub = _build_orch_stub(tmp_path, [_session("A-1")], budget_usd=1.0)
        stub._cost_tracker.record("A-0", "T-0", "opus", 0, 0, cost_usd=1.5)

        stub._enforce_budget_killswitch()
        assert stub._budget_stop_fired_at is not None

        # Operator bumps the budget (hot-reload path) — spend ratio drops.
        stub._cost_tracker.budget_usd = 100.0
        assert stub._cost_tracker.status().should_stop is False

        stub._enforce_budget_killswitch()

        assert stub._budget_stop_fired_at is None
        assert stub._budget_stop_killed_agents == set()
