"""Tests for ORCH-005: Heartbeat timeout escalation ladder."""

from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.heartbeat_escalation import (
    EscalationAction,
    EscalationThresholds,
    EscalationTier,
    HeartbeatEscalationLadder,
)

# ---------------------------------------------------------------------------
# EscalationTier
# ---------------------------------------------------------------------------


class TestEscalationTier:
    """Tests for the EscalationTier enum."""

    def test_ordering(self) -> None:
        assert EscalationTier.NONE < EscalationTier.WARN
        assert EscalationTier.WARN < EscalationTier.SIGUSR1
        assert EscalationTier.SIGUSR1 < EscalationTier.SIGTERM
        assert EscalationTier.SIGTERM < EscalationTier.SIGKILL

    def test_five_tiers(self) -> None:
        assert len(EscalationTier) == 5


# ---------------------------------------------------------------------------
# EscalationThresholds
# ---------------------------------------------------------------------------


class TestEscalationThresholds:
    """Tests for threshold configuration."""

    def test_defaults(self) -> None:
        t = EscalationThresholds()
        assert t.warn_s == pytest.approx(60.0)
        assert t.sigusr1_s == pytest.approx(90.0)
        assert t.sigterm_s == pytest.approx(120.0)
        assert t.sigkill_s == pytest.approx(150.0)

    def test_custom_thresholds(self) -> None:
        t = EscalationThresholds(
            warn_s=30.0,
            sigusr1_s=60.0,
            sigterm_s=90.0,
            sigkill_s=120.0,
        )
        assert t.warn_s == pytest.approx(30.0)
        assert t.sigkill_s == pytest.approx(120.0)

    def test_validation_passes(self) -> None:
        t = EscalationThresholds()
        errors = t.validate()
        assert errors == []

    def test_validation_fails_non_monotonic(self) -> None:
        t = EscalationThresholds(warn_s=100.0, sigusr1_s=50.0)
        errors = t.validate()
        assert len(errors) > 0

    def test_disabled_tier_skipped_in_validation(self) -> None:
        # Disable SIGUSR1 (0); WARN=60, SIGTERM=120 still monotonic
        t = EscalationThresholds(warn_s=60.0, sigusr1_s=0.0, sigterm_s=120.0, sigkill_s=150.0)
        errors = t.validate()
        assert errors == []


# ---------------------------------------------------------------------------
# HeartbeatEscalationLadder — basic escalation
# ---------------------------------------------------------------------------


class TestBasicEscalation:
    """Tests for the escalation ladder behavior."""

    def test_no_escalation_below_warn(self) -> None:
        ladder = HeartbeatEscalationLadder()
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=30.0, pid=1234)
        assert result is None

    def test_warn_at_threshold(self) -> None:
        ladder = HeartbeatEscalationLadder()
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0, pid=1234)
        assert result is not None
        assert result.tier == EscalationTier.WARN
        assert result.action_taken is True

    @patch("bernstein.core.platform_compat.kill_process_group")
    def test_sigusr1_at_threshold(self, mock_kill: MagicMock) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        # Skip through WARN
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=90.0)
        assert result is not None
        assert result.tier == EscalationTier.SIGUSR1
        assert result.action_taken is True
        mock_kill.assert_called_once_with(1234, signal.SIGUSR1)

    @patch("bernstein.core.platform_compat.kill_process_group")
    def test_sigterm_at_threshold(self, mock_kill: MagicMock) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=90.0)
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=120.0)
        assert result is not None
        assert result.tier == EscalationTier.SIGTERM
        mock_kill.assert_called_with(1234, signal.SIGTERM)

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL not available on Windows")
    @patch("bernstein.core.platform_compat.kill_process_group")
    def test_sigkill_at_threshold(self, mock_kill: MagicMock) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=90.0)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=120.0)
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=150.0)
        assert result is not None
        assert result.tier == EscalationTier.SIGKILL
        mock_kill.assert_called_with(1234, signal.SIGKILL)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Tests for escalation idempotency."""

    def test_no_duplicate_escalation(self) -> None:
        ladder = HeartbeatEscalationLadder()
        result1 = ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0, pid=1234)
        result2 = ladder.check_and_escalate("agent-1", heartbeat_age_s=65.0, pid=1234)
        assert result1 is not None
        assert result1.tier == EscalationTier.WARN
        assert result2 is None  # already warned

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL not available on Windows")
    def test_escalates_to_next_tier(self) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        r1 = ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        assert r1 is not None and r1.tier == EscalationTier.WARN
        # Same age, should not re-warn
        r2 = ladder.check_and_escalate("agent-1", heartbeat_age_s=70.0)
        assert r2 is None
        # Jump to sigkill age directly — should escalate through skipped tiers
        with patch("bernstein.core.platform_compat.kill_process_group"):
            r3 = ladder.check_and_escalate("agent-1", heartbeat_age_s=150.0)
        assert r3 is not None and r3.tier == EscalationTier.SIGKILL


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for resetting escalation state."""

    def test_reset_allows_re_escalation(self) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0, pid=1234)
        ladder.reset_agent("agent-1")
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0, pid=1234)
        assert result is not None
        assert result.tier == EscalationTier.WARN


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------


