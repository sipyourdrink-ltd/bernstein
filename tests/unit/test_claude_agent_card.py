"""Tests for bernstein.core.claude_agent_card (CLAUDE-015)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.claude_agent_card import (
    AgentCapability,
    AgentCard,
    AgentCardRegistry,
    AgentSkill,
    load_agent_card,
    parse_agent_card,
)


class TestAgentCapability:
    def test_to_dict(self) -> None:
        cap = AgentCapability(name="code_edit", description="Edit code files")
        d = cap.to_dict()
        assert d["name"] == "code_edit"
        assert d["description"] == "Edit code files"


class TestAgentSkill:
    def test_to_dict(self) -> None:
        skill = AgentSkill(id="bash", name="Shell", tags=("cli", "system"))
        d = skill.to_dict()
        assert d["id"] == "bash"
        assert d["tags"] == ["cli", "system"]


class TestAgentCard:
    def test_has_capability(self) -> None:
        card = AgentCard(
            name="test",
            capabilities=[AgentCapability(name="code_edit")],
        )
        assert card.has_capability("code_edit")
        assert not card.has_capability("deploy")

    def test_has_skill(self) -> None:
        card = AgentCard(
            name="test",
            skills=[AgentSkill(id="bash")],
        )
        assert card.has_skill("bash")
        assert not card.has_skill("deploy")

    def test_supports_model_no_restrictions(self) -> None:
        card = AgentCard(name="test")
        assert card.supports_model("opus")
        assert card.supports_model("anything")

    def test_supports_model_with_restrictions(self) -> None:
        card = AgentCard(
            name="test",
            supported_models=["opus", "sonnet"],
        )
        assert card.supports_model("opus")
        assert card.supports_model("sonnet")
        assert not card.supports_model("gpt-4")

    def test_to_dict(self) -> None:
        card = AgentCard(
            name="test",
            version="1.0",
            capabilities=[AgentCapability(name="edit")],
        )
        d = card.to_dict()
        assert d["name"] == "test"
        assert len(d["capabilities"]) == 1


class TestParseAgentCard:
    def test_parse_full_card(self) -> None:
        data = {
            "name": "Claude Code",
            "description": "CLI coding agent",
            "version": "2.0.0",
            "protocol_version": "1.0",
            "capabilities": [
                {"name": "code_edit", "description": "Edit code"},
            ],
            "skills": [
                {"id": "bash", "name": "Shell", "tags": ["cli"]},
            ],
            "supported_models": ["opus", "sonnet"],
            "supported_tools": ["Bash", "Read", "Write"],
        }
        card = parse_agent_card(data)
        assert card.name == "Claude Code"
        assert len(card.capabilities) == 1
        assert len(card.skills) == 1
        assert "opus" in card.supported_models

    def test_parse_minimal_card(self) -> None:
        card = parse_agent_card({"name": "minimal"})
        assert card.name == "minimal"
        assert len(card.capabilities) == 0

    def test_parse_string_capabilities(self) -> None:
        data = {"name": "test", "capabilities": ["code_edit", "deploy"]}
        card = parse_agent_card(data)
        assert len(card.capabilities) == 2
        assert card.capabilities[0].name == "code_edit"

    def test_parse_with_metadata(self) -> None:
        data = {"name": "test", "custom_field": "value"}
        card = parse_agent_card(data)
        assert "custom_field" in card.metadata

    def test_parse_a2a_style_fields(self) -> None:
        data = {
            "name": "test",
            "protocolVersion": "1.0",
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text", "file"],
        }
        card = parse_agent_card(data)
        assert card.protocol_version == "1.0"
        assert "text" in card.input_modes
        assert "file" in card.output_modes


class TestLoadAgentCard:
    def test_load_from_file(self, tmp_path: Path) -> None:
        card_data = {"name": "TestAgent", "version": "1.0"}
        card_path = tmp_path / "agent.json"
        card_path.write_text(json.dumps(card_data))
        card = load_agent_card(card_path)
        assert card is not None
        assert card.name == "TestAgent"

    def test_load_from_directory(self, tmp_path: Path) -> None:
        card_data = {"name": "DirAgent"}
        (tmp_path / "agent.json").write_text(json.dumps(card_data))
        card = load_agent_card(tmp_path)
        assert card is not None
        assert card.name == "DirAgent"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        card = load_agent_card(tmp_path / "nonexistent.json")
        assert card is None

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.json"
        path.write_text("not json")
        card = load_agent_card(path)
        assert card is None


class TestAgentCardRegistry:
    def test_register_and_find_by_capability(self) -> None:
        registry = AgentCardRegistry()
        registry.register(
            AgentCard(
                name="editor",
                capabilities=[AgentCapability(name="code_edit")],
            )
        )
        registry.register(
            AgentCard(
                name="deployer",
                capabilities=[AgentCapability(name="deploy")],
            )
        )
        results = registry.find_by_capability("code_edit")
        assert len(results) == 1
        assert results[0].name == "editor"

    def test_find_by_skill(self) -> None:
        registry = AgentCardRegistry()
        registry.register(
            AgentCard(
                name="shell",
                skills=[AgentSkill(id="bash")],
            )
        )
        results = registry.find_by_skill("bash")
        assert len(results) == 1

    def test_find_by_model(self) -> None:
        registry = AgentCardRegistry()
        registry.register(
            AgentCard(
                name="claude",
                supported_models=["opus", "sonnet"],
            )
        )
        registry.register(
            AgentCard(
                name="openai",
                supported_models=["gpt-4"],
            )
        )
        results = registry.find_by_model("opus")
        assert len(results) == 1
        assert results[0].name == "claude"

    def test_scan_directory(self, tmp_path: Path) -> None:
        # Create two agent cards in subdirs.
        for name in ("agent_a", "agent_b"):
            subdir = tmp_path / name
            subdir.mkdir()
            (subdir / "agent.json").write_text(json.dumps({"name": name}))
        registry = AgentCardRegistry()
        count = registry.scan_directory(tmp_path)
        assert count == 2
        assert len(registry.all_cards()) == 2

    def test_scan_empty_directory(self, tmp_path: Path) -> None:
        registry = AgentCardRegistry()
        count = registry.scan_directory(tmp_path)
        assert count == 0

    def test_scan_nonexistent_directory(self, tmp_path: Path) -> None:
        registry = AgentCardRegistry()
        count = registry.scan_directory(tmp_path / "nope")
        assert count == 0
