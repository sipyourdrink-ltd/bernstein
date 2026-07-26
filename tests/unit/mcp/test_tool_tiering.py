"""Tests for tiered MCP tool exposure (issue #1625).

Covers:

  * the tier model in ``bernstein.core.protocols.mcp.tool_tiers``;
  * tier filtering applied by ``create_mcp_server`` (only the active tier's
    tools are advertised and callable);
  * the default tier;
  * the ``BERNSTEIN_MCP_TOOL_TIER`` env-var knob and the explicit override;
  * the ``bernstein mcp tools`` audit command.
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.mcp_cmd import mcp_server
from bernstein.core.protocols.mcp.tool_tiers import (
    DEFAULT_TIER,
    TIER_ENV_VAR,
    TIER_ORDER,
    TOOL_TIERS,
    normalize_tier,
    resolve_active_tier,
    tier_audit,
    tier_rank,
    tool_in_tier,
    tools_for_tier,
)
from bernstein.mcp.server import create_mcp_server


def _advertised(tier: str | None) -> set[str]:
    """Return the set of tool names a server advertises for ``tier``.

    Deprecated aliases (#3087) are registered (callable) but hidden from the
    wire ``tools/list``; filter them the same way the list handler does.
    """
    from bernstein.core.protocols.mcp.tool_tiers import DEPRECATED_TOOL_ALIASES

    mcp = create_mcp_server(tier=tier)
    return {tool.name for tool in asyncio.run(mcp.list_tools())} - set(DEPRECATED_TOOL_ALIASES)


# --------------------------------------------------------------------------
# Tier model
# --------------------------------------------------------------------------


def test_tiers_are_three_named_values() -> None:
    assert TIER_ORDER == ("core", "standard", "all")


def test_default_tier_is_standard() -> None:
    assert DEFAULT_TIER == "standard"


def test_tier_rank_is_monotonic() -> None:
    assert tier_rank("core") < tier_rank("standard") < tier_rank("all")


def test_every_shipped_tool_has_a_valid_tier() -> None:
    assert TOOL_TIERS, "tool tier map must not be empty"
    assert all(t in TIER_ORDER for t in TOOL_TIERS.values())


def test_tiers_are_cumulative() -> None:
    core = set(tools_for_tier("core"))
    standard = set(tools_for_tier("standard"))
    every = set(tools_for_tier("all"))
    assert core <= standard <= every


def test_core_is_a_strict_subset_of_all() -> None:
    assert set(tools_for_tier("core")) < set(tools_for_tier("all"))


def test_tool_in_tier_respects_declared_rank() -> None:
    # bernstein_cancel is a standard tool: out of core, present from standard.
    assert not tool_in_tier("bernstein_cancel", "core")
    assert tool_in_tier("bernstein_cancel", "standard")
    assert tool_in_tier("bernstein_cancel", "all")


def test_unannotated_tool_defaults_to_all_only() -> None:
    assert not tool_in_tier("some_future_tool", "core")
    assert not tool_in_tier("some_future_tool", "standard")
    assert tool_in_tier("some_future_tool", "all")


# --------------------------------------------------------------------------
# Tier resolution / knob
# --------------------------------------------------------------------------


def test_normalize_tier_blank_falls_back_to_default() -> None:
    assert normalize_tier(None) == DEFAULT_TIER
    assert normalize_tier("") == DEFAULT_TIER
    assert normalize_tier("   ") == DEFAULT_TIER


def test_normalize_tier_is_case_insensitive() -> None:
    assert normalize_tier("CORE") == "core"
    assert normalize_tier(" All ") == "all"


def test_normalize_tier_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown MCP tool tier"):
        normalize_tier("turbo")


def test_resolve_active_tier_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIER_ENV_VAR, "core")
    assert resolve_active_tier() == "core"


def test_resolve_active_tier_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIER_ENV_VAR, raising=False)
    assert resolve_active_tier() == DEFAULT_TIER


def test_explicit_override_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIER_ENV_VAR, "core")
    assert resolve_active_tier("all") == "all"


# --------------------------------------------------------------------------
# Server-level filtering: each tier exposes the correct subset
# --------------------------------------------------------------------------


def test_core_tier_exposes_only_core_tools() -> None:
    advertised = _advertised("core")
    assert advertised == set(tools_for_tier("core"))
    assert "bernstein_run" in advertised
    assert "bernstein_cancel" not in advertised
    assert "bernstein_scenario" not in advertised


def test_standard_tier_adds_mutation_and_skill_tools() -> None:
    advertised = _advertised("standard")
    assert "bernstein_cancel" in advertised
    assert "load_skill" in advertised
    assert "bernstein_scenario" not in advertised


def test_context_capsule_tool_is_in_the_standard_tier() -> None:
    # bernstein_task_capsule lets a worker that lost its session reload its
    # own signed assignment capsule, so it must be reachable at the default
    # tier rather than only when an operator sets the tier to ``all``.
    assert TOOL_TIERS["bernstein_task_capsule"] == "standard"
    assert "bernstein_task_capsule" in tools_for_tier("standard")
    assert "bernstein_task_capsule" in tools_for_tier("all")
    assert "bernstein_task_capsule" not in tools_for_tier("core")


def test_default_tier_advertises_the_context_capsule_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIER_ENV_VAR, raising=False)
    assert "bernstein_task_capsule" in _advertised(None)


def test_all_tier_exposes_scenario_bridge() -> None:
    advertised = _advertised("all")
    assert "bernstein_scenario" in advertised


def test_default_server_uses_standard_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIER_ENV_VAR, raising=False)
    assert _advertised(None) == set(tools_for_tier("standard"))


def test_env_var_drives_server_without_explicit_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIER_ENV_VAR, "core")
    assert _advertised(None) == set(tools_for_tier("core"))


def test_out_of_tier_tool_is_not_callable() -> None:
    mcp = create_mcp_server(tier="core")
    # bernstein_cancel is a standard tool, so calling it under core must fail.
    with pytest.raises(Exception):  # noqa: B017 - any error proves it is unroutable
        asyncio.run(mcp.call_tool("bernstein_cancel", {"task_id": "t-1"}))


def test_lineage_verify_tool_only_in_all_tier(tmp_path: object) -> None:
    # bernstein_verify_lineage is an 'all'-tier tool; even with lineage
    # enabled it must not be advertised under core.
    mcp = create_mcp_server(tier="core", lineage_enabled=True, lineage_root=tmp_path)  # type: ignore[arg-type]
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "bernstein_verify_lineage" not in names
    assert "verify_chain" not in names

    mcp_all = create_mcp_server(tier="all", lineage_enabled=True, lineage_root=tmp_path)  # type: ignore[arg-type]
    names_all = {tool.name for tool in asyncio.run(mcp_all.list_tools())}
    assert "bernstein_verify_lineage" in names_all


# --------------------------------------------------------------------------
# Audit command
# --------------------------------------------------------------------------


def test_tier_audit_returns_all_tiers() -> None:
    audit = tier_audit()
    assert set(audit) == set(TIER_ORDER)
    assert audit["core"] == tools_for_tier("core")


def test_mcp_tools_command_lists_every_tier() -> None:
    runner = CliRunner()
    result = runner.invoke(mcp_server, ["tools"])
    assert result.exit_code == 0, result.output
    assert "core" in result.output
    assert "standard" in result.output
    assert "all" in result.output
    assert "bernstein_run" in result.output


def test_mcp_tools_command_single_tier_json() -> None:
    import json

    runner = CliRunner()
    result = runner.invoke(mcp_server, ["tools", "--tier", "core", "--json-output"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"core"}
    assert payload["core"] == tools_for_tier("core")


def test_mcp_tools_command_rejects_unknown_tier() -> None:
    runner = CliRunner()
    result = runner.invoke(mcp_server, ["tools", "--tier", "turbo"])
    assert result.exit_code != 0
