"""CFG-008: Config templates for common use cases."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfigTemplate:
    name: str
    description: str
    config: dict[str, Any]
    tags: tuple[str, ...] = ()

    def to_yaml(self) -> str:
        return yaml.dump(self.config, default_flow_style=False, sort_keys=False)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "tags": list(self.tags), "config": self.config}


_WEB_APP_TEMPLATE = ConfigTemplate(
    name="web-app",
    description="Full-stack web application.",
    config={
        "goal": "Build and maintain a web application",
        "cli": "auto",
        "max_agents": 4,
        "team": ["backend", "frontend", "qa"],
        "merge_strategy": "pr",
        "auto_merge": False,
        "quality_gates": {"enabled": True, "lint": True, "type_check": True, "tests": True},
    },
    tags=("web", "fullstack", "frontend", "backend"),
)
_MICROSERVICES_TEMPLATE = ConfigTemplate(
    name="microservices",
    description="Microservices architecture.",
    config={
        "goal": "Develop and maintain microservices",
        "cli": "auto",
        "max_agents": 8,
        "team": ["backend", "devops", "qa", "security"],
        "merge_strategy": "pr",
        "auto_merge": False,
        "quality_gates": {"enabled": True, "lint": True, "type_check": True, "tests": True},
        "constraints": ["Each service must be independently deployable", "Use API contracts between services"],
    },
    tags=("microservices", "distributed", "api", "devops"),
)
_MONOREPO_TEMPLATE = ConfigTemplate(
    name="monorepo",
    description="Monorepo with multiple packages.",
    config={
        "goal": "Manage a monorepo with multiple packages",
        "cli": "auto",
        "max_agents": 6,
        "team": "auto",
        "merge_strategy": "pr",
        "auto_merge": True,
        "quality_gates": {"enabled": True, "lint": True, "type_check": True, "tests": True},
        "constraints": ["Respect package boundaries", "Run only affected tests"],
    },
    tags=("monorepo", "multi-package", "shared"),
)
_DATA_PIPELINE_TEMPLATE = ConfigTemplate(
    name="data-pipeline",
    description="Data processing pipeline.",
    config={
        "goal": "Build and maintain data processing pipelines",
        "cli": "auto",
        "max_agents": 4,
        "team": ["backend", "ml-engineer", "qa"],
        "merge_strategy": "pr",
        "auto_merge": False,
        "quality_gates": {"enabled": True, "lint": True, "tests": True},
    },
    tags=("data", "pipeline", "etl", "ml"),
)
_LIBRARY_TEMPLATE = ConfigTemplate(
    name="library",
    description="Reusable library or SDK.",
    config={
        "goal": "Develop a reusable library",
        "cli": "auto",
        "max_agents": 3,
        "team": ["backend", "qa", "docs"],
        "merge_strategy": "pr",
        "auto_merge": False,
        "quality_gates": {"enabled": True, "lint": True, "type_check": True, "tests": True},
        "constraints": ["Maintain backward compatibility", "Document all public APIs"],
    },
    tags=("library", "sdk", "package", "api"),
)


@dataclass
class TemplateRegistry:
    templates: dict[str, ConfigTemplate] = field(default_factory=dict[str, ConfigTemplate])

    def register(self, template: ConfigTemplate) -> None:
        self.templates[template.name] = template

    def get(self, name: str) -> ConfigTemplate | None:
        return self.templates.get(name)

    def list_all(self) -> list[ConfigTemplate]:
        return sorted(self.templates.values(), key=lambda t: t.name)

    def search(self, tag: str) -> list[ConfigTemplate]:
        tag_lower = tag.lower()
        return [t for t in self.templates.values() if tag_lower in (x.lower() for x in t.tags)]

    def names(self) -> list[str]:
        return sorted(self.templates.keys())


def default_registry() -> TemplateRegistry:
    registry = TemplateRegistry()
    for t in (
        _WEB_APP_TEMPLATE,
        _MICROSERVICES_TEMPLATE,
        _MONOREPO_TEMPLATE,
        _DATA_PIPELINE_TEMPLATE,
        _LIBRARY_TEMPLATE,
    ):
        registry.register(t)
    return registry
