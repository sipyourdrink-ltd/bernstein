"""Tests for MCP-012: MCP server version compatibility checking."""

from __future__ import annotations

import pytest

from bernstein.core.mcp_version_compat import (
    CompatLevel,
    CompatResult,
    ParsedVersion,
    VersionChecker,
)


# ---------------------------------------------------------------------------
# Tests — ParsedVersion
# ---------------------------------------------------------------------------


class TestParsedVersion:
    def test_parse_semver(self) -> None:
        v = ParsedVersion.parse("1.2.3")
        assert v.parts == (1, 2, 3)
        assert v.is_date is False

    def test_parse_semver_prefix(self) -> None:
        v = ParsedVersion.parse("v2.0.1")
        assert v.parts == (2, 0, 1)

    def test_parse_two_part(self) -> None:
        v = ParsedVersion.parse("1.0")
        assert v.parts == (1, 0)

    def test_parse_date(self) -> None:
        v = ParsedVersion.parse("2025-11-05")
        assert v.parts == (2025, 11, 5)
        assert v.is_date is True

    def test_parse_single_number(self) -> None:
        v = ParsedVersion.parse("3")
        assert v.parts == (3,)
        assert v.is_date is False

    def test_parse_empty(self) -> None:
        v = ParsedVersion.parse("")
        assert v.parts == ()

    def test_parse_nonsense(self) -> None:
        v = ParsedVersion.parse("abc")
        assert v.parts == ()


# ---------------------------------------------------------------------------
# Tests — VersionChecker date-based
# ---------------------------------------------------------------------------


class TestDateVersions:
    def test_exact_match(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        result = checker.check("github", "2025-11-05")
        assert result.compatible is True
        assert result.level == CompatLevel.COMPATIBLE

    def test_server_newer(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        result = checker.check("github", "2026-01-15")
        assert result.compatible is True

    def test_server_older(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        result = checker.check("github", "2024-06-01")
        assert result.compatible is False
        assert result.level == CompatLevel.INCOMPATIBLE


# ---------------------------------------------------------------------------
# Tests — VersionChecker semver
# ---------------------------------------------------------------------------


class TestSemverVersions:
    def test_exact_match(self) -> None:
        checker = VersionChecker(required_version="1.2.0")
        result = checker.check("test", "1.2.0")
        assert result.compatible is True

    def test_server_newer_minor(self) -> None:
        checker = VersionChecker(required_version="1.2.0")
        result = checker.check("test", "1.3.0")
        assert result.compatible is True

    def test_server_older_minor(self) -> None:
        checker = VersionChecker(required_version="1.3.0")
        result = checker.check("test", "1.2.0")
        assert result.compatible is True  # non-strict
        assert result.level == CompatLevel.MINOR_MISMATCH

    def test_server_older_minor_strict(self) -> None:
        checker = VersionChecker(required_version="1.3.0", strict=True)
        result = checker.check("test", "1.2.0")
        assert result.compatible is False

    def test_major_mismatch(self) -> None:
        checker = VersionChecker(required_version="2.0.0")
        result = checker.check("test", "1.9.9")
        assert result.compatible is False
        assert result.level == CompatLevel.INCOMPATIBLE

    def test_major_mismatch_server_ahead(self) -> None:
        checker = VersionChecker(required_version="1.0.0")
        result = checker.check("test", "2.0.0")
        assert result.compatible is False

    def test_patch_difference_compatible(self) -> None:
        checker = VersionChecker(required_version="1.2.3")
        result = checker.check("test", "1.2.5")
        assert result.compatible is True


# ---------------------------------------------------------------------------
# Tests — Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unparseable_version(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        result = checker.check("test", "unknown")
        assert result.compatible is True  # permissive
        assert result.level == CompatLevel.UNKNOWN

    def test_unparseable_required(self) -> None:
        checker = VersionChecker(required_version="latest")
        result = checker.check("test", "1.0.0")
        assert result.compatible is True
        assert result.level == CompatLevel.UNKNOWN


# ---------------------------------------------------------------------------
# Tests — Batch checking
# ---------------------------------------------------------------------------


class TestBatchChecking:
    def test_check_many(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        results = checker.check_many(
            {
                "github": "2025-11-05",
                "database": "2024-01-01",
            }
        )
        assert len(results) == 2
        compatible = [r for r in results if r.compatible]
        assert len(compatible) == 1

    def test_get_incompatible(self) -> None:
        checker = VersionChecker(required_version="2025-11-05")
        checker.check("github", "2025-11-05")
        checker.check("old", "2020-01-01")
        incompatible = checker.get_incompatible()
        assert len(incompatible) == 1
        assert incompatible[0].server_name == "old"

    def test_all_results(self) -> None:
        checker = VersionChecker(required_version="1.0.0")
        checker.check("a", "1.0.0")
        checker.check("b", "1.1.0")
        assert len(checker.all_results()) == 2


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_result_to_dict(self) -> None:
        result = CompatResult(
            server_name="test",
            server_version="1.0.0",
            required_version="1.0.0",
            level=CompatLevel.COMPATIBLE,
            compatible=True,
        )
        d = result.to_dict()
        assert d["compatible"] is True
        assert d["level"] == "compatible"

    def test_checker_to_dict(self) -> None:
        checker = VersionChecker(required_version="1.0.0")
        checker.check("test", "1.0.0")
        d = checker.to_dict()
        assert d["required_version"] == "1.0.0"
        assert "test" in d["results"]

    def test_get_result(self) -> None:
        checker = VersionChecker(required_version="1.0.0")
        checker.check("test", "1.0.0")
        result = checker.get_result("test")
        assert result is not None
        assert result.compatible is True

    def test_get_result_unknown(self) -> None:
        checker = VersionChecker(required_version="1.0.0")
        assert checker.get_result("nonexistent") is None
