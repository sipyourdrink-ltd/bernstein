"""Tests for ENT-014: Enterprise license management with feature gates."""

from __future__ import annotations

import logging
import time

import pytest
from bernstein.core.license_manager import (
    LICENSE_DISABLED_ENV,
    Feature,
    LicenseManager,
    LicenseTier,
    decode_license,
    encode_license,
    validate_license_signature,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_KEY = "test-signing-key-for-unit-tests"


def _make_license_str(
    org_id: str = "acme-corp",
    tier: LicenseTier = LicenseTier.ENTERPRISE,
    features: frozenset[str] | None = None,
    max_seats: int = 50,
    max_nodes: int = 10,
    expires_at: float | None = None,
) -> str:
    if features is None:
        features = frozenset({Feature.SSO_OIDC, Feature.AUDIT_EXPORT})
    if expires_at is None:
        expires_at = time.time() + 86400 * 365  # 1 year
    return encode_license(
        org_id=org_id,
        tier=tier,
        features=features,
        max_seats=max_seats,
        max_nodes=max_nodes,
        expires_at=expires_at,
        signing_key=SIGNING_KEY,
    )


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------


class TestEncodeDecode:
    def test_roundtrip(self) -> None:
        encoded = _make_license_str()
        decoded = decode_license(encoded)
        assert decoded["org_id"] == "acme-corp"
        assert decoded["tier"] == "enterprise"

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(ValueError, match="Failed to decode"):
            decode_license("not-valid-base64!!!")


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------


class TestSignatureValidation:
    def test_valid_signature(self) -> None:
        encoded = _make_license_str()
        payload = decode_license(encoded)
        assert validate_license_signature(payload, SIGNING_KEY)

    def test_wrong_key_fails(self) -> None:
        encoded = _make_license_str()
        payload = decode_license(encoded)
        assert not validate_license_signature(payload, "wrong-key")

    def test_tampered_payload_fails(self) -> None:
        encoded = _make_license_str()
        payload = decode_license(encoded)
        payload["org_id"] = "tampered"
        assert not validate_license_signature(payload, SIGNING_KEY)


# ---------------------------------------------------------------------------
# LicenseManager.load_license
# ---------------------------------------------------------------------------


class TestLoadLicense:
    def test_valid_license(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        result = mgr.load_license(_make_license_str())
        assert result.valid
        assert result.license is not None
        assert result.license.org_id == "acme-corp"
        assert result.license.tier == LicenseTier.ENTERPRISE

    def test_expired_license(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        encoded = _make_license_str(expires_at=time.time() - 1)
        result = mgr.load_license(encoded)
        assert not result.valid
        assert "expired" in result.error.lower()

    def test_invalid_format(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        result = mgr.load_license("garbage")
        assert not result.valid

    def test_invalid_signature(self) -> None:
        mgr = LicenseManager(signing_key="different-key")
        result = mgr.load_license(_make_license_str())
        assert not result.valid
        assert "signature" in result.error.lower()

    def test_expiring_soon_warning(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        encoded = _make_license_str(expires_at=time.time() + 86400 * 10)
        result = mgr.load_license(encoded)
        assert result.valid
        assert len(result.warnings) > 0
        assert any("expires" in w.lower() for w in result.warnings)

    def test_empty_signing_key_rejects_license(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audit-050: empty signing key must fail closed, not skip check."""
        monkeypatch.delenv(LICENSE_DISABLED_ENV, raising=False)
        mgr = LicenseManager(signing_key="")
        result = mgr.load_license(_make_license_str())
        assert not result.valid
        assert "signing key" in result.error.lower()

    def test_missing_signing_key_rejects_forged_license(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audit-050: a license forged with empty-string HMAC key is rejected.

        Regression guard: the reproducer in the ticket was
        ``LicenseManager(signing_key='').load_license(forged_unlimited)``.
        """
        monkeypatch.delenv(LICENSE_DISABLED_ENV, raising=False)
        # Forge a license signed with the empty key (what an attacker would do
        # to exploit the old bypass path).
        forged = encode_license(
            org_id="attacker",
            tier=LicenseTier.UNLIMITED,
            features=frozenset({Feature.SSO_OIDC}),
            max_seats=0,
            max_nodes=0,
            expires_at=time.time() + 86400 * 365 * 100,
            signing_key="x",  # encode_license now refuses "", so use any key
        )
        mgr = LicenseManager()  # no signing key configured
        result = mgr.load_license(forged)
        assert not result.valid
        assert mgr.current_license is None

    def test_valid_key_valid_signature_accepts(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        result = mgr.load_license(_make_license_str())
        assert result.valid
        assert result.license is not None
        assert result.license.tier == LicenseTier.ENTERPRISE

    def test_valid_key_tampered_payload_rejected(self) -> None:
        """audit-050: tampered payloads must be rejected even with valid key."""
        encoded = _make_license_str()
        payload = decode_license(encoded)
        # Flip the tier to UNLIMITED without re-signing.
        payload["tier"] = "unlimited"
        import base64
        import json

        tampered = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True).encode(),
        ).decode()
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        result = mgr.load_license(tampered)
        assert not result.valid
        assert "signature" in result.error.lower()

    def test_disabled_env_skips_validation_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """audit-050: explicit opt-in disables signature check + logs warning."""
        monkeypatch.setenv(LICENSE_DISABLED_ENV, "1")
        # Forge a license with a different key; with validation disabled it
        # should still load.
        forged = encode_license(
            org_id="dev",
            tier=LicenseTier.ENTERPRISE,
            features=frozenset(),
            max_seats=10,
            max_nodes=1,
            expires_at=time.time() + 86400,
            signing_key="not-the-real-key",
        )
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        with caplog.at_level(logging.WARNING):
            result = mgr.load_license(forged)
        assert result.valid
        assert any(LICENSE_DISABLED_ENV in rec.getMessage() for rec in caplog.records)
        assert any(LICENSE_DISABLED_ENV in w for w in result.warnings)

    def test_disabled_env_wrong_value_still_enforces(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audit-050: only "1" opts out — "true", "0", "" stay enforcing."""
        for bad_val in ("0", "true", "yes", "", "TRUE"):
            monkeypatch.setenv(LICENSE_DISABLED_ENV, bad_val)
            mgr = LicenseManager(signing_key="")
            result = mgr.load_license(_make_license_str())
            assert not result.valid, f"Expected rejection for env value {bad_val!r}"


class TestEncodeLicenseValidation:
    """audit-050: encode_license refuses empty signing keys."""

    def test_empty_signing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            encode_license(
                org_id="acme",
                tier=LicenseTier.COMMUNITY,
                features=frozenset(),
                max_seats=1,
                max_nodes=1,
                expires_at=time.time() + 3600,
                signing_key="",
            )


class TestValidateSignatureFailClosed:
    """audit-050: validate_license_signature fails closed on empty key."""

    def test_empty_key_returns_false(self) -> None:
        encoded = _make_license_str()
        payload = decode_license(encoded)
        assert validate_license_signature(payload, "") is False

    def test_empty_signature_returns_false(self) -> None:
        encoded = _make_license_str()
        payload = decode_license(encoded)
        payload["signature"] = ""
        assert validate_license_signature(payload, SIGNING_KEY) is False


# ---------------------------------------------------------------------------
# Feature checks
# ---------------------------------------------------------------------------


class TestFeatureChecks:
    def test_explicit_feature_allowed(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(
            _make_license_str(
                features=frozenset({Feature.SSO_OIDC}),
            )
        )
        result = mgr.check_feature(Feature.SSO_OIDC)
        assert result.allowed

    def test_tier_feature_allowed(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(
            _make_license_str(
                tier=LicenseTier.ENTERPRISE,
                features=frozenset(),
            )
        )
        # CLUSTER_MODE is a tier-default feature for enterprise
        result = mgr.check_feature(Feature.CLUSTER_MODE)
        assert result.allowed

    def test_feature_denied(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(
            _make_license_str(
                tier=LicenseTier.COMMUNITY,
                features=frozenset(),
            )
        )
        result = mgr.check_feature(Feature.SSO_OIDC)
        assert not result.allowed

    def test_no_license_denies_all(self) -> None:
        mgr = LicenseManager()
        result = mgr.check_feature(Feature.SSO_OIDC)
        assert not result.allowed
        assert "no license" in result.reason.lower()


# ---------------------------------------------------------------------------
# Seat / node limits
# ---------------------------------------------------------------------------


class TestSeatNodeLimits:
    def test_seats_within_limit(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(_make_license_str(max_seats=10))
        assert mgr.check_seats(5)
        assert mgr.check_seats(10)

    def test_seats_over_limit(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(_make_license_str(max_seats=10))
        assert not mgr.check_seats(11)

    def test_unlimited_seats(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(_make_license_str(max_seats=0))
        assert mgr.check_seats(9999)

    def test_nodes_within_limit(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(_make_license_str(max_nodes=5))
        assert mgr.check_nodes(5)

    def test_nodes_over_limit(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(_make_license_str(max_nodes=5))
        assert not mgr.check_nodes(6)

    def test_no_license_allows_all(self) -> None:
        mgr = LicenseManager()
        assert mgr.check_seats(999)
        assert mgr.check_nodes(999)


# ---------------------------------------------------------------------------
# get_allowed_features
# ---------------------------------------------------------------------------


class TestGetAllowedFeatures:
    def test_combines_tier_and_explicit(self) -> None:
        mgr = LicenseManager(signing_key=SIGNING_KEY)
        mgr.load_license(
            _make_license_str(
                tier=LicenseTier.TEAM,
                features=frozenset({Feature.AUDIT_EXPORT}),
            )
        )
        allowed = mgr.get_allowed_features()
        # Should include tier defaults (multi_tenant, ip_allowlist) + explicit
        assert Feature.MULTI_TENANT in allowed
        assert Feature.AUDIT_EXPORT in allowed

    def test_no_license_empty(self) -> None:
        mgr = LicenseManager()
        assert len(mgr.get_allowed_features()) == 0
