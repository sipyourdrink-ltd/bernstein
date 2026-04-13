"""Tests for organizational policy templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from bernstein.core.policy_templates import (
    OrgPolicyTemplate,
    apply_org_policies,
    load_org_policies,
)


def _write_policy(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    """Helper: write a YAML policy file and return its path."""
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.dump(data))
    return p


class TestLoadOrgPolicies:
    """Tests for load_org_policies."""

    def test_load_single_policy(self, tmp_path: Path) -> None:
        p = _write_policy(
            tmp_path,
            "security",
            {
                "name": "security-baseline",
                "description": "Enforce security defaults",
                "overrides": {"max_agents": 4, "cli": "claude"},
            },
        )
        result = load_org_policies([str(p)])
        assert len(result) == 1
        assert result[0].name == "security-baseline"
        assert result[0].description == "Enforce security defaults"
        assert result[0].overrides == {"max_agents": 4, "cli": "claude"}

    def test_missing_file_is_skipped(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does-not-exist.yaml")
        result = load_org_policies([missing])
        assert result == []

    def test_missing_file_does_not_block_others(self, tmp_path: Path) -> None:
        good = _write_policy(
            tmp_path,
            "good",
            {
                "name": "good-policy",
                "description": "Valid",
                "overrides": {"max_agents": 2},
            },
        )
        missing = str(tmp_path / "missing.yaml")
        result = load_org_policies([missing, str(good)])
        assert len(result) == 1
        assert result[0].name == "good-policy"

    def test_malformed_yaml_is_skipped(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(":::not valid yaml[[[")
        result = load_org_policies([str(bad)])
        assert result == []

    def test_non_mapping_yaml_is_skipped(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text(yaml.dump(["a", "b", "c"]))
        result = load_org_policies([str(bad)])
        assert result == []

    def test_defaults_for_missing_fields(self, tmp_path: Path) -> None:
        p = _write_policy(tmp_path, "minimal", {"overrides": {"cli": "codex"}})
        result = load_org_policies([str(p)])
        assert len(result) == 1
        assert result[0].name == "minimal"  # stem of the file
        assert result[0].description == ""
        assert result[0].overrides == {"cli": "codex"}

    def test_empty_overrides(self, tmp_path: Path) -> None:
        p = _write_policy(
            tmp_path,
            "noop",
            {
                "name": "noop",
                "description": "No overrides",
            },
        )
        result = load_org_policies([str(p)])
        assert len(result) == 1
        assert result[0].overrides == {}


class TestApplyOrgPolicies:
    """Tests for apply_org_policies."""

    def test_merge_not_replace(self) -> None:
        config: dict[str, Any] = {
            "goal": "build something",
            "max_agents": 6,
            "cli": "auto",
        }
        templates = [
            OrgPolicyTemplate(
                name="limit-agents",
                description="Cap agents at 3",
                overrides={"max_agents": 3},
            ),
        ]
        result = apply_org_policies(config, templates)
        assert result["max_agents"] == 3
        assert result["goal"] == "build something"
        assert result["cli"] == "auto"

    def test_original_config_not_mutated(self) -> None:
        config: dict[str, Any] = {"max_agents": 6}
        templates = [
            OrgPolicyTemplate(name="t", description="", overrides={"max_agents": 1}),
        ]
        apply_org_policies(config, templates)
        assert config["max_agents"] == 6

    def test_deep_merge_nested_dicts(self) -> None:
        config: dict[str, Any] = {
            "sandbox": {"allowed_paths": ["src/"], "denied_paths": [".env"]},
            "max_agents": 6,
        }
        templates = [
            OrgPolicyTemplate(
                name="sandbox-policy",
                description="Tighten sandbox",
                overrides={"sandbox": {"denied_paths": [".env", "secrets/"]}},
            ),
        ]
        result = apply_org_policies(config, templates)
        assert result["sandbox"]["allowed_paths"] == ["src/"]
        assert result["sandbox"]["denied_paths"] == [".env", "secrets/"]

    def test_multiple_templates_stack(self) -> None:
        config: dict[str, Any] = {
            "max_agents": 6,
            "cli": "auto",
            "model": None,
        }
        templates = [
            OrgPolicyTemplate(
                name="first",
                description="Set agents and cli",
                overrides={"max_agents": 4, "cli": "claude"},
            ),
            OrgPolicyTemplate(
                name="second",
                description="Override agents again and set model",
                overrides={"max_agents": 2, "model": "sonnet"},
            ),
        ]
        result = apply_org_policies(config, templates)
        assert result["max_agents"] == 2  # second template wins
        assert result["cli"] == "claude"  # from first template, not overridden
        assert result["model"] == "sonnet"  # from second template

    def test_empty_templates_returns_copy(self) -> None:
        config: dict[str, Any] = {"max_agents": 6}
        result = apply_org_policies(config, [])
        assert result == config
        assert result is not config

    def test_adds_new_keys(self) -> None:
        config: dict[str, Any] = {"goal": "test"}
        templates = [
            OrgPolicyTemplate(
                name="add-key",
                description="",
                overrides={"new_field": "new_value"},
            ),
        ]
        result = apply_org_policies(config, templates)
        assert result["new_field"] == "new_value"
        assert result["goal"] == "test"
