"""Unit tests for JWT TokenRefreshScheduler — proactive lifecycle management."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from bernstein.core.jwt_tokens import (
    JWTManager,
    TokenRefreshFatalError,
    TokenRefreshScheduler,
)


@pytest.fixture()
def manager() -> JWTManager:
    return JWTManager(secret="test-secret", expiry_hours=1)


def _make_scheduler(
    manager: JWTManager,
    *,
    refresh_buffer: float = 300.0,
    max_failures: int = 3,
) -> TokenRefreshScheduler:
    return TokenRefreshScheduler(
        _manager=manager,
        _session_id="sess-abc",
        _user_id="user-1",
        _scopes=["read"],
        _refresh_buffer=refresh_buffer,
        _max_failures=max_failures,
    )


class TestTokenRefreshSchedulerInit:
    def test_initial_token_is_valid(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        payload = manager.verify_token(sched.token)
        assert payload is not None
        assert payload.session_id == "sess-abc"

    def test_initial_generation_is_zero(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        assert sched.generation == 0

    def test_initial_fail_count_is_zero(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        assert sched._fail_count == 0


class TestNeedsRefresh:
    def test_fresh_token_does_not_need_refresh(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, refresh_buffer=300.0)
        # Token expires in 3600 s; 300 s buffer → not yet in window
        assert not sched.needs_refresh()

    def test_near_expiry_token_needs_refresh(self, manager: JWTManager) -> None:
        # Use a buffer slightly shorter than the full expiry so needs_refresh triggers
        # Simulate expiry in 100 s by setting expires_at to now + 100
        sched = _make_scheduler(manager, refresh_buffer=300.0)
        # Manually move expires_at to now + 200 (inside the 300 s buffer)
        with sched._lock:
            sched._payload = sched._payload.__class__(
                session_id=sched._payload.session_id,
                user_id=sched._payload.user_id,
                issued_at=sched._payload.issued_at,
                expires_at=time.time() + 200,
                scopes=sched._payload.scopes,
            )
        assert sched.needs_refresh()

    def test_already_expired_needs_refresh(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, refresh_buffer=300.0)
        with sched._lock:
            sched._payload = sched._payload.__class__(
                session_id=sched._payload.session_id,
                user_id=sched._payload.user_id,
                issued_at=sched._payload.issued_at,
                expires_at=time.time() - 1,
                scopes=sched._payload.scopes,
            )
        assert sched.needs_refresh()


class TestRefreshSuccess:
    def test_refresh_increments_generation(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        sched.refresh()
        assert sched.generation == 1

    def test_refresh_resets_fail_count(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        # Manually set fail count to 1 (below max)
        sched._fail_count = 1
        sched.refresh()
        assert sched._fail_count == 0

    def test_refresh_produces_valid_token(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        sched.refresh()
        payload = manager.verify_token(sched.token)
        assert payload is not None

    def test_refresh_with_matching_caller_generation(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        result = sched.refresh(caller_generation=0)
        assert result is True
        assert sched.generation == 1

    def test_stale_generation_skipped(self, manager: JWTManager) -> None:
        """Refresh with outdated caller_generation is a no-op."""
        sched = _make_scheduler(manager)
        # Do a real refresh so generation becomes 1
        sched.refresh()
        old_token = sched.token

        # Now call with the OLD generation — should be skipped
        result = sched.refresh(caller_generation=0)
        assert result is True
        # Token should not have changed (generation still 1, not 2)
        assert sched.token == old_token
        assert sched.generation == 1


class TestRefreshFailures:
    def test_single_failure_returns_false(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=3)
        with patch.object(sched, "_issue", side_effect=RuntimeError("network")):
            result = sched.refresh()
        assert result is False
        assert sched._fail_count == 1
        assert not sched.is_fatal()

    def test_two_failures_not_yet_fatal(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=3)
        err = RuntimeError("transient")
        with patch.object(sched, "_issue", side_effect=err):
            sched.refresh()
            sched.refresh()
        assert sched._fail_count == 2
        assert not sched.is_fatal()

    def test_third_failure_raises_fatal(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=3)
        err = RuntimeError("permanent")
        with patch.object(sched, "_issue", side_effect=err):
            sched.refresh()
            sched.refresh()
            with pytest.raises(TokenRefreshFatalError):
                sched.refresh()
        assert sched.is_fatal()

    def test_fatal_state_raises_on_subsequent_calls(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=1)
        err = RuntimeError("dead")
        with patch.object(sched, "_issue", side_effect=err):
            with pytest.raises(TokenRefreshFatalError):
                sched.refresh()
        # Second call also raises without touching _issue again
        with pytest.raises(TokenRefreshFatalError):
            sched.refresh()

    def test_success_after_partial_failures_resets_count(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=3)
        err = RuntimeError("blip")
        with patch.object(sched, "_issue", side_effect=err):
            sched.refresh()  # fail 1
        # Now a real refresh succeeds
        sched.refresh()
        assert sched._fail_count == 0
        assert sched.generation == 1


class TestIsFatal:
    def test_not_fatal_initially(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager)
        assert not sched.is_fatal()

    def test_fatal_when_fail_count_reaches_max(self, manager: JWTManager) -> None:
        sched = _make_scheduler(manager, max_failures=2)
        sched._fail_count = 2
        assert sched.is_fatal()
