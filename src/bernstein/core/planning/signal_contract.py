"""The existing native completion-signal grammar at the plan boundary."""

from __future__ import annotations

from typing import cast, get_args

from bernstein.core.tasks.models import CompletionSignal, CompletionSignalType

COMPLETION_SIGNAL_TYPES: tuple[str, ...] = get_args(CompletionSignalType)
SIGNAL_STRING_FIELDS = ("value", "path", "command", "contains")


def parse_completion_signal(raw: object) -> CompletionSignal:
    """Normalize existing plan aliases, rejecting a declared gate that cannot run.

    Callers add the stage/step/index context to these field-level errors.
    Unknown keys retain the plan validator's separate typo diagnostics.
    """
    if not isinstance(raw, dict):
        raise ValueError("expected a mapping")
    kind = raw.get("type")
    if not isinstance(kind, str):
        raise ValueError("type: expected a string")
    if kind not in COMPLETION_SIGNAL_TYPES:
        raise ValueError(f"type: expected a supported completion signal, got {kind!r}")
    for field in SIGNAL_STRING_FIELDS:
        if field in raw:
            value = raw[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field}: expected a non-empty string")
    if kind == "file_contains":
        if "value" in raw:
            value = raw["value"]
            if " :: " not in value:
                raise ValueError("file_contains value must be '<path> :: <text>' (or give 'path' and 'contains')")
        elif "path" in raw and "contains" in raw:
            value = f"{raw['path']} :: {raw['contains']}"
        else:
            raise ValueError("file_contains needs both 'path' and 'contains'")
    else:
        value = next((raw[field] for field in ("value", "path", "command") if field in raw), None)
        if value is None:
            raise ValueError("expected a non-empty 'value', 'path', or 'command'")
    return CompletionSignal(type=cast("CompletionSignalType", kind), value=value)
