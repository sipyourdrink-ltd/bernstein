"""Agent Client Protocol (ACP) native bridge.

Submodules:

* :mod:`bernstein.core.protocols.acp.schema` - schema validation for ACP v1 and legacy frames.
* :mod:`bernstein.core.protocols.acp.handlers` - request/notification dispatch and capabilities.
* :mod:`bernstein.core.protocols.acp.projection` - deterministic ``EventJournal`` -> ``session/update`` projection.
* :mod:`bernstein.core.protocols.acp.runner` - embedded workspace orchestrator and approval-gate bridge.
* :mod:`bernstein.core.protocols.acp.transport` - bidirectional stdio JSON-RPC and HTTP/SSE transports.
* :mod:`bernstein.core.protocols.acp.session` - persistent per-IDE session state.
* :mod:`bernstein.core.protocols.acp.metrics` - Prometheus counters and gauges.
* :mod:`bernstein.core.protocols.acp.server` - composition root wiring runner, handlers, and transports.
"""

from __future__ import annotations

import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_legacy_path = _Path(__file__).resolve().parent.parent / "acp.py"
if _legacy_path.exists():
    _legacy_name = "bernstein.core.protocols._acp_legacy"
    _spec = _ilu.spec_from_file_location(_legacy_name, str(_legacy_path))
    if _spec is not None and _spec.loader is not None:
        _legacy_module = _ilu.module_from_spec(_spec)
        _sys.modules[_legacy_name] = _legacy_module
        _spec.loader.exec_module(_legacy_module)
        ACPHandler = _legacy_module.ACPHandler
        ACPRun = _legacy_module.ACPRun
        ACPRunStatus = _legacy_module.ACPRunStatus

from bernstein.core.protocols.acp.client import (  # noqa: E402
    ACP_EVENT_TYPE,
    AcpDivergence,
    ACPEvent,
    ACPEventJournalSink,
    AcpLifecycleResult,
    canonical_frame_bytes,
    compare_acp_journals,
    drive_acp_lifecycle,
    frame_content_hash,
    is_terminal_frame,
    parse_inbound_frame,
    replay_acp_content_hashes,
)
from bernstein.core.protocols.acp.handlers import (  # noqa: E402
    ACPHandlerRegistry,
    ACPRequestContext,
    AuthHandler,
    ClientRequester,
    PromptResult,
    PromptRunner,
    PromptRunOutcome,
    SessionLoader,
    SessionMode,
    extract_prompt_text,
)
from bernstein.core.protocols.acp.metrics import (  # noqa: E402
    acp_active_sessions,
    acp_messages_total,
    record_acp_message,
)
from bernstein.core.protocols.acp.projection import (  # noqa: E402
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    VALID_ACP_TOOL_KINDS,
    JournalACPProjector,
    infer_tool_kind,
    project_journal_to_acp_updates,
)
from bernstein.core.protocols.acp.runner import (  # noqa: E402
    ACPApprovalGateBridge,
    EmbeddedOrchestratorRunner,
    resolve_embedded_adapter_name,
)
from bernstein.core.protocols.acp.schema import (  # noqa: E402
    ACP_LEGACY_PROTOCOL_VERSION,
    ACP_PROTOCOL_VERSION,
    ACP_V1_PROTOCOL_VERSION,
    AUTH_REQUIRED,
    REQUEST_CANCELLED,
    RESOURCE_NOT_FOUND,
    ACPSchemaError,
    make_request,
    validate_request,
    validate_response,
)
from bernstein.core.protocols.acp.server import (  # noqa: E402
    DEFAULT_TERMINAL_AUTH_METHOD,
    ACPServer,
    AdapterDescriptor,
    SandboxBackendDescriptor,
    ServerCapabilities,
    build_default_server,
)
from bernstein.core.protocols.acp.session import (  # noqa: E402
    ACPSession,
    ACPSessionStore,
    load_persisted_session,
    sanitize_session_run_id,
    save_persisted_session,
)
from bernstein.core.protocols.acp.transport import (  # noqa: E402
    HttpAcpTransport,
    JsonRpcFraming,
    StdioAcpTransport,
)

__all__ = [
    "ACP_EVENT_TYPE",
    "ACP_LEGACY_PROTOCOL_VERSION",
    "ACP_PROTOCOL_VERSION",
    "ACP_V1_PROTOCOL_VERSION",
    "AUTH_REQUIRED",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_TERMINAL_AUTH_METHOD",
    "REQUEST_CANCELLED",
    "RESOURCE_NOT_FOUND",
    "VALID_ACP_TOOL_KINDS",
    "ACPApprovalGateBridge",
    "ACPEvent",
    "ACPEventJournalSink",
    "ACPHandler",
    "ACPHandlerRegistry",
    "ACPRequestContext",
    "ACPRun",
    "ACPRunStatus",
    "ACPSchemaError",
    "ACPServer",
    "ACPSession",
    "ACPSessionStore",
    "AcpDivergence",
    "AcpLifecycleResult",
    "AdapterDescriptor",
    "AuthHandler",
    "ClientRequester",
    "EmbeddedOrchestratorRunner",
    "HttpAcpTransport",
    "JournalACPProjector",
    "JsonRpcFraming",
    "PromptResult",
    "PromptRunOutcome",
    "PromptRunner",
    "SandboxBackendDescriptor",
    "ServerCapabilities",
    "SessionLoader",
    "SessionMode",
    "StdioAcpTransport",
    "acp_active_sessions",
    "acp_messages_total",
    "build_default_server",
    "canonical_frame_bytes",
    "compare_acp_journals",
    "drive_acp_lifecycle",
    "extract_prompt_text",
    "frame_content_hash",
    "infer_tool_kind",
    "is_terminal_frame",
    "load_persisted_session",
    "make_request",
    "parse_inbound_frame",
    "project_journal_to_acp_updates",
    "record_acp_message",
    "replay_acp_content_hashes",
    "resolve_embedded_adapter_name",
    "sanitize_session_run_id",
    "save_persisted_session",
    "validate_request",
    "validate_response",
]
