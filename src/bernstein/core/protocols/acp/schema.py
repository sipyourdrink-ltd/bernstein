"""JSON-RPC 2.0 + ACP message schema validation.

Supports both the official Agent Client Protocol v1 wire schema
(``protocolVersion = 1``: ``initialize``, ``authenticate``, ``session/new``,
``session/set_mode``, ``session/prompt``, ``session/cancel``,
``session/update``, ``session/request_permission``) and the legacy
``"2025-04-01"`` dialect (``initialized``, ``prompt``, ``streamUpdate``,
``cancel``, ``setMode``, ``requestPermission``) so existing clients,
adapter fixtures, and audit-parity harnesses remain dual-compatible.

Schemas are kept hand-rolled (no extra runtime dependency) so every
malformed frame produces a precise JSON-RPC 2.0 / ACP error code.

JSON-RPC 2.0 error codes used:

* ``-32700`` - parse error (malformed JSON).
* ``-32600`` - invalid request (missing ``jsonrpc``, ``method``, pre-init
  ACP v1 calls, request/notification ``id`` mismatch, etc.).
* ``-32601`` - method not found.
* ``-32602`` - invalid params (schema mismatch).
* ``-32603`` - internal error.

ACP v1 standard application error codes:

* ``-32000`` - authentication required.
* ``-32002`` - resource / session not found.
* ``-32800`` - request cancelled.

Legacy Bernstein error codes retained for backward compatibility:

* ``-32001`` - legacy session not found.
* ``-32002`` - legacy permission denied.
* ``-32003`` - legacy mode invalid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final, cast

JSONRPC_VERSION: Final[str] = "2.0"

# JSON-RPC 2.0 reserved error codes
PARSE_ERROR: Final[int] = -32700
INVALID_REQUEST: Final[int] = -32600
METHOD_NOT_FOUND: Final[int] = -32601
INVALID_PARAMS: Final[int] = -32602
INTERNAL_ERROR: Final[int] = -32603

# ACP v1 standard error codes
AUTH_REQUIRED: Final[int] = -32000
RESOURCE_NOT_FOUND: Final[int] = -32002
REQUEST_CANCELLED: Final[int] = -32800

# Legacy Bernstein error codes (kept for backward compatibility)
SESSION_NOT_FOUND: Final[int] = -32001
PERMISSION_DENIED: Final[int] = -32002
MODE_INVALID: Final[int] = -32003

ACP_V1_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialize",
        "authenticate",
        "session/new",
        "session/load",
        "session/set_mode",
        "session/prompt",
        "session/cancel",
        "session/update",
        "session/request_permission",
    }
)

ACP_V1_PRE_INIT_BLOCKED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "authenticate",
        "session/new",
        "session/load",
        "session/set_mode",
        "session/prompt",
        "session/cancel",
    }
)

ACP_LEGACY_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialized",
        "prompt",
        "streamUpdate",
        "cancel",
        "setMode",
        "requestPermission",
    }
)

SUPPORTED_METHODS: Final[frozenset[str]] = ACP_V1_METHODS | ACP_LEGACY_METHODS

ACP_AUTH_EXEMPT_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialize",
        "initialized",
        "authenticate",
    }
)

NOTIFICATION_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialized",
        "streamUpdate",
        "session/cancel",
        "session/update",
    }
)

VALID_MODES: Final[frozenset[str]] = frozenset({"auto", "manual"})

VALID_STOP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "end_turn",
        "max_tokens",
        "max_turn_requests",
        "refusal",
        "cancelled",
    }
)

VALID_CONTENT_BLOCK_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text",
        "resource_link",
        "resource",
        "image",
        "audio",
    }
)

VALID_PERMISSION_OPTION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    }
)

# Legacy string dialect kept under the original constant name for back-compat.
ACP_PROTOCOL_VERSION: Final[str] = "2025-04-01"
ACP_LEGACY_PROTOCOL_VERSION: Final[str] = ACP_PROTOCOL_VERSION
# Official ACP v1 integer protocol version (spec schema-v1).
ACP_V1_PROTOCOL_VERSION: Final[int] = 1
ACP_V1_PROTOCOL_VERSION_MAX: Final[int] = 65535


class ACPSchemaError(Exception):
    """Raised when a JSON-RPC frame or ACP payload fails validation.

    Attributes:
        code: JSON-RPC error code suitable for the response envelope.
        message: Human-readable diagnostic.
        data: Optional structured detail (e.g. offending field name).
    """

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class ParsedRequest:
    """A validated JSON-RPC request or notification.

    Attributes:
        method: ACP method name (always present, always in
            :data:`SUPPORTED_METHODS`).
        params: Parameter dict (already shape-validated).
        request_id: ``id`` field - ``None`` when the frame is a
            notification.  JSON-RPC permits string, integer, or null IDs.
        is_notification: ``True`` when the frame had no ``id`` field; such
            frames must NOT receive a response envelope.
    """

    method: str
    params: dict[str, Any]
    request_id: str | int | None
    is_notification: bool


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_absolute_path(path_str: str) -> bool:
    return PurePosixPath(path_str).is_absolute() or PureWindowsPath(path_str).is_absolute()


def _require_str(d: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    """Validate that ``d[key]`` is a string and return it."""
    value = d.get(key)
    if not isinstance(value, str):
        raise ACPSchemaError(INVALID_PARAMS, f"missing or non-string field {key!r}", {"field": key})
    if not allow_empty and not value:
        raise ACPSchemaError(INVALID_PARAMS, f"field {key!r} must not be empty", {"field": key})
    return value


def _validate_optional_dict(params: dict[str, Any], key: str) -> None:
    value = params.get(key)
    if value is not None and not isinstance(value, dict):
        raise ACPSchemaError(INVALID_PARAMS, f"{key} must be an object", {"field": key})


def _validate_initialize(params: dict[str, Any]) -> None:
    """Validate the params for the ``initialize`` request (v1 or legacy)."""
    proto = params.get("protocolVersion")
    if proto is not None:
        if _is_int(proto):
            if proto < 0 or proto > ACP_V1_PROTOCOL_VERSION_MAX:
                raise ACPSchemaError(
                    INVALID_PARAMS,
                    f"protocolVersion must be between 0 and {ACP_V1_PROTOCOL_VERSION_MAX}",
                    {"field": "protocolVersion"},
                )
        elif isinstance(proto, str):
            if not proto:
                raise ACPSchemaError(
                    INVALID_PARAMS,
                    "protocolVersion must not be empty",
                    {"field": "protocolVersion"},
                )
        else:
            raise ACPSchemaError(
                INVALID_PARAMS,
                "protocolVersion must be an integer or string",
                {"field": "protocolVersion"},
            )
    _validate_optional_dict(params, "clientCapabilities")
    _validate_optional_dict(params, "clientInfo")
    _validate_optional_dict(params, "_meta")


def _validate_authenticate(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``authenticate`` request."""
    _require_str(params, "methodId")
    _validate_optional_dict(params, "_meta")


