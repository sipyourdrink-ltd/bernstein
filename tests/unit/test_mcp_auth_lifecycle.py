"""Tests for MCP-006: MCP server auth lifecycle."""

from __future__ import annotations

import time

import pytest

from bernstein.core.mcp_auth_lifecycle import (
    AuthLifecycleManager,
    AuthSession,
    AuthState,
    RefreshResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> AuthSession:
    return AuthSession(
        server_name="github",
        access_token="tok_old",
        refresh_token="ref_123",
        token_endpoint="https://auth.example.com/token",
        client_id="client_abc",
        expires_at=time.time() + 3600,
    )


@pytest.fixture()
def expired_session() -> AuthSession:
    return AuthSession(
        server_name="github",
        access_token="tok_expired",
        refresh_token="ref_123",
        token_endpoint="https://auth.example.com/token",
        client_id="client_abc",
        expires_at=time.time() - 100,
    )


def _fake_refresher(session: AuthSession) -> tuple[str, float]:
    """Stub token refresher that always succeeds."""
    return ("tok_new", time.time() + 3600)


def _failing_refresher(session: AuthSession) -> tuple[str, float]:
    """Stub token refresher that always fails."""
    raise RuntimeError("Token endpoint unavailable")


@pytest.fixture()
def manager_with_refresher() -> AuthLifecycleManager:
    return AuthLifecycleManager(
        refresh_cooldown=0.0,  # No cooldown for tests
        token_refresher=_fake_refresher,
    )


@pytest.fixture()
def manager_no_refresher() -> AuthLifecycleManager:
    return AuthLifecycleManager(refresh_cooldown=0.0)


# ---------------------------------------------------------------------------
# Tests — AuthSession
# ---------------------------------------------------------------------------


class TestAuthSession:
    def test_not_expired_when_future(self, session: AuthSession) -> None:
        assert session.is_expired() is False

    def test_expired_when_past(self, expired_session: AuthSession) -> None:
        assert expired_session.is_expired() is True

    def test_seconds_until_expiry(self, session: AuthSession) -> None:
        remaining = session.seconds_until_expiry()
        assert remaining > 3500

    def test_seconds_until_expiry_past(self, expired_session: AuthSession) -> None:
        assert expired_session.seconds_until_expiry() == 0.0

    def test_to_dict_excludes_tokens(self, session: AuthSession) -> None:
        d = session.to_dict()
        assert "access_token" not in d
        assert "refresh_token" not in d
        assert d["server_name"] == "github"
        assert d["has_refresh_token"] is True

    def test_no_expiry_not_expired(self) -> None:
        s = AuthSession(server_name="test")
        assert s.is_expired() is False
        assert s.seconds_until_expiry() == float("inf")


# ---------------------------------------------------------------------------
# Tests — AuthLifecycleManager
# ---------------------------------------------------------------------------


class TestAuthLifecycleManager:
    def test_register_and_get(self, manager_with_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_with_refresher.register_session("github", session)
        assert manager_with_refresher.get_session("github") is session

    def test_get_unknown_returns_none(self, manager_with_refresher: AuthLifecycleManager) -> None:
        assert manager_with_refresher.get_session("unknown") is None

    def test_handle_non_auth_error(self, manager_with_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_with_refresher.register_session("github", session)
        result = manager_with_refresher.handle_auth_failure("github", 200)
        assert result.result == RefreshResult.SUCCESS

    def test_handle_401_no_session(self, manager_with_refresher: AuthLifecycleManager) -> None:
        result = manager_with_refresher.handle_auth_failure("unknown", 401)
        assert result.result == RefreshResult.NO_SESSION

    def test_handle_401_success(self, manager_with_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_with_refresher.register_session("github", session)
        result = manager_with_refresher.handle_auth_failure("github", 401)
        assert result.result == RefreshResult.SUCCESS
        assert result.new_token == "tok_new"
        assert session.state == AuthState.ACTIVE
        assert session.access_token == "tok_new"

    def test_handle_401_no_refresher(self, manager_no_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_no_refresher.register_session("github", session)
        result = manager_no_refresher.handle_auth_failure("github", 401)
        assert result.result == RefreshResult.REFRESH_FAILED

    def test_cooldown_prevents_rapid_refresh(self, session: AuthSession) -> None:
        mgr = AuthLifecycleManager(
            refresh_cooldown=9999.0,
            token_refresher=_fake_refresher,
        )
        mgr.register_session("github", session)
        # First refresh succeeds
        r1 = mgr.handle_auth_failure("github", 401)
        assert r1.result == RefreshResult.SUCCESS
        # Second refresh is blocked by cooldown
        r2 = mgr.handle_auth_failure("github", 401)
        assert r2.result == RefreshResult.COOLDOWN

    def test_max_retries(self, session: AuthSession) -> None:
        mgr = AuthLifecycleManager(
            refresh_cooldown=0.0,
            max_retries=2,
            token_refresher=_failing_refresher,
        )
        mgr.register_session("github", session)
        mgr.handle_auth_failure("github", 401)
        mgr.handle_auth_failure("github", 401)
        # Third attempt should hit max retries
        r3 = mgr.handle_auth_failure("github", 401)
        assert r3.result == RefreshResult.MAX_RETRIES
        assert session.state == AuthState.FAILED


# ---------------------------------------------------------------------------
# Tests — Proactive refresh
# ---------------------------------------------------------------------------


class TestProactiveRefresh:
    def test_check_expiring_soon(self, manager_with_refresher: AuthLifecycleManager) -> None:
        expiring = AuthSession(
            server_name="expiring",
            expires_at=time.time() + 60,  # 1 min left
        )
        manager_with_refresher.register_session("expiring", expiring)
        result = manager_with_refresher.check_expiring_soon(buffer=300.0)
        assert len(result) == 1
        assert result[0].server_name == "expiring"

    def test_proactive_refresh_triggers(self, manager_with_refresher: AuthLifecycleManager) -> None:
        expiring = AuthSession(
            server_name="expiring",
            refresh_token="ref",
            expires_at=time.time() + 60,
        )
        manager_with_refresher.register_session("expiring", expiring)
        outcomes = manager_with_refresher.proactive_refresh(buffer=300.0)
        assert len(outcomes) == 1
        assert outcomes[0].result == RefreshResult.SUCCESS


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict(self, manager_with_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_with_refresher.register_session("github", session)
        d = manager_with_refresher.to_dict()
        assert "github" in d
        assert d["github"]["server_name"] == "github"

    def test_list_sessions(self, manager_with_refresher: AuthLifecycleManager, session: AuthSession) -> None:
        manager_with_refresher.register_session("github", session)
        assert len(manager_with_refresher.list_sessions()) == 1
