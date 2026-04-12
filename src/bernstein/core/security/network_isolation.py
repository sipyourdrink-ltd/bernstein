"""SEC-014: Network isolation validation for sandboxed agents.

Verifies agents cannot reach unauthorized endpoints.  Validates network
policies by probing connectivity and checking firewall rules against the
configured sandbox network mode.

Usage::

    from bernstein.core.network_isolation import (
        NetworkIsolationValidator,
        NetworkPolicy,
        IsolationCheckResult,
    )

    policy = NetworkPolicy(allowed_endpoints=[("127.0.0.1", 8052)])
    validator = NetworkIsolationValidator(policy)
    result = validator.validate_isolation(agent_id="agent-1")
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class IsolationLevel(StrEnum):
    """Network isolation levels for sandboxed agents."""

    NONE = "none"  # No network access at all
    LOCAL_ONLY = "local_only"  # Only loopback/localhost
    RESTRICTED = "restricted"  # Only specific endpoints
    FULL = "full"  # Unrestricted network access


class CheckStatus(StrEnum):
    """Status of a single isolation check."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@dataclass(frozen=True)
class Endpoint:
    """A network endpoint (host + port).

    Attributes:
        host: Hostname or IP address.
        port: Port number.
    """

    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class NetworkPolicy:
    """Network policy for a sandboxed agent.

    Attributes:
        isolation_level: The required isolation level.
        allowed_endpoints: Endpoints the agent is permitted to reach.
        denied_endpoints: Endpoints that must be unreachable (for testing).
        dns_allowed: Whether DNS resolution is permitted.
        timeout_seconds: Timeout for connectivity probes.
    """

    isolation_level: IsolationLevel = IsolationLevel.RESTRICTED
    allowed_endpoints: tuple[Endpoint, ...] = ()
    denied_endpoints: tuple[Endpoint, ...] = ()
    dns_allowed: bool = False
    timeout_seconds: float = 2.0


@dataclass(frozen=True)
class IsolationCheck:
    """Result of a single isolation check.

    Attributes:
        name: Descriptive name of the check.
        status: Whether the check passed.
        endpoint: The endpoint that was tested.
        detail: Additional detail about the check result.
        latency_ms: Time taken for the check in milliseconds.
    """

    name: str
    status: CheckStatus
    endpoint: str
    detail: str
    latency_ms: float = 0.0


@dataclass(frozen=True)
class IsolationCheckResult:
    """Aggregate result of all isolation checks for an agent.

    Attributes:
        agent_id: The agent that was validated.
        policy: The network policy that was enforced.
        checks: Individual check results.
        passed: Whether all checks passed.
        timestamp: When the validation was performed.
    """

    agent_id: str
    policy: NetworkPolicy
    checks: tuple[IsolationCheck, ...]
    passed: bool
    timestamp: float = field(default_factory=time.time)


class NetworkIsolationValidator:
    """Validates network isolation for sandboxed agents.

    Probes configured endpoints to verify that the agent's network
    boundaries are properly enforced.

    Args:
        policy: The network policy to validate against.
    """

    def __init__(self, policy: NetworkPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> NetworkPolicy:
        """Return the network policy."""
        return self._policy

    def check_endpoint_reachable(self, endpoint: Endpoint) -> IsolationCheck:
        """Probe whether an endpoint is reachable via TCP connect.

        Args:
            endpoint: The endpoint to probe.

        Returns:
            Check result indicating whether the endpoint is reachable.
        """
        start = time.monotonic()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._policy.timeout_seconds)
            sock.connect((endpoint.host, endpoint.port))
            sock.close()
            latency = (time.monotonic() - start) * 1000
            return IsolationCheck(
                name=f"reachability:{endpoint}",
                status=CheckStatus.PASS,
                endpoint=str(endpoint),
                detail=f"Successfully connected to {endpoint}",
                latency_ms=latency,
            )
        except (OSError, TimeoutError) as exc:
            latency = (time.monotonic() - start) * 1000
            return IsolationCheck(
                name=f"reachability:{endpoint}",
                status=CheckStatus.FAIL,
                endpoint=str(endpoint),
                detail=f"Cannot reach {endpoint}: {exc}",
                latency_ms=latency,
            )

    def check_endpoint_blocked(self, endpoint: Endpoint) -> IsolationCheck:
        """Verify that an endpoint is NOT reachable (should be blocked).

        Args:
            endpoint: The endpoint that should be blocked.

        Returns:
            PASS if blocked, FAIL if reachable.
        """
        reachable = self.check_endpoint_reachable(endpoint)
        if reachable.status == CheckStatus.FAIL:
            return IsolationCheck(
                name=f"blocked:{endpoint}",
                status=CheckStatus.PASS,
                endpoint=str(endpoint),
                detail=f"Correctly blocked: {endpoint}",
                latency_ms=reachable.latency_ms,
            )
        return IsolationCheck(
            name=f"blocked:{endpoint}",
            status=CheckStatus.FAIL,
            endpoint=str(endpoint),
            detail=f"VIOLATION: {endpoint} should be blocked but is reachable",
            latency_ms=reachable.latency_ms,
        )

    def check_dns(self, hostname: str = "example.com") -> IsolationCheck:
        """Check whether DNS resolution is available.

        Args:
            hostname: Hostname to resolve.

        Returns:
            Check result based on policy DNS setting.
        """
        start = time.monotonic()
        try:
            socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
            resolved = True
        except socket.gaierror:
            resolved = False

        latency = (time.monotonic() - start) * 1000

        if self._policy.dns_allowed:
            status = CheckStatus.PASS if resolved else CheckStatus.FAIL
            detail = f"DNS {'resolved' if resolved else 'failed'} (expected: allowed)"
        else:
            status = CheckStatus.PASS if not resolved else CheckStatus.FAIL
            detail = f"DNS {'resolved' if resolved else 'blocked'} (expected: blocked)"

        return IsolationCheck(
            name="dns_resolution",
            status=status,
            endpoint=hostname,
            detail=detail,
            latency_ms=latency,
        )

    def validate_isolation(self, agent_id: str) -> IsolationCheckResult:
        """Run all isolation checks for an agent.

        Tests allowed endpoints (should be reachable), denied endpoints
        (should be blocked), and DNS resolution.

        Args:
            agent_id: Identifier of the agent being validated.

        Returns:
            Aggregate result of all checks.
        """
        checks: list[IsolationCheck] = []

        # Check allowed endpoints are reachable
        for endpoint in self._policy.allowed_endpoints:
            checks.append(self.check_endpoint_reachable(endpoint))

        # Check denied endpoints are blocked
        for endpoint in self._policy.denied_endpoints:
            checks.append(self.check_endpoint_blocked(endpoint))

        # Check DNS policy
        if self._policy.isolation_level != IsolationLevel.FULL:
            checks.append(self.check_dns())

        passed = all(c.status == CheckStatus.PASS for c in checks)

        result = IsolationCheckResult(
            agent_id=agent_id,
            policy=self._policy,
            checks=tuple(checks),
            passed=passed,
        )

        if not passed:
            failed = [c for c in checks if c.status == CheckStatus.FAIL]
            logger.warning(
                "Network isolation validation FAILED for agent %s: %d check(s) failed",
                agent_id,
                len(failed),
            )

        return result
