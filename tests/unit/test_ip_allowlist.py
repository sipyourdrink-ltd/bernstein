"""Tests for ENT-011: IP allowlisting for API access."""

from __future__ import annotations

from bernstein.core.ip_allowlist import (
    check_ip_allowed,
)

# ---------------------------------------------------------------------------
# check_ip_allowed (exercises network parsing internally)
# ---------------------------------------------------------------------------


class TestCheckIPAllowed:
    def test_localhost_always_allowed(self) -> None:
        assert check_ip_allowed("127.0.0.1", [])
        assert check_ip_allowed("::1", [])
        assert check_ip_allowed("localhost", [])

    def test_ip_in_range(self) -> None:
        assert check_ip_allowed("10.0.0.5", ["10.0.0.0/8"])

    def test_ip_not_in_range(self) -> None:
        assert not check_ip_allowed("192.168.1.1", ["10.0.0.0/8"])

    def test_exact_ip_match(self) -> None:
        assert check_ip_allowed("10.0.0.1", ["10.0.0.1/32"])
        assert not check_ip_allowed("10.0.0.2", ["10.0.0.1/32"])

    def test_multiple_ranges(self) -> None:
        ranges = ["10.0.0.0/8", "172.16.0.0/12"]
        assert check_ip_allowed("10.1.2.3", ranges)
        assert check_ip_allowed("172.16.5.1", ranges)
        assert not check_ip_allowed("192.168.1.1", ranges)

    def test_invalid_ip_rejected(self) -> None:
        assert not check_ip_allowed("not-an-ip", ["10.0.0.0/8"])

    def test_ipv6_in_range(self) -> None:
        assert check_ip_allowed("fd00::1", ["fd00::/8"])

    def test_single_ip_cidr(self) -> None:
        assert check_ip_allowed("10.0.0.1", ["10.0.0.1"])

    def test_empty_allowlist_denies_non_localhost(self) -> None:
        assert not check_ip_allowed("10.0.0.1", [])

    def test_invalid_cidr_ignored(self) -> None:
        # Invalid CIDR should not match, but valid ones still work
        assert check_ip_allowed("10.0.0.1", ["invalid", "10.0.0.0/8"])
        assert not check_ip_allowed("192.168.1.1", ["invalid"])
