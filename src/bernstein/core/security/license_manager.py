"""ENT-014: Enterprise license management with feature gates.

Manages license keys, validates entitlements, and enforces feature gates.
Licenses are signed JSON payloads with expiration dates, seat counts, and
feature flags.

License format: base64-encoded JSON with HMAC-SHA256 signature.
The license key contains: org_id, tier, features, seats, expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

logger = logging.getLogger(__name__)

# Environment variable that must be set to "1" to explicitly disable license
# signature validation (e.g. in local dev). Any other value — including unset —
# means validation is enforced. This prevents silent bypass when a signing key
# is accidentally omitted from production config.
LICENSE_DISABLED_ENV = "BERNSTEIN_LICENSE_DISABLED"

# ---------------------------------------------------------------------------
# License tiers and features
# ---------------------------------------------------------------------------


class LicenseTier(StrEnum):
    """Available license tiers."""

    COMMUNITY = "community"
    TEAM = "team"
    ENTERPRISE = "enterprise"
    UNLIMITED = "unlimited"


class Feature(StrEnum):
    """Gated enterprise features."""

    MULTI_TENANT = "multi_tenant"
    CLUSTER_MODE = "cluster_mode"
    SSO_OIDC = "sso_oidc"
    AUDIT_EXPORT = "audit_export"
    DATA_RESIDENCY = "data_residency"
    WAL_REPLICATION = "wal_replication"
    TASK_STEALING = "task_stealing"
    AUTOSCALING = "autoscaling"
    IP_ALLOWLIST = "ip_allowlist"
    PRIORITY_SUPPORT = "priority_support"
    CUSTOM_ROLES = "custom_roles"


# Default feature sets per tier
_TIER_FEATURES: dict[LicenseTier, frozenset[Feature]] = {
    LicenseTier.COMMUNITY: frozenset(),
    LicenseTier.TEAM: frozenset(
        {
            Feature.MULTI_TENANT,
            Feature.IP_ALLOWLIST,
        }
    ),
    LicenseTier.ENTERPRISE: frozenset(
        {
            Feature.MULTI_TENANT,
            Feature.CLUSTER_MODE,
            Feature.SSO_OIDC,
            Feature.AUDIT_EXPORT,
            Feature.DATA_RESIDENCY,
            Feature.WAL_REPLICATION,
            Feature.TASK_STEALING,
            Feature.AUTOSCALING,
            Feature.IP_ALLOWLIST,
            Feature.CUSTOM_ROLES,
        }
    ),
    LicenseTier.UNLIMITED: frozenset(Feature),
}


# ---------------------------------------------------------------------------
# License dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class License:
    """Validated license information.

    Attributes:
        org_id: Organization identifier.
        tier: License tier.
        features: Explicitly granted features.
        max_seats: Maximum number of seats (0 = unlimited).
        max_nodes: Maximum cluster nodes (0 = unlimited).
        issued_at: Timestamp when the license was issued.
        expires_at: Timestamp when the license expires.
        signature: HMAC signature for validation.
        raw: Original encoded license string.
    """

    org_id: str = ""
    tier: LicenseTier = LicenseTier.COMMUNITY
    features: frozenset[str] = field(default_factory=frozenset[str])
    max_seats: int = 0
    max_nodes: int = 0
    issued_at: float = 0.0
    expires_at: float = 0.0
    signature: str = ""
    raw: str = ""


@dataclass(frozen=True)
class LicenseValidationResult:
    """Result of license validation.

    Attributes:
        valid: Whether the license is valid.
        license: Parsed license if valid.
        error: Error message if invalid.
        warnings: Non-fatal warnings.
    """

    valid: bool = False
    license: License | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class FeatureCheckResult:
    """Result of a feature gate check.

    Attributes:
        allowed: Whether the feature is allowed.
        feature: Feature that was checked.
        reason: Explanation of the decision.
    """

    allowed: bool = False
    feature: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# License encoding/decoding
# ---------------------------------------------------------------------------


def encode_license(
    org_id: str,
    tier: LicenseTier,
    features: frozenset[str],
    max_seats: int,
    max_nodes: int,
    expires_at: float,
    signing_key: str,
) -> str:
    """Encode and sign a license.

    Args:
        org_id: Organization identifier.
        tier: License tier.
        features: Feature flags to include.
        max_seats: Maximum seats.
        max_nodes: Maximum cluster nodes.
        expires_at: Expiration timestamp.
        signing_key: HMAC signing key. Must be non-empty.

    Returns:
        Base64-encoded signed license string.

    Raises:
        ValueError: If ``signing_key`` is empty.
    """
    if not signing_key:
        msg = "signing_key must be a non-empty string"
        raise ValueError(msg)
    payload: dict[str, Any] = {
        "org_id": org_id,
        "tier": tier,
        "features": sorted(features),
        "max_seats": max_seats,
        "max_nodes": max_nodes,
        "issued_at": time.time(),
        "expires_at": expires_at,
    }
    payload_json = json.dumps(payload, sort_keys=True)
    signature = hmac.new(
        signing_key.encode(),
        payload_json.encode(),
        hashlib.sha256,
    ).hexdigest()
    payload["signature"] = signature
    full_json = json.dumps(payload, sort_keys=True)
    return base64.urlsafe_b64encode(full_json.encode()).decode()


def decode_license(encoded: str) -> dict[str, Any]:
    """Decode a license string without validation.

    Args:
        encoded: Base64-encoded license string.

    Returns:
        Decoded license payload.

    Raises:
        ValueError: If decoding fails.
    """
    try:
        raw = base64.urlsafe_b64decode(encoded.encode())
        return dict(json.loads(raw))
    except Exception as exc:
        msg = f"Failed to decode license: {exc}"
        raise ValueError(msg) from exc


def validate_license_signature(
    payload: dict[str, Any],
    signing_key: str,
) -> bool:
    """Validate the HMAC signature on a license payload.

    This function fails closed: an empty or missing ``signing_key`` always
    returns ``False``, regardless of the payload contents. Callers that
    intentionally want to skip validation (e.g. in dev mode) must do so
    explicitly via the :data:`LICENSE_DISABLED_ENV` opt-in.

    Args:
        payload: Decoded license payload (including signature).
        signing_key: Expected signing key. Must be non-empty.

    Returns:
        True if the signature is valid; False if ``signing_key`` is empty
        or the signature does not match.
    """
    if not signing_key:
        return False
    signature = payload.get("signature", "")
    if not isinstance(signature, str) or not signature:
        return False
    verify_payload = {k: v for k, v in payload.items() if k != "signature"}
    payload_json = json.dumps(verify_payload, sort_keys=True)
    expected = hmac.new(
        signing_key.encode(),
        payload_json.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# License manager
# ---------------------------------------------------------------------------


def _license_validation_disabled() -> bool:
    """Return True if signature validation has been explicitly disabled.

    Only the exact value ``"1"`` in :data:`LICENSE_DISABLED_ENV` counts as an
    opt-in. All other values (including empty string, ``"0"``, ``"true"``,
    etc.) leave validation enabled. Requiring an exact value prevents silent
    misconfiguration from disabling signature checks in production.
    """
    return os.environ.get(LICENSE_DISABLED_ENV, "") == "1"


class LicenseManager:
    """Manages enterprise licenses and feature gates.

    Signature validation is fail-closed: if ``signing_key`` is empty and
    :envvar:`BERNSTEIN_LICENSE_DISABLED` is not set to ``"1"``, every
    ``load_license`` call returns ``valid=False``. This prevents the
    historical bug where an unconfigured signing key silently bypassed
    signature verification and accepted forged licenses.

    Args:
        signing_key: Key used to validate license signatures. Must be a
            non-empty string unless signature validation has been explicitly
            disabled via :envvar:`BERNSTEIN_LICENSE_DISABLED` = ``"1"``.
    """

    def __init__(self, signing_key: str = "") -> None:
        self._signing_key = signing_key
        self._license: License | None = None

    @property
    def current_license(self) -> License | None:
        """Return the currently loaded license."""
        return self._license

    def load_license(self, encoded: str) -> LicenseValidationResult:
        """Load and validate a license from an encoded string.

        Validation rules:
          * If :envvar:`BERNSTEIN_LICENSE_DISABLED` is ``"1"``, signature
            validation is skipped and a warning is logged.
          * Otherwise, the signing key must be non-empty and the HMAC
            signature on the payload must match. An empty or unset signing
            key causes the license to be rejected.

        Args:
            encoded: Base64-encoded license string.

        Returns:
            LicenseValidationResult with validation outcome.
        """
        try:
            payload = decode_license(encoded)
        except ValueError as exc:
            return LicenseValidationResult(
                valid=False,
                error=str(exc),
            )

        warnings: list[str] = []

        # Decide signature policy. Fail-closed: missing key + no explicit
        # opt-in means we reject.
        if _license_validation_disabled():
            logger.warning(
                "License signature validation disabled via %s=1; accepting license without signature check",
                LICENSE_DISABLED_ENV,
            )
            warnings.append(
                f"License signature validation disabled via {LICENSE_DISABLED_ENV}=1",
            )
        elif not self._signing_key:
            logger.error(
                "License signing key is empty; refusing to validate. "
                "Configure a signing key or set %s=1 to explicitly disable "
                "signature validation.",
                LICENSE_DISABLED_ENV,
            )
            return LicenseValidationResult(
                valid=False,
                error=(
                    "License signing key not configured; refusing to "
                    "validate. Set a signing key or opt out explicitly "
                    f"with {LICENSE_DISABLED_ENV}=1."
                ),
            )
        elif not validate_license_signature(payload, self._signing_key):
            return LicenseValidationResult(
                valid=False,
                error="Invalid license signature",
            )

        # Parse fields
        now = time.time()
        expires_at = float(payload.get("expires_at", 0))
        if expires_at < now:
            return LicenseValidationResult(
                valid=False,
                error="License has expired",
            )

        # Warn if expiring within 30 days
        days_remaining = (expires_at - now) / 86400
        if days_remaining < 30:
            warnings.append(
                f"License expires in {days_remaining:.0f} days",
            )

        tier_str = str(payload.get("tier", "community"))
        try:
            tier = LicenseTier(tier_str)
        except ValueError:
            tier = LicenseTier.COMMUNITY
            warnings.append(f"Unknown tier '{tier_str}', defaulting to community")

        features_raw: Any = payload.get("features", [])
        features: frozenset[str]
        if isinstance(features_raw, list):
            features = frozenset(str(f) for f in cast("list[Any]", features_raw))
        else:
            features = frozenset[str]()

        lic = License(
            org_id=str(payload.get("org_id", "")),
            tier=tier,
            features=features,
            max_seats=int(payload.get("max_seats", 0)),
            max_nodes=int(payload.get("max_nodes", 0)),
            issued_at=float(payload.get("issued_at", 0)),
            expires_at=expires_at,
            signature=str(payload.get("signature", "")),
            raw=encoded,
        )

        self._license = lic
        logger.info(
            "License loaded: org=%s tier=%s expires=%.0f",
            lic.org_id,
            lic.tier,
            lic.expires_at,
        )
        return LicenseValidationResult(
            valid=True,
            license=lic,
            warnings=warnings,
        )

    def check_feature(self, feature: str) -> FeatureCheckResult:
        """Check whether a feature is allowed by the current license.

        Args:
            feature: Feature identifier to check.

        Returns:
            FeatureCheckResult with the decision.
        """
        if self._license is None:
            return FeatureCheckResult(
                allowed=False,
                feature=feature,
                reason="No license loaded",
            )

        # Check expiry
        if self._license.expires_at < time.time():
            return FeatureCheckResult(
                allowed=False,
                feature=feature,
                reason="License has expired",
            )

        # Check explicit features
        if feature in self._license.features:
            return FeatureCheckResult(
                allowed=True,
                feature=feature,
                reason=f"Explicitly granted by {self._license.tier} license",
            )

        # Check tier defaults
        try:
            tier = LicenseTier(self._license.tier)
        except ValueError:
            tier = LicenseTier.COMMUNITY
        tier_features = _TIER_FEATURES.get(tier, frozenset())
        if feature in {f.value for f in tier_features}:
            return FeatureCheckResult(
                allowed=True,
                feature=feature,
                reason=f"Included in {tier} tier",
            )

        return FeatureCheckResult(
            allowed=False,
            feature=feature,
            reason=f"Not included in {self._license.tier} tier",
        )

    def check_seats(self, current_seats: int) -> bool:
        """Check if the current seat count is within the license limit.

        Args:
            current_seats: Current number of active seats.

        Returns:
            True if within limit (0 = unlimited).
        """
        if self._license is None:
            return True
        if self._license.max_seats == 0:
            return True
        return current_seats <= self._license.max_seats

    def check_nodes(self, current_nodes: int) -> bool:
        """Check if the current node count is within the license limit.

        Args:
            current_nodes: Current number of cluster nodes.

        Returns:
            True if within limit (0 = unlimited).
        """
        if self._license is None:
            return True
        if self._license.max_nodes == 0:
            return True
        return current_nodes <= self._license.max_nodes

    def get_allowed_features(self) -> frozenset[str]:
        """Return all features allowed by the current license.

        Returns:
            Set of allowed feature identifiers.
        """
        if self._license is None:
            return frozenset()

        # Start with tier defaults
        try:
            tier = LicenseTier(self._license.tier)
        except ValueError:
            tier = LicenseTier.COMMUNITY
        tier_features = {f.value for f in _TIER_FEATURES.get(tier, frozenset())}

        # Add explicit features
        return frozenset(tier_features | set(self._license.features))
