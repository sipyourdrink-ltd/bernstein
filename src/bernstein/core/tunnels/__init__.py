"""Tunnel provider abstraction and registry.

This package wraps a handful of local-tunnel binaries behind a single
interface so ``bernstein tunnel`` can pick whichever is installed.
"""

from __future__ import annotations

from bernstein.core.tunnels.process_guard import (
    TerminateReason,
    argv_matches_markers,
    is_signallable_tunnel_pid,
    pid_command_argv,
    terminate_tunnel_process,
    terminate_tunnel_process_with_reason,
)
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
    "TerminateReason",
    "TunnelHandle",
    "TunnelProvider",
    "TunnelRegistry",
    "argv_matches_markers",
    "is_signallable_tunnel_pid",
    "pid_command_argv",
    "terminate_tunnel_process",
    "terminate_tunnel_process_with_reason",
]