def _validate_session_new(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/new`` request."""
    cwd = _require_str(params, "cwd")
    if not _is_absolute_path(cwd):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "cwd must be an absolute path",
            {"field": "cwd"},
        )
    mcp_servers_raw = params.get("mcpServers")
    if not isinstance(mcp_servers_raw, list):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "missing or non-array field 'mcpServers'",
            {"field": "mcpServers"},
        )
    mcp_servers = cast("list[Any]", mcp_servers_raw)
    for idx, entry in enumerate(mcp_servers):
        if not isinstance(entry, dict):
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"mcpServers[{idx}] must be an object",
                {"field": f"mcpServers[{idx}]"},
            )

    additional_dirs_raw = params.get("additionalDirectories")
    if additional_dirs_raw is not None:
        if not isinstance(additional_dirs_raw, list):
            raise ACPSchemaError(
                INVALID_PARAMS,
                "additionalDirectories must be an array of absolute paths",
                {"field": "additionalDirectories"},
            )
        additional_dirs = cast("list[Any]", additional_dirs_raw)
        for idx, item in enumerate(additional_dirs):
            if not isinstance(item, str) or not item or not _is_absolute_path(item):
                raise ACPSchemaError(
                    INVALID_PARAMS,
                    f"additionalDirectories[{idx}] must be an absolute path string",
                    {"field": f"additionalDirectories[{idx}]"},
                )

    system_prompt = params.get("systemPrompt")
    if system_prompt is not None and not isinstance(system_prompt, (str, dict)):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "systemPrompt must be a string or object",
            {"field": "systemPrompt"},
        )

    config_options_raw = params.get("configOptions")
    if config_options_raw is not None:
        if not isinstance(config_options_raw, list):
            raise ACPSchemaError(
                INVALID_PARAMS,
                "configOptions must be an array of objects",
                {"field": "configOptions"},
            )
        config_options = cast("list[Any]", config_options_raw)
        if not all(isinstance(x, dict) for x in config_options):
            raise ACPSchemaError(
                INVALID_PARAMS,
                "configOptions must be an array of objects",
                {"field": "configOptions"},
            )
    _validate_optional_dict(params, "_meta")


def _validate_session_load(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/load`` request."""
    _require_str(params, "sessionId")
    _validate_session_new(params)


def _validate_session_set_mode(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/set_mode`` request."""
    _require_str(params, "sessionId")
    mode_id = _require_str(params, "modeId")
    if mode_id not in VALID_MODES:
        raise ACPSchemaError(
            INVALID_PARAMS,
            f"modeId must be one of {sorted(VALID_MODES)}",
            {"field": "modeId", "got": mode_id},
        )
    _validate_optional_dict(params, "_meta")


def _validate_content_block(block: Any, idx: int) -> None:
    if not isinstance(block, dict):
        raise ACPSchemaError(
            INVALID_PARAMS,
            f"prompt[{idx}] must be an object",
            {"field": f"prompt[{idx}]"},
        )
    block_dict = cast("dict[str, Any]", block)
    block_type = block_dict.get("type")
    if not isinstance(block_type, str) or block_type not in VALID_CONTENT_BLOCK_TYPES:
        raise ACPSchemaError(
            INVALID_PARAMS,
            f"prompt[{idx}].type must be one of {sorted(VALID_CONTENT_BLOCK_TYPES)}",
            {"field": f"prompt[{idx}].type"},
        )
    if block_type == "text":
        if not isinstance(block_dict.get("text"), str):
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"prompt[{idx}].text must be a string",
                {"field": f"prompt[{idx}].text"},
            )
    elif block_type == "resource_link":
        _require_str(block_dict, "name")
        _require_str(block_dict, "uri")
    elif block_type == "resource":
        res_raw = block_dict.get("resource")
        if not isinstance(res_raw, dict):
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"prompt[{idx}].resource must be an object with non-empty 'uri'",
                {"field": f"prompt[{idx}].resource"},
            )
        res = cast("dict[str, Any]", res_raw)
        uri = res.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"prompt[{idx}].resource must be an object with non-empty 'uri'",
                {"field": f"prompt[{idx}].resource"},
            )
    else:
        # image / audio
        _require_str(block_dict, "data")
        _require_str(block_dict, "mimeType")


def _validate_session_prompt(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/prompt`` request."""
    _require_str(params, "sessionId")
    blocks_raw = params.get("prompt")
    if not isinstance(blocks_raw, list):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "missing or non-array field 'prompt'",
            {"field": "prompt"},
        )
    blocks = cast("list[Any]", blocks_raw)
    for idx, block in enumerate(blocks):
        _validate_content_block(block, idx)
    _validate_optional_dict(params, "_meta")


def _validate_session_cancel(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/cancel`` notification."""
    _require_str(params, "sessionId")
    _validate_optional_dict(params, "_meta")


def _validate_session_update(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/update`` notification."""
    _require_str(params, "sessionId")
    update_raw = params.get("update")
    if not isinstance(update_raw, dict):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "missing or non-object field 'update'",
            {"field": "update"},
        )
    update = cast("dict[str, Any]", update_raw)
    session_update = update.get("sessionUpdate")
    if not isinstance(session_update, str) or not session_update:
        raise ACPSchemaError(
            INVALID_PARAMS,
            "update.sessionUpdate must be a non-empty string",
            {"field": "update.sessionUpdate"},
        )
    _validate_optional_dict(params, "_meta")


def _validate_session_request_permission(params: dict[str, Any]) -> None:
    """Validate the params for the ACP v1 ``session/request_permission`` request."""
    _require_str(params, "sessionId")
    tool_call_raw = params.get("toolCall")
    if not isinstance(tool_call_raw, dict):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "missing or non-object field 'toolCall'",
            {"field": "toolCall"},
        )
    tool_call = cast("dict[str, Any]", tool_call_raw)
    tc_id = tool_call.get("toolCallId")
    if not isinstance(tc_id, str) or not tc_id:
        raise ACPSchemaError(
            INVALID_PARAMS,
            "toolCall.toolCallId must be a non-empty string",
            {"field": "toolCall.toolCallId"},
        )
    options_raw = params.get("options")
    if not isinstance(options_raw, list):
        raise ACPSchemaError(
            INVALID_PARAMS,
            "missing or non-array field 'options'",
            {"field": "options"},
        )
    options = cast("list[Any]", options_raw)
    for idx, option in enumerate(options):
        if not isinstance(option, dict):
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"options[{idx}] must be an object",
                {"field": f"options[{idx}]"},
            )
        opt_dict = cast("dict[str, Any]", option)
        _require_str(opt_dict, "optionId")
        _require_str(opt_dict, "name")
        kind = opt_dict.get("kind")
        if not isinstance(kind, str) or kind not in VALID_PERMISSION_OPTION_KINDS:
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"options[{idx}].kind must be one of {sorted(VALID_PERMISSION_OPTION_KINDS)}",
                {"field": f"options[{idx}].kind"},
            )
    _validate_optional_dict(params, "_meta")


