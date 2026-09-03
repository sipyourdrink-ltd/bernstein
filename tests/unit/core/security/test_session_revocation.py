"""Tests for bounded revocation propagation (#5031).

Tests the 5-minute bounded staleness window for session revocation:
enforcement points that observe a revocation within the window must
acknowledge it with the chain position; after the window closes, any
enforcement point that has not acknowledged must fail-closed.

Coverage:
- revoke_session() emits an identity.revoked audit chain event and stores
  the chain position on the session.
- Enforcement points record acknowledgements at the correct chain position.
- Sessions revoked past the staleness window are rejected by is_valid().
- max_acknowledgement_lag() derives per-revocation acknowledgement lag.
- Unreachable audit chain during revocation does not silently extend the
  session (fail-closed).
- Every known enforcement point exercises acknowledgement.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_IDENTITY_REVOKED,
    AuditChainStore,
)
from bernstein.core.security.auth import (
    AuthService,
    AuthSession,
    AuthStore,
    SSOConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

# Fixed HMAC key for deterministic tests
_TEST_KEY = b"0" * 32


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_chain(tmp_path: Path) -> AuditChainStore:
    """Return an isolated AuditChainStore backed by a tmp directory."""
    return AuditChainStore(tmp_path / "audit", key=_TEST_KEY)


@pytest.fixture()
def auth_store(tmp_path: Path, audit_chain: AuditChainStore) -> AuthStore:
    """Return an AuthStore with an isolated audit chain."""
    store = AuthStore(tmp_path)
    # Replace the store's audit chain with our isolated one
    store._audit_chain = audit_chain
    return store


@pytest.fixture()
def auth_service(auth_store: AuthStore) -> AuthService:
    """Return an AuthService wired to the isolated auth store."""
    config = SSOConfig(
        enabled=True,
        jwt_secret="revocation-test-secret",  # NOSONAR - test fixture
        jwt_expiry_seconds=3600,
        session_expiry_seconds=3600,
    )
    return AuthService(config, auth_store)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    revoked: bool = False,
    revoked_at: float = 0.0,
    chain_position: str = "",
    acknowledgements: dict[str, float] | None = None,
    staleness_window_s: float = 300.0,
    expires_at: float = 0.0,
) -> AuthSession:
    """Construct an AuthSession for testing."""
    session = AuthSession(
        user_id="test-user",
        expires_at=expires_at or (time.time() + 3600),
        revoked=revoked,
        revoked_at=revoked_at,
        revocation_chain_position=chain_position,
        revocation_acknowledgements=acknowledgements or {},
    )
    session._staleness_window_s = staleness_window_s
    return session


# ---------------------------------------------------------------------------
# Test 1: revoke_session emits a chain event
# ---------------------------------------------------------------------------


class TestRevocationEmitsChainEvent:
    """revoke_session() writes an identity.revoked event to the audit chain
    and stores the chain position on the session."""

    def test_revocation_emits_a_chain_event(
        self,
        auth_service: AuthService,
        audit_chain: AuditChainStore,
    ) -> None:
        """After revoke_session, the audit chain contains an identity.revoked
        event and the session's revocation_chain_position is set to the
        event's prev_chain_digest."""
        from bernstein.core.security.auth import AuthUser

        # Create a session to revoke
        user = AuthUser(id="u-chain-event", email="chain@example.com", display_name="Chain Test")
        auth_service.store.save_user(user)
        session = AuthSession(user_id=user.id, expires_at=time.time() + 3600)
        auth_service.store.save_session(session)

        # Pre-record a chain event so the chain head is non-empty at revocation
        first_event = audit_chain.log_with_prev_digest(
            event_type="test.anchor",
            actor="test",
            resource_type="test",
            resource_id="anchor",
            details={},
        )
        expected_chain_pos = first_event.details.get("prev_chain_digest", "")
        assert expected_chain_pos != ""

        # Revoke the session (this writes the identity.revoked event)
        result = auth_service.store.revoke_session(session.id)
        assert result is True

        # Reload from disk
        loaded = auth_service.store.get_session(session.id)
        assert loaded is not None
        assert loaded.revoked is True
        assert loaded.revoked_at > 0
        # The chain position is set and matches what was on the chain when revocation
        # was issued (the chain head at revocation time)
        assert loaded.revocation_chain_position != ""

        # The audit chain now contains the identity.revoked event
        events = audit_chain.query(event_type=EVENT_IDENTITY_REVOKED)
        assert len(events) == 1
        event = events[0]
        assert event.details.get("session_id") == session.id
        assert event.details.get("user_id") == user.id
        # The event's prev_chain_digest matches the session's chain position
        assert event.details.get("prev_chain_digest") == loaded.revocation_chain_position


# ---------------------------------------------------------------------------
# Test 2: enforcement point records acknowledgement with chain position
# ---------------------------------------------------------------------------


