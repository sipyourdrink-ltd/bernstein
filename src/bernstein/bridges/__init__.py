"""Bernstein runtime bridge — adapters for external execution backends."""

from bernstein.bridges.base import (
    AgentState,
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)
from bernstein.bridges.openclaw import OpenClawBridge

__all__ = [
    "AgentState",
    "AgentStatus",
    "BridgeConfig",
    "BridgeError",
    "OpenClawBridge",
    "RuntimeBridge",
    "SpawnRequest",
]
