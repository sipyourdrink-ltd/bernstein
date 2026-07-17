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


def _call(**fields: object) -> object:
    return validate_tool_call(
        "bernstein_post_artifact",
        {"task_id": "task-1", "key": "k", "poster": "w", **fields},
    )


class TestTypeContract:
    def test_report_without_body_is_rejected(self) -> None:
        assert isinstance(_call(artifact_type="report"), ValidationError)

    def test_report_empty_body_is_rejected(self) -> None:
        assert isinstance(_call(artifact_type="report", body=""), ValidationError)

    def test_table_requires_columns_and_rows(self) -> None:
        assert isinstance(_call(artifact_type="table"), ValidationError)
        assert isinstance(_call(artifact_type="table", columns=["a"]), ValidationError)

    def test_valid_table_passes(self) -> None:
        assert isinstance(_call(artifact_type="table", columns=["a"], rows=[["1"]]), ValidatedPayload)

    def test_link_requires_url_and_kind(self) -> None:
        assert isinstance(_call(artifact_type="link"), ValidationError)
        assert isinstance(_call(artifact_type="link", url="https://x"), ValidationError)

    def test_link_kind_must_be_declared(self) -> None:
        assert isinstance(_call(artifact_type="link", url="https://x", link_kind="wild"), ValidationError)

    def test_valid_link_passes(self) -> None:
        assert isinstance(_call(artifact_type="link", url="https://x", link_kind="preview"), ValidatedPayload)
