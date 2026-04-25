"""``bernstein preview`` — sandboxed dev-server with public tunnel link.

The :mod:`bernstein.core.preview` package stitches together pieces that
already exist in the codebase — :class:`SandboxBackend`, the
``bernstein tunnel`` wrapper, and the security layer's signed-token
issuer — into a single ``bernstein preview {start|stop|list|status}``
flow.

Public API::

    from bernstein.core.preview import (
        AuthMode,
        DiscoveredCommand,
        Preview,
        PreviewManager,
        PreviewState,
        PreviewStore,
        discover_commands,
        ensure_port,
    )
"""

from __future__ import annotations

from bernstein.core.preview.command_discovery import (
    DiscoveredCommand,
    discover_commands,
    list_candidates,
)
from bernstein.core.preview.manager import (
    AuthMode,
    Preview,
    PreviewError,
    PreviewManager,
    PreviewState,
    PreviewStore,
)
from bernstein.core.preview.metrics import (
    record_link_issued,
    record_preview_started,
    record_preview_stopped,
)
from bernstein.core.preview.port_capture import (
    PORT_REGEX_PATTERNS,
    PortNotDetectedError,
    capture_port,
    probe_port,
)
from bernstein.core.preview.token_issuer import PreviewTokenIssuer
from bernstein.core.preview.tunnel_bridge import (
    TunnelBridge,
    TunnelBridgeError,
)

__all__ = [
    "PORT_REGEX_PATTERNS",
    "AuthMode",
    "DiscoveredCommand",
    "PortNotDetectedError",
    "Preview",
    "PreviewError",
    "PreviewManager",
    "PreviewState",
    "PreviewStore",
    "PreviewTokenIssuer",
    "TunnelBridge",
    "TunnelBridgeError",
    "capture_port",
    "discover_commands",
    "ensure_port",
    "list_candidates",
    "probe_port",
    "record_link_issued",
    "record_preview_started",
    "record_preview_stopped",
]


def ensure_port(port: int, timeout_seconds: float = 30.0) -> bool:
    """Probe ``localhost:<port>`` over TCP up to *timeout_seconds*.

    Thin re-export of :func:`probe_port` so callers don't have to import
    the submodule for the most common verification step.

    Args:
        port: Local TCP port the dev server should be bound to.
        timeout_seconds: Wall-clock budget for the probe. Defaults to 30
            seconds, matching the acceptance criteria.

    Returns:
        ``True`` once the port accepts a TCP connection; ``False`` if the
        budget elapsed without a successful connect.
    """
    return probe_port(port, timeout_seconds=timeout_seconds)
