"""Telemetry contract — strict schema for agent output metadata.

Every eval run validates agent telemetry against this schema.
Schema violations degrade the reliability gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast


@dataclass(frozen=True)
class AgentTelemetry:
    """Validated telemetry from a single agent task execution.

    Attributes:
        task_id: ID of the evaluated task.
        duration_s: Wall-clock seconds the agent spent.
        turns_used: Number of LLM turns consumed.
        files_read: Files the agent read.
        files_modified: Files the agent modified.
        tokens_input: Total input tokens consumed.
        tokens_output: Total output tokens generated.
        cost_usd: Estimated cost in USD.
        tests_run: Number of tests executed.
        tests_passed: Number of tests that passed.
        tests_failed: Number of tests that failed.
        completion_signals_checked: Signals the harness checked.
        completion_signals_passed: Signals that passed.
    """

    task_id: str
    duration_s: float = 0.0
    turns_used: int = 0
    files_read: list[str] = field(default_factory=list[str])
    files_modified: list[str] = field(default_factory=list[str])
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    completion_signals_checked: int = 0
    completion_signals_passed: int = 0


_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "task_id",
        "duration_s",
        "turns_used",
        "tokens_input",
        "tokens_output",
        "cost_usd",
        "tests_run",
        "tests_passed",
        "tests_failed",
        "completion_signals_checked",
        "completion_signals_passed",
    }
)


@dataclass(frozen=True)
class TelemetryValidation:
    """Result of validating a telemetry payload."""

    valid: bool
    missing_fields: list[str] = field(default_factory=list[str])
    invalid_fields: list[str] = field(default_factory=list[str])
    penalty: float = 1.0  # Multiplicative penalty (1.0 = no penalty, 0.5 = schema violation)


def validate_telemetry(raw: dict[str, object]) -> TelemetryValidation:
    """Validate a raw telemetry dict against the schema.

    Args:
        raw: Dict of telemetry key-value pairs from agent output.

    Returns:
        TelemetryValidation with validity status and any penalties.
    """
    missing: list[str] = []
    invalid: list[str] = []

    for f in _REQUIRED_FIELDS:
        if f not in raw:
            missing.append(f)
        elif not isinstance(raw[f], int | float | str):
            invalid.append(f)

    # Numeric fields must be non-negative
    _NUMERIC = _REQUIRED_FIELDS - {"task_id"}
    for f in _NUMERIC:
        val = raw.get(f)
        if isinstance(val, int | float) and val < 0:
            invalid.append(f"{f} (negative)")

    if missing or invalid:
        return TelemetryValidation(
            valid=False,
            missing_fields=missing,
            invalid_fields=invalid,
            penalty=0.5,
        )

    return TelemetryValidation(valid=True, penalty=1.0)


def parse_telemetry(raw: dict[str, object]) -> AgentTelemetry:
    """Parse a raw dict into an AgentTelemetry, using defaults for missing fields.

    Args:
        raw: Dict of telemetry key-value pairs.

    Returns:
        AgentTelemetry dataclass with parsed values.
    """

    def _float(key: str, default: float = 0.0) -> float:
        v = raw.get(key, default)
        return float(v) if isinstance(v, int | float) else default

    def _int(key: str, default: int = 0) -> int:
        v = raw.get(key, default)
        return int(v) if isinstance(v, int | float) else default

    def _str_list(key: str) -> list[str]:
        v: object = raw.get(key, [])
        if isinstance(v, list):
            return [str(item) for item in cast("list[object]", v)]
        return []

    return AgentTelemetry(
        task_id=str(raw.get("task_id", "")),
        duration_s=_float("duration_s"),
        turns_used=_int("turns_used"),
        files_read=_str_list("files_read"),
        files_modified=_str_list("files_modified"),
        tokens_input=_int("tokens_input"),
        tokens_output=_int("tokens_output"),
        cost_usd=_float("cost_usd"),
        tests_run=_int("tests_run"),
        tests_passed=_int("tests_passed"),
        tests_failed=_int("tests_failed"),
        completion_signals_checked=_int("completion_signals_checked"),
        completion_signals_passed=_int("completion_signals_passed"),
    )
