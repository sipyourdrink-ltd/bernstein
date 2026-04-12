"""ENT-009: Data residency controls.

Ensures task data, logs, and agent outputs stay within configured geographic
regions.  Provides region tagging, residency validation, and attestation
records for compliance auditing.

Each tenant can be assigned one or more allowed regions.  Every data write
is checked against the tenant's residency policy before proceeding.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


class Region(StrEnum):
    """Supported data residency regions."""

    US_EAST = "us-east"
    US_WEST = "us-west"
    EU_WEST = "eu-west"
    EU_CENTRAL = "eu-central"
    AP_SOUTHEAST = "ap-southeast"
    AP_NORTHEAST = "ap-northeast"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResidencyPolicy:
    """Data residency policy for a tenant.

    Attributes:
        tenant_id: Tenant identifier.
        allowed_regions: Regions where data may reside.
        primary_region: Default region for new data.
        enforce_strict: If True, reject writes to non-allowed regions.
            If False, log warnings but allow writes.
        require_encryption_at_rest: Whether data must be encrypted at rest.
    """

    tenant_id: str = ""
    allowed_regions: frozenset[str] = field(
        default_factory=lambda: frozenset({Region.US_EAST}),
    )
    primary_region: str = Region.US_EAST
    enforce_strict: bool = True
    require_encryption_at_rest: bool = False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ResidencyViolation(Exception):
    """Raised when a data write violates residency policy."""


@dataclass(frozen=True)
class ResidencyCheckResult:
    """Result of a residency validation check.

    Attributes:
        allowed: Whether the operation is allowed.
        tenant_id: Tenant being checked.
        requested_region: Region the write targets.
        policy_regions: Regions allowed by policy.
        violation_reason: Explanation if denied.
    """

    allowed: bool = True
    tenant_id: str = ""
    requested_region: str = ""
    policy_regions: frozenset[str] = field(default_factory=frozenset[str])
    violation_reason: str = ""


@dataclass(frozen=True)
class ResidencyAttestation:
    """Attestation record proving data residency compliance.

    Attributes:
        tenant_id: Tenant the attestation covers.
        region: Region where data resides.
        resource_id: Identifier of the data resource (task, log, etc.).
        resource_type: Type of resource (task, audit_log, agent_output).
        timestamp: When the attestation was created.
        compliant: Whether the resource is in a compliant region.
    """

    tenant_id: str = ""
    region: str = ""
    resource_id: str = ""
    resource_type: str = ""
    timestamp: float = field(default_factory=time.time)
    compliant: bool = True


# ---------------------------------------------------------------------------
# Residency controller
# ---------------------------------------------------------------------------


class DataResidencyController:
    """Manages data residency policies and validates write operations.

    Args:
        node_region: The region where this node is located.
    """

    def __init__(self, node_region: str = Region.US_EAST) -> None:
        self._node_region = node_region
        self._policies: dict[str, ResidencyPolicy] = {}
        self._attestations: list[ResidencyAttestation] = []

    @property
    def node_region(self) -> str:
        """Return the region of this node."""
        return self._node_region

    def set_policy(self, policy: ResidencyPolicy) -> None:
        """Register or update a residency policy for a tenant.

        Args:
            policy: Residency policy to set.
        """
        self._policies[policy.tenant_id] = policy
        logger.info(
            "Set residency policy for tenant %s: regions=%s",
            policy.tenant_id,
            sorted(policy.allowed_regions),
        )

    def get_policy(self, tenant_id: str) -> ResidencyPolicy | None:
        """Get the residency policy for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Policy if configured, None otherwise.
        """
        return self._policies.get(tenant_id)

    def check_write(
        self,
        tenant_id: str,
        target_region: str,
    ) -> ResidencyCheckResult:
        """Validate whether a data write to a region is allowed.

        Args:
            tenant_id: Tenant performing the write.
            target_region: Region where data would be written.

        Returns:
            ResidencyCheckResult indicating whether the write is allowed.
        """
        policy = self._policies.get(tenant_id)
        if policy is None:
            # No policy configured — allow by default
            return ResidencyCheckResult(
                allowed=True,
                tenant_id=tenant_id,
                requested_region=target_region,
            )

        if target_region in policy.allowed_regions:
            return ResidencyCheckResult(
                allowed=True,
                tenant_id=tenant_id,
                requested_region=target_region,
                policy_regions=policy.allowed_regions,
            )

        result = ResidencyCheckResult(
            allowed=not policy.enforce_strict,
            tenant_id=tenant_id,
            requested_region=target_region,
            policy_regions=policy.allowed_regions,
            violation_reason=(f"Region {target_region} not in allowed regions {sorted(policy.allowed_regions)}"),
        )

        if policy.enforce_strict:
            logger.warning(
                "Residency violation for tenant %s: %s",
                tenant_id,
                result.violation_reason,
            )
        else:
            logger.info(
                "Residency warning for tenant %s: %s",
                tenant_id,
                result.violation_reason,
            )

        return result

    def validate_write_or_raise(
        self,
        tenant_id: str,
        target_region: str,
    ) -> None:
        """Validate a write and raise if it violates strict policy.

        Args:
            tenant_id: Tenant performing the write.
            target_region: Target region.

        Raises:
            ResidencyViolation: If the write is denied.
        """
        result = self.check_write(tenant_id, target_region)
        if not result.allowed:
            raise ResidencyViolation(result.violation_reason)

    def create_attestation(
        self,
        tenant_id: str,
        resource_id: str,
        resource_type: str,
        region: str,
    ) -> ResidencyAttestation:
        """Create an attestation record for a data resource.

        Args:
            tenant_id: Tenant owning the resource.
            resource_id: Resource identifier.
            resource_type: Type of resource.
            region: Region where the resource is stored.

        Returns:
            Attestation record.
        """
        policy = self._policies.get(tenant_id)
        compliant = True
        if policy is not None:
            compliant = region in policy.allowed_regions

        attestation = ResidencyAttestation(
            tenant_id=tenant_id,
            region=region,
            resource_id=resource_id,
            resource_type=resource_type,
            compliant=compliant,
        )
        self._attestations.append(attestation)
        return attestation

    def get_attestations(
        self,
        tenant_id: str | None = None,
    ) -> list[ResidencyAttestation]:
        """Return attestation records, optionally filtered by tenant.

        Args:
            tenant_id: If provided, filter to this tenant only.

        Returns:
            List of attestation records.
        """
        if tenant_id is None:
            return list(self._attestations)
        return [a for a in self._attestations if a.tenant_id == tenant_id]

    def get_non_compliant(
        self,
        tenant_id: str | None = None,
    ) -> list[ResidencyAttestation]:
        """Return non-compliant attestation records.

        Args:
            tenant_id: If provided, filter to this tenant only.

        Returns:
            List of non-compliant attestation records.
        """
        attestations = self.get_attestations(tenant_id)
        return [a for a in attestations if not a.compliant]
