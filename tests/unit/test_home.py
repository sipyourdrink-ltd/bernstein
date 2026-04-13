"""Tests for bernstein.core.home — BernsteinHome global config."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.home import BernsteinHome, resolve_config

# ---------------------------------------------------------------------------
# BernsteinHome.ensure()
# ---------------------------------------------------------------------------


class TestBernsteinHomeEnsure:
    def test_creates_home_directory(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert (tmp_path / ".bernstein").is_dir()

    def test_creates_agents_subdir(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert (tmp_path / ".bernstein" / "agents").is_dir()

    def test_creates_metrics_subdir(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert (tmp_path / ".bernstein" / "metrics").is_dir()

    def test_creates_mcp_subdir(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert (tmp_path / ".bernstein" / "mcp").is_dir()

    def test_writes_default_config_yaml(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        config_path = tmp_path / ".bernstein" / "config.yaml"
        assert config_path.exists()
        text = config_path.read_text()
        assert "cli:" in text
        assert "budget:" in text

    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        config_path = tmp_path / ".bernstein" / "config.yaml"
        config_path.write_text("cli: codex\n")
        home.ensure()
        assert config_path.read_text() == "cli: codex\n"

    def test_idempotent_on_repeated_calls(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        home.ensure()  # should not raise


# ---------------------------------------------------------------------------
# BernsteinHome.get() / set()
# ---------------------------------------------------------------------------


class TestBernsteinHomeGetSet:
    def test_get_returns_default_cli(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert home.get("cli") == "claude"

    def test_get_returns_default_budget(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert home.get("budget") is None

    def test_set_persists_value(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        home.set("cli", "codex")
        assert home.get("cli") == "codex"

    def test_set_budget_as_float(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        home.set("budget", 10.0)
        assert home.get("budget") == pytest.approx(10.0)

    def test_get_unknown_key_returns_none(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        assert home.get("nonexistent_key") is None

    def test_set_creates_home_if_missing(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "gemini")
        assert home.get("cli") == "gemini"

    def test_all_returns_full_config_dict(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.ensure()
        cfg = home.all()
        assert isinstance(cfg, dict)
        assert "cli" in cfg


# ---------------------------------------------------------------------------
# resolve_config() — precedence
# ---------------------------------------------------------------------------


class TestResolveConfig:
    def test_defaults_when_no_configs_present(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "claude"
        assert result["source"] == "default"

    def test_global_config_overrides_default(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "codex"
        assert result["source"] == "global"

    def test_project_sdd_config_overrides_global(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True)
        sdd_config.write_text("cli: gemini\n")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert result["value"] == "gemini"
        assert result["source"] == "project"

    def test_resolve_config_returns_source_info(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        assert "value" in result
        assert "source" in result
        assert "source_chain" in result

    def test_unknown_key_returns_none_default(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("nonexistent", home=home, project_dir=tmp_path)
        assert result["value"] is None
        assert result["source"] == "default"

    def test_session_override_wins_and_appears_in_chain(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True)
        sdd_config.write_text("cli: gemini\n")

        result = resolve_config(
            "cli",
            home=home,
            project_dir=tmp_path,
            session_overrides={"cli": "qwen"},
        )

        assert result["value"] == "qwen"
        assert result["source"] == "session"
        assert [layer["source"] for layer in result["source_chain"]] == [
            "session",
            "project",
            "global",
            "default",
        ]

    def test_sensitive_keys_are_redacted_in_source_chain(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")

        result = resolve_config(
            "api_key",
            home=home,
            project_dir=tmp_path,
            session_overrides={"api_key": "secret-value"},
        )

        assert result["value"] == "secret-value"
        assert result["source_chain"][0]["redacted_value"] == "***REDACTED***"


# ---------------------------------------------------------------------------
# BernsteinHome.default() — uses real ~/.bernstein
# ---------------------------------------------------------------------------


class TestBernsteinHomeDefault:
    def test_default_uses_home_dir(self) -> None:
        home = BernsteinHome.default()
        expected = Path.home() / ".bernstein"
        assert home.path == expected
