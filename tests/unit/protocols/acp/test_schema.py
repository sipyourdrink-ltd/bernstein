"""Schema-validation tests for the ACP bridge.

Covers: handshake versioning, malformed-frame rejection, every supported
method's parameter shape, and notification vs request semantics.
"""

from __future__ import annotations

import pytest

from bernstein.core.protocols.acp.schema import (
    ACP_LEGACY_PROTOCOL_VERSION,
    ACP_PROTOCOL_VERSION,
    ACP_V1_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MODE_INVALID,
    PARSE_ERROR,
    ACPSchemaError,
    is_response_frame,
    make_error,
    make_notification,
    make_request,
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
            validate_request({"jsonrpc": "2.0", "method": "unknown_method", "id": 1, "params": {}})
        assert exc.value.code == METHOD_NOT_FOUND

    def test_rejects_non_object_params(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "method": "prompt", "id": 1, "params": []})
        assert exc.value.code == INVALID_PARAMS

    def test_initialize_accepts_optional_protocol_version(self) -> None:
        parsed = validate_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert parsed.method == "initialize"
        assert parsed.is_notification is False

    def test_initialize_accepts_v1_int_and_legacy_str_protocol_version(self) -> None:
        v1 = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        assert v1.params["protocolVersion"] == 1
        legacy = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2025-04-01"},
            }
        )
        assert legacy.params["protocolVersion"] == "2025-04-01"

    @pytest.mark.parametrize("bad_version", [True, -1, 70000, "", [], {}])
    def test_initialize_rejects_invalid_protocol_version(self, bad_version: object) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": bad_version},
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_initialized_must_be_notification(self) -> None:
        # No id => OK
        parsed = validate_request({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        assert parsed.is_notification is True
        # With id => rejected
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "id": 1, "method": "initialized", "params": {}})
        assert exc.value.code == INVALID_REQUEST

    def test_prompt_requires_prompt_text(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "id": 1, "method": "prompt", "params": {"cwd": "/tmp"}})
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
            validate_request({"jsonrpc": "2.0", "id": 1, "method": "cancel", "params": {}})
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
        validate_response({"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "s1"}})

    def test_accepts_error_envelope(self) -> None:
        validate_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "oops"}})

    def test_rejects_both_result_and_error(self) -> None:
        with pytest.raises(ACPSchemaError):
            validate_response({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 0, "message": ""}})

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


def test_protocol_version_constants() -> None:
    """Pin ACP v1 integer protocol version and legacy compatibility constant."""
    assert ACP_V1_PROTOCOL_VERSION == 1
    assert isinstance(ACP_V1_PROTOCOL_VERSION, int)
    assert ACP_PROTOCOL_VERSION == "2025-04-01"
    assert isinstance(ACP_PROTOCOL_VERSION, str)
    assert ACP_LEGACY_PROTOCOL_VERSION == "2025-04-01"


class TestACPv1Schemas:
    """Schema validation for official ACP v1 methods and helpers."""

    def test_rejects_boolean_request_id(self) -> None:
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "id": True, "method": "initialize", "params": {"protocolVersion": 1}})
        assert exc.value.code == INVALID_REQUEST

    def test_authenticate_requires_method_id(self) -> None:
        parsed = validate_request(
            {"jsonrpc": "2.0", "id": 1, "method": "authenticate", "params": {"methodId": "bernstein-setup"}}
        )
        assert parsed.method == "authenticate"
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "id": 1, "method": "authenticate", "params": {}})
        assert exc.value.code == INVALID_PARAMS

    def test_session_new_requires_absolute_cwd_and_mcp_servers_list(self) -> None:
        parsed = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/new",
                "params": {"cwd": "/tmp/work", "mcpServers": [], "additionalDirectories": ["/tmp/extra"]},
            }
        )
        assert parsed.method == "session/new"

        with pytest.raises(ACPSchemaError) as rel_exc:
            validate_request(
                {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "rel/dir", "mcpServers": []}}
            )
        assert rel_exc.value.code == INVALID_PARAMS

        with pytest.raises(ACPSchemaError) as mcp_exc:
            validate_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": "/tmp/work"}})
        assert mcp_exc.value.code == INVALID_PARAMS

        with pytest.raises(ACPSchemaError) as add_dir_exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/new",
                    "params": {"cwd": "/tmp/work", "mcpServers": [], "additionalDirectories": ["relative"]},
                }
            )
        assert add_dir_exc.value.code == INVALID_PARAMS

    def test_session_set_mode_validates_mode_id_with_invalid_params(self) -> None:
        parsed = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/set_mode",
                "params": {"sessionId": "s1", "modeId": "auto"},
            }
        )
        assert parsed.params["modeId"] == "auto"

        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/set_mode",
                    "params": {"sessionId": "s1", "modeId": "yolo"},
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_session_prompt_validates_content_blocks(self) -> None:
        parsed = validate_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": "s1",
                    "prompt": [
                        {"type": "text", "text": "hello"},
                        {"type": "resource_link", "name": "f.py", "uri": "file:///f.py"},
                        {"type": "resource", "resource": {"uri": "file:///f.py", "text": "x=1"}},
                    ],
                },
            }
        )
        assert len(parsed.params["prompt"]) == 3

        with pytest.raises(ACPSchemaError) as exc:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {"sessionId": "s1", "prompt": [{"type": "unknown"}]},
                }
            )
        assert exc.value.code == INVALID_PARAMS

    def test_session_cancel_is_notification_only(self) -> None:
        parsed = validate_request({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}})
        assert parsed.is_notification is True
        with pytest.raises(ACPSchemaError) as exc:
            validate_request({"jsonrpc": "2.0", "id": 9, "method": "session/cancel", "params": {"sessionId": "s1"}})
        assert exc.value.code == INVALID_REQUEST

    def test_session_update_and_request_permission_validation(self) -> None:
        upd = validate_request(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
                },
            }
        )
        assert upd.is_notification is True

        perm = validate_request(
            make_request(
                "acp-srv-1",
                "session/request_permission",
                {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "tc-1"},
                    "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"}],
                },
            )
        )
        assert perm.is_notification is False
        assert perm.request_id == "acp-srv-1"

        with pytest.raises(ACPSchemaError) as bad_kind:
            validate_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "s1",
                        "toolCall": {"toolCallId": "tc-1"},
                        "options": [{"optionId": "x", "name": "X", "kind": "bogus"}],
                    },
                }
            )
        assert bad_kind.value.code == INVALID_PARAMS

    def test_is_response_frame_helper(self) -> None:
        assert is_response_frame({"jsonrpc": "2.0", "id": 1, "result": {}}) is True
        assert is_response_frame({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}) is True
        assert is_response_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) is False
        assert is_response_frame({"jsonrpc": "2.0", "method": "session/cancel", "params": {}}) is False
