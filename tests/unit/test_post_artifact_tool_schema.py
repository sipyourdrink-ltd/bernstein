"""Input-firewall schema for the bernstein_post_artifact MCP tool (#2553)."""

from __future__ import annotations

from bernstein.mcp.input_validation import ValidatedPayload, ValidationError, validate_tool_call


def test_valid_report_payload_passes() -> None:
    result = validate_tool_call(
        "bernstein_post_artifact",
        {"task_id": "task-1", "key": "summary", "artifact_type": "report", "poster": "w", "body": "hi"},
    )
    assert isinstance(result, ValidatedPayload)


def test_unknown_artifact_type_is_rejected() -> None:
    result = validate_tool_call(
        "bernstein_post_artifact",
        {"task_id": "task-1", "key": "summary", "artifact_type": "video", "poster": "w"},
    )
    assert isinstance(result, ValidationError)


def test_missing_required_field_is_rejected() -> None:
    result = validate_tool_call(
        "bernstein_post_artifact",
        {"task_id": "task-1", "artifact_type": "report", "poster": "w"},
    )
    assert isinstance(result, ValidationError)


def test_bad_key_pattern_is_rejected() -> None:
    result = validate_tool_call(
        "bernstein_post_artifact",
        {"task_id": "task-1", "key": ".hidden", "artifact_type": "report", "poster": "w", "body": "x"},
    )
    assert isinstance(result, ValidationError)
