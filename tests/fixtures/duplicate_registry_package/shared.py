"""The single registry instance ``first.py`` and ``second.py`` both register into."""

from __future__ import annotations

from bernstein.agents.registry import AgentRegistry

REGISTRY = AgentRegistry()

DUPLICATE_ID = "fixture-duplicate-agent"
