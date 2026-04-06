"""Tests for SEC-014: Network isolation validation for sandboxed agents."""

from __future__ import annotations

from bernstein.core.network_isolation import (
    CheckStatus,
    Endpoint,
    IsolationLevel,
    NetworkIsolationValidator,
    NetworkPolicy,
)


class TestEndpoint:
    def test_str(self) -> None:
        ep = Endpoint(host="127.0.0.1", port=8052)
        assert str(ep) == "127.0.0.1:8052"

    def test_frozen(self) -> None:
        ep = Endpoint(host="localhost", port=80)
        assert ep.host == "localhost"
        assert ep.port == 80


class TestNetworkPolicy:
    def test_defaults(self) -> None:
        policy = NetworkPolicy()
        assert policy.isolation_level == IsolationLevel.RESTRICTED
        assert not policy.dns_allowed
        assert policy.timeout_seconds == 2.0

    def test_custom_policy(self) -> None:
        policy = NetworkPolicy(
            isolation_level=IsolationLevel.LOCAL_ONLY,
            allowed_endpoints=(Endpoint("127.0.0.1", 8052),),
            denied_endpoints=(Endpoint("8.8.8.8", 53),),
            dns_allowed=False,
        )
        assert len(policy.allowed_endpoints) == 1
        assert len(policy.denied_endpoints) == 1


class TestNetworkIsolationValidator:
    def test_blocked_endpoint_passes(self) -> None:
        """An endpoint that is unreachable should pass the 'blocked' check."""
        policy = NetworkPolicy(
            denied_endpoints=(Endpoint("192.0.2.1", 1),),
            timeout_seconds=0.5,
        )
        validator = NetworkIsolationValidator(policy)
        check = validator.check_endpoint_blocked(Endpoint("192.0.2.1", 1))
        # This endpoint is RFC 5737 documentation — it should be unreachable
        assert check.status == CheckStatus.PASS

    def test_check_endpoint_reachable_unreachable_host(self) -> None:
        """Probing a non-routable address should return FAIL."""
        policy = NetworkPolicy(timeout_seconds=0.5)
        validator = NetworkIsolationValidator(policy)
        check = validator.check_endpoint_reachable(Endpoint("192.0.2.1", 1))
        assert check.status == CheckStatus.FAIL

    def test_validate_isolation_with_only_blocked(self) -> None:
        """Validation should pass when all denied endpoints are blocked."""
        policy = NetworkPolicy(
            denied_endpoints=(Endpoint("192.0.2.1", 1),),
            timeout_seconds=0.5,
            isolation_level=IsolationLevel.FULL,  # skip DNS check
        )
        validator = NetworkIsolationValidator(policy)
        result = validator.validate_isolation("agent-1")
        assert result.passed
        assert result.agent_id == "agent-1"

    def test_validate_isolation_empty_policy(self) -> None:
        """Empty policy with full isolation should pass (no checks to run)."""
        policy = NetworkPolicy(isolation_level=IsolationLevel.FULL)
        validator = NetworkIsolationValidator(policy)
        result = validator.validate_isolation("agent-1")
        assert result.passed

    def test_latency_recorded(self) -> None:
        policy = NetworkPolicy(timeout_seconds=0.5)
        validator = NetworkIsolationValidator(policy)
        check = validator.check_endpoint_reachable(Endpoint("192.0.2.1", 1))
        assert check.latency_ms >= 0

    def test_policy_property(self) -> None:
        policy = NetworkPolicy()
        validator = NetworkIsolationValidator(policy)
        assert validator.policy == policy

    def test_dns_check_included_for_non_full_isolation(self) -> None:
        """DNS check should be included when isolation is not FULL."""
        policy = NetworkPolicy(
            isolation_level=IsolationLevel.RESTRICTED,
            dns_allowed=True,
        )
        validator = NetworkIsolationValidator(policy)
        result = validator.validate_isolation("agent-1")
        dns_checks = [c for c in result.checks if c.name == "dns_resolution"]
        assert len(dns_checks) == 1

    def test_dns_check_skipped_for_full_isolation(self) -> None:
        """DNS check should be skipped when isolation is FULL."""
        policy = NetworkPolicy(isolation_level=IsolationLevel.FULL)
        validator = NetworkIsolationValidator(policy)
        result = validator.validate_isolation("agent-1")
        dns_checks = [c for c in result.checks if c.name == "dns_resolution"]
        assert len(dns_checks) == 0
