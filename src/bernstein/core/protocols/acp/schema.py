"""JSON-RPC 2.0 + ACP message schema validation.

The bridge implements only the ratified subset of the Agent Client
Protocol that Bernstein needs: ``initialize``, ``initialized`` (notification),
``prompt``, ``streamUpdate`` (notification), ``cancel``, ``setMode``, and
``requestPermission``.  Schemas are kept hand-rolled (no extra runtime
dependency) because every method is fixed and small; this also lets us
emit precise error codes for malformed frames.

JSON-RPC 2.0 error codes used:

* ``-32700`` — parse error (malformed JSON).
* ``-32600`` — invalid request (missing ``jsonrpc``, ``method``, ``id`` for
  request/response confusion, etc.).
* ``-32601`` — method not found.
* ``-32602`` — invalid params (schema mismatch).
* ``-32603`` — internal error.

Bernstein-specific codes start at ``-32001``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

JSONRPC_VERSION: Final[str] = "2.0"

# JSON-RPC reserved error codes
PARSE_ERROR: Final[int] = -32700
INVALID_REQUEST: Final[int] = -32600
METHOD_NOT_FOUND: Final[int] = -32601
INVALID_PARAMS: Final[int] = -32602
INTERNAL_ERROR: Final[int] = -32603

# Bernstein-specific error codes
SESSION_NOT_FOUND: Final[int] = -32001
PERMISSION_DENIED: Final[int] = -32002
MODE_INVALID: Final[int] = -32003


SUPPORTED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "initialize",
        "initialized",
        "prompt",
        "streamUpdate",
        "cancel",
        "setMode",
        "requestPermission",
    }
)

NOTIFICATION_METHODS: Final[frozenset[str]] = frozenset({"initialized", "streamUpdate"})

VALID_MODES: Final[frozenset[str]] = frozenset({"auto", "manual"})

# ACP protocol version Bernstein speaks.  Negotiated during ``initialize``.
ACP_PROTOCOL_VERSION: Final[str] = "2025-04-01"


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
        request_id: ``id`` field — ``None`` when the frame is a
            notification.  JSON-RPC permits string, integer, or null IDs.
        is_notification: ``True`` when the frame had no ``id`` field; such
            frames must NOT receive a response envelope.
    """

    method: str
    params: dict[str, Any]
    request_id: str | int | None
    is_notification: bool