class TestCustomThresholds:
    """Tests for per-agent custom thresholds."""

    def test_per_agent_thresholds(self) -> None:
        default = EscalationThresholds(warn_s=60.0)
        custom = EscalationThresholds(warn_s=10.0, sigusr1_s=20.0, sigterm_s=30.0, sigkill_s=40.0)
        ladder = HeartbeatEscalationLadder(thresholds=default)
        ladder.register_agent("fast-agent", pid=1234, thresholds=custom)
        # fast-agent should warn at 10s
        result = ladder.check_and_escalate("fast-agent", heartbeat_age_s=10.0)
        assert result is not None
        assert result.tier == EscalationTier.WARN
        # Default agent should not warn at 10s
        result2 = ladder.check_and_escalate("default-agent", heartbeat_age_s=10.0, pid=5678)
        assert result2 is None


# ---------------------------------------------------------------------------
# Signal delivery errors
# ---------------------------------------------------------------------------


class TestSignalErrors:
    """Tests for signal delivery failure handling."""

    @patch("bernstein.core.platform_compat.kill_process_group", side_effect=ProcessLookupError)
    def test_process_already_exited(self, mock_kill: MagicMock) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=90.0)
        assert result is not None
        assert result.action_taken is False

    def test_no_pid_available(self) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=None)
        ladder.check_and_escalate("agent-1", heartbeat_age_s=60.0)
        result = ladder.check_and_escalate("agent-1", heartbeat_age_s=90.0)
        assert result is not None
        assert result.action_taken is False

    def test_auto_register_on_first_check(self) -> None:
        ladder = HeartbeatEscalationLadder()
        # Not previously registered
        result = ladder.check_and_escalate("new-agent", heartbeat_age_s=60.0, pid=9999)
        assert result is not None
        assert result.tier == EscalationTier.WARN
        state = ladder.get_state("new-agent")
        assert state is not None
        assert state.pid == 9999


# ---------------------------------------------------------------------------
# EscalationAction
# ---------------------------------------------------------------------------


class TestEscalationAction:
    """Tests for the action dataclass."""

    def test_action_fields(self) -> None:
        action = EscalationAction(
            session_id="agent-1",
            tier=EscalationTier.WARN,
            heartbeat_age_s=65.0,
            action_taken=True,
            detail="WARN escalation (executed) at 65s heartbeat age",
        )
        assert action.session_id == "agent-1"
        assert action.tier == EscalationTier.WARN
        assert action.action_taken is True


# ---------------------------------------------------------------------------
# Unregister
# ---------------------------------------------------------------------------


class TestUnregister:
    """Tests for unregistering agents."""

    def test_unregister_removes_state(self) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.register_agent("agent-1", pid=1234)
        ladder.unregister_agent("agent-1")
        assert ladder.get_state("agent-1") is None

    def test_unregister_nonexistent_is_safe(self) -> None:
        ladder = HeartbeatEscalationLadder()
        ladder.unregister_agent("nonexistent")  # should not raise