# -- Legacy 2025-04-01 validators -------------------------------------------


def _validate_prompt(params: dict[str, Any]) -> None:
    """Validate the params for the legacy ``prompt`` request."""
    _require_str(params, "prompt")
    cwd = params.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ACPSchemaError(INVALID_PARAMS, "cwd must be a string")
    role = params.get("role")
    if role is not None and not isinstance(role, str):
        raise ACPSchemaError(INVALID_PARAMS, "role must be a string")


def _validate_stream_update(params: dict[str, Any]) -> None:
    """Validate the params for the legacy ``streamUpdate`` notification."""
    _require_str(params, "sessionId")
    if "delta" in params and not isinstance(params["delta"], (str, dict)):
        raise ACPSchemaError(INVALID_PARAMS, "delta must be a string or object")


def _validate_cancel(params: dict[str, Any]) -> None:
    """Validate the params for the legacy ``cancel`` request."""
    _require_str(params, "sessionId")
    reason = params.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ACPSchemaError(INVALID_PARAMS, "reason must be a string")


def _validate_set_mode(params: dict[str, Any]) -> None:
    """Validate the params for the legacy ``setMode`` request."""
    _require_str(params, "sessionId")
    mode = _require_str(params, "mode")
    if mode not in VALID_MODES:
        raise ACPSchemaError(
            MODE_INVALID,
            f"mode must be one of {sorted(VALID_MODES)}",
            {"got": mode},
        )


