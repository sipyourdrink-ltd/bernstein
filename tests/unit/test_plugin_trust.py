"""Tests for plugin trust checking — file-based inspection with tmp_path."""

from __future__ import annotations

import pytest

from bernstein.plugin_trust import (
    PluginTrust,
    _compute_signature_fingerprint,
    _compute_trust_score,
    _derive_risk_level,
    _discover_readme,
    _has_pyproject_metadata,
    _has_tests,
    check_plugin_trust,
    format_trust_warning,
)

# --- Fixtures ---


@pytest.fixture()
def trusted_plugin_dir(tmp_path):
    """Create a plugin directory with all trust signals (trusted)."""
    d = tmp_path / "trusted-plugin"
    d.mkdir()
    (d / "plugin.py").write_text("# plugin", encoding="utf-8")
    (d / "README.md").write_text("# Trusted Plugin\n", encoding="utf-8")
    (d / ".signature").write_text("fake-sig", encoding="utf-8")
    (d / "tests" / "test_plugin.py").parent.mkdir(parents=True)
    (d / "tests" / "test_plugin.py").write_text("# tests", encoding="utf-8")
    (d / "pyproject.toml").write_text(
        '[project]\nname = "trusted-plugin"\nversion = "1.0.0"\nauthor = "Author"\n',
        encoding="utf-8",
    )
    return d


@pytest.fixture()
def minimal_plugin_dir(tmp_path):
    """Create a minimal plugin directory with no trust signals (unknown)."""
    d = tmp_path / "bare-minimum"
    d.mkdir()
    (d / "plugin.py").write_text("# plugin", encoding="utf-8")
    return d


@pytest.fixture()
def community_plugin_dir(tmp_path):
    """Create a plugin with README but no signature or tests (community)."""
    d = tmp_path / "community-plugin"
    d.mkdir()
    (d / "plugin.py").write_text("# plugin", encoding="utf-8")
    (d / "README.md").write_text("# Community Plugin\n", encoding="utf-8")
    return d


@pytest.fixture()
def verified_plugin_dir(tmp_path):
    """Create a plugin with signature and README (verified)."""
    d = tmp_path / "verified-plugin"
    d.mkdir()
    (d / "plugin.py").write_text("# plugin", encoding="utf-8")
    (d / "README.md").write_text("# Verified Plugin\n", encoding="utf-8")
    (d / ".signature").write_text("fake-sig", encoding="utf-8")
    return d


# --- TestCheckPluginTrust ---


