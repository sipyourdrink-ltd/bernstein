"""Tests for bernstein.core.config_schema (CFG-001 through CFG-005)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import bernstein.core.config_schema as _config_schema_mod
import pytest
import yaml
from bernstein.core.config_schema import (
    CURRENT_CONFIG_VERSION,
    BernsteinConfig,
    ConfigPathError,
    EnvExpansionError,
    expand_env_recursive,
    expand_env_vars,
    export_json_schema,
    load_and_validate,
    migrate_config,
    register_migration,
    validate_file_paths,
)
from pydantic import ValidationError

# Access the internal migration registry for testing.  Tests restore it
# after each mutation so other tests are not affected.
_MIGRATIONS = _config_schema_mod._MIGRATIONS  # pyright: ignore[reportPrivateUsage]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid config dict."""
    base: dict[str, Any] = {"goal": "Test goal"}
    base.update(overrides)
    return base


def _write_yaml(tmp_path: Path, data: dict[str, Any], name: str = "bernstein.yaml") -> Path:
    """Write a YAML config file and return its path."""
    p = tmp_path / name
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


# =========================================================================
# CFG-001: JSON Schema for bernstein.yaml
# =========================================================================


class TestCFG001_PydanticModel:
    """Pydantic model matches bernstein.yaml structure."""

    def test_minimal_config(self) -> None:
        cfg = BernsteinConfig(**_minimal_config())
        assert cfg.goal == "Test goal"
        assert cfg.cli == "auto"
        assert cfg.max_agents == 6

    def test_full_config(self) -> None:
        data = _minimal_config(
            cli="claude",
            max_agents=4,
            model="opus",
            budget="$20",
            team=["backend", "qa"],
            constraints=["Python 3.12+"],
            context_files=["CLAUDE.md"],
            evolution_enabled=False,
            auto_decompose=False,
            internal_llm_provider="claude",
            internal_llm_model="claude-sonnet-4-6",
            merge_strategy="direct",
            auto_merge=False,
            pr_labels=["test"],
        )
        cfg = BernsteinConfig(**data)
        assert cfg.cli == "claude"
        assert cfg.max_agents == 4
        assert cfg.model == "opus"
        assert cfg.budget == "$20"
        assert cfg.team == ["backend", "qa"]
        assert cfg.constraints == ["Python 3.12+"]
        assert cfg.evolution_enabled is False

    def test_nested_quality_gates(self) -> None:
        data = _minimal_config(quality_gates={"enabled": True, "lint": True, "lint_command": "ruff check ."})
        cfg = BernsteinConfig(**data)
        assert cfg.quality_gates is not None
        assert cfg.quality_gates.lint is True
        assert cfg.quality_gates.lint_command == "ruff check ."

    def test_nested_role_model_policy(self) -> None:
        data = _minimal_config(
            role_model_policy={
                "manager": {"model": "opus", "effort": "max"},
                "backend": {"model": "sonnet"},
            }
        )
        cfg = BernsteinConfig(**data)
        assert cfg.role_model_policy is not None
        assert cfg.role_model_policy["manager"].model == "opus"
        assert cfg.role_model_policy["manager"].effort == "max"
        assert cfg.role_model_policy["backend"].model == "sonnet"

    def test_nested_worktree_setup(self) -> None:
        data = _minimal_config(
            worktree_setup={
                "symlink_dirs": [".venv"],
                "copy_files": [".env"],
                "setup_command": "uv sync",
            }
        )
        cfg = BernsteinConfig(**data)
        assert cfg.worktree_setup is not None
        assert cfg.worktree_setup.symlink_dirs == [".venv"]
        assert cfg.worktree_setup.copy_files == [".env"]
        assert cfg.worktree_setup.setup_command == "uv sync"

    def test_nested_cluster(self) -> None:
        data = _minimal_config(cluster={"enabled": False, "topology": "star", "auth_token": "secret123"})
        cfg = BernsteinConfig(**data)
        assert cfg.cluster is not None
        assert cfg.cluster.topology == "star"
        assert cfg.cluster.auth_token == "secret123"

    def test_nested_remote(self) -> None:
        data = _minimal_config(remote={"host": "example.com", "user": "ubuntu", "port": 22})
        cfg = BernsteinConfig(**data)
        assert cfg.remote is not None
        assert cfg.remote.host == "example.com"
        assert cfg.remote.user == "ubuntu"

    def test_nested_agency(self) -> None:
        data = _minimal_config(agency={"path": "/tmp/agents"})
        cfg = BernsteinConfig(**data)
        assert cfg.agency is not None
        assert cfg.agency.path == "/tmp/agents"

    def test_nested_catalogs(self) -> None:
        data = _minimal_config(
            catalogs=[
                {"name": "agency", "type": "agency", "source": "https://github.com/test"},
            ]
        )
        cfg = BernsteinConfig(**data)
        assert cfg.catalogs is not None
        assert len(cfg.catalogs) == 1
        assert cfg.catalogs[0].name == "agency"

    def test_invalid_cli(self) -> None:
        with pytest.raises(ValidationError):
            BernsteinConfig(**_minimal_config(cli="invalid"))

    def test_invalid_max_agents_zero(self) -> None:
        with pytest.raises(ValidationError):
            BernsteinConfig(**_minimal_config(max_agents=0))

    def test_invalid_max_agents_negative(self) -> None:
        with pytest.raises(ValidationError):
            BernsteinConfig(**_minimal_config(max_agents=-1))

    def test_missing_goal(self) -> None:
        with pytest.raises(ValidationError):
            BernsteinConfig(goal="")

    def test_json_schema_export(self) -> None:
        schema = BernsteinConfig.json_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema
        assert "goal" in schema["properties"]
        assert "cli" in schema["properties"]
        assert "max_agents" in schema["properties"]

    def test_export_json_schema_string(self) -> None:
        schema_str = export_json_schema()
        parsed = json.loads(schema_str)
        assert "properties" in parsed
        assert "goal" in parsed["properties"]

    def test_config_version_default(self) -> None:
        cfg = BernsteinConfig(**_minimal_config())
        assert cfg.config_version == 1

    def test_config_version_explicit(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(config_version=1))
        assert cfg.config_version == 1

    def test_team_auto(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(team="auto"))
        assert cfg.team == "auto"

    def test_team_list(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(team=["backend", "qa"]))
        assert cfg.team == ["backend", "qa"]