def _validate_request_permission(params: dict[str, Any]) -> None:
    """Validate the params for the legacy ``requestPermission`` request."""
    _require_str(params, "sessionId")
    _require_str(params, "promptId")
    decision = params.get("decision")
    if decision is not None and decision not in {"approved", "rejected"}:
        raise ACPSchemaError(INVALID_PARAMS, "decision must be 'approved' or 'rejected'")


def _validate_noop(_params: dict[str, Any]) -> None:
    return None


_PARAM_VALIDATORS: dict[str, Any] = {
    # ACP v1
    "initialize": _validate_initialize,
    "authenticate": _validate_authenticate,
    "session/new": _validate_session_new,
    "session/load": _validate_session_load,
    "session/set_mode": _validate_session_set_mode,
    "session/prompt": _validate_session_prompt,
    "session/cancel": _validate_session_cancel,
    "session/update": _validate_session_update,
    "session/request_permission": _validate_session_request_permission,
    # Legacy 2025-04-01
    "initialized": _validate_noop,
    "prompt": _validate_prompt,
    "streamUpdate": _validate_stream_update,
    "cancel": _validate_cancel,
    "setMode": _validate_set_mode,
    "requestPermission": _validate_request_permission,
}


def is_response_frame(frame: Any) -> bool:
    """Return ``True`` if *frame* is a JSON-RPC 2.0 response envelope."""
    return (
        isinstance(frame, dict) and "method" not in frame and "id" in frame and ("result" in frame or "error" in frame)
    )


