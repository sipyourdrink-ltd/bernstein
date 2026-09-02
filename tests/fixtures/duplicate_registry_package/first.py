"""Registers ``DUPLICATE_ID`` the first time. This import always succeeds."""

from __future__ import annotations

from bernstein.core.models import ModelConfig

from bernstein.agents.registry import AgentDefinition
from tests.fixtures.duplicate_registry_package.shared import DUPLICATE_ID, REGISTRY

REGISTRY.register_definition(
    AgentDefinition(
        name=DUPLICATE_ID,
        role="first-registrant",
        model_config=ModelConfig(model="sonnet", effort="normal"),
        version="1.0.0",
    )
)