# =========================================================================
# CFG-002: Cross-field validation
# =========================================================================


class TestCFG002_CrossFieldValidation:
    """Cross-field validators catch conflicting settings."""

    def test_zero_budget_means_unlimited(self) -> None:
        """budget=$0 means unlimited in Bernstein -- should NOT reject."""
        cfg = BernsteinConfig(**_minimal_config(budget="$0", max_agents=10))
        assert cfg.max_agents == 10

    def test_zero_budget_numeric_means_unlimited(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(budget=0, max_agents=5))
        assert cfg.max_agents == 5

    def test_negative_budget_rejects(self) -> None:
        with pytest.raises(Exception, match="negative"):
            BernsteinConfig(**_minimal_config(budget=-1))

    def test_auto_decompose_no_provider_rejects(self) -> None:
        with pytest.raises(Exception, match="auto_decompose.*internal_llm_provider"):
            BernsteinConfig(**_minimal_config(auto_decompose=True, internal_llm_provider="none"))

    def test_evolution_no_provider_rejects(self) -> None:
        with pytest.raises(Exception, match="evolution_enabled.*internal_llm_provider"):
            BernsteinConfig(**_minimal_config(evolution_enabled=True, internal_llm_provider="none"))

    def test_cluster_no_auth_token_rejects(self) -> None:
        with pytest.raises(Exception, match="cluster.*auth_token"):
            BernsteinConfig(**_minimal_config(cluster={"enabled": True, "auth_token": None}))

    def test_postgres_no_url_rejects(self) -> None:
        with pytest.raises(Exception, match="postgres.*database_url"):
            BernsteinConfig(**_minimal_config(storage={"backend": "postgres"}))

    def test_redis_no_url_rejects(self) -> None:
        with pytest.raises(Exception, match="redis.*redis_url"):
            BernsteinConfig(**_minimal_config(storage={"backend": "redis"}))

    def test_valid_budget_with_agents_passes(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(budget="$20", max_agents=4))
        assert cfg.max_agents == 4

    def test_no_budget_passes(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(budget=None, max_agents=4))
        assert cfg.max_agents == 4

    def test_decompose_disabled_no_provider_passes(self) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(
                auto_decompose=False,
                evolution_enabled=False,
                internal_llm_provider="none",
            )
        )
        assert cfg.auto_decompose is False

    def test_cluster_disabled_no_auth_passes(self) -> None:
        cfg = BernsteinConfig(**_minimal_config(cluster={"enabled": False}))
        assert cfg.cluster is not None
        assert cfg.cluster.enabled is False

    def test_postgres_with_url_passes(self) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(storage={"backend": "postgres", "database_url": "postgresql://localhost/db"})
        )
        assert cfg.storage is not None
        assert cfg.storage.backend == "postgres"