def validate_request(frame: Any) -> ParsedRequest:
    """Validate a single JSON-RPC frame and return a :class:`ParsedRequest`.

    Args:
        frame: The decoded frame (already a Python object - the transport
            layer is responsible for turning bytes into JSON, not this
            module).

    Returns:
        A :class:`ParsedRequest`.

    Raises:
        ACPSchemaError: If the frame is not a valid JSON-RPC 2.0 request
            or if its params do not match the ACP method schema.
    """
    if not isinstance(frame, dict):
        raise ACPSchemaError(INVALID_REQUEST, "frame must be a JSON object")
    frame_dict = cast("dict[str, Any]", frame)
    if frame_dict.get("jsonrpc") != JSONRPC_VERSION:
        raise ACPSchemaError(INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = frame_dict.get("method")
    if not isinstance(method, str):
        raise ACPSchemaError(INVALID_REQUEST, "method must be a string")
    if method not in SUPPORTED_METHODS:
        raise ACPSchemaError(METHOD_NOT_FOUND, f"unknown method {method!r}", {"method": method})

    params_raw: Any = frame_dict.get("params", {})
    if params_raw is None:
        params_raw = {}
    if not isinstance(params_raw, dict):
        raise ACPSchemaError(INVALID_PARAMS, "params must be an object")
    params_dict = cast("dict[str, Any]", params_raw)

    _PARAM_VALIDATORS[method](params_dict)

    is_notification = "id" not in frame_dict
    request_id = frame_dict.get("id")
    if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        raise ACPSchemaError(INVALID_REQUEST, "id must be a string, integer, or omitted")

    if not is_notification and method in NOTIFICATION_METHODS:
        raise ACPSchemaError(
            INVALID_REQUEST,
            f"method {method!r} is a notification and must not have an id",
            {"method": method},
        )
    if is_notification and method not in NOTIFICATION_METHODS:
        raise ACPSchemaError(
            INVALID_REQUEST,
            f"method {method!r} requires an id",
            {"method": method},
        )

    return ParsedRequest(
        method=method,
        params=dict(params_dict),
        request_id=request_id,
        is_notification=is_notification,
    )


def validate_response(frame: Any) -> dict[str, Any]:
    """Validate a JSON-RPC response envelope (used by tests + clients).

    Args:
        frame: Decoded frame.

    Returns:
        The frame as a dict.

    Raises:
        ACPSchemaError: If the envelope is malformed.
    """
    if not isinstance(frame, dict):
        raise ACPSchemaError(INVALID_REQUEST, "response must be a JSON object")
    frame_dict = cast("dict[str, Any]", frame)
    if frame_dict.get("jsonrpc") != JSONRPC_VERSION:
        raise ACPSchemaError(INVALID_REQUEST, "jsonrpc must be '2.0'")
    has_result = "result" in frame_dict
    has_error = "error" in frame_dict
    if has_result == has_error:
        raise ACPSchemaError(
            INVALID_REQUEST,
            "response must have exactly one of 'result' or 'error'",
        )
    if "id" not in frame_dict:
        raise ACPSchemaError(INVALID_REQUEST, "response must have an 'id' field")
    return frame_dict


def make_error(request_id: str | int | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC error envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def make_result(request_id: str | int | None, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success envelope."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_request(request_id: str | int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC request frame (Agent -> Client RPC)."""
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"{method!r} is not a known ACP method")
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params}


def make_notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC notification frame (no id)."""
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"{method!r} is not a known ACP method")
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}
