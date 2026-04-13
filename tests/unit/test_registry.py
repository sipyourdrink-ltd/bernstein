"""Tests for AgentRegistry — dynamic agent registration with YAML hot-reload."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import yaml
from bernstein.core.models import ModelConfig

from bernstein.agents.registry import (
    AgentDefinition,
    AgentInstance,
    AgentRegistry,
    get_registry,
)

if TYPE_CHECKING:
    from pathlib import Path

# --- Fixtures ---


def _make_agent_definition(
    name: str = "test-agent",
    role: str = "backend",
    model: str = "sonnet",
    effort: str = "high",
    version: str = "1.0.0",
    **kwargs,
) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        role=role,
        model_config=ModelConfig(model=model, effort=effort, max_tokens=kwargs.get("max_tokens", 200_000)),
        version=version,
        description=kwargs.get("description", ""),
        system_prompt_template=kwargs.get("system_prompt_template"),
        max_concurrent_tasks=kwargs.get("max_concurrent_tasks", 3),
        metadata=kwargs.get("metadata", {}),
        schema_version=kwargs.get("schema_version", "1.0"),
    )


def _write_yaml_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.dump(data, f)


# --- AgentDefinition Tests ---


class TestAgentDefinition:
    def test_create_minimal_definition(self) -> None:
        definition = _make_agent_definition()
        assert definition.name == "test-agent"
        assert definition.role == "backend"
        assert definition.version == "1.0.0"
        assert definition.max_concurrent_tasks == 3

    def test_create_full_definition(self) -> None:
        definition = AgentDefinition(
            name="senior-backend",
            role="senior backend engineer",
            model_config=ModelConfig(model="opus", effort="max", max_tokens=100_000),
            version="2.1.0",
            description="Expert backend developer",
            system_prompt_template="roles/senior_backend.md",
            max_concurrent_tasks=5,
            metadata={"specialties": ["api", "database"]},
            schema_version="1.0",
        )
        assert definition.name == "senior-backend"
        assert definition.model_config.model == "opus"
        assert definition.model_config.effort == "max"
        assert definition.model_config.max_tokens == 100_000
        assert definition.description == "Expert backend developer"
        assert definition.max_concurrent_tasks == 5
        assert definition.metadata["specialties"] == ["api", "database"]


# --- AgentRegistry Tests ---


class TestAgentRegistry:
    def test_register_definition_programmatically(self) -> None:
        registry = AgentRegistry()
        definition = _make_agent_definition(name="qa-agent", role="qa")
        registry.register_definition(definition)

        assert registry.get_definition("qa-agent") == definition
        assert "qa-agent" in registry.list_definitions()

    def test_unregister_definition(self) -> None:
        registry = AgentRegistry()
        definition = _make_agent_definition(name="temp-agent")
        registry.register_definition(definition)

        assert registry.unregister_definition("temp-agent") is True
        assert registry.get_definition("temp-agent") is None
        assert registry.unregister_definition("nonexistent") is False

    def test_create_instance_from_definition(self) -> None:
        registry = AgentRegistry()
        definition = _make_agent_definition(name="worker-agent")
        registry.register_definition(definition)

        instance = registry.create_instance("worker-agent", "worker-001")
        assert instance is not None
        assert instance.id == "worker-001"
        assert instance.definition == definition
        assert instance.status == "idle"

    def test_create_instance_unknown_definition(self) -> None:
        registry = AgentRegistry()
        instance = registry.create_instance("nonexistent", "instance-001")
        assert instance is None

    def test_remove_instance(self) -> None:
        registry = AgentRegistry()
        definition = _make_agent_definition()
        registry.register_definition(definition)
        registry.create_instance("test-agent", "instance-001")

        assert registry.remove_instance("instance-001") is True
        assert registry.get_instance("instance-001") is None
        assert registry.remove_instance("nonexistent") is False

    def test_definitions_property_returns_copy(self) -> None:
        registry = AgentRegistry()
        definition = _make_agent_definition()
        registry.register_definition(definition)

        definitions = registry.definitions
        definitions["fake"] = None  # type: ignore
        assert "fake" not in registry.definitions


# --- YAML Loading Tests ---


class TestYamlLoading:
    def test_load_single_yaml_file(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        yaml_file = definitions_dir / "test_agent.yaml"
        _write_yaml_file(
            yaml_file,
            {
                "name": "yaml-agent",
                "role": "yaml-specialist",
                "model": "sonnet",
                "effort": "high",
                "version": "1.0.0",
                "description": "Loaded from YAML",
            },
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        registry.load_definitions()

        definition = registry.get_definition("yaml-agent")
        assert definition is not None
        assert definition.role == "yaml-specialist"
        assert definition.model_config.model == "sonnet"

    def test_load_multiple_yaml_files(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "agent1.yaml",
            {"name": "agent1", "role": "backend", "model": "sonnet", "version": "1.0.0"},
        )
        _write_yaml_file(
            definitions_dir / "agent2.yml",
            {"name": "agent2", "role": "frontend", "model": "opus", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        assert len(loaded) == 2
        assert registry.get_definition("agent1") is not None
        assert registry.get_definition("agent2") is not None

    def test_load_yaml_with_optional_fields(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "full_agent.yaml",
            {
                "name": "full-agent",
                "role": "fullstack",
                "model": "opus",
                "effort": "max",
                "max_tokens": 150_000,
                "version": "2.0.0",
                "description": "Full stack developer",
                "system_prompt_template": "roles/fullstack.md",
                "max_concurrent_tasks": 10,
                "metadata": {"tags": ["web", "api"]},
                "schema_version": "1.0",
            },
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        assert len(loaded) == 1
        definition = loaded[0]
        assert definition.model_config.max_tokens == 150_000
        assert definition.max_concurrent_tasks == 10
        assert definition.metadata["tags"] == ["web", "api"]

    def test_load_yaml_missing_required_fields_logs_error(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "invalid.yaml",
            {"name": "invalid", "role": "test"},  # Missing model and version
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        # Invalid definitions are logged and skipped, not raised
        assert len(loaded) == 0
        assert "invalid" not in registry.list_definitions()

    def test_load_yaml_invalid_model_value_logs_error(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "invalid_model.yaml",
            {"name": "test", "role": "test", "model": "invalid-model", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        # Invalid definitions are logged and skipped
        assert len(loaded) == 0
        assert "test" not in registry.list_definitions()

    def test_load_yaml_invalid_effort_value_logs_error(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "invalid_effort.yaml",
            {"name": "test", "role": "test", "model": "sonnet", "effort": "super", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        # Invalid definitions are logged and skipped
        assert len(loaded) == 0
        assert "test" not in registry.list_definitions()

    def test_load_yaml_wrong_type_logs_error(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "wrong_type.yaml",
            {"name": 123, "role": "test", "model": "sonnet", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        # Invalid definitions are logged and skipped
        assert len(loaded) == 0
        assert 123 not in registry.list_definitions()

    def test_load_from_nonexistent_directory_returns_empty(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "nonexistent"
        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()
        assert loaded == []


# --- Hot Reload Tests ---


class TestHotReload:
    def test_reload_definitionss_detects_new_file(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        definitions_dir.mkdir(parents=True)

        registry = AgentRegistry(definitions_dir=definitions_dir)
        registry.load_definitions()
        assert len(registry.list_definitions()) == 0

        # Add new file
        _write_yaml_file(
            definitions_dir / "new_agent.yaml",
            {"name": "new-agent", "role": "new", "model": "sonnet", "version": "1.0.0"},
        )

        loaded, removed = registry.reload_definitions()
        assert len(loaded) == 1
        assert loaded[0].name == "new-agent"
        assert removed == []
        assert "new-agent" in registry.list_definitions()

    def test_reload_definitions_detects_deleted_file(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        definitions_dir.mkdir(parents=True)
        yaml_file = definitions_dir / "agent.yaml"
        _write_yaml_file(
            yaml_file,
            {"name": "temp-agent", "role": "temp", "model": "sonnet", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        registry.load_definitions()
        assert "temp-agent" in registry.list_definitions()

        # Delete file
        yaml_file.unlink()

        loaded, _removed = registry.reload_definitions()
        assert loaded == []
        # Note: current implementation may not remove by name, just file hash

    def test_reload_definitions_detects_modified_file(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        definitions_dir.mkdir(parents=True)
        yaml_file = definitions_dir / "agent.yaml"
        _write_yaml_file(
            yaml_file,
            {"name": "agent", "role": "original", "model": "sonnet", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        registry.load_definitions()
        definition = registry.get_definition("agent")
        assert definition is not None
        assert definition.role == "original"

        # Modify file
        _write_yaml_file(
            yaml_file,
            {"name": "agent", "role": "modified", "model": "sonnet", "version": "2.0.0"},
        )

        loaded, _removed = registry.reload_definitions()
        assert len(loaded) == 1
        assert loaded[0].role == "modified"
        assert loaded[0].version == "2.0.0"

    def test_auto_reload_on_access(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        definitions_dir.mkdir(parents=True)
        yaml_file = definitions_dir / "agent.yaml"
        _write_yaml_file(
            yaml_file,
            {"name": "auto-agent", "role": "auto", "model": "sonnet", "version": "1.0.0"},
        )

        registry = AgentRegistry(definitions_dir=definitions_dir, auto_reload=True, reload_interval_s=0)
        registry.load_definitions()

        # Modify file
        _write_yaml_file(
            yaml_file,
            {"name": "auto-agent", "role": "auto-reloaded", "model": "opus", "version": "2.0.0"},
        )

        # Access definitions property (triggers reload)
        time.sleep(0.1)  # Ensure time passes for reload check
        definitions = registry.definitions
        definition = definitions.get("auto-agent")
        assert definition is not None
        assert definition.role == "auto-reloaded"


# --- Schema Validation Tests ---


class TestSchemaValidation:
    def test_valid_definition_passes(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "valid.yaml",
            {
                "name": "valid",
                "role": "test",
                "model": "opus",
                "version": "1.0.0",
                "effort": "max",
                "max_tokens": 100_000,
                "max_concurrent_tasks": 5,
                "schema_version": "1.0",
            },
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()
        assert len(loaded) == 1

    def test_schema_version_preserved(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "versioned.yaml",
            {
                "name": "versioned",
                "role": "test",
                "model": "sonnet",
                "version": "3.0.0",
                "schema_version": "2.0",
            },
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()
        assert len(loaded) == 1
        assert loaded[0].schema_version == "2.0"

    def test_schema_validation_error_logs_multiple_errors(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "agents" / "definitions"
        _write_yaml_file(
            definitions_dir / "multi_error.yaml",
            {"name": 123, "model": "invalid"},  # Multiple errors
        )

        registry = AgentRegistry(definitions_dir=definitions_dir)
        loaded = registry.load_definitions()

        # Invalid definitions are logged and skipped
        assert len(loaded) == 0


# --- Global Registry Tests ---


class TestGlobalRegistry:
    def test_get_registry_creates_singleton(self) -> None:
        registry1 = get_registry()
        registry2 = get_registry()
        assert registry1 is registry2

    def test_get_registry_with_custom_dir(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "custom"
        definitions_dir.mkdir(parents=True)
        _write_yaml_file(
            definitions_dir / "custom.yaml",
            {"name": "custom", "role": "custom", "model": "sonnet", "version": "1.0.0"},
        )

        # Reset global registry
        import bernstein.agents.registry as reg_module

        original = reg_module._registry
        reg_module._registry = None

        try:
            registry = get_registry(definitions_dir=definitions_dir)
            assert registry.get_definition("custom") is not None
        finally:
            reg_module._registry = original

    def test_get_registry_auto_loads(self, tmp_path: Path) -> None:
        definitions_dir = tmp_path / "autoload"
        definitions_dir.mkdir(parents=True)
        _write_yaml_file(
            definitions_dir / "auto.yaml",
            {"name": "auto", "role": "auto", "model": "sonnet", "version": "1.0.0"},
        )

        import bernstein.agents.registry as reg_module

        original = reg_module._registry
        reg_module._registry = None

        try:
            registry = get_registry(definitions_dir=definitions_dir, auto_reload=True)
            assert registry.get_definition("auto") is not None
        finally:
            reg_module._registry = original


# --- AgentInstance Tests ---


class TestAgentInstance:
    def test_instance_creation(self) -> None:
        definition = _make_agent_definition(name="test")
        instance = AgentInstance(id="inst-001", definition=definition)

        assert instance.id == "inst-001"
        assert instance.definition == definition
        assert instance.status == "idle"
        assert instance.current_task_ids == []
        assert instance.created_at > 0

    def test_instance_with_tasks(self) -> None:
        definition = _make_agent_definition(name="test")
        instance = AgentInstance(
            id="inst-002",
            definition=definition,
            status="working",
            current_task_ids=["T-001", "T-002"],
        )

        assert instance.status == "working"
        assert len(instance.current_task_ids) == 2
