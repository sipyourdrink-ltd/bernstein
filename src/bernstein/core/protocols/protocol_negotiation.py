"""Protocol version negotiation for forward/backward compatibility.

Negotiates protocol versions between local and remote endpoints for MCP,
A2A, and ACP protocols.  Finds the highest compatible version (same major,
highest minor) and reports any capabilities lost due to downgrade.

Usage::

    from bernstein.core.protocols.protocol_negotiation import (
        get_supported_versions,
        negotiate_version,
    )

    local = get_supported_versions("mcp")
    remote = [ProtocolVersion(protocol="mcp", major=1, minor=0, patch=0)]
    result = negotiate_version(local, remote)
    assert result.success

GitHub: #693
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class ProtocolName(StrEnum):
    """Supported protocol identifiers."""

    MCP = "mcp"
    A2A = "a2a"
    ACP = "acp"


@dataclass(frozen=True)
class ProtocolVersion:
    """A single protocol version with its capability set.

    Attributes:
        protocol: Protocol identifier (mcp, a2a, or acp).
        major: Major version number (breaking changes).
        minor: Minor version number (additive features).
        patch: Patch version number (bug fixes).
        capabilities: Set of capability strings supported at this version.
    """

    protocol: str
    major: int
    minor: int
    patch: int = 0
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset[str]())

    @property
    def version_string(self) -> str:
        """Return a semver-style version string."""
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        return f"{self.protocol} {self.version_string}"


@dataclass(frozen=True)
class NegotiationResult:
    """Outcome of a protocol version negotiation.

    Attributes:
        protocol: The protocol that was negotiated.
        local_version: The local endpoint's best candidate version.
        remote_version: The remote endpoint's best candidate version.
        negotiated_version: The agreed-upon version, or None on failure.
        degraded_capabilities: Capabilities lost compared to the local version.
        success: Whether negotiation succeeded.
    """

    protocol: str
    local_version: ProtocolVersion
    remote_version: ProtocolVersion
    negotiated_version: ProtocolVersion | None
    degraded_capabilities: frozenset[str]
    success: bool


# ---------------------------------------------------------------------------
# Supported version registry (hardcoded for now)
# ---------------------------------------------------------------------------

_SUPPORTED_VERSIONS: dict[str, list[ProtocolVersion]] = {
    ProtocolName.MCP: [
        ProtocolVersion(
            protocol="mcp",
            major=1,
            minor=0,
            patch=0,
            capabilities=frozenset({"tools", "resources", "prompts"}),
        ),
        ProtocolVersion(
            protocol="mcp",
            major=1,
            minor=1,
            patch=0,
            capabilities=frozenset({"tools", "resources", "prompts", "elicitation", "sampling"}),
        ),
    ],
    ProtocolName.A2A: [
        ProtocolVersion(
            protocol="a2a",
            major=0,
            minor=1,
            patch=0,
            capabilities=frozenset({"task_send", "task_status"}),
        ),
        ProtocolVersion(
            protocol="a2a",
            major=0,
            minor=2,
            patch=0,
            capabilities=frozenset({"task_send", "task_status", "streaming", "push_notifications"}),
        ),
    ],
    ProtocolName.ACP: [
        ProtocolVersion(
            protocol="acp",
            major=1,
            minor=0,
            patch=0,
            capabilities=frozenset({"runs", "agents", "discovery"}),
        ),
    ],
}


def get_supported_versions(protocol: str) -> list[ProtocolVersion]:
    """Return the locally supported versions for a protocol.

    Args:
        protocol: Protocol name (mcp, a2a, or acp).

    Returns:
        List of supported ``ProtocolVersion`` objects, ordered oldest-first.

    Raises:
        ValueError: If the protocol is not recognised.
    """
    key = protocol.lower()
    if key not in _SUPPORTED_VERSIONS:
        raise ValueError(f"Unknown protocol {protocol!r}; expected one of {sorted(_SUPPORTED_VERSIONS)}")
    return list(_SUPPORTED_VERSIONS[key])


def version_is_compatible(local: ProtocolVersion, remote: ProtocolVersion) -> bool:
    """Check whether two versions are compatible (same major version).

    Args:
        local: The local version.
        remote: The remote version.

    Returns:
        True if both versions share the same major number.
    """
    return local.major == remote.major


def degrade_capabilities(
    full: ProtocolVersion,
    negotiated: ProtocolVersion,
) -> frozenset[str]:
    """Compute capabilities lost when downgrading from *full* to *negotiated*.

    Args:
        full: The version with the full capability set.
        negotiated: The version actually agreed upon.

    Returns:
        Frozenset of capability names present in *full* but absent in
        *negotiated*.
    """
    return frozenset(full.capabilities - negotiated.capabilities)


def negotiate_version(
    local_versions: list[ProtocolVersion],
    remote_versions: list[ProtocolVersion],
) -> NegotiationResult:
    """Find the highest mutually compatible protocol version.

    The algorithm picks the highest minor version that both sides support
    within the same major version.  When multiple major versions overlap,
    the highest major is preferred.

    Args:
        local_versions: Versions supported by the local endpoint.
        remote_versions: Versions supported by the remote endpoint.

    Returns:
        A ``NegotiationResult`` describing the outcome.

    Raises:
        ValueError: If either version list is empty.
    """
    if not local_versions:
        raise ValueError("local_versions must not be empty")
    if not remote_versions:
        raise ValueError("remote_versions must not be empty")

    protocol = local_versions[0].protocol

    # Sort both lists so highest version is last.
    local_sorted = sorted(local_versions, key=lambda v: (v.major, v.minor, v.patch))
    remote_sorted = sorted(remote_versions, key=lambda v: (v.major, v.minor, v.patch))

    local_best = local_sorted[-1]
    remote_best = remote_sorted[-1]

    # Build a lookup: major -> list of (minor, patch, version) for each side.
    local_by_major: dict[int, list[ProtocolVersion]] = {}
    for v in local_sorted:
        local_by_major.setdefault(v.major, []).append(v)

    remote_by_major: dict[int, list[ProtocolVersion]] = {}
    for v in remote_sorted:
        remote_by_major.setdefault(v.major, []).append(v)

    # Find common major versions, prefer highest.
    common_majors = sorted(
        set(local_by_major) & set(remote_by_major),
        reverse=True,
    )

    if not common_majors:
        logger.warning(
            "Protocol %s: no compatible major version (local=%s, remote=%s)",
            protocol,
            [v.version_string for v in local_sorted],
            [v.version_string for v in remote_sorted],
        )
        return NegotiationResult(
            protocol=protocol,
            local_version=local_best,
            remote_version=remote_best,
            negotiated_version=None,
            degraded_capabilities=frozenset(),
            success=False,
        )

    # Within the best common major, find highest minor both support.
    best_major = common_majors[0]
    local_minors = {v.minor: v for v in local_by_major[best_major]}
    remote_minors = {v.minor: v for v in remote_by_major[best_major]}

    common_minors = sorted(set(local_minors) & set(remote_minors), reverse=True)

    if common_minors:
        # Exact minor match — pick the highest.
        negotiated = local_minors[common_minors[0]]
    else:
        # No exact minor overlap — pick the minimum of each side's highest.
        local_max_minor = max(local_minors)
        remote_max_minor = max(remote_minors)
        chosen_minor = min(local_max_minor, remote_max_minor)

        # Use whichever side owns that minor.
        if chosen_minor in local_minors:
            negotiated = local_minors[chosen_minor]
        elif chosen_minor in remote_minors:
            negotiated = remote_minors[chosen_minor]
        else:
            # Fall back to lowest minor on the side that has the lower max.
            if local_max_minor < remote_max_minor:
                negotiated = local_minors[max(local_minors)]
            else:
                negotiated = remote_minors[max(remote_minors)]

    degraded = degrade_capabilities(local_best, negotiated)

    if degraded:
        logger.info(
            "Protocol %s: negotiated %s (degraded: %s)",
            protocol,
            negotiated.version_string,
            ", ".join(sorted(degraded)),
        )

    return NegotiationResult(
        protocol=protocol,
        local_version=local_best,
        remote_version=remote_best,
        negotiated_version=negotiated,
        degraded_capabilities=degraded,
        success=True,
    )
