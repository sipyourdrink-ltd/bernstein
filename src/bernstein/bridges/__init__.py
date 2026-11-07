"""Bernstein runtime bridge - adapters for external execution backends."""

from bernstein.bridges.base import (
    AgentState,
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)
from bernstein.bridges.openclaw import OpenClawBridge
from bernstein.bridges.prefect import PrefectBridge

__all__ = [
    "AgentState",
    "AgentStatus",
    "BridgeConfig",
    "BridgeError",
    "OpenClawBridge",
    "PrefectBridge",
    "RuntimeBridge",
    "SpawnRequest",
]
