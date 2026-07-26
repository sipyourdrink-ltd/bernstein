"""Unit tests for the lethal-trifecta capability matrix.

Covers the registry, chain evaluator, default-deny semantics, the YAML
loader, and the spawn-time recording helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.capability_matrix import (
    Capability,
    CapabilityRegistry,
    EnforcementMode,
    LethalTrifectaError,
    ToolCapabilities,
    find_violating_chains,
    record_spawn_capabilities,
)


@pytest.fixture()
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(
        ToolCapabilities(
            tool_name="fs.read_secret",
            capabilities=frozenset({Capability.PRIVATE_DATA}),
        )
    )
    reg.register(
        ToolCapabilities(
            tool_name="github.fetch_issue",
            capabilities=frozenset({Capability.UNTRUSTED_INPUT, Capability.EXTERNAL_COMM}),
        )
    )
    reg.register(
        ToolCapabilities(
            tool_name="github.post_comment",
            capabilities=frozenset({Capability.EXTERNAL_COMM}),
        )
    )
    reg.register(
        ToolCapabilities(
            tool_name="git.commit",
            capabilities=frozenset(),
        )
    )
    return reg


class TestEvaluateChain:
    def test_empty_chain_is_allowed(self, registry: CapabilityRegistry) -> None:
        decision = registry.evaluate_chain([])
        assert decision.allowed is True
        assert decision.triggered == frozenset()

    def test_two_capabilities_is_allowed(self, registry: CapabilityRegistry) -> None:
        decision = registry.evaluate_chain(["fs.read_secret", "git.commit"])
        assert decision.allowed is True
        assert Capability.PRIVATE_DATA in decision.triggered
        assert Capability.UNTRUSTED_INPUT not in decision.triggered

    def test_full_trifecta_is_denied_in_enforce_mode(self, registry: CapabilityRegistry) -> None:
        decision = registry.evaluate_chain(["fs.read_secret", "github.fetch_issue", "github.post_comment"])
        assert decision.allowed is False
        assert decision.reason == CapabilityRegistry.DEFAULT_REASON
        assert decision.triggered == frozenset(Capability)
        assert "fs.read_secret" in decision.offending_tools
        assert "github.fetch_issue" in decision.offending_tools

    def test_warn_mode_allows_but_marks_reason(self, registry: CapabilityRegistry) -> None:
        registry.mode = EnforcementMode.WARN
        decision = registry.evaluate_chain(["fs.read_secret", "github.fetch_issue", "github.post_comment"])
        assert decision.allowed is True
        assert "warn-only" in decision.reason
        assert decision.triggered == frozenset(Capability)

    def test_off_mode_disables_check(self, registry: CapabilityRegistry) -> None:
        registry.mode = EnforcementMode.OFF
        decision = registry.evaluate_chain(["fs.read_secret", "github.fetch_issue", "github.post_comment"])
        assert decision.allowed is True
        assert "enforcement off" in decision.reason


class TestUnknownTools:
    def test_unknown_tool_defaults_to_all_capabilities(self) -> None:
        reg = CapabilityRegistry()
        decision = reg.evaluate_chain(["mystery.tool"])
        assert decision.allowed is False
        assert decision.unknown_tools == ("mystery.tool",)
        assert decision.triggered == frozenset(Capability)
        assert "unknown tool" in decision.reason

    def test_unknown_tool_in_warn_mode_still_flags(self) -> None:
        reg = CapabilityRegistry(mode=EnforcementMode.WARN)
        decision = reg.evaluate_chain(["mystery.tool"])
        assert decision.allowed is True
        assert "unknown tool" in decision.reason
        assert decision.unknown_tools == ("mystery.tool",)

    def test_partial_unknown_with_declared_tools(self, registry: CapabilityRegistry) -> None:
        decision = registry.evaluate_chain(["fs.read_secret", "mystery.tool"])
        assert decision.allowed is False
        assert "mystery.tool" in decision.unknown_tools
        assert decision.triggered == frozenset(Capability)


class TestYAMLLoader:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        directory = tmp_path / "capabilities"
        directory.mkdir()
        (directory / "tools.yaml").write_text(
            "tools:\n"
            "  - name: x.read\n"
            "    capabilities: [private_data]\n"
            "  - name: x.fetch\n"
            "    capabilities: [untrusted_input, external_comm]\n",
            encoding="utf-8",
        )
        reg = CapabilityRegistry.from_directory(directory)
        assert "x.read" in reg.tools
        assert reg.tools["x.read"].capabilities == frozenset({Capability.PRIVATE_DATA})
        assert reg.tools["x.fetch"].capabilities == frozenset({Capability.UNTRUSTED_INPUT, Capability.EXTERNAL_COMM})

    def test_unknown_tokens_are_dropped_safely(self, tmp_path: Path) -> None:
        directory = tmp_path / "capabilities"
        directory.mkdir()
        (directory / "tools.yaml").write_text(
            "tools:\n  - name: y.weird\n    capabilities: [private_data, nonsense]\n",
            encoding="utf-8",
        )
        reg = CapabilityRegistry.from_directory(directory)
        assert reg.tools["y.weird"].capabilities == frozenset({Capability.PRIVATE_DATA})

    def test_missing_directory_returns_empty_registry(self, tmp_path: Path) -> None:
        reg = CapabilityRegistry.from_directory(tmp_path / "missing")
        assert reg.tools == {}


class TestRecordSpawn:
    def test_records_manifest_for_allowed_chain(self, tmp_path: Path, registry: CapabilityRegistry) -> None:
        decision = record_spawn_capabilities(
            tmp_path,
            "agent-1",
            "backend",
            ["fs.read_secret", "git.commit"],
            registry=registry,
        )
        assert decision.allowed is True
        manifest_path = tmp_path / ".sdd" / "runtime" / "spawn_capabilities" / "agent-1.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["allowed"] is True
        assert manifest["agent_id"] == "agent-1"
        assert manifest["role"] == "backend"

    def test_raises_lethal_trifecta_for_full_chain(self, tmp_path: Path, registry: CapabilityRegistry) -> None:
        with pytest.raises(LethalTrifectaError) as excinfo:
            record_spawn_capabilities(
                tmp_path,
                "agent-2",
                "backend",
                ["fs.read_secret", "github.fetch_issue", "github.post_comment"],
                registry=registry,
            )
        assert "lethal trifecta" in str(excinfo.value)
        manifest_path = tmp_path / ".sdd" / "runtime" / "spawn_capabilities" / "agent-2.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["allowed"] is False
        assert manifest["reason"] == CapabilityRegistry.DEFAULT_REASON


class TestFindViolatingChains:
    def test_returns_only_violating_chains(self, registry: CapabilityRegistry) -> None:
        chains: list[list[str]] = [
            ["fs.read_secret", "git.commit"],
            ["fs.read_secret", "github.fetch_issue", "github.post_comment"],
        ]
        violations = find_violating_chains(registry, chains)
        assert len(violations) == 1
        assert violations[0].triggered == frozenset(Capability)


class TestSpawnTimeRefusal:
    """Sanity check that a declared trifecta chain is denied at spawn time."""

    def test_full_trifecta_via_declared_tools_is_denied(self, tmp_path: Path, registry: CapabilityRegistry) -> None:
        with pytest.raises(LethalTrifectaError):
            record_spawn_capabilities(
                tmp_path,
                "agent-x",
                "backend",
                ["fs.read_secret", "github.fetch_issue", "github.post_comment"],
                registry=registry,
            )


class TestBundledTemplates:
    """Sanity-check that the bundled YAML files load cleanly."""

    def test_default_registry_covers_all_adapters(self) -> None:
        reg = CapabilityRegistry.load_default()
        adapters = [
            "aider",
            "amp",
            "claude",
            "codex",
            "cody",
            "continue_dev",
            "cursor",
            "gemini",
            "generic",
            "goose",
            "iac",
            "kilo",
            "kiro",
            "ollama",
            "opencode",
            "qwen",
        ]
        for name in adapters:
            assert f"adapter.{name}" in reg.tools, f"adapter.{name} missing from bundled YAML"

    def test_default_registry_covers_built_in_mcp_tools(self) -> None:
        reg = CapabilityRegistry.load_default()
        for tool in (
            "mcp.bernstein_run",
            "mcp.bernstein_status",
            "mcp.bernstein_shutdown_orchestrator",
            "mcp.bernstein_cancel",
            "mcp.bernstein_approve",
            "mcp.load_skill",
            # Deprecated aliases stay registered while they stay callable.
            "mcp.bernstein_health",
            "mcp.bernstein_tasks",
            "mcp.bernstein_cost",
            "mcp.bernstein_stop",
            "mcp.bernstein_create_subtask",
        ):
            assert tool in reg.tools, f"{tool} missing from bundled YAML"
