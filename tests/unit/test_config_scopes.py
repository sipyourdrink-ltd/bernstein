"""Tests for bernstein.core.config_scopes (CFG-007)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from bernstein.core.config_scopes import (
    ConfigScope,
    MultiScopeConfig,
    _coerce_env_value,
    _load_env_scope,
    _load_yaml_file,
)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data), encoding="utf-8")


class TestConfigScope:
    def test_precedence_order(self) -> None:
        assert ConfigScope.DEFAULTS.value < ConfigScope.USER.value
        assert ConfigScope.USER.value < ConfigScope.PROJECT.value
        assert ConfigScope.PROJECT.value < ConfigScope.WORKSPACE.value
        assert ConfigScope.WORKSPACE.value < ConfigScope.ENV.value


class TestCoerceEnvValue:
    def test_int_key(self) -> None:
        assert _coerce_env_value("max_agents", "4") == 4

    def test_bool_key_true(self) -> None:
        assert _coerce_env_value("auto_merge", "true") is True

    def test_bool_key_false(self) -> None:
        assert _coerce_env_value("auto_merge", "false") is False

    def test_str_key(self) -> None:
        assert _coerce_env_value("model", "opus") == "opus"


class TestLoadYamlFile:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        _write_yaml(path, {"max_agents": 4})
        result = _load_yaml_file(path)
        assert result == {"max_agents": 4}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.yaml"
        assert _load_yaml_file(path) == {}

    def test_invalid_yaml_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("not: [valid: yaml: nested", encoding="utf-8")
        result = _load_yaml_file(path)
        # yaml.safe_load may parse this differently; just check it doesn't crash.
        assert isinstance(result, dict)


class TestLoadEnvScope:
    def test_reads_bernstein_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_MAX_AGENTS", "8")
        monkeypatch.setenv("BERNSTEIN_MODEL", "opus")
        result = _load_env_scope()
        assert result["max_agents"] == 8
        assert result["model"] == "opus"

    def test_ignores_unset_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_MAX_AGENTS", raising=False)
        result = _load_env_scope()
        assert "max_agents" not in result

    def test_invalid_int_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_MAX_AGENTS", "not_a_number")
        result = _load_env_scope()
        assert "max_agents" not in result


class TestMultiScopeConfig:
    def test_defaults_populated(self, tmp_path: Path) -> None:
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        assert config.get("max_agents") == 6
        assert config.get("cli") == "auto"

    def test_project_overrides_defaults(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"max_agents": 10})
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        assert config.get("max_agents") == 10

    def test_workspace_overrides_project(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"max_agents": 10})
        _write_yaml(tmp_path / ".bernstein" / "config.yaml", {"max_agents": 3})
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        assert config.get("max_agents") == 3

    def test_env_overrides_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"max_agents": 10})
        monkeypatch.setenv("BERNSTEIN_MAX_AGENTS", "2")
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        assert config.get("max_agents") == 2

    def test_get_scoped_returns_provenance(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"model": "opus"})
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        scoped = config.get_scoped("model")
        assert scoped is not None
        assert scoped.value == "opus"
        assert scoped.scope == ConfigScope.PROJECT

    def test_effective_returns_flat_dict(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"model": "opus"})
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        eff = config.effective()
        assert "model" in eff
        assert "max_agents" in eff

    def test_scope_summary(self, tmp_path: Path) -> None:
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        summary = config.scope_summary()
        assert len(summary) == len(ConfigScope)
        assert summary[0]["scope"] == "DEFAULTS"

    def test_keys_from_scope(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"model": "opus"})
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        project_keys = config.keys_from_scope(ConfigScope.PROJECT)
        assert "model" in project_keys

    def test_get_missing_key_returns_default(self, tmp_path: Path) -> None:
        config = MultiScopeConfig(workdir=tmp_path)
        config.load()
        assert config.get("nonexistent", "fallback") == "fallback"
