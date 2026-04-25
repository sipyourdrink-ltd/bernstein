"""Schema-validation tests for the ACP bridge.

Covers: handshake versioning, malformed-frame rejection, every supported
method's parameter shape, and notification vs request semantics.
"""

from __future__ import annotations

import pytest

from bernstein.core.protocols.acp.schema import (
    ACP_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MODE_INVALID,
    PARSE_ERROR,
    ACPSchemaError,
    make_error,
    make_notification,
    make_result,
    validate_request,
    validate_response,
)


class TestValidateRequest:
    """Validate :func:`validate_request` rejects malformed frames cleanly."""

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request("not a dict")
        assert exc.value.code == INVALID_REQUEST

    def test_rejects_missing_jsonrpc(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"method": "initialize", "id": 1})
        assert exc.value.code == INVALID_REQUEST

    def test_rejects_unknown_method(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {"jsonrpc": "2.0", "method": "unknown_method", "id": 1, "params": {}}
            )
        assert exc.value.code == METHOD_NOT_FOUND

    def test_rejects_non_object_params(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {"jsonrpc": "2.0", "method": "prompt", "id": 1, "params": []}
            )
        assert exc.value.code == INVALID_PARAMS

    def test_initialize_accepts_optional_protocol_version(self) -> None:
        parsed = validate_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert parsed.method == "initialize"
        assert parsed.is_notification is False

    def test_initialize_rejects_non_string_protocol_version(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 42},
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_initialized_must_be_notification(self) -> None:
        # No id => OK
        parsed = validate_request(
            {"jsonrpc": "2.0", "method": "initialized", "params": {}}
        )
        assert parsed.is_notification is True
        # With id => rejected
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {"jsonrpc": "2.0", "id": 1, "method": "initialized", "params": {}}
            )
        assert exc.value.code == INVALID_REQUEST

    def test_prompt_requires_prompt_text(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"cwd": "/tmp"}}
            )
        assert exc.value.code == INVALID_PARAMS

    def test_prompt_rejects_non_string_role(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "prompt",
                    "params": {"prompt": "do x", "role": 42},
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_set_mode_validates_mode(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "setMode",
                    "params": {"sessionId": "s1", "mode": "yolo"},
                }
            )
        assert exc.value.code == MODE_INVALID

    def test_cancel_requires_session_id(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {"jsonrpc": "2.0", "id": 1, "method": "cancel", "params": {}}
            )
        assert exc.value.code == INVALID_PARAMS

    def test_request_permission_requires_decision_when_present(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "requestPermission",
                    "params": {
                        "sessionId": "s1",
                        "promptId": "p1",
                        "decision": "maybe",
                    },
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_request_permission_accepts_approved(self) -> None:
        parsed = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "requestPermission",
                "params": {"sessionId": "s1", "promptId": "p1", "decision": "approved"},
            }
        )
        assert parsed.method == "requestPermission"

    def test_stream_update_rejects_when_id_present(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "streamUpdate",
                    "params": {"sessionId": "s1"},
                }
            )
        assert exc.value.code == INVALID_REQUEST


class TestValidateResponse:
    """Validate :func:`validate_response` envelope checks."""

    def test_accepts_result_envelope(self) -> None:
        validate_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}}
        )

    def test_accepts_error_envelope(self) -> None:
        validate_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "oops"}}
        )

    def test_rejects_both_result_and_error(self) -> None:
        with pytest.raises(ACPSchemaError):
            validate_response(
                {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 0, "message": ""}}
            )

    def test_rejects_missing_id(self) -> None:
        with pytest.raises(ACPSchemaError):
            validate_response({"jsonrpc": "2.0", "result": {}})


class TestEnvelopeBuilders:
    """Quick sanity checks for the response builders."""

    def test_make_result(self) -> None:
        env = make_result(7, {"ok": True})
        assert env == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}

    def test_make_error_with_data(self) -> None:
        env = make_error(7, PARSE_ERROR, "boom", {"line": 12})
        assert env["error"] == {"code": PARSE_ERROR, "message": "boom", "data": {"line": 12}}

    def test_make_notification_rejects_unknown_methods(self) -> None:
        with pytest.raises(ValueError):
            make_notification("frobnicate", {})

    def test_make_notification_for_stream_update(self) -> None:
        env = make_notification("streamUpdate", {"sessionId": "s1", "delta": "hi"})
        assert "id" not in env
        assert env["method"] == "streamUpdate"

    def test_make_notification_for_request_permission(self) -> None:
        env = make_notification(
            "requestPermission",
            {"sessionId": "s1", "promptId": "p1", "tool": "write_file", "detail": ""},
        )
        assert "id" not in env
        assert env["method"] == "requestPermission"


def test_protocol_version_is_string() -> None:
    """Sanity guard so a refactor cannot accidentally null out the version."""
    assert isinstance(ACP_PROTOCOL_VERSION, str)
    assert ACP_PROTOCOL_VERSION