class TestCheckPluginTrust:
    def test_raises_for_missing_path(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            check_plugin_trust(tmp_path / "does-not-exist")

    def test_trusted_plugin(self, trusted_plugin_dir) -> None:
        trust = check_plugin_trust(trusted_plugin_dir)
        assert trust.plugin_name == "trusted-plugin"
        assert trust.risk_level == "trusted"
        assert trust.signed is True
        assert trust.source_verified is True
        assert trust.has_readme is True
        assert trust.has_tests is True
        assert trust.trust_score == 100

    def test_minimal_plugin_returns_unknown(self, minimal_plugin_dir) -> None:
        trust = check_plugin_trust(minimal_plugin_dir)
        assert trust.plugin_name == "bare-minimum"
        assert trust.risk_level == "unknown"
        assert trust.signed is False
        assert trust.source_verified is False
        assert trust.has_readme is False
        assert trust.has_tests is False
        assert trust.trust_score == 0

    def test_community_plugin_has_readme(self, community_plugin_dir) -> None:
        trust = check_plugin_trust(community_plugin_dir)
        assert trust.risk_level == "community"
        assert trust.has_readme is True
        assert trust.signed is False
        assert 0 < trust.trust_score < 100

    def test_verified_plugin_has_signature(self, verified_plugin_dir) -> None:
        trust = check_plugin_trust(verified_plugin_dir)
        assert trust.risk_level == "verified"
        assert trust.signed is True

    def test_single_file_plugin(self, tmp_path) -> None:
        f = tmp_path / "my_plugin.py"
        f.write_text("# standalone", encoding="utf-8")
        trust = check_plugin_trust(f)
        assert trust.plugin_name == "my_plugin"
        assert trust.risk_level == "unknown"
        assert trust.trust_score == 0

    def test_trust_score_is_capped_at_100(self, trusted_plugin_dir) -> None:
        trust = check_plugin_trust(trusted_plugin_dir)
        assert trust.trust_score <= 100


# --- TestHelpers ---


class TestDiscoverReadme:
    def test_finds_readme_md(self, tmp_path) -> None:
        (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
        assert _discover_readme(tmp_path) == tmp_path / "README.md"

    def test_returns_none_when_absent(self, tmp_path) -> None:
        assert _discover_readme(tmp_path) is None


class TestHasTests:
    def test_finds_tests_directory(self, tmp_path) -> None:
        (tmp_path / "tests" / "test_foo.py").parent.mkdir(parents=True)
        (tmp_path / "tests" / "test_foo.py").write_text("# test", encoding="utf-8")
        assert _has_tests(tmp_path) is True

    def test_finds_test_files_recursively(self, tmp_path) -> None:
        (tmp_path / "test_integration.py").write_text("# test", encoding="utf-8")
        assert _has_tests(tmp_path) is True

    def test_returns_false_when_absent(self, tmp_path) -> None:
        assert _has_tests(tmp_path) is False


class TestComputeSignatureFingerprint:
    def test_returns_hex_when_present(self, tmp_path) -> None:
        (tmp_path / ".signature").write_text("sig-data", encoding="utf-8")
        result = _compute_signature_fingerprint(tmp_path)
        assert result is not None
        assert len(result) == 64  # SHA-256 hex digest

    def test_returns_none_when_absent(self, tmp_path) -> None:
        assert _compute_signature_fingerprint(tmp_path) is None


class TestHasPyprojectMetadata:
    def test_true_when_complete(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\nversion = "1"\nauthor = "a"\n',
            encoding="utf-8",
        )
        assert _has_pyproject_metadata(tmp_path) is True

    def test_false_when_missing(self, tmp_path) -> None:
        assert _has_pyproject_metadata(tmp_path) is False

    def test_false_when_fields_missing(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "p"\n',
            encoding="utf-8",
        )
        assert _has_pyproject_metadata(tmp_path) is False


class TestDeriveRiskLevel:
    def test_trusted_when_all_signals(self) -> None:
        assert (
            _derive_risk_level(signed=True, source_verified=True, has_readme=True, has_tests=True, has_pyproject=True)
            == "trusted"
        )

    def test_verified_with_signature(self) -> None:
        assert (
            _derive_risk_level(
                signed=True, source_verified=False, has_readme=True, has_tests=False, has_pyproject=False
            )
            == "verified"
        )

    def test_community_with_readme(self) -> None:
        assert (
            _derive_risk_level(
                signed=False, source_verified=False, has_readme=True, has_tests=False, has_pyproject=False
            )
            == "community"
        )

    def test_unknown_with_nothing(self) -> None:
        assert (
            _derive_risk_level(
                signed=False, source_verified=False, has_readme=False, has_tests=False, has_pyproject=False
            )
            == "unknown"
        )


class TestComputeTrustScore:
    def test_max_score(self) -> None:
        assert (
            _compute_trust_score(signed=True, source_verified=True, has_readme=True, has_tests=True, has_pyproject=True)
            == 100
        )

    def test_partial_score(self) -> None:
        score = _compute_trust_score(
            signed=False, source_verified=False, has_readme=True, has_tests=False, has_pyproject=False
        )
        assert score == 15  # readme only

    def test_zero_score(self) -> None:
        assert (
            _compute_trust_score(
                signed=False, source_verified=False, has_readme=False, has_tests=False, has_pyproject=False
            )
            == 0
        )


# --- TestFormatTrustWarning ---


class TestFormatTrustWarning:
    def test_produces_string_output(self) -> None:
        trust = PluginTrust(
            plugin_name="test-plugin",
            risk_level="community",
            signed=False,
            source_verified=False,
            has_readme=True,
            has_tests=False,
            trust_score=15,
        )
        result = format_trust_warning(trust)
        assert isinstance(result, str)
        assert "test-plugin" in result
        assert "community" in result

    def test_warning_for_unknown_risk(self) -> None:
        trust = PluginTrust(
            plugin_name="suspicious",
            risk_level="unknown",
            signed=False,
            source_verified=False,
            has_readme=False,
            has_tests=False,
            trust_score=0,
        )
        result = format_trust_warning(trust)
        assert "WARNING" in result

    def test_trusted_does_not_warn(self) -> None:
        trust = PluginTrust(
            plugin_name="safe",
            risk_level="trusted",
            signed=True,
            source_verified=True,
            has_readme=True,
            has_tests=True,
            trust_score=100,
        )
        result = format_trust_warning(trust)
        assert "WARNING" not in result
