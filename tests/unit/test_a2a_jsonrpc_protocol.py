"""Pure JSON-RPC 2.0 + A2A projection helpers for the server surface (#2609).

These cover the framework-agnostic core: request parsing, error/result
envelopes, A2A ``Message``/``Task`` projection, and the completed-task
artifact that carries a lineage receipt reference. No HTTP here - the route
module wires these into FastAPI.
"""

from __future__ import annotations

import pytest

from bernstein.core.protocols.a2a.jsonrpc import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    JSONRPCError,
    a2a_state_for_bernstein,
    build_completed_artifact,
    build_task_object,
    extract_message_text,
    jsonrpc_error_response,
    jsonrpc_result_response,
    parse_jsonrpc_request,
)

# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def test_parse_valid_request() -> None:
    req = parse_jsonrpc_request({"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"x": 1}})
    assert req.id == 7
    assert req.method == "message/send"
    assert req.params == {"x": 1}


def test_missing_jsonrpc_version_is_invalid_request() -> None:
    with pytest.raises(JSONRPCError) as exc:
        parse_jsonrpc_request({"id": 1, "method": "message/send"})
    assert exc.value.code == JSONRPC_INVALID_REQUEST


def test_wrong_jsonrpc_version_is_invalid_request() -> None:
    with pytest.raises(JSONRPCError) as exc:
        parse_jsonrpc_request({"jsonrpc": "1.0", "id": 1, "method": "m"})
    assert exc.value.code == JSONRPC_INVALID_REQUEST


def test_missing_method_is_invalid_request() -> None:
    with pytest.raises(JSONRPCError) as exc:
        parse_jsonrpc_request({"jsonrpc": "2.0", "id": 1})
    assert exc.value.code == JSONRPC_INVALID_REQUEST


def test_non_object_body_is_invalid_request() -> None:
    with pytest.raises(JSONRPCError) as exc:
        parse_jsonrpc_request([1, 2, 3])  # type: ignore[arg-type]
    assert exc.value.code == JSONRPC_INVALID_REQUEST


def test_params_default_to_empty_object() -> None:
    req = parse_jsonrpc_request({"jsonrpc": "2.0", "id": 1, "method": "tasks/get"})
    assert req.params == {}


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def test_result_envelope_shape() -> None:
    env = jsonrpc_result_response(7, {"ok": True})
    assert env == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_error_envelope_shape() -> None:
    env = jsonrpc_error_response(7, JSONRPC_METHOD_NOT_FOUND, "no such method")
    assert env["jsonrpc"] == "2.0"
    assert env["id"] == 7
    assert env["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
    assert env["error"]["message"] == "no such method"
    assert "result" not in env


def test_error_constants_match_spec() -> None:
    assert JSONRPC_PARSE_ERROR == -32700
    assert JSONRPC_INVALID_REQUEST == -32600
    assert JSONRPC_METHOD_NOT_FOUND == -32601
    assert JSONRPC_INVALID_PARAMS == -32602


# ---------------------------------------------------------------------------
# A2A message extraction
# ---------------------------------------------------------------------------


def test_extract_text_from_parts() -> None:
    params = {
        "message": {
            "role": "user",
            "parts": [
                {"kind": "text", "text": "review "},
                {"kind": "text", "text": "the auth module"},
            ],
            "messageId": "m1",
        }
    }
    assert extract_message_text(params) == "review the auth module"


def test_extract_text_ignores_non_text_parts() -> None:
    params = {
        "message": {
            "parts": [
                {"kind": "text", "text": "hello"},
                {"kind": "data", "data": {"k": "v"}},
            ]
        }
    }
    assert extract_message_text(params) == "hello"


def test_missing_message_raises_invalid_params() -> None:
    with pytest.raises(JSONRPCError) as exc:
        extract_message_text({})
    assert exc.value.code == JSONRPC_INVALID_PARAMS


def test_message_with_no_text_raises_invalid_params() -> None:
    with pytest.raises(JSONRPCError) as exc:
        extract_message_text({"message": {"parts": [{"kind": "data", "data": {}}]}})
    assert exc.value.code == JSONRPC_INVALID_PARAMS


# ---------------------------------------------------------------------------
# A2A task projection
# ---------------------------------------------------------------------------


def test_state_projection_maps_done_to_completed() -> None:
    assert a2a_state_for_bernstein("done") == "completed"
    assert a2a_state_for_bernstein("open") == "submitted"
    assert a2a_state_for_bernstein("in_progress") == "working"
    assert a2a_state_for_bernstein("blocked") == "input-required"
    assert a2a_state_for_bernstein("failed") == "failed"


def test_build_task_object_shape() -> None:
    task = build_task_object(
        task_id="abc123",
        context_id="ctx1",
        state="submitted",
        timestamp="2026-07-24T00:00:00Z",
    )
    assert task["id"] == "abc123"
    assert task["contextId"] == "ctx1"
    assert task["kind"] == "task"
    assert task["status"]["state"] == "submitted"
    assert task["status"]["timestamp"] == "2026-07-24T00:00:00Z"


def test_completed_artifact_carries_receipt_and_attested_payload() -> None:
    receipt = {"entry_hash": "e", "content_hash": "sha256:c", "kid": "k", "head_signature": {}}
    artifact = build_completed_artifact(
        task_id="abc123",
        result="all tests pass",
        receipt=receipt,
    )
    assert artifact["artifactId"] == "abc123-result"
    kinds = [p["kind"] for p in artifact["parts"]]
    assert "text" in kinds
    assert "data" in kinds
    text_part = next(p for p in artifact["parts"] if p["kind"] == "text")
    assert text_part["text"] == "all tests pass"
    data_part = next(p for p in artifact["parts"] if p["kind"] == "data")
    assert data_part["data"]["lineageReceipt"] == receipt
    # The attested payload is exactly what the receipt's content_hash covers,
    # so a peer reconstructs it from the artifact and verifies offline.
    assert data_part["data"]["attested"] == {"taskId": "abc123", "result": "all tests pass"}


def test_attested_payload_is_stable_for_the_same_inputs() -> None:
    a = build_completed_artifact(task_id="t", result="r", receipt={})
    b = build_completed_artifact(task_id="t", result="r", receipt={})
    da = next(p for p in a["parts"] if p["kind"] == "data")["data"]["attested"]
    db = next(p for p in b["parts"] if p["kind"] == "data")["data"]["attested"]
    assert da == db
