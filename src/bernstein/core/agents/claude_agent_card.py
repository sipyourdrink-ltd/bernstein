"""CLAUDE-015: Agent.json card parsing for capability discovery.

Parses agent.json card files (A2A protocol) to discover agent
capabilities, supported tools, model preferences, and protocol
versions.  Used for capability-based agent routing and compatibility
checking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default agent.json file name (A2A convention).
AGENT_CARD_FILENAME = "agent.json"


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_LIST_OBJ = "list[object]"


@dataclass(frozen=True, slots=True)
class AgentCapability:
    """A single declared agent capability.

    Attributes:
        name: Capability identifier (e.g. "code_edit", "test_run").
        description: Human-readable description.
        version: Capability version string.
    """

    name: str
    description: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize to a dict."""
        result: dict[str, str] = {"name": self.name}
        if self.description:
            result["description"] = self.description
        if self.version:
            result["version"] = self.version
        return result


@dataclass(frozen=True, slots=True)
class AgentSkill:
    """A skill declared in the agent card.

    Attributes:
        id: Skill identifier.
        name: Human-readable name.
        description: What the skill does.
        tags: Categorization tags.
    """

    id: str
    name: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass
class AgentCard:
    """Parsed agent.json card with capability information.

    Attributes:
        name: Agent display name.
        description: Agent description.
        version: Agent version string.
        protocol_version: A2A protocol version.
        url: Agent endpoint URL (if remote).
        capabilities: Declared capabilities.
        skills: Declared skills.
        supported_models: Models the agent can use.
        supported_tools: Tools the agent supports.
        input_modes: Supported input modes (e.g. "text", "file").
        output_modes: Supported output modes.
        metadata: Additional metadata from the card.
        source_path: Path to the agent.json file (if loaded from disk).
    """

    name: str = ""
    description: str = ""
    version: str = ""
    protocol_version: str = ""
    url: str = ""
    capabilities: list[AgentCapability] = field(default_factory=list[AgentCapability])
    skills: list[AgentSkill] = field(default_factory=list[AgentSkill])
    supported_models: list[str] = field(default_factory=list[str])
    supported_tools: list[str] = field(default_factory=list[str])
    input_modes: list[str] = field(default_factory=list[str])
    output_modes: list[str] = field(default_factory=list[str])
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    source_path: str = ""

    def has_capability(self, name: str) -> bool:
        """Check if the agent declares a specific capability.

        Args:
            name: Capability name to check.

        Returns:
            True if the capability is declared.
        """
        return any(c.name == name for c in self.capabilities)

    def has_skill(self, skill_id: str) -> bool:
        """Check if the agent declares a specific skill.

        Args:
            skill_id: Skill identifier to check.

        Returns:
            True if the skill is declared.
        """
        return any(s.id == skill_id for s in self.skills)

    def supports_model(self, model: str) -> bool:
        """Check if the agent supports a specific model.

        Args:
            model: Model name to check.

        Returns:
            True if supported (or no restrictions declared).
        """
        if not self.supported_models:
            return True  # No restrictions means all models.
        lower = model.lower()
        return any(lower in m.lower() or m.lower() in lower for m in self.supported_models)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "url": self.url,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "skills": [s.to_dict() for s in self.skills],
            "supported_models": self.supported_models,
            "supported_tools": self.supported_tools,
            "input_modes": self.input_modes,
            "output_modes": self.output_modes,
            "metadata": self.metadata,
        }


def _parse_capabilities(raw_caps: object) -> list[AgentCapability]:
    """Parse capabilities from raw agent card data."""
    capabilities: list[AgentCapability] = []
    if not isinstance(raw_caps, list):
        return capabilities
    for cap_item in cast(_CAST_LIST_OBJ, raw_caps):
        if isinstance(cap_item, dict):
            cap_dict = cast("dict[str, object]", cap_item)
            capabilities.append(
                AgentCapability(
                    name=str(cap_dict.get("name", "")),
                    description=str(cap_dict.get("description", "")),
                    version=str(cap_dict.get("version", "")),
                )
            )
        elif isinstance(cap_item, str):
            capabilities.append(AgentCapability(name=cap_item))
    return capabilities


def _parse_skills(raw_skills: object) -> list[AgentSkill]:
    """Parse skills from raw agent card data."""
    skills: list[AgentSkill] = []
    if not isinstance(raw_skills, list):
        return skills
    for skill_item in cast(_CAST_LIST_OBJ, raw_skills):
        if isinstance(skill_item, dict):
            skill_dict = cast("dict[str, object]", skill_item)
            tags_raw: object = skill_dict.get("tags", [])
            tag_tuple = tuple(str(t) for t in cast(_CAST_LIST_OBJ, tags_raw)) if isinstance(tags_raw, list) else ()
            skills.append(
                AgentSkill(
                    id=str(skill_dict.get("id", "")),
                    name=str(skill_dict.get("name", "")),
                    description=str(skill_dict.get("description", "")),
                    tags=tag_tuple,
                )
            )
    return skills