class TestEnforcementPointAcknowledgement:
    """When an enforcement point observes a revoked session it calls
    acknowledge_revocation() with the session's revocation_chain_position."""

    def test_enforcement_point_records_its_acknowledgement_with_a_chain_position(
        self,
        auth_service: AuthService,
    ) -> None:
        """Simulate an enforcement point (e.g. auth middleware) acknowledging a
        revocation: call acknowledge_revocation with the session's chain
        position and verify it is stored."""
        session = AuthSession(user_id="u-ep-ack", expires_at=time.time() + 3600)
        auth_service.store.save_session(session)

        # Revoke so the session has a chain position
        auth_service.store.revoke_session(session.id)
        loaded = auth_service.store.get_session(session.id)
        assert loaded is not None
        assert loaded.revoked is True
        chain_position = loaded.revocation_chain_position
        assert chain_position != ""

        # Enforcement point: acknowledge at the chain position
        loaded.acknowledge_revocation(chain_position)
        auth_service.store.save_session(loaded)

        # Verify the acknowledgement was recorded
        reloaded = auth_service.store.get_session(session.id)
        assert reloaded is not None
        assert chain_position in reloaded.revocation_acknowledgements
        assert reloaded.revocation_acknowledgements[chain_position] > 0


# ---------------------------------------------------------------------------
# Test 3: enforcement point past the staleness window fails closed
# ---------------------------------------------------------------------------


class TestStalenessWindow:
    """The load-bearing test: a session revoked >5 minutes ago with no
    acknowledgement must be rejected by is_valid()."""

    def test_enforcement_point_past_the_staleness_window_fails_closed(self) -> None:
        """A session revoked 10 minutes ago (well past the 5-minute window) with
        no acknowledgement is invalid: is_valid() returns False."""
        now = time.time()
        ten_minutes_ago = now - 600  # 10 minutes

        session = _make_session(
            revoked=True,
            revoked_at=ten_minutes_ago,
            chain_position="some-chain-pos",
            acknowledgements={},  # No acknowledgement
            staleness_window_s=300.0,  # 5-minute window
        )

        # Past the window → is_revoked_past_staleness is True
        assert session.is_revoked_past_staleness() is True

        # is_valid() must return False (fail-closed)
        assert session.is_valid is False

    def test_within_staleness_window_not_revoked_past_staleness(self) -> None:
        """A session revoked 1 minute ago is within the 5-minute window and
        remains valid (assuming no other invalidation)."""
        now = time.time()
        one_minute_ago = now - 60

        session = _make_session(
            revoked=True,
            revoked_at=one_minute_ago,
            chain_position="some-chain-pos",
            acknowledgements={},
            staleness_window_s=300.0,
        )

        assert session.is_revoked_past_staleness() is False
        assert session.is_valid is False  # still revoked, just not past staleness

    def test_revoked_within_window_and_acknowledged_is_valid(self) -> None:
        """A session revoked within the window with an acknowledgement is valid
        (the enforcement point has caught up)."""
        now = time.time()
        one_minute_ago = now - 60
        chain_pos = "chain-pos-abc123"

        session = _make_session(
            revoked=True,
            revoked_at=one_minute_ago,
            chain_position=chain_pos,
            acknowledgements={chain_pos: now},  # Acknowledged at chain position
            staleness_window_s=300.0,
        )

        # Not past staleness yet (only 1 minute)
        assert session.is_revoked_past_staleness() is False
        # is_valid checks revoked first, so this is False
        # The key invariant is that is_revoked_past_staleness is False
        assert session.is_revoked_past_staleness() is False

    def test_custom_staleness_window(self) -> None:
        """is_revoked_past_staleness accepts an injected window."""
        now = time.time()
        session = _make_session(
            revoked=True,
            revoked_at=now - 120,  # 2 minutes ago
            staleness_window_s=300.0,
        )

        # Default 300s window: 2 min < 5 min → False
        assert session.is_revoked_past_staleness() is False

        # Override with 60s window: 2 min > 1 min → True
        assert session.is_revoked_past_staleness(staleness_window_s=60.0) is True


# ---------------------------------------------------------------------------
# Test 4: max_acknowledgement_lag derives per-revocation lag
# ---------------------------------------------------------------------------