# =========================================================================
# CFG-003: Secure env var expansion
# =========================================================================


class TestCFG003_EnvVarExpansion:
    """Secure ${VAR} expansion with ${VAR:-default} fallback."""

    def test_simple_expansion(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert expand_env_vars("${MY_VAR}") == "hello"

    def test_expansion_with_surrounding_text(self) -> None:
        with patch.dict(os.environ, {"HOST": "example.com"}):
            assert expand_env_vars("https://${HOST}/api") == "https://example.com/api"

    def test_default_fallback(self) -> None:
        env = dict(os.environ)
        env.pop("UNSET_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            assert expand_env_vars("${UNSET_VAR:-fallback}") == "fallback"

    def test_default_empty_string(self) -> None:
        env = dict(os.environ)
        env.pop("UNSET_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            assert expand_env_vars("${UNSET_VAR:-}") == ""

    def test_set_var_ignores_default(self) -> None:
        with patch.dict(os.environ, {"SET_VAR": "actual"}):
            assert expand_env_vars("${SET_VAR:-default}") == "actual"

    def test_unset_no_default_raises(self) -> None:
        env = dict(os.environ)
        env.pop("MISSING_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(EnvExpansionError, match="MISSING_VAR.*not set"):
                expand_env_vars("${MISSING_VAR}", field_name="test_field")

    def test_blocked_var_raises(self) -> None:
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "secret"}):
            with pytest.raises(EnvExpansionError, match="blocked"):
                expand_env_vars("${AWS_SECRET_ACCESS_KEY}", field_name="test_field")

    def test_multiple_vars_in_one_string(self) -> None:
        with patch.dict(os.environ, {"HOST": "localhost", "PORT": "8080"}):
            result = expand_env_vars("http://${HOST}:${PORT}")
            assert result == "http://localhost:8080"

    def test_no_vars_passthrough(self) -> None:
        assert expand_env_vars("plain string") == "plain string"

    def test_dollar_without_braces_passthrough(self) -> None:
        assert expand_env_vars("$NOT_A_VAR") == "$NOT_A_VAR"

    def test_recursive_expansion_dict(self) -> None:
        with patch.dict(os.environ, {"DB_HOST": "db.local"}):
            raw_result = expand_env_recursive(
                {
                    "host": "${DB_HOST}",
                    "port": 5432,
                    "nested": {"url": "pg://${DB_HOST}/mydb"},
                }
            )
            result = cast("dict[str, Any]", raw_result)
            assert result["host"] == "db.local"
            assert result["port"] == 5432
            nested = cast("dict[str, Any]", result["nested"])
            assert nested["url"] == "pg://db.local/mydb"

    def test_recursive_expansion_list(self) -> None:
        with patch.dict(os.environ, {"PREFIX": "/opt"}):
            result = expand_env_recursive(["${PREFIX}/bin", "${PREFIX}/lib"])
            assert result == ["/opt/bin", "/opt/lib"]

    def test_recursive_expansion_preserves_non_strings(self) -> None:
        result = expand_env_recursive({"count": 42, "flag": True, "nothing": None})
        assert result == {"count": 42, "flag": True, "nothing": None}


# =========================================================================
# CFG-004: Config migration between versions
# =========================================================================


class TestCFG004_ConfigMigration:
    """Version field and migration registry."""

    def test_current_version_no_migration(self) -> None:
        data = _minimal_config(config_version=CURRENT_CONFIG_VERSION)
        result = migrate_config(data)
        assert result["config_version"] == CURRENT_CONFIG_VERSION
        assert result["goal"] == "Test goal"

    def test_missing_version_defaults_to_1(self) -> None:
        data = _minimal_config()
        # No config_version key
        data.pop("config_version", None)
        result = migrate_config(data)
        assert result["goal"] == "Test goal"

    def test_future_version_raises(self) -> None:
        data = _minimal_config(config_version=CURRENT_CONFIG_VERSION + 99)
        with pytest.raises(ValueError, match="newer than supported"):
            migrate_config(data)

    def test_negative_version_raises(self) -> None:
        data = _minimal_config(config_version=-1)
        with pytest.raises(ValueError, match="must be >= 1"):
            migrate_config(data)

    def test_non_int_version_raises(self) -> None:
        data = _minimal_config(config_version="1")
        with pytest.raises(ValueError, match="must be an integer"):
            migrate_config(data)

    def test_register_and_apply_migration(self) -> None:
        """Register a v1->v2 migration and verify it runs."""
        saved = dict(_MIGRATIONS)
        original_version = _config_schema_mod.CURRENT_CONFIG_VERSION
        try:
            _config_schema_mod.CURRENT_CONFIG_VERSION = 2

            def migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
                data = dict(data)
                if "old_field" in data:
                    data["new_field"] = data.pop("old_field")
                return data

            register_migration(1, migrate_v1_to_v2)

            data = _minimal_config(config_version=1, old_field="value")
            result = migrate_config(data)
            assert result["config_version"] == 2
            assert result.get("new_field") == "value"
            assert "old_field" not in result
        finally:
            _MIGRATIONS.clear()
            _MIGRATIONS.update(saved)
            _config_schema_mod.CURRENT_CONFIG_VERSION = original_version

    def test_broken_migration_chain_raises(self) -> None:
        """Missing migration in chain raises ValueError."""
        saved = dict(_MIGRATIONS)
        original_version = _config_schema_mod.CURRENT_CONFIG_VERSION
        try:
            _config_schema_mod.CURRENT_CONFIG_VERSION = 3

            # Register v2->v3 but NOT v1->v2
            register_migration(2, lambda d: d)

            data = _minimal_config(config_version=1)
            with pytest.raises(ValueError, match="No migration registered.*1.*2"):
                migrate_config(data)
        finally:
            _MIGRATIONS.clear()
            _MIGRATIONS.update(saved)
            _config_schema_mod.CURRENT_CONFIG_VERSION = original_version

    def test_duplicate_migration_raises(self) -> None:
        saved = dict(_MIGRATIONS)
        try:
            register_migration(99, lambda d: d)
            with pytest.raises(ValueError, match="already registered"):
                register_migration(99, lambda d: d)
        finally:
            _MIGRATIONS.clear()
            _MIGRATIONS.update(saved)


# =========================================================================
# CFG-005: Validate file paths exist
# =========================================================================


class TestCFG005_ValidateFilePaths:
    """Check config-referenced paths exist during startup."""

    def test_context_files_exist(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "DESIGN.md").write_text("design")
        (tmp_path / "CLAUDE.md").write_text("claude")

        cfg = BernsteinConfig(**_minimal_config(context_files=["docs/DESIGN.md", "CLAUDE.md"]))
        errors = validate_file_paths(cfg, tmp_path)
        assert errors == []

    def test_context_files_missing(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(**_minimal_config(context_files=["missing.md", "also_missing.txt"]))
        errors = validate_file_paths(cfg, tmp_path)
        assert len(errors) == 2
        assert "missing.md" in errors[0]
        assert "also_missing.txt" in errors[1]

    def test_agency_path_exists(self, tmp_path: Path) -> None:
        (tmp_path / "agents").mkdir()
        cfg = BernsteinConfig(**_minimal_config(agency={"path": str(tmp_path / "agents")}))
        errors = validate_file_paths(cfg, tmp_path)
        assert errors == []

    def test_agency_path_missing(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(**_minimal_config(agency={"path": "/nonexistent/agents"}))
        errors = validate_file_paths(cfg, tmp_path)
        assert len(errors) == 1
        assert "agency.path" in errors[0]

    def test_agency_relative_path(self, tmp_path: Path) -> None:
        (tmp_path / "local-agents").mkdir()
        cfg = BernsteinConfig(**_minimal_config(agency={"path": "local-agents"}))
        errors = validate_file_paths(cfg, tmp_path)
        assert errors == []

    def test_worktree_symlink_dirs_missing(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(worktree_setup={"symlink_dirs": ["node_modules"], "copy_files": [".env"]})
        )
        errors = validate_file_paths(cfg, tmp_path)
        assert len(errors) == 2
        assert "symlink_dirs" in errors[0]
        assert "copy_files" in errors[1]

    def test_worktree_dirs_exist(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / ".env").write_text("KEY=val")
        cfg = BernsteinConfig(
            **_minimal_config(worktree_setup={"symlink_dirs": ["node_modules"], "copy_files": [".env"]})
        )
        errors = validate_file_paths(cfg, tmp_path)
        assert errors == []

    def test_formal_verification_lemmas_missing(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(
                formal_verification={
                    "enabled": True,
                    "properties": [
                        {
                            "name": "test_prop",
                            "invariant": "x > 0",
                            "checker": "lean4",
                            "lemmas_file": "proofs/lemmas.lean",
                        }
                    ],
                }
            )
        )
        errors = validate_file_paths(cfg, tmp_path)
        assert len(errors) == 1
        assert "lemmas_file" in errors[0]

    def test_catalog_local_path_missing(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(
                catalogs=[{"name": "local", "type": "generic", "path": "custom-agents/", "enabled": True}]
            )
        )
        errors = validate_file_paths(cfg, tmp_path)
        assert len(errors) == 1
        assert "catalogs" in errors[0]

    def test_catalog_disabled_skipped(self, tmp_path: Path) -> None:
        cfg = BernsteinConfig(
            **_minimal_config(catalogs=[{"name": "local", "type": "generic", "path": "missing/", "enabled": False}])
        )
        errors = validate_file_paths(cfg, tmp_path)
        assert errors == []

    def test_no_optional_paths(self) -> None:
        cfg = BernsteinConfig(**_minimal_config())
        errors = validate_file_paths(cfg, Path("/tmp"))
        assert errors == []


# =========================================================================
# Integration: load_and_validate
# =========================================================================


class TestLoadAndValidate:
    """End-to-end config loading."""

    def test_load_minimal(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _minimal_config())
        cfg = load_and_validate(p)
        assert cfg.goal == "Test goal"

    def test_load_with_env_expansion(self, tmp_path: Path) -> None:
        data = _minimal_config(internal_llm_model="${TEST_MODEL:-fallback-model}")
        p = _write_yaml(tmp_path, data)
        env = dict(os.environ)
        env.pop("TEST_MODEL", None)
        with patch.dict(os.environ, env, clear=True):
            cfg = load_and_validate(p)
        assert cfg.internal_llm_model == "fallback-model"

    def test_load_with_env_expansion_set(self, tmp_path: Path) -> None:
        data = _minimal_config(internal_llm_model="${TEST_MODEL:-fallback-model}")
        p = _write_yaml(tmp_path, data)
        with patch.dict(os.environ, {"TEST_MODEL": "actual-model"}):
            cfg = load_and_validate(p)
        assert cfg.internal_llm_model == "actual-model"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError):
            load_and_validate(p)

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bernstein.yaml"
        p.write_text("{{invalid yaml", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_and_validate(p)

    def test_load_non_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "bernstein.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_and_validate(p)

    def test_load_with_path_check_passes(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("docs")
        data = _minimal_config(context_files=["CLAUDE.md"])
        p = _write_yaml(tmp_path, data)
        cfg = load_and_validate(p, check_paths=True)
        assert cfg.context_files == ["CLAUDE.md"]

    def test_load_with_path_check_fails(self, tmp_path: Path) -> None:
        data = _minimal_config(context_files=["missing.md"])
        p = _write_yaml(tmp_path, data)
        with pytest.raises(ConfigPathError, match="missing.md"):
            load_and_validate(p, check_paths=True)

    def test_load_without_env_expansion(self, tmp_path: Path) -> None:
        data = _minimal_config(internal_llm_model="${SHOULD_NOT_EXPAND}")
        p = _write_yaml(tmp_path, data)
        cfg = load_and_validate(p, expand_env=False)
        assert cfg.internal_llm_model == "${SHOULD_NOT_EXPAND}"

    def test_cross_field_validation_on_load(self, tmp_path: Path) -> None:
        data = _minimal_config(budget=-5, max_agents=10)
        p = _write_yaml(tmp_path, data)
        with pytest.raises(Exception, match="negative"):
            load_and_validate(p)

    def test_load_real_bernstein_yaml(self) -> None:
        """Verify that the actual project bernstein.yaml passes validation."""
        project_root = Path(__file__).resolve().parent.parent.parent
        config_path = project_root / "bernstein.yaml"
        if not config_path.exists():
            pytest.skip("No bernstein.yaml in project root")
        # Should not raise -- load with env expansion disabled since
        # CI may not have the required env vars set.
        cfg = load_and_validate(config_path, expand_env=False)
        assert cfg.goal