def parse_agent_card(data: dict[str, Any], *, source_path: str = "") -> AgentCard:
    """Parse a raw agent.json dict into an AgentCard.

    Handles multiple schema versions and optional fields gracefully.

    Args:
        data: Parsed JSON dict from agent.json.
        source_path: Path to the source file (for tracking).

    Returns:
        Parsed AgentCard.
    """
    capabilities = _parse_capabilities(data.get("capabilities", []))
    skills = _parse_skills(data.get("skills", []))

    # Parse models.
    raw_models: object = data.get("supported_models", data.get("models", []))
    models: list[str] = [str(m) for m in cast(_CAST_LIST_OBJ, raw_models)] if isinstance(raw_models, list) else []

    # Parse tools.
    raw_tools: object = data.get("supported_tools", data.get("tools", []))
    tools: list[str] = [str(t) for t in cast(_CAST_LIST_OBJ, raw_tools)] if isinstance(raw_tools, list) else []

    # Parse input/output modes.
    raw_input: object = data.get("input_modes", data.get("defaultInputModes", []))
    input_modes: list[str] = [str(m) for m in cast(_CAST_LIST_OBJ, raw_input)] if isinstance(raw_input, list) else []

    raw_output: object = data.get("output_modes", data.get("defaultOutputModes", []))
    output_modes: list[str] = [str(m) for m in cast(_CAST_LIST_OBJ, raw_output)] if isinstance(raw_output, list) else []

    return AgentCard(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        version=str(data.get("version", "")),
        protocol_version=str(data.get("protocol_version", data.get("protocolVersion", ""))),
        url=str(data.get("url", "")),
        capabilities=capabilities,
        skills=skills,
        supported_models=models,
        supported_tools=tools,
        input_modes=input_modes,
        output_modes=output_modes,
        metadata={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
        source_path=source_path,
    )


# Keys we explicitly parse (everything else goes to metadata).
_KNOWN_KEYS = frozenset(
    {
        "name",
        "description",
        "version",
        "protocol_version",
        "protocolVersion",
        "url",
        "capabilities",
        "skills",
        "supported_models",
        "models",
        "supported_tools",
        "tools",
        "input_modes",
        "defaultInputModes",
        "output_modes",
        "defaultOutputModes",
    }
)


def load_agent_card(path: Path) -> AgentCard | None:
    """Load and parse an agent.json file.

    Args:
        path: Path to agent.json (or directory containing it).

    Returns:
        Parsed AgentCard, or None if file not found or unparseable.
    """
    if path.is_dir():
        path = path / AGENT_CARD_FILENAME

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("agent.json at %s is not a JSON object", path)
            return None
        return parse_agent_card(cast("dict[str, Any]", data), source_path=str(path))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load agent card from %s: %s", path, exc)
        return None


@dataclass
class AgentCardRegistry:
    """Registry of discovered agent cards.

    Attributes:
        cards: Mapping from agent name to parsed card.
    """

    cards: dict[str, AgentCard] = field(default_factory=dict[str, AgentCard])

    def register(self, card: AgentCard) -> None:
        """Register an agent card.

        Args:
            card: Agent card to register.
        """
        key = card.name or card.source_path
        self.cards[key] = card

    def find_by_capability(self, capability: str) -> list[AgentCard]:
        """Find agents with a specific capability.

        Args:
            capability: Capability name to search for.

        Returns:
            List of matching agent cards.
        """
        return [c for c in self.cards.values() if c.has_capability(capability)]

    def find_by_skill(self, skill_id: str) -> list[AgentCard]:
        """Find agents with a specific skill.

        Args:
            skill_id: Skill identifier to search for.

        Returns:
            List of matching agent cards.
        """
        return [c for c in self.cards.values() if c.has_skill(skill_id)]

    def find_by_model(self, model: str) -> list[AgentCard]:
        """Find agents that support a specific model.

        Args:
            model: Model name to check.

        Returns:
            List of matching agent cards.
        """
        return [c for c in self.cards.values() if c.supports_model(model)]

    def scan_directory(self, directory: Path) -> int:
        """Scan a directory for agent.json files and register them.

        Args:
            directory: Directory to scan.

        Returns:
            Number of cards discovered.
        """
        if not directory.exists():
            return 0

        count = 0
        for card_path in directory.rglob(AGENT_CARD_FILENAME):
            card = load_agent_card(card_path)
            if card is not None:
                self.register(card)
                count += 1

        logger.info("Discovered %d agent cards in %s", count, directory)
        return count

    def all_cards(self) -> list[AgentCard]:
        """Return all registered cards.

        Returns:
            List of all agent cards.
        """
        return list(self.cards.values())
