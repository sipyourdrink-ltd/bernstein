"""Tests for ENT-009: Data residency controls."""

from __future__ import annotations

import pytest
from bernstein.core.data_residency import (
    DataResidencyController,
    Region,
    ResidencyPolicy,
    ResidencyViolation,
)

# ---------------------------------------------------------------------------
# Policy management
# ---------------------------------------------------------------------------


class TestPolicyManagement:
    def test_set_and_get_policy(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST, Region.EU_CENTRAL}),
            primary_region=Region.EU_WEST,
        )
        controller.set_policy(policy)
        fetched = controller.get_policy("acme")
        assert fetched is not None
        assert fetched.tenant_id == "acme"
        assert Region.EU_WEST in fetched.allowed_regions

    def test_no_policy_returns_none(self) -> None:
        controller = DataResidencyController()
        assert controller.get_policy("unknown") is None


# ---------------------------------------------------------------------------
# Write validation
# ---------------------------------------------------------------------------


class TestWriteValidation:
    def test_allowed_region_passes(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.US_EAST, Region.US_WEST}),
        )
        controller.set_policy(policy)
        result = controller.check_write("acme", Region.US_EAST)
        assert result.allowed

    def test_disallowed_region_strict(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
            enforce_strict=True,
        )
        controller.set_policy(policy)
        result = controller.check_write("acme", Region.US_EAST)
        assert not result.allowed
        assert "not in allowed regions" in result.violation_reason

    def test_disallowed_region_lenient(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
            enforce_strict=False,
        )
        controller.set_policy(policy)
        result = controller.check_write("acme", Region.US_EAST)
        assert result.allowed
        assert result.violation_reason  # Warning still present

    def test_no_policy_allows_all(self) -> None:
        controller = DataResidencyController()
        result = controller.check_write("unknown", Region.AP_SOUTHEAST)
        assert result.allowed


# ---------------------------------------------------------------------------
# validate_write_or_raise
# ---------------------------------------------------------------------------


class TestValidateOrRaise:
    def test_raises_on_violation(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
            enforce_strict=True,
        )
        controller.set_policy(policy)
        with pytest.raises(ResidencyViolation):
            controller.validate_write_or_raise("acme", Region.US_EAST)

    def test_no_raise_on_allowed(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.US_EAST}),
        )
        controller.set_policy(policy)
        # Should not raise
        controller.validate_write_or_raise("acme", Region.US_EAST)


# ---------------------------------------------------------------------------
# Attestations
# ---------------------------------------------------------------------------


class TestAttestations:
    def test_create_compliant_attestation(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
        )
        controller.set_policy(policy)
        att = controller.create_attestation(
            "acme",
            "task-1",
            "task",
            Region.EU_WEST,
        )
        assert att.compliant
        assert att.region == Region.EU_WEST

    def test_create_non_compliant_attestation(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
        )
        controller.set_policy(policy)
        att = controller.create_attestation(
            "acme",
            "task-2",
            "task",
            Region.US_EAST,
        )
        assert not att.compliant

    def test_filter_by_tenant(self) -> None:
        controller = DataResidencyController()
        controller.create_attestation("acme", "r1", "task", Region.US_EAST)
        controller.create_attestation("other", "r2", "task", Region.US_EAST)
        acme_atts = controller.get_attestations("acme")
        assert len(acme_atts) == 1
        assert acme_atts[0].tenant_id == "acme"

    def test_get_non_compliant(self) -> None:
        controller = DataResidencyController()
        policy = ResidencyPolicy(
            tenant_id="acme",
            allowed_regions=frozenset({Region.EU_WEST}),
        )
        controller.set_policy(policy)
        controller.create_attestation("acme", "r1", "task", Region.EU_WEST)
        controller.create_attestation("acme", "r2", "task", Region.US_EAST)
        non_compliant = controller.get_non_compliant("acme")
        assert len(non_compliant) == 1
        assert non_compliant[0].resource_id == "r2"


# ---------------------------------------------------------------------------
# Node region
# ---------------------------------------------------------------------------


class TestNodeRegion:
    def test_default_region(self) -> None:
        controller = DataResidencyController()
        assert controller.node_region == Region.US_EAST

    def test_custom_region(self) -> None:
        controller = DataResidencyController(node_region=Region.EU_WEST)
        assert controller.node_region == Region.EU_WEST
