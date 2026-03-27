"""Dynamic agent registry with YAML-based definitions and hot-reload support."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml

from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

AGENT_DEFINITIONS_DIR = Path(".sdd/agents/definitions")


@dataclass(frozen=True)
class AgentDefinition:
    """Definition of a registerable agent type.

    Args:
        name: Unique agent type identifier.
        role: Specialist role description.
        model_config: Default model configuration for this agent.
        version: Version string for this definition (semver recommended).
        description: Human-readable description of agent capabilities.
        system_prompt_template: Path to system prompt template relative to templates_dir.
        max_concurrent_tasks: Maximum tasks this agent type can handle concurrently.
        metadata: Additional arbitrary configuration.
        schema_version: Schema version for validation.
    """

    name: str
    role: str
    model_config: ModelConfig
    version: str
    description: str = ""
    system_prompt_template: str | None = None
    max_concurrent_tasks: int = 3
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    schema_version: str = "1.0"


@dataclass
class AgentInstance:
    """Runtime instance of a registered agent type."""

    id: str
    definition: AgentDefinition
    created_at: float = field(default_factory=time.time)
    status: str = "idle"
    current_task_ids: list[str] = field(default_factory=list[str])


class SchemaValidationError(Exception):
    """Raised when agent definition schema validation fails."""

    def __init__(self, errors: list[str], definition: dict[str, Any]) -> None:
        self.errors = errors
        self.definition = definition
        error_str = "; ".join(errors)
        super().__init__(f"Schema validation failed: {error_str}")


class AgentRegistry:
    """Dynamic registry for agent types with hot-reload support.

    Supports loading agent definitions from YAML files in .sdd/agents/definitions/.
    Definitions are validated against a schema and can be hot-reloaded.

    Args:
        definitions_dir: Directory containing YAML agent definitions.
        auto_reload: Whether to automatically reload definitions on change.
        reload_interval_s: Seconds between auto-reload checks.
    """

    SCHEMA_VERSION = "1.0"
    REQUIRED_FIELDS: ClassVar[set[str]] = {"name", "role", "model", "version"}
    VALID_MODEL_VALUES: ClassVar[set[str]] = {"opus", "sonnet", "gpt-4.1", "gpt-4", "gemini-pro", "qwen-max"}
    VALID_EFFORT_VALUES: ClassVar[set[str]] = {"max", "high", "normal", "low"}

    def __init__(
        self,
        definitions_dir: Path | None = None,
        auto_reload: bool = False,
        reload_interval_s: int = 30,
    ) -> None:
        self._definitions_dir = definitions_dir or AGENT_DEFINITIONS_DIR
        self._auto_reload = auto_reload
        self._reload_interval_s = reload_interval_s
        self._definitions: dict[str, AgentDefinition] = {}
        self._file_hashes: dict[str, str] = {}
        self._last_reload: float = 0.0
        self._instances: dict[str, AgentInstance] = {}

    @property
    def definitions_dir(self) -> Path:
        """Get the definitions directory path."""
        return self._definitions_dir

    @property
    def definitions(self) -> dict[str, AgentDefinition]:
        """Get all registered agent definitions."""
        if self._auto_reload:
            self._maybe_reload()
        return self._definitions.copy()

    @property
    def instances(self) -> dict[str, AgentInstance]:
        """Get all active agent instances."""
        return self._instances.copy()

    def register_definition(self, definition: AgentDefinition) -> None:
        """Register an agent definition programmatically.

        Args:
            definition: AgentDefinition instance to register.

        Raises:
            ValueError: If definition name already exists.
        """
        if definition.name in self._definitions:
            logger.warning("Overwriting existing agent definition: %s", definition.name)
        self._definitions[definition.name] = definition
        logger.info("Registered agent definition: %s (v%s)", definition.name, definition.version)

    def unregister_definition(self, name: str) -> bool:
        """Unregister an agent definition by name.

        Args:
            name: Name of the definition to remove.

        Returns:
            True if removed, False if not found.
        """
        if name in self._definitions:
            del self._definitions[name]
            logger.info("Unregistered agent definition: %s", name)
            return True
        return False

    def get_definition(self, name: str) -> AgentDefinition | None:
        """Get an agent definition by name.

        Args:
            name: Name of the definition.

        Returns:
            AgentDefinition if found, None otherwise.
        """
        if self._auto_reload:
            self._maybe_reload()
        return self._definitions.get(name)

    def create_instance(self, definition_name: str, instance_id: str) -> AgentInstance | None:
        """Create a runtime instance of a registered agent type.

        Args:
            definition_name: Name of the agent definition to instantiate.
            instance_id: Unique ID for this instance.

        Returns:
            AgentInstance if definition exists, None otherwise.
        """
        definition = self.get_definition(definition_name)
        if definition is None:
            logger.error("Cannot create instance: unknown definition '%s'", definition_name)
            return None

        instance = AgentInstance(id=instance_id, definition=definition)
        self._instances[instance_id] = instance
        logger.info("Created agent instance '%s' of type '%s'", instance_id, definition_name)
        return instance

    def remove_instance(self, instance_id: str) -> bool:
        """Remove an agent instance.

        Args:
            instance_id: ID of the instance to remove.

        Returns:
            True if removed, False if not found.
        """
        if instance_id in self._instances:
            del self._instances[instance_id]
            logger.info("Removed agent instance: %s", instance_id)
            return True
        return False

    def load_definitions(self) -> list[AgentDefinition]:
        """Load all agent definitions from YAML files.

        Returns:
            List of successfully loaded definitions.
        """
        loaded: list[AgentDefinition] = []
        definitions_path = self._definitions_dir

        if not definitions_path.exists():
            logger.debug("Agent definitions directory does not exist: %s", definitions_path)
            return loaded

        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))

        for yaml_file in yaml_files:
            try:
                definition = self._load_yaml_file(yaml_file)
                if definition:
                    self._definitions[definition.name] = definition
                    loaded.append(definition)
                    self._file_hashes[yaml_file.name] = self._compute_file_hash(yaml_file)
            except Exception as exc:
                logger.error("Failed to load agent definition from %s: %s", yaml_file, exc)

        self._last_reload = time.time()
        logger.info("Loaded %d agent definitions from %s", len(loaded), definitions_path)
        return loaded

    def reload_definitions(self) -> tuple[list[AgentDefinition], list[str]]:
        """Force reload of all agent definitions from YAML files.

        Returns:
            Tuple of (newly_loaded_definitions, removed_definition_names).
        """
        definitions_path = self._definitions_dir

        if not definitions_path.exists():
            return [], []

        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))
        current_files = {f.name for f in yaml_files}

        # Remove definitions for deleted files
        removed_files = set(self._file_hashes.keys()) - current_files
        removed_names: list[str] = []
        for filename in removed_files:
            for name in list(self._definitions.keys()):
                defn = self._definitions[name]
                if defn.system_prompt_template and filename in str(defn.system_prompt_template):
                    del self._definitions[name]
                    removed_names.append(name)
            del self._file_hashes[filename]

        # Reload all files
        loaded: list[AgentDefinition] = []
        for yaml_file in yaml_files:
            try:
                definition = self._load_yaml_file(yaml_file)
                if definition:
                    self._definitions[definition.name] = definition
                    loaded.append(definition)
                    self._file_hashes[yaml_file.name] = self._compute_file_hash(yaml_file)
            except Exception as exc:
                logger.error("Failed to reload agent definition from %s: %s", yaml_file, exc)

        self._last_reload = time.time()
        logger.info("Reloaded %d agent definitions", len(loaded))
        return loaded, removed_names

    def _maybe_reload(self) -> None:
        """Check if auto-reload is needed and perform it."""
        if not self._auto_reload:
            return

        if time.time() - self._last_reload < self._reload_interval_s:
            return

        definitions_path = self._definitions_dir
        if not definitions_path.exists():
            return

        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))

        needs_reload = False
        for yaml_file in yaml_files:
            current_hash = self._compute_file_hash(yaml_file)
            stored_hash = self._file_hashes.get(yaml_file.name)
            if stored_hash != current_hash:
                needs_reload = True
                break

        # Check for new or deleted files
        current_files = {f.name for f in yaml_files}
        if current_files != set(self._file_hashes.keys()):
            needs_reload = True

        if needs_reload:
            logger.debug("Auto-reloading agent definitions due to file changes")
            self.reload_definitions()

    def _load_yaml_file(self, yaml_file: Path) -> AgentDefinition | None:
        """Load and validate a single YAML definition file.

        Args:
            yaml_file: Path to the YAML file.

        Returns:
            AgentDefinition if valid, None otherwise.

        Raises:
            SchemaValidationError: If validation fails.
        """
        content = yaml_file.read_text(encoding="utf-8")
        raw_data: object = yaml.safe_load(content)

        if not isinstance(raw_data, dict):
            raise SchemaValidationError(["YAML must contain a mapping (dictionary)"], {})

        data: dict[str, Any] = cast("dict[str, Any]", raw_data)

        self._validate_schema(data, yaml_file)

        model_config = ModelConfig(
            model=str(data["model"]),
            effort=str(data.get("effort", "normal")),
            max_tokens=int(data.get("max_tokens", 200_000)),
        )

        return AgentDefinition(
            name=str(data["name"]),
            role=str(data["role"]),
            model_config=model_config,
            version=str(data["version"]),
            description=str(data.get("description", "")),
            system_prompt_template=data.get("system_prompt_template"),
            max_concurrent_tasks=int(data.get("max_concurrent_tasks", 3)),
            metadata=cast("dict[str, Any]", data.get("metadata", {})),
            schema_version=str(data.get("schema_version", self.SCHEMA_VERSION)),
        )

    def _validate_schema(self, data: dict[str, Any], source: Path) -> None:
        """Validate agent definition against schema.

        Args:
            data: Parsed YAML data dictionary.
            source: Source file path for error messages.

        Raises:
            SchemaValidationError: If validation fails.
        """
        errors: list[str] = []

        # Check required fields
        missing = self.REQUIRED_FIELDS - set(data.keys())
        if missing:
            errors.append(f"Missing required fields: {', '.join(missing)}")

        # Validate field types
        if "name" in data and not isinstance(data["name"], str):
            errors.append("Field 'name' must be a string")

        if "role" in data and not isinstance(data["role"], str):
            errors.append("Field 'role' must be a string")

        if "version" in data and not isinstance(data["version"], str):
            errors.append("Field 'version' must be a string")

        if "model" in data:
            if not isinstance(data["model"], str):
                errors.append("Field 'model' must be a string")
            elif data["model"] not in self.VALID_MODEL_VALUES:
                errors.append(f"Invalid model '{data['model']}'. Valid values: {', '.join(self.VALID_MODEL_VALUES)}")

        if "effort" in data:
            if not isinstance(data["effort"], str):
                errors.append("Field 'effort' must be a string")
            elif data["effort"] not in self.VALID_EFFORT_VALUES:
                errors.append(f"Invalid effort '{data['effort']}'. Valid values: {', '.join(self.VALID_EFFORT_VALUES)}")

        if "max_tokens" in data and not isinstance(data["max_tokens"], int):
            errors.append("Field 'max_tokens' must be an integer")

        if "max_concurrent_tasks" in data and not isinstance(data["max_concurrent_tasks"], int):
            errors.append("Field 'max_concurrent_tasks' must be an integer")

        if "schema_version" in data and not isinstance(data["schema_version"], str):
            errors.append("Field 'schema_version' must be a string")

        if errors:
            raise SchemaValidationError(errors, data)

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of a file for change detection."""
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def list_definitions(self) -> list[str]:
        """List all registered agent definition names."""
        return list(self._definitions.keys())

    def get_instance(self, instance_id: str) -> AgentInstance | None:
        """Get an agent instance by ID."""
        return self._instances.get(instance_id)


# Global registry instance (lazy-initialized)
_registry: AgentRegistry | None = None


def get_registry(
    definitions_dir: Path | None = None,
    auto_reload: bool = False,
) -> AgentRegistry:
    """Get or create the global agent registry.

    Args:
        definitions_dir: Optional custom definitions directory.
        auto_reload: Whether to enable auto-reload.

    Returns:
        AgentRegistry instance.
    """
    global _registry
    if _registry is None:
        _registry = AgentRegistry(
            definitions_dir=definitions_dir,
            auto_reload=auto_reload,
        )
        _registry.load_definitions()
    elif definitions_dir is not None and _registry.definitions_dir != definitions_dir:
        # Reinitialize with new directory
        _registry = AgentRegistry(
            definitions_dir=definitions_dir,
            auto_reload=auto_reload,
        )
        _registry.load_definitions()
    return _registry