def _require_str(d: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    """Validate that ``d[key]`` is a string and return it.

    Args:
        d: Dict to inspect.
        key: Key whose value must be a non-empty string.
        allow_empty: When ``True`` an empty string is accepted.

    Returns:
        The string value.

    Raises:
        ACPSchemaError: If the key is missing or not a string.
    """
    value = d.get(key)
    if not isinstance(value, str):
        raise ACPSchemaError(INVALID_PARAMS, f"missing or non-string field {key!r}", {"field": key})
    if not allow_empty and not value:
        raise ACPSchemaError(INVALID_PARAMS, f"field {key!r} must not be empty", {"field": key})
    return value


def _validate_initialize(params: dict[str, Any]) -> None:
    """Validate the params for the ``initialize`` request."""
    proto = params.get("protocolVersion")
    if proto is not None and not isinstance(proto, str):
        raise ACPSchemaError(INVALID_PARAMS, "protocolVersion must be a string")
    caps = params.get("clientCapabilities")
    if caps is not None and not isinstance(caps, dict):
        raise ACPSchemaError(INVALID_PARAMS, "clientCapabilities must be an object")


def _validate_prompt(params: dict[str, Any]) -> None:
    """Validate the params for the ``prompt`` request."""
    _require_str(params, "prompt")
    cwd = params.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ACPSchemaError(INVALID_PARAMS, "cwd must be a string")
    role = params.get("role")
    if role is not None and not isinstance(role, str):
        raise ACPSchemaError(INVALID_PARAMS, "role must be a string")


def _validate_stream_update(params: dict[str, Any]) -> None:
    """Validate the params for the ``streamUpdate`` notification.

    ``streamUpdate`` is server -> client; we only validate it when echoed
    back through the framing layer in tests.
    """
    _require_str(params, "sessionId")
    if "delta" in params and not isinstance(params["delta"], (str, dict)):
        raise ACPSchemaError(INVALID_PARAMS, "delta must be a string or object")


def _validate_cancel(params: dict[str, Any]) -> None:
    """Validate the params for the ``cancel`` request."""
    _require_str(params, "sessionId")
    reason = params.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ACPSchemaError(INVALID_PARAMS, "reason must be a string")


def _validate_set_mode(params: dict[str, Any]) -> None:
    """Validate the params for the ``setMode`` request."""
    _require_str(params, "sessionId")
    mode = _require_str(params, "mode")
    if mode not in VALID_MODES:
        raise ACPSchemaError(
            MODE_INVALID,
            f"mode must be one of {sorted(VALID_MODES)}",
            {"got": mode},
        )


def _validate_request_permission(params: dict[str, Any]) -> None:
    """Validate the params for the ``requestPermission`` request."""
    _require_str(params, "sessionId")
    _require_str(params, "promptId")
    decision = params.get("decision")
    if decision is not None and decision not in {"approved", "rejected"}:
        raise ACPSchemaError(INVALID_PARAMS, "decision must be 'approved' or 'rejected'")


_PARAM_VALIDATORS: dict[str, Any] = {
    "initialize": _validate_initialize,
    "initialized": lambda _params: None,
    "prompt": _validate_prompt,
    "streamUpdate": _validate_stream_update,
    "cancel": _validate_cancel,
    "setMode": _validate_set_mode,
    "requestPermission": _validate_request_permission,
}


def validate_request(frame: Any) -> ParsedRequest:
    """Validate a single JSON-RPC frame and return a :class:`ParsedRequest`.

    Args:
        frame: The decoded frame (already a Python object — the transport
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
    if frame.get("jsonrpc") != JSONRPC_VERSION:
        raise ACPSchemaError(INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = frame.get("method")
    if not isinstance(method, str):
        raise ACPSchemaError(INVALID_REQUEST, "method must be a string")
    if method not in SUPPORTED_METHODS:
        raise ACPSchemaError(METHOD_NOT_FOUND, f"unknown method {method!r}", {"method": method})

    params_raw: Any = frame.get("params", {})
    if params_raw is None:
        params_raw = {}
    if not isinstance(params_raw, dict):
        raise ACPSchemaError(INVALID_PARAMS, "params must be an object")

    _PARAM_VALIDATORS[method](params_raw)

    is_notification = "id" not in frame
    request_id = frame.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise ACPSchemaError(INVALID_REQUEST, "id must be a string, integer, or omitted")

    if not is_notification and method in NOTIFICATION_METHODS:
        # Notification methods MUST NOT carry an id.
        raise ACPSchemaError(
            INVALID_REQUEST,
            f"method {method!r} is a notification and must not have an id",
            {"method": method},
        )
    if is_notification and method not in NOTIFICATION_METHODS:
        # Some clients omit id for fire-and-forget calls; ACP forbids that
        # for these methods because they require a response envelope.
        raise ACPSchemaError(
            INVALID_REQUEST,
            f"method {method!r} requires an id",
            {"method": method},
        )

    return ParsedRequest(
        method=method,
        params=dict(params_raw),
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
    if frame.get("jsonrpc") != JSONRPC_VERSION:
        raise ACPSchemaError(INVALID_REQUEST, "jsonrpc must be '2.0'")
    has_result = "result" in frame
    has_error = "error" in frame
    if has_result == has_error:
        raise ACPSchemaError(
            INVALID_REQUEST,
            "response must have exactly one of 'result' or 'error'",
        )
    if "id" not in frame:
        raise ACPSchemaError(INVALID_REQUEST, "response must have an 'id' field")
    return frame


def make_error(request_id: str | int | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC error envelope.

    Args:
        request_id: The id of the inbound request, or ``None`` when the
            inbound frame could not be parsed.
        code: JSON-RPC error code.
        message: Human-readable diagnostic.
        data: Optional structured payload.

    Returns:
        A dict ready for serialisation.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def make_result(request_id: str | int | None, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC success envelope.

    Args:
        request_id: The id of the inbound request.
        result: Method-specific result payload.

    Returns:
        A dict ready for serialisation.
    """
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC notification frame (no id).

    For the small set of methods Bernstein both consumes and emits
    (``streamUpdate`` and ``requestPermission`` — server → IDE prompt
    as a notification, IDE → server reply as a request envelope with a
    ``decision`` payload) we permit any supported ACP method here; the
    validate path enforces the ``id``-vs-no-``id`` invariant on ingress.

    Args:
        method: ACP method name.
        params: Notification payload.

    Returns:
        A dict ready for serialisation.

    Raises:
        ValueError: If *method* is not a known ACP method.
    """
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"{method!r} is not a known ACP method")
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}