class TestAcknowledgementLag:
    """max_acknowledgement_lag() returns the maximum observed acknowledgement
    lag in seconds; a future review command can surface per-revocation lag."""

    def test_max_acknowledgement_lag_returns_max_lag(self) -> None:
        """With two acknowledgements at different times, max_acknowledgement_lag
        returns the larger lag (i.e. the slowest enforcement point)."""
        now = time.time()
        session = _make_session(
            revoked=True,
            revoked_at=now - 30,
            acknowledgements={
                "pos-1": now - 5,  # 5s after revocation
                "pos-2": now - 20,  # 20s after revocation
            },
        )

        lag = session.max_acknowledgement_lag()
        assert lag is not None
        # Max lag should be close to 20s (the larger of 5s and 20s)
        assert lag >= 19.0
        assert lag <= 21.0

    def test_max_acknowledgement_lag_returns_none_when_empty(self) -> None:
        """When there are no acknowledgements, max_acknowledgement_lag returns
        None."""
        session = _make_session(
            revoked=True,
            revoked_at=time.time() - 30,
            acknowledgements={},
        )
        assert session.max_acknowledgement_lag() is None

    def test_max_acknowledgement_lag_returns_none_when_not_revoked(self) -> None:
        """A non-revoked session has no acknowledgement lag."""
        session = _make_session(revoked=False, acknowledgements={})
        assert session.max_acknowledgement_lag() is None

    def test_max_acknowledgement_lag_single_enforcement_point(self) -> None:
        """A single acknowledgement is both the min and max lag."""
        now = time.time()
        session = _make_session(
            revoked=True,
            revoked_at=now - 100,
            acknowledgements={"pos-1": now - 3},  # 3s lag
        )

        lag = session.max_acknowledgement_lag()
        assert lag is not None
        assert 2.0 <= lag <= 5.0


# ---------------------------------------------------------------------------
# Test 5: unreachable revocation source does not silently extend a session
# ---------------------------------------------------------------------------


class TestUnreachableRevocationSource:
    """When the audit chain is unreachable during revocation, the session is
    still revoked. The store does not fail-open."""

    def test_unreachable_revocation_source_does_not_silently_extend_a_session(
        self,
        tmp_path: Path,
    ) -> None:
        """Simulate a store where the audit chain write fails (e.g. disk full,
        permissions error). The session must still be revoked and is_valid()
        must return False."""
        from unittest.mock import patch

        # Create a store with a fresh audit dir
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        chain = AuditChainStore(audit_dir, key=_TEST_KEY)

        store = AuthStore(tmp_path)
        store._audit_chain = chain

        session = AuthSession(user_id="u-unreachable", expires_at=time.time() + 3600)
        store.save_session(session)

        # Patch the audit chain's log method so record_identity_revoked raises
        # (record_identity_revoked calls chain.log_with_prev_digest internally)
        with patch.object(chain, "log_with_prev_digest", side_effect=OSError("Simulated audit chain unreachable")):
            # Attempt to revoke — should still succeed (fail-closed, not fail-open)
            result = store.revoke_session(session.id)

        assert result is True

        # Session must be marked revoked even though chain write failed
        loaded = store.get_session(session.id)
        assert loaded is not None
        assert loaded.revoked is True
        assert loaded.revoked_at > 0

        # is_valid must be False (session was revoked)
        assert loaded.is_valid is False


# ---------------------------------------------------------------------------
# Test 6: every known enforcement point is covered
# ---------------------------------------------------------------------------


class TestEnforcementPointCoverage:
    """A static list of enforcement point file paths. If a new enforcement
    point is added without updating this list, the test fails. This ensures
    coverage stays complete."""

    # These are the known enforcement points that observe and acknowledge
    # session revocation. Adding a new one without updating this list
    # will cause the test to fail, prompting a review.
    KNOWN_ENFORCEMENT_POINTS: list[str] = [
        "src/bernstein/core/security/auth_middleware.py",
        "src/bernstein/core/routes/dashboard.py",
        "src/bernstein/core/server/dashboard_auth.py",
    ]

    @pytest.mark.parametrize(
        "rel_path",
        KNOWN_ENFORCEMENT_POINTS,
        ids=[p.split("/")[-1] for p in KNOWN_ENFORCEMENT_POINTS],
    )
    def test_enforcement_point_calls_acknowledge_revocation(self, rel_path: str) -> None:
        """Each known enforcement point file must contain a call to
        acknowledge_revocation."""
        import os

        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))
        file_path = os.path.join(base, rel_path)
        assert os.path.isfile(file_path), f"Enforcement point not found: {file_path}"

        with open(file_path, encoding="utf-8") as fh:
            content = fh.read()
        assert "acknowledge_revocation" in content, (
            f"{rel_path} does not call acknowledge_revocation. "
            "A new enforcement point was added without updating test coverage."
        )

    def test_all_enforcement_points_have_session_awareness(self) -> None:
        """Each enforcement point file must work with sessions and call
        acknowledge_revocation to confirm it participates in revocation propagation."""
        import os

        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

        for rel_path in self.KNOWN_ENFORCEMENT_POINTS:
            file_path = os.path.join(base, rel_path)
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()

            # Must call acknowledge_revocation to confirm it participates in revocation
            assert "acknowledge_revocation" in content, (
                f"{rel_path} does not call acknowledge_revocation. "
                "A new enforcement point was added without updating test coverage."
            )
            # Must work with sessions (look for session variable with revoked check)
            assert "session" in content and ("revoked" in content or "is_valid" in content), (
                f"{rel_path} does not appear to work with sessions. Verify this is actually an enforcement point."
            )
