"""Tests for config source chain inspection, precedence, and redaction.

Validates that resolved configuration is traceable across user, project,
local, and session-only sources with correct precedence ordering and
sensitive value redaction.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.config_watcher import ConfigWatcher, discover_config_paths
from bernstein.core.home import (
    BernsteinHome,
    _redact_config_value,
    resolve_config,
    resolve_config_bundle,
)

# ---------------------------------------------------------------------------
# Precedence ordering
# ---------------------------------------------------------------------------


class TestConfigPrecedence:
    """Verify session > project > global > default ordering."""

    def test_session_overrides_project(self, tmp_path: Path) -> None:
        """Session-level override must win over project config."""
        home = BernsteinHome(tmp_path / ".bernstein")
        sdd = tmp_path / ".sdd" / "config.yaml"
        sdd.parent.mkdir(parents=True)
        sdd.write_text("cli: gemini\n")

        result = resolve_config(
            "cli", home=home, project_dir=tmp_path, session_overrides={"cli": "codex"}
        )
        assert result["value"] == "codex"
        assert result["source"] == "session"

    def test_project_overrides_global(self, tmp_path: Path) -> None:
        """Project config must win over global (~/.bernstein) config."""
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd = tmp_path / ".sdd" / "config.yaml"
        sdd.parent.mkdir(parents=True)
        sdd.write_text("cli: gemini\n")

        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "gemini"
        assert result["source"] == "project"

    def test_global_overrides_default(self, tmp_path: Path) -> None:
        """Global config must win over built-in defaults."""
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")

        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "codex"
        assert result["source"] == "global"

    def test_default_used_when_no_other_source(self, tmp_path: Path) -> None:
        """Built-in default is returned when no source sets the key."""
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "claude"
        assert result["source"] == "default"

    def test_source_chain_contains_all_layers(self, tmp_path: Path) -> None:
        """source_chain should list every layer that defines the key."""
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd = tmp_path / ".sdd" / "config.yaml"
        sdd.parent.mkdir(parents=True)
        sdd.write_text("cli: gemini\n")

        result = resolve_config(
            "cli", home=home, project_dir=tmp_path, session_overrides={"cli": "qwen"}
        )
        sources = [layer["source"] for layer in result["source_chain"]]
        assert sources == ["session", "project", "global", "default"]

    def test_env_var_acts_as_session_override(self, tmp_path: Path, monkeypatch: object) -> None:
        """BERNSTEIN_CLI env var should behave as a session-level override."""
        import pytest

        mp = pytest.MonkeyPatch()
        mp.setenv("BERNSTEIN_CLI", "aider")
        try:
            home = BernsteinHome(tmp_path / ".bernstein")
            result = resolve_config("cli", home=home, project_dir=tmp_path)
            assert result["value"] == "aider"
            assert result["source"] == "session"
        finally:
            mp.undo()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestConfigRedaction:
    """Verify that sensitive config values are redacted in provenance output."""

    def test_secret_key_is_redacted(self) -> None:
        assert _redact_config_value("api_secret", "abc123") == "***REDACTED***"

    def test_token_key_is_redacted(self) -> None:
        assert _redact_config_value("auth_token", "tok_xyz") == "***REDACTED***"

    def test_password_key_is_redacted(self) -> None:
        assert _redact_config_value("db_password", "hunter2") == "***REDACTED***"

    def test_key_key_is_redacted(self) -> None:
        assert _redact_config_value("api_key", "sk-live-xxx") == "***REDACTED***"

    def test_non_sensitive_key_is_not_redacted(self) -> None:
        assert _redact_config_value("cli", "claude") == "claude"

    def test_none_value_is_not_redacted(self) -> None:
        assert _redact_config_value("api_secret", None) is None

    def test_redacted_value_appears_in_source_chain(self, tmp_path: Path) -> None:
        """Redacted values should appear in provenance layers."""
        home = BernsteinHome(tmp_path / ".bernstein")
        # "cli" is not a secret key, so redacted_value should equal value
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        default_layer = result["source_chain"][-1]
        assert default_layer["redacted_value"] == default_layer["value"]


# ---------------------------------------------------------------------------
# Source chain inspection via ConfigWatcher
# ---------------------------------------------------------------------------


class TestSourceChainInspection:
    """Verify that ConfigWatcher.source_chain() is operator-inspectable."""

    def test_source_chain_includes_all_cascade_labels(self, tmp_path: Path) -> None:
        """source_chain should list every watched config path with label."""
        watcher = ConfigWatcher.snapshot(tmp_path)
        chain = watcher.source_chain()
        labels = {entry["label"] for entry in chain}
        assert labels == {"user", "project", "project_alt", "local", "sdd_project", "cli_overrides", "managed"}

    def test_source_chain_truncates_checksum(self, tmp_path: Path) -> None:
        """Checksum should be truncated for display (12 chars + ...)."""
        cfg = tmp_path / "bernstein.yaml"
        cfg.write_text("goal: test\n")
        watcher = ConfigWatcher.snapshot(tmp_path)
        chain = watcher.source_chain()
        proj = next(e for e in chain if e["label"] == "project")
        checksum = proj["checksum"]
        assert isinstance(checksum, str)
        assert checksum.endswith("...")
        assert len(checksum) == 15  # 12 hex chars + "..."

    def test_source_chain_shows_exists_status(self, tmp_path: Path) -> None:
        """Each entry should indicate whether the file exists."""
        cfg = tmp_path / "bernstein.yaml"
        cfg.write_text("goal: test\n")
        watcher = ConfigWatcher.snapshot(tmp_path)
        chain = watcher.source_chain()
        proj = next(e for e in chain if e["label"] == "project")
        local = next(e for e in chain if e["label"] == "local")
        assert proj["exists"] is True
        assert local["exists"] is False

    def test_discover_config_paths_all_relative_to_workdir(self, tmp_path: Path) -> None:
        """Non-user paths should all be relative to the workdir."""
        paths = discover_config_paths(tmp_path)
        for label, path in paths:
            if label != "user":
                assert str(path).startswith(str(tmp_path)), f"{label} path not under workdir: {path}"


# ---------------------------------------------------------------------------
# Bundle resolution
# ---------------------------------------------------------------------------


class TestConfigBundle:
    """Verify resolve_config_bundle returns a complete traceable bundle."""

    def test_bundle_contains_all_default_keys(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        assert "cli" in bundle
        assert "budget" in bundle
        assert "max_agents" in bundle
        assert "effort" in bundle
        assert "model" in bundle

    def test_bundle_keys_have_source_chain(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        for key, resolution in bundle.items():
            assert "source_chain" in resolution, f"{key} missing source_chain"
            assert len(resolution["source_chain"]) >= 1, f"{key} has empty source_chain"

    def test_bundle_with_mixed_sources(self, tmp_path: Path) -> None:
        """Bundle should track different sources for different keys."""
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("effort", "medium")
        sdd = tmp_path / ".sdd" / "config.yaml"
        sdd.parent.mkdir(parents=True)
        sdd.write_text("cli: gemini\n")

        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        assert bundle["cli"]["source"] == "project"
        assert bundle["effort"]["source"] == "global"
        assert bundle["model"]["source"] == "default"
