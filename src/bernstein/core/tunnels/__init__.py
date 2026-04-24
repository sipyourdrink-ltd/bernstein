"""Tunnel provider abstraction and registry.

This package wraps a handful of local-tunnel binaries behind a single
interface so ``bernstein tunnel`` can pick whichever is installed.
"""

from __future__ import annotations

from bernstein.core.tunnels.protocol import (
    Detected,
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)
from bernstein.core.tunnels.registry import TunnelRegistry

__all__ = [
    "Detected",
    "ProviderNotAvailable",
    "TunnelHandle",
    "TunnelProvider",
    "TunnelRegistry",
]
