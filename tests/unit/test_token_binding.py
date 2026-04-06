"""Tests for SEC-005: token binding and replay prevention."""

from __future__ import annotations

import time

from bernstein.core.token_binding import (
    TokenBindingValidator,
)

# ---------------------------------------------------------------------------
# Token expiration
# ---------------------------------------------------------------------------


class TestTokenExpiration:
    """Test token expiration checks."""

    def test_valid_unexpired_token(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"exp": time.time() + 3600, "iat": time.time()}
        result = validator.validate(claims)
        assert result.valid

    def test_expired_token_rejected(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"exp": time.time() - 100, "iat": time.time() - 200}
        result = validator.validate(claims)
        assert not result.valid
        assert "expired" in result.reason

    def test_invalid_exp_claim_rejected(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"exp": "not-a-number", "iat": time.time()}
        result = validator.validate(claims)
        assert not result.valid
        assert "invalid exp" in result.reason


# ---------------------------------------------------------------------------
# Maximum token age
# ---------------------------------------------------------------------------


class TestTokenMaxAge:
    """Test iat-based maximum token age enforcement."""

    def test_token_within_max_age(self) -> None:
        validator = TokenBindingValidator(
            max_age_s=3600,
            enforce_run_binding=False,
        )
        claims = {"iat": time.time() - 100}
        result = validator.validate(claims)
        assert result.valid

    def test_token_exceeds_max_age(self) -> None:
        validator = TokenBindingValidator(
            max_age_s=60,
            enforce_run_binding=False,
        )
        claims = {"iat": time.time() - 120}
        result = validator.validate(claims)
        assert not result.valid
        assert "maximum age" in result.reason

    def test_invalid_iat_rejected(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"iat": "bad-value"}
        result = validator.validate(claims)
        assert not result.valid
        assert "invalid iat" in result.reason


# ---------------------------------------------------------------------------
# Run ID binding
# ---------------------------------------------------------------------------


class TestRunIdBinding:
    """Test token-to-run_id binding."""

    def test_matching_run_id_accepted(self) -> None:
        validator = TokenBindingValidator(run_id="run-123")
        claims = {
            "run_id": "run-123",
            "iat": time.time(),
            "exp": time.time() + 3600,
        }
        result = validator.validate(claims)
        assert result.valid

    def test_mismatched_run_id_rejected(self) -> None:
        validator = TokenBindingValidator(run_id="run-123")
        claims = {
            "run_id": "run-456",
            "iat": time.time(),
            "exp": time.time() + 3600,
        }
        result = validator.validate(claims)
        assert not result.valid
        assert "run_id" in result.reason

    def test_missing_run_id_rejected(self) -> None:
        validator = TokenBindingValidator(run_id="run-123")
        claims = {
            "iat": time.time(),
            "exp": time.time() + 3600,
        }
        result = validator.validate(claims)
        assert not result.valid

    def test_empty_run_id_skips_binding(self) -> None:
        # When server has no run_id set, binding is not enforced
        validator = TokenBindingValidator(run_id="")
        claims = {
            "run_id": "anything",
            "iat": time.time(),
            "exp": time.time() + 3600,
        }
        result = validator.validate(claims)
        assert result.valid

    def test_binding_disabled_skips_check(self) -> None:
        validator = TokenBindingValidator(
            run_id="run-123",
            enforce_run_binding=False,
        )
        claims = {
            "run_id": "wrong-run",
            "iat": time.time(),
            "exp": time.time() + 3600,
        }
        result = validator.validate(claims)
        assert result.valid


# ---------------------------------------------------------------------------
# Per-token rate limiting
# ---------------------------------------------------------------------------


class TestTokenRateLimit:
    """Test per-token rate limiting."""

    def test_within_rate_limit(self) -> None:
        validator = TokenBindingValidator(
            rate_limit_per_minute=10,
            enforce_run_binding=False,
        )
        claims = {"jti": "token-1", "iat": time.time()}
        for _ in range(5):
            result = validator.validate(claims)
            assert result.valid

    def test_exceeds_rate_limit(self) -> None:
        validator = TokenBindingValidator(
            rate_limit_per_minute=3,
            enforce_run_binding=False,
        )
        claims = {"jti": "token-1", "iat": time.time()}
        results = []
        for _ in range(5):
            results.append(validator.validate(claims))
        # First 3 should succeed, 4th and 5th should fail
        assert results[0].valid
        assert results[1].valid
        assert results[2].valid
        assert not results[3].valid
        assert "Rate limit" in results[3].reason

    def test_different_tokens_independent(self) -> None:
        validator = TokenBindingValidator(
            rate_limit_per_minute=2,
            enforce_run_binding=False,
        )
        for i in range(3):
            claims = {"jti": f"token-{i}", "iat": time.time()}
            result = validator.validate(claims)
            assert result.valid

    def test_no_jti_skips_rate_limit(self) -> None:
        validator = TokenBindingValidator(
            rate_limit_per_minute=1,
            enforce_run_binding=False,
        )
        # Without jti, rate limiting is not applied
        claims = {"iat": time.time()}
        result = validator.validate(claims)
        assert result.valid
        result = validator.validate(claims)
        assert result.valid


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestTokenCleanup:
    """Test stale entry cleanup."""

    def test_cleanup_removes_stale(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"jti": "old-token", "iat": time.time()}
        validator.validate(claims)

        # Manually age the entry
        entry = validator._rate_entries.get("old-token")
        assert entry is not None
        entry.request_timestamps = [time.time() - 400]

        removed = validator.cleanup_stale_entries(max_age_s=300)
        assert removed == 1
        assert "old-token" not in validator._rate_entries

    def test_cleanup_keeps_recent(self) -> None:
        validator = TokenBindingValidator(enforce_run_binding=False)
        claims = {"jti": "fresh-token", "iat": time.time()}
        validator.validate(claims)

        removed = validator.cleanup_stale_entries(max_age_s=300)
        assert removed == 0
        assert "fresh-token" in validator._rate_entries
