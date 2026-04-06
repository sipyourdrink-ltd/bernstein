"""Tests for bernstein.core.config_cli_overrides (CFG-012)."""

from __future__ import annotations

import pytest

from bernstein.core.config_cli_overrides import (
    CLIOverride,
    CLIOverrideManager,
    apply_overrides,
    parse_cli_overrides,
)


class TestParseCLIOverrides:
    def test_parse_int_flag(self) -> None:
        overrides = parse_cli_overrides({"--max-agents": "4"})
        assert len(overrides) == 1
        assert overrides[0].config_key == "max_agents"
        assert overrides[0].value == 4

    def test_parse_str_flag(self) -> None:
        overrides = parse_cli_overrides({"--model": "opus"})
        assert overrides[0].value == "opus"

    def test_parse_budget_flag(self) -> None:
        overrides = parse_cli_overrides({"--budget": "$20"})
        assert overrides[0].value == "$20"

    def test_parse_float_flag(self) -> None:
        overrides = parse_cli_overrides({"--max-cost-per-agent": "5.50"})
        assert overrides[0].value == 5.50

    def test_parse_bool_flag_no_prefix(self) -> None:
        overrides = parse_cli_overrides({"--no-evolution": ""})
        assert overrides[0].config_key == "evolution_enabled"
        assert overrides[0].value is False

    def test_parse_bool_flag_auto_merge(self) -> None:
        overrides = parse_cli_overrides({"--auto-merge": "true"})
        assert overrides[0].value is True

    def test_unknown_flag_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown CLI flag"):
            parse_cli_overrides({"--nonexistent": "value"})

    def test_invalid_int_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid value"):
            parse_cli_overrides({"--max-agents": "not_a_number"})

    def test_short_flag(self) -> None:
        overrides = parse_cli_overrides({"-n": "3"})
        assert overrides[0].config_key == "max_agents"
        assert overrides[0].value == 3

    def test_multiple_flags(self) -> None:
        overrides = parse_cli_overrides(
            {
                "--max-agents": "4",
                "--model": "opus",
                "--budget": "$10",
            }
        )
        assert len(overrides) == 3


class TestApplyOverrides:
    def test_apply_to_config(self) -> None:
        config = {"max_agents": 6, "model": None}
        overrides = [
            CLIOverride(flag="--max-agents", config_key="max_agents", value=4, raw="4"),
        ]
        result = apply_overrides(config, overrides)
        assert result["max_agents"] == 4
        assert result["model"] is None  # Unchanged.

    def test_does_not_mutate_original(self) -> None:
        config = {"max_agents": 6}
        overrides = [
            CLIOverride(flag="--max-agents", config_key="max_agents", value=4, raw="4"),
        ]
        result = apply_overrides(config, overrides)
        assert config["max_agents"] == 6
        assert result["max_agents"] == 4

    def test_adds_new_keys(self) -> None:
        config = {"max_agents": 6}
        overrides = [
            CLIOverride(flag="--model", config_key="model", value="opus", raw="opus"),
        ]
        result = apply_overrides(config, overrides)
        assert result["model"] == "opus"


class TestCLIOverrideManager:
    def test_parse_and_apply(self) -> None:
        mgr = CLIOverrideManager()
        mgr.parse({"--max-agents": "4", "--model": "opus"})
        config = {"max_agents": 6}
        result = mgr.apply(config)
        assert result["max_agents"] == 4
        assert result["model"] == "opus"

    def test_as_dict(self) -> None:
        mgr = CLIOverrideManager()
        mgr.parse({"--max-agents": "4"})
        d = mgr.as_dict()
        assert d["max_agents"] == 4

    def test_supported_flags_list(self) -> None:
        flags = CLIOverrideManager.supported_flags()
        assert len(flags) > 0
        flag_names = [f["flag"] for f in flags]
        assert "--max-agents" in flag_names
        assert "--budget" in flag_names
