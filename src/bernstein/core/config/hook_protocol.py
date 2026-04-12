"""Hook payload validation and command-hook chaining helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator


class HookValidationError(ValueError):
    """Raised when a hook payload or hook response payload is invalid."""

    def __init__(self, hook_name: str, message: str) -> None:
        super().__init__(f"Invalid payload for {hook_name!r}: {message}")
        self.hook_name = hook_name
        self.message = message


@dataclass(frozen=True)
class HookSuccessResponse:
    """Normalized command-hook response."""

    status: str
    message: str
    data: dict[str, Any] | None
    abort_chain: bool = False


def substitute_template_vars(value: Any, variables: Mapping[str, str]) -> Any:
    """Recursively substitute ``${VAR}`` placeholders while preserving JSON types."""
    if isinstance(value, str):
        return _substitute_text(value, variables)
    if isinstance(value, list):
        return [substitute_template_vars(item, variables) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        typed_value = cast("dict[object, Any]", value)
        return {str(key): substitute_template_vars(item, variables) for key, item in typed_value.items()}
    return value


def validate_hook_payload(hook_name: str, payload: Mapping[str, object]) -> None:
    """Validate a hook payload against the documented schema, if one exists."""
    schema = _HOOK_EVENT_SCHEMAS.get(hook_name)
    if schema is None:
        return
    try:
        validator = Draft202012Validator(cast("dict[str, Any]", schema))
        validator.validate(dict(payload))
    except Exception as exc:
        raise HookValidationError(hook_name, str(exc)) from exc


def merge_hook_payload(base_payload: Mapping[str, object], update: Mapping[str, object]) -> dict[str, object]:
    """Deep-merge hook response ``data`` into the current payload."""
    merged: dict[str, object] = dict(base_payload)
    for key, value in cast("Mapping[str, object]", update).items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = merge_hook_payload(cast("dict[str, object]", existing), cast("Mapping[str, object]", value))
        else:
            merged[key] = value
    return merged


def parse_hook_response(stdout: str) -> dict[str, Any] | None:
    """Parse a JSON hook response from stdout."""
    if not stdout.strip():
        return None
    payload = cast("object", json.loads(stdout))
    if not isinstance(payload, dict):
        raise ValueError("hook stdout JSON must be an object")
    typed_payload = cast("dict[object, Any]", payload)
    return {str(key): value for key, value in typed_payload.items()}


def normalize_hook_response(response: Mapping[str, object]) -> HookSuccessResponse:
    """Normalize a parsed hook response object."""
    raw_data = response.get("data")
    data: dict[str, Any] | None = None
    if isinstance(raw_data, Mapping):
        typed_data = cast("Mapping[object, Any]", raw_data)
        data = {str(key): value for key, value in typed_data.items()}
    status = str(response.get("status", "ok")).strip().lower() or "ok"
    message = str(response.get("message", ""))
    abort_chain = bool(response.get("abort")) or bool(response.get("continue") is False) or status == "abort"
    return HookSuccessResponse(
        status=status,
        message=message,
        data=data,
        abort_chain=abort_chain,
    )


def _substitute_text(text: str, variables: Mapping[str, str]) -> str:
    import re

    def _replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1), "")

    return re.sub(r"\$\{(\w+)\}", _replace, text)


def _string_property() -> dict[str, object]:
    return {"type": "string"}


def _integer_property() -> dict[str, object]:
    return {"type": "integer"}


def _boolean_property() -> dict[str, object]:
    return {"type": "boolean"}


def _object_property() -> dict[str, object]:
    return {"type": "object"}


def _string_array_property() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string"}}


def _nullable_string_property() -> dict[str, object]:
    return {"type": ["string", "null"]}


def _schema(
    *,
    required: list[str],
    properties: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": True,
    }


_HOOK_EVENT_SCHEMAS: dict[str, dict[str, object]] = {
    "on_task_created": _schema(
        required=["task_id", "role", "title"],
        properties={"task_id": _string_property(), "role": _string_property(), "title": _string_property()},
    ),
    "on_task_completed": _schema(
        required=["task_id", "role", "result_summary"],
        properties={"task_id": _string_property(), "role": _string_property(), "result_summary": _string_property()},
    ),
    "on_task_failed": _schema(
        required=["task_id", "role", "error"],
        properties={"task_id": _string_property(), "role": _string_property(), "error": _string_property()},
    ),
    "on_agent_spawned": _schema(
        required=["session_id", "role", "model"],
        properties={"session_id": _string_property(), "role": _string_property(), "model": _string_property()},
    ),
    "on_agent_reaped": _schema(
        required=["session_id", "role", "outcome"],
        properties={"session_id": _string_property(), "role": _string_property(), "outcome": _string_property()},
    ),
    "on_tool_error": _schema(
        required=["session_id", "tool", "error"],
        properties={
            "session_id": _string_property(),
            "tool": _string_property(),
            "error": _string_property(),
            "batch_id": _nullable_string_property(),
        },
    ),
    "on_evolve_proposal": _schema(
        required=["proposal_id", "title", "verdict"],
        properties={"proposal_id": _string_property(), "title": _string_property(), "verdict": _string_property()},
    ),
    "on_pre_task_create": _schema(
        required=["task_id", "role", "title", "description"],
        properties={
            "task_id": _string_property(),
            "role": _string_property(),
            "title": _string_property(),
            "description": _string_property(),
        },
    ),
    "on_permission_denied": _schema(
        required=["task_id", "reason", "tool", "args"],
        properties={
            "task_id": _string_property(),
            "reason": _string_property(),
            "tool": _string_property(),
            "args": _object_property(),
        },
    ),
    "on_pre_tool_use": _schema(
        required=["session_id", "tool", "tool_input"],
        properties={"session_id": _string_property(), "tool": _string_property(), "tool_input": _object_property()},
    ),
    "on_post_tool_use": _schema(
        required=["session_id", "tool", "tool_input", "result", "success"],
        properties={
            "session_id": _string_property(),
            "tool": _string_property(),
            "tool_input": _object_property(),
            "result": _string_property(),
            "success": _boolean_property(),
        },
    ),
    "on_post_tool_use_failure": _schema(
        required=["session_id", "tool", "tool_input", "error", "retries"],
        properties={
            "session_id": _string_property(),
            "tool": _string_property(),
            "tool_input": _object_property(),
            "error": _string_property(),
            "retries": _integer_property(),
        },
    ),
    "on_notification": _schema(
        required=["session_id", "level", "message"],
        properties={"session_id": _string_property(), "level": _string_property(), "message": _string_property()},
    ),
    "on_user_prompt_submit": _schema(
        required=["session_id", "prompt"],
        properties={"session_id": _string_property(), "prompt": _string_property()},
    ),
    "on_session_start": _schema(
        required=["session_id", "role", "task_id"],
        properties={"session_id": _string_property(), "role": _string_property(), "task_id": _string_property()},
    ),
    "on_session_end": _schema(
        required=["session_id", "role", "reason"],
        properties={"session_id": _string_property(), "role": _string_property(), "reason": _string_property()},
    ),
    "on_stop": _schema(
        required=["session_id", "reason", "signal"],
        properties={
            "session_id": _string_property(),
            "reason": _string_property(),
            "signal": _string_property(),
        },
    ),
    "on_stop_failure": _schema(
        required=["session_id", "reason", "error"],
        properties={"session_id": _string_property(), "reason": _string_property(), "error": _string_property()},
    ),
    "on_subagent_start": _schema(
        required=["session_id", "sub_id", "role"],
        properties={"session_id": _string_property(), "sub_id": _string_property(), "role": _string_property()},
    ),
    "on_subagent_stop": _schema(
        required=["session_id", "sub_id", "outcome"],
        properties={"session_id": _string_property(), "sub_id": _string_property(), "outcome": _string_property()},
    ),
    "on_permission_request": _schema(
        required=["session_id", "tool", "mode"],
        properties={"session_id": _string_property(), "tool": _string_property(), "mode": _string_property()},
    ),
    "on_setup": _schema(
        required=["session_id", "role", "workdir"],
        properties={"session_id": _string_property(), "role": _string_property(), "workdir": _string_property()},
    ),
    "on_teammate_idle": _schema(
        required=["session_id", "role", "queue_depth"],
        properties={"session_id": _string_property(), "role": _string_property(), "queue_depth": _integer_property()},
    ),
    "on_elicitation": _schema(
        required=["session_id", "prompt", "options"],
        properties={
            "session_id": _string_property(),
            "prompt": _string_property(),
            "options": _string_array_property(),
        },
    ),
    "on_elicitation_result": _schema(
        required=["session_id", "prompt", "response"],
        properties={"session_id": _string_property(), "prompt": _string_property(), "response": _string_property()},
    ),
    "on_config_change": _schema(
        required=["key", "old_value", "new_value"],
        properties={"key": _string_property(), "old_value": _string_property(), "new_value": _string_property()},
    ),
    "on_worktree_create": _schema(
        required=["session_id", "worktree_path", "branch"],
        properties={
            "session_id": _string_property(),
            "worktree_path": _string_property(),
            "branch": _string_property(),
        },
    ),
    "on_worktree_remove": _schema(
        required=["session_id", "worktree_path"],
        properties={"session_id": _string_property(), "worktree_path": _string_property()},
    ),
    "on_instructions_loaded": _schema(
        required=["session_id", "role", "source_paths"],
        properties={
            "session_id": _string_property(),
            "role": _string_property(),
            "source_paths": _string_array_property(),
        },
    ),
    "on_cwd_changed": _schema(
        required=["session_id", "old_cwd", "new_cwd"],
        properties={"session_id": _string_property(), "old_cwd": _string_property(), "new_cwd": _string_property()},
    ),
    "on_file_changed": _schema(
        required=["session_id", "file_path", "change_type"],
        properties={
            "session_id": _string_property(),
            "file_path": _string_property(),
            "change_type": _string_property(),
        },
    ),
}
