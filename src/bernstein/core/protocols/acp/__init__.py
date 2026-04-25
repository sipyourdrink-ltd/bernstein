"""Agent Client Protocol (ACP) native bridge.

The Agent Client Protocol — https://agentclientprotocol.org — is an open
JSON-RPC 2.0 specification for IDE -> agent communication.  Editors that
ship native ACP support (Zed and others) can plug any compliant server in
as their backend; this package exposes Bernstein as such a server.

The bridge is a *protocol adapter*, not a re-implementation: ACP
``prompt`` opens a Bernstein task via the existing task store, ``cancel``
walks the standard drain pipeline, ``setMode`` toggles the existing
janitor approval gate, and ``streamUpdate`` notifications tail the
existing streaming-merge utility.  Cost-aware routing, HMAC audit, and
sandbox-backend selection are inherited unchanged.

Submodules:

* :mod:`bernstein.core.protocols.acp.schema` — schema validation for
  every ACP message Bernstein accepts.
* :mod:`bernstein.core.protocols.acp.handlers` — pure handler layer
  mapping ACP requests onto the existing Bernstein primitives.
* :mod:`bernstein.core.protocols.acp.transport` — stdio JSON-RPC and
  HTTP/SSE transports.
* :mod:`bernstein.core.protocols.acp.session` — per-IDE session state
  (mode, working dir, pending permission prompts).
* :mod:`bernstein.core.protocols.acp.metrics` — Prometheus counters and
  gauges exported through the existing observability stack.
* :mod:`bernstein.core.protocols.acp.server` — composition root that
  wires handlers + transport + metrics together.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# Back-compat re-exports for the legacy BeeAI ACP module
# (``bernstein.core.protocols.acp.py``).  When this directory shadows
# that file, Python resolves ``bernstein.core.protocols.acp`` to this
# package; we re-export the legacy public names so downstream imports
# (e.g. ``from bernstein.core.acp import ACPHandler`` via the redirect
# map) keep working.
# ----------------------------------------------------------------------
import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_legacy_path = _Path(__file__).resolve().parent.parent / "acp.py"
if _legacy_path.exists():
    _legacy_name = "bernstein.core.protocols._acp_legacy"
    _spec = _ilu.spec_from_file_location(_legacy_name, str(_legacy_path))
    if _spec is not None and _spec.loader is not None:
        _legacy_module = _ilu.module_from_spec(_spec)
        # Register before exec so dataclass can find the module via
        # ``cls.__module__`` while the class body runs.
        _sys.modules[_legacy_name] = _legacy_module
        _spec.loader.exec_module(_legacy_module)
        ACPHandler = _legacy_module.ACPHandler
        ACPRun = _legacy_module.ACPRun
        ACPRunStatus = _legacy_module.ACPRunStatus

from bernstein.core.protocols.acp.handlers import (  # noqa: E402 — legacy shim runs first
    ACPHandlerRegistry,
    ACPRequestContext,
    PromptResult,
    SessionMode,
)
from bernstein.core.protocols.acp.metrics import (  # noqa: E402
    acp_active_sessions,
    acp_messages_total,
    record_acp_message,
)
from bernstein.core.protocols.acp.schema import (  # noqa: E402
    ACPSchemaError,
    validate_request,
    validate_response,
)
from bernstein.core.protocols.acp.server import (  # noqa: E402
    ACPServer,
    AdapterDescriptor,
    SandboxBackendDescriptor,
    ServerCapabilities,
    build_default_server,
)
from bernstein.core.protocols.acp.session import (  # noqa: E402
    ACPSession,
    ACPSessionStore,
)
from bernstein.core.protocols.acp.transport import (  # noqa: E402
    HttpAcpTransport,
    JsonRpcFraming,
    StdioAcpTransport,
)

__all__ = [
    "ACPHandler",
    "ACPHandlerRegistry",
    "ACPRequestContext",
    "ACPRun",
    "ACPRunStatus",
    "ACPSchemaError",
    "ACPServer",
    "ACPSession",
    "ACPSessionStore",
    "AdapterDescriptor",
    "HttpAcpTransport",
    "JsonRpcFraming",
    "PromptResult",
    "SandboxBackendDescriptor",
    "ServerCapabilities",
    "SessionMode",
    "StdioAcpTransport",
    "acp_active_sessions",
    "acp_messages_total",
    "build_default_server",
    "record_acp_message",
    "validate_request",
    "validate_response",
]
