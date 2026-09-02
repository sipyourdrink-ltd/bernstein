"""Registers ``DUPLICATE_ID`` a second time. This import must raise."""

from __future__ import annotations

from bernstein.core.models import ModelConfig

from bernstein.agents.registry import AgentDefinition
from tests.fixtures.duplicate_registry_package.shared import DUPLICATE_ID, REGISTRY

REGISTRY.register_definition(
    AgentDefinition(
        name=DUPLICATE_ID,
        role="second-registrant",
        model_config=ModelConfig(model="sonnet", effort="normal"),
        version="1.0.0",
    )
)
