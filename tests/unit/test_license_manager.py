"""Tests for ENT-014: Enterprise license management with feature gates."""

from __future__ import annotations

import time

import pytest

from bernstein.core.license_manager import (
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

    def test_no_signing_key_skips_validation(self) -> None:
        mgr = LicenseManager(signing_key="")
        result = mgr.load_license(_make_license_str())
        assert result.valid


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
