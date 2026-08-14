"""Drift guards for the host effects advertised by every MCP tool."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.core.protocols.mcp.tool_tiers import DEPRECATED_TOOL_ALIASES, TOOL_TIERS, ToolTier
from bernstein.mcp.input_validation import get_registry
from bernstein.mcp.server import create_mcp_server

_ROOT = Path(__file__).resolve().parents[3]
_ALLOWED_EFFECTS = frozenset(
    {
        "reads files",
        "writes files",
        "spawns agent processes",
        "makes network requests",
        "none",
    }
)

# A tool's row is (effective tier, effects visible to the caller). Keeping the
# rows explicit makes additions and alias behaviour deliberate review points.
_EXPECTED: dict[str, tuple[ToolTier, frozenset[str]]] = {
    "bernstein_approve": ("standard", frozenset({"makes network requests"})),
    "bernstein_cancel": ("standard", frozenset({"makes network requests"})),
    "bernstein_claim": ("standard", frozenset({"makes network requests"})),
    "bernstein_complete": ("standard", frozenset({"makes network requests"})),
    "bernstein_context": ("standard", frozenset({"reads files", "writes files"})),
    "bernstein_cost": ("core", frozenset({"makes network requests"})),
    "bernstein_create_subtask": (
        "core",
        frozenset({"writes files", "spawns agent processes", "makes network requests"}),
    ),
    "bernstein_health": ("core", frozenset({"none"})),
    "bernstein_post_artifact": ("standard", frozenset({"makes network requests"})),
    "bernstein_post_message": ("standard", frozenset({"makes network requests"})),
    "bernstein_run": (
        "core",
        frozenset({"writes files", "spawns agent processes", "makes network requests"}),
    ),
    "bernstein_run_status": ("core", frozenset({"reads files"})),
    "bernstein_scenario": (
        "all",
        frozenset({"reads files", "writes files", "spawns agent processes", "makes network requests"}),
    ),
    "bernstein_scenario_status": ("all", frozenset({"makes network requests"})),
    "bernstein_scenarios": ("all", frozenset({"reads files"})),
    "bernstein_shutdown_orchestrator": ("standard", frozenset({"writes files"})),
    "bernstein_status": ("core", frozenset({"makes network requests"})),
    "bernstein_stop": ("standard", frozenset({"writes files"})),
    "bernstein_task_capsule": ("standard", frozenset({"reads files", "writes files"})),
    "bernstein_task_handle": ("core", frozenset({"reads files"})),
    "bernstein_tasks": ("core", frozenset({"makes network requests"})),
    "bernstein_update": ("standard", frozenset({"makes network requests"})),
    "bernstein_verify_lineage": ("all", frozenset({"reads files"})),
    "load_skill": ("standard", frozenset({"reads files"})),
    "verify_chain": ("all", frozenset({"reads files"})),
}

_SHARED_WORDING = (
    "Every MCP tool description states whether the call reads files, writes files, "
    "spawns agent processes, or makes network requests. "
    "load_skill only returns file contents and executes nothing."
)


def _effects(description: str) -> frozenset[str]:
    marker = "Host effects: "
    assert marker in description
    labels = frozenset(description.split(marker, 1)[1].split(".", 1)[0].split("; "))
    assert labels
    assert labels <= _ALLOWED_EFFECTS
    assert labels == {"none"} or "none" not in labels
    return labels


def _normalise_whitespace(value: str) -> str:
    return " ".join(value.split())


def test_every_schema_declares_reviewed_host_effects_and_effective_tier() -> None:
    """Enumeration fails when a schema is added without an effect review."""
    schemas = get_registry().schemas
    assert set(schemas) == set(_EXPECTED)

    for name, schema in schemas.items():
        expected_tier, expected_effects = _EXPECTED[name]
        canonical_name = DEPRECATED_TOOL_ALIASES.get(name, name)
        assert TOOL_TIERS[canonical_name] == expected_tier
        description = schema.get("description")
        assert isinstance(description, str) and description.strip()
        assert _effects(description) == expected_effects


@pytest.mark.asyncio
async def test_tools_list_uses_the_schema_host_effect_descriptions() -> None:
    """The audited JSON text, not an older Python docstring, reaches clients."""
    schemas = get_registry().schemas
    mcp = create_mcp_server(tier="all", lineage_enabled=True)
    advertised = {tool.name: tool.description for tool in await mcp.list_tools()}

    assert set(advertised) == set(schemas)
    assert advertised == {name: schemas[name]["description"] for name in advertised}


def test_load_skill_says_it_returns_contents_and_executes_nothing() -> None:
    description = get_registry().schemas["load_skill"]["description"]
    assert "Returns file contents as text; executes nothing." in description


def test_server_docs_and_docker_catalog_share_the_host_effect_wording() -> None:
    docs = (_ROOT / "docs/mcp/server.md").read_text(encoding="utf-8")
    catalog = yaml.safe_load((_ROOT / "packaging/docker-mcp/server.yaml").read_text(encoding="utf-8"))

    assert _SHARED_WORDING in _normalise_whitespace(docs)
    assert _SHARED_WORDING in _normalise_whitespace(catalog["about"]["description"])
