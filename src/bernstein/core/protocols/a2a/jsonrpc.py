"""JSON-RPC 2.0 + A2A projection helpers for the inbound server surface (#2609).

The binding design directive fixes the wire binding as JSON-RPC 2.0 (with SSE
for ``message/stream``). This module holds the framework-agnostic core so the
FastAPI route in :mod:`bernstein.core.routes.a2a_jsonrpc` is a thin adapter:

* request parsing and the ``result`` / ``error`` envelopes (JSON-RPC 2.0);
* extracting the caller's text from an A2A ``Message``;
* projecting a Bernstein task's status onto an A2A ``Task`` state; and
* building the completed-task ``Artifact`` whose ``Part`` list carries a
  lineage-receipt reference plus the exact payload that receipt attests.

The last point is the callable-node's whole reason for existing: a peer that
cannot take streaming or artifacts still reaches the same completed ``Task``
by polling ``tasks/get``, and the ``Artifact`` it receives contains both the
receipt and the bytes the receipt covers, so it verifies the answer offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "JSONRPCError",
    "JSONRPCRequest",
    "a2a_state_for_bernstein",
    "build_completed_artifact",
    "build_task_object",
    "extract_message_text",
    "jsonrpc_error_response",
    "jsonrpc_result_response",
    "parse_jsonrpc_request",
]

# JSON-RPC 2.0 reserved error codes (§5.1).
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class JSONRPCError(Exception):
    """A JSON-RPC error to project into an ``error`` envelope.

    Attributes:
        code: JSON-RPC error code.
        message: Human-readable summary.
        data: Optional structured detail.
    """

    def __init__(self, code: int, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class JSONRPCRequest:
    """A parsed, validated JSON-RPC 2.0 request object.

    Attributes:
        id: The request id echoed on the response. May be ``None`` for
            notifications, though the A2A methods here always carry one.
        method: The invoked method name (e.g. ``message/send``).
        params: The params object (always a dict; positional params are
            rejected because every A2A method takes named params).
    """

    id: Any
    method: str
    params: dict[str, Any]


def parse_jsonrpc_request(body: Any) -> JSONRPCRequest:
    """Validate a decoded JSON body as a JSON-RPC 2.0 request.

    Args:
        body: The already-JSON-decoded request body.

    Returns:
        A :class:`JSONRPCRequest`.

    Raises:
        JSONRPCError: With :data:`JSONRPC_INVALID_REQUEST` when the body is not
            a conformant request object.
    """
    if not isinstance(body, dict):
        raise JSONRPCError(JSONRPC_INVALID_REQUEST, "request must be a JSON object")
    if body.get("jsonrpc") != "2.0":
        raise JSONRPCError(JSONRPC_INVALID_REQUEST, "jsonrpc version must be exactly '2.0'")
    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise JSONRPCError(JSONRPC_INVALID_REQUEST, "method must be a non-empty string")
    params = body.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # A2A methods all take named params; positional lists are refused so a
        # malformed call fails at the boundary rather than deep in a handler.
        raise JSONRPCError(JSONRPC_INVALID_PARAMS, "params must be an object")
    return JSONRPCRequest(id=body.get("id"), method=method, params=params)


def jsonrpc_result_response(request_id: Any, result: Any) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 success envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error_response(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 error envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ---------------------------------------------------------------------------
# A2A message extraction
# ---------------------------------------------------------------------------


def extract_message_text(params: dict[str, Any]) -> str:
    """Return the concatenated text of an A2A ``message/send`` message.

    A2A messages carry a ``parts`` array; this joins every ``text`` part in
    order and ignores non-text parts (file / data). An empty result is a
    protocol error - there is nothing to act on.

    Raises:
        JSONRPCError: :data:`JSONRPC_INVALID_PARAMS` when no message or no
            usable text is present.
    """
    message = params.get("message")
    if not isinstance(message, dict):
        raise JSONRPCError(JSONRPC_INVALID_PARAMS, "params.message is required")
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise JSONRPCError(JSONRPC_INVALID_PARAMS, "params.message.parts must be an array")
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    joined = "".join(chunks)
    if not joined.strip():
        raise JSONRPCError(JSONRPC_INVALID_PARAMS, "message carries no text part")
    return joined


# ---------------------------------------------------------------------------
# A2A task projection
# ---------------------------------------------------------------------------

#: Bernstein task status -> A2A task state. Mirrors ``_BERNSTEIN_TO_A2A`` in
#: :mod:`bernstein.core.protocols.a2a.a2a` but yields the A2A *string* the wire
#: expects, and covers the states an inbound task can reach.
_BERNSTEIN_TO_A2A_STATE: dict[str, str] = {
    "planned": "submitted",
    "open": "submitted",
    "claimed": "working",
    "in_progress": "working",
    "waiting_for_subtasks": "working",
    "blocked": "input-required",
    "blocked_by_abandon": "input-required",
    "pending_approval": "input-required",
    "suspended": "input-required",
    "done": "completed",
    "closed": "completed",
    "failed": "failed",
    "refused": "failed",
    "abandoned": "failed",
    "orphaned": "failed",
    "cancelled": "canceled",
}


def a2a_state_for_bernstein(status: str) -> str:
    """Return the A2A task-state string for a Bernstein task status.

    Unknown statuses degrade to ``submitted`` rather than raising: a caller
    polling ``tasks/get`` should keep polling, not receive a terminal error
    for a status this projection has not enumerated.
    """
    return _BERNSTEIN_TO_A2A_STATE.get(status, "submitted")


def build_task_object(
    *,
    task_id: str,
    context_id: str,
    state: str,
    timestamp: str,
    artifacts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an A2A ``Task`` object for a JSON-RPC result.

    Args:
        task_id: The A2A task id (echoed to the caller for ``tasks/get``).
        context_id: The A2A context id grouping related tasks.
        state: A2A task state string (see :func:`a2a_state_for_bernstein`).
        timestamp: ISO-8601 status timestamp.
        artifacts: Completed-task artifacts, when any.
        metadata: Free-form metadata (used to carry the acceptance receipt).
    """
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": context_id,
        "kind": "task",
        "status": {"state": state, "timestamp": timestamp},
        "artifacts": artifacts or [],
        "history": [],
    }
    if metadata:
        task["metadata"] = metadata
    return task


def attested_completion_payload(*, task_id: str, result: str) -> dict[str, Any]:
    """Return the exact object a completion receipt attests to.

    Kept tiny and explicit so a peer reconstructs it byte-for-byte from the
    artifact and verifies the receipt offline: it is the ``content_hash``
    pre-image, nothing more.
    """
    return {"taskId": task_id, "result": result}


def build_completed_artifact(
    *,
    task_id: str,
    result: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Build the completed-task ``Artifact`` carrying a receipt reference.

    The artifact has two parts:

    * a ``text`` part with the human-readable result, for any client; and
    * a ``data`` part carrying ``{attested, lineageReceipt}`` - the payload
      the receipt covers and the receipt itself - so a client that cannot take
      artifacts still gets everything it needs to verify the answer offline via
      ``bernstein a2a verify``.
    """
    return {
        "artifactId": f"{task_id}-result",
        "name": "result",
        "parts": [
            {"kind": "text", "text": result},
            {
                "kind": "data",
                "data": {
                    "attested": attested_completion_payload(task_id=task_id, result=result),
                    "lineageReceipt": receipt,
                },
            },
        ],
    }
