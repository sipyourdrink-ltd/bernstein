"""Tests for protocol version negotiation."""

from __future__ import annotations

import pytest

from bernstein.core.protocols.protocol_negotiation import (
    ProtocolVersion,
    degrade_capabilities,
    get_supported_versions,
    negotiate_version,
    version_is_compatible,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp(major: int, minor: int, patch: int = 0, caps: frozenset[str] | None = None) -> ProtocolVersion:
    return ProtocolVersion(
        protocol="mcp",
        major=major,
        minor=minor,
        patch=patch,
        capabilities=caps or frozenset(),
    )


# ---------------------------------------------------------------------------
# ProtocolVersion dataclass
# ---------------------------------------------------------------------------


class TestProtocolVersion:
    """Tests for the ProtocolVersion dataclass."""

    def test_version_string(self) -> None:
        v = _mcp(1, 2, 3)
        assert v.version_string == "1.2.3"

    def test_str_representation(self) -> None:
        v = _mcp(1, 0)
        assert str(v) == "mcp 1.0.0"

    def test_frozen(self) -> None:
        v = _mcp(1, 0)
        with pytest.raises(AttributeError):
            v.major = 2  # type: ignore[misc]

    def test_default_capabilities_empty(self) -> None:
        v = ProtocolVersion(protocol="mcp", major=1, minor=0, patch=0)
        assert v.capabilities == frozenset()


# ---------------------------------------------------------------------------
# get_supported_versions
# ---------------------------------------------------------------------------


class TestGetSupportedVersions:
    """Tests for the get_supported_versions function."""

    def test_mcp_versions(self) -> None:
        versions = get_supported_versions("mcp")
        assert len(versions) == 2
        assert all(v.protocol == "mcp" for v in versions)

    def test_a2a_versions(self) -> None:
        versions = get_supported_versions("a2a")
        assert len(versions) == 2
        assert all(v.protocol == "a2a" for v in versions)

    def test_acp_versions(self) -> None:
        versions = get_supported_versions("acp")
        assert len(versions) == 1
        assert versions[0].major == 1

    def test_case_insensitive(self) -> None:
        assert get_supported_versions("MCP") == get_supported_versions("mcp")

    def test_unknown_protocol_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown protocol"):
            get_supported_versions("unknown")


# ---------------------------------------------------------------------------
# version_is_compatible
# ---------------------------------------------------------------------------


class TestVersionIsCompatible:
    """Tests for the version_is_compatible function."""

    def test_same_version(self) -> None:
        v = _mcp(1, 0)
        assert version_is_compatible(v, v) is True

    def test_same_major_different_minor(self) -> None:
        assert version_is_compatible(_mcp(1, 0), _mcp(1, 1)) is True

    def test_different_major(self) -> None:
        assert version_is_compatible(_mcp(1, 0), _mcp(2, 0)) is False


# ---------------------------------------------------------------------------
# degrade_capabilities
# ---------------------------------------------------------------------------


class TestDegradeCapabilities:
    """Tests for the degrade_capabilities function."""

    def test_no_degradation(self) -> None:
        caps = frozenset({"tools", "resources"})
        full = _mcp(1, 1, caps=caps)
        negotiated = _mcp(1, 1, caps=caps)
        assert degrade_capabilities(full, negotiated) == frozenset()

    def test_partial_degradation(self) -> None:
        full = _mcp(1, 1, caps=frozenset({"tools", "resources", "sampling"}))
        negotiated = _mcp(1, 0, caps=frozenset({"tools", "resources"}))
        lost = degrade_capabilities(full, negotiated)
        assert lost == frozenset({"sampling"})

    def test_full_degradation(self) -> None:
        full = _mcp(1, 1, caps=frozenset({"elicitation", "sampling"}))
        negotiated = _mcp(1, 0, caps=frozenset())
        lost = degrade_capabilities(full, negotiated)
        assert lost == frozenset({"elicitation", "sampling"})


# ---------------------------------------------------------------------------
# negotiate_version
# ---------------------------------------------------------------------------


class TestNegotiateVersion:
    """Tests for the negotiate_version function."""

    def test_exact_same_version(self) -> None:
        """Both sides support only v1.0 -- trivial agreement."""
        local = [_mcp(1, 0)]
        remote = [_mcp(1, 0)]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.negotiated_version is not None
        assert result.negotiated_version.major == 1
        assert result.negotiated_version.minor == 0

    def test_compatible_picks_highest_common(self) -> None:
        """Local supports 1.0/1.1, remote supports 1.0 -- pick 1.0."""
        local = [_mcp(1, 0, caps=frozenset({"a"})), _mcp(1, 1, caps=frozenset({"a", "b"}))]
        remote = [_mcp(1, 0, caps=frozenset({"a"}))]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.negotiated_version is not None
        assert result.negotiated_version.minor == 0

    def test_incompatible_different_major(self) -> None:
        """Local is v1.x, remote is v2.x -- no common major."""
        local = [_mcp(1, 0)]
        remote = [_mcp(2, 0)]
        result = negotiate_version(local, remote)

        assert result.success is False
        assert result.negotiated_version is None

    def test_capability_degradation_reported(self) -> None:
        """When downgrading, lost capabilities should be listed."""
        caps_10 = frozenset({"tools", "resources"})
        caps_11 = frozenset({"tools", "resources", "elicitation", "sampling"})

        local = [_mcp(1, 0, caps=caps_10), _mcp(1, 1, caps=caps_11)]
        remote = [_mcp(1, 0, caps=caps_10)]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.degraded_capabilities == frozenset({"elicitation", "sampling"})

    def test_empty_local_raises(self) -> None:
        with pytest.raises(ValueError, match="local_versions must not be empty"):
            negotiate_version([], [_mcp(1, 0)])

    def test_empty_remote_raises(self) -> None:
        with pytest.raises(ValueError, match="remote_versions must not be empty"):
            negotiate_version([_mcp(1, 0)], [])

    def test_multiple_majors_prefers_highest(self) -> None:
        """When both sides support major 1 and 2, negotiate on major 2."""
        local = [_mcp(1, 0), _mcp(2, 0)]
        remote = [_mcp(1, 0), _mcp(2, 0)]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.negotiated_version is not None
        assert result.negotiated_version.major == 2

    def test_negotiation_result_frozen(self) -> None:
        result = negotiate_version([_mcp(1, 0)], [_mcp(1, 0)])
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]

    def test_real_mcp_versions(self) -> None:
        """Negotiate using the actual hardcoded MCP registry."""
        local = get_supported_versions("mcp")
        remote = [
            ProtocolVersion(
                protocol="mcp",
                major=1,
                minor=0,
                patch=0,
                capabilities=frozenset({"tools", "resources", "prompts"}),
            )
        ]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.negotiated_version is not None
        assert result.negotiated_version.minor == 0
        assert "elicitation" in result.degraded_capabilities
        assert "sampling" in result.degraded_capabilities

    def test_real_a2a_versions(self) -> None:
        """Negotiate A2A with only v0.1 on the remote side."""
        local = get_supported_versions("a2a")
        remote = [
            ProtocolVersion(
                protocol="a2a",
                major=0,
                minor=1,
                patch=0,
                capabilities=frozenset({"task_send", "task_status"}),
            )
        ]
        result = negotiate_version(local, remote)

        assert result.success is True
        assert result.negotiated_version is not None
        assert result.negotiated_version.minor == 1
        assert "streaming" in result.degraded_capabilities
