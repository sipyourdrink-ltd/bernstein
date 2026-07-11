"""Gate-decision ingestion into the hook JSONL sidecar (issue #2360).

Every in-process gate decision the worker's hook makes is streamed into the
same JSONL sidecar the orchestrator and monitors already consume, linked to the
gate receipt it sealed. This is the event-to-receipt mapping in ingestion: an
operator reading the sidecar sees the decision and the receipt id, and the
existing hook-event pipeline recognises the new record type.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.server.hooks_receiver import (
    HookEventType,
    parse_hook_event,
    write_gate_decision_event,
)

if TYPE_CHECKING:
    from pathlib import Path


def _read_sidecar(tmp_path: Path, session_id: str) -> list[dict[str, object]]:
    sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / f"{session_id}.jsonl"
    if not sidecar.is_file():
        return []
    return [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line]


def test_gate_decision_event_type_is_recognised() -> None:
    body = {"hook_event_name": "GateDecision", "gate_event": "pretooluse"}
    event = parse_hook_event("qa-abc12345", body)
    assert event.event_type is HookEventType.GATE_DECISION


def test_write_gate_decision_appends_linked_record(tmp_path: Path) -> None:
    write_gate_decision_event(
        "qa-abc12345",
        tmp_path,
        gate_event="pretooluse",
        blocked=True,
        reason="out-of-scope write refused: infra/prod.tf",
        receipt_task_id="T-9#gate:pretooluse:1700000000",
    )
    records = _read_sidecar(tmp_path, "qa-abc12345")
    assert len(records) == 1
    row = records[0]
    assert row["event_type"] == "GateDecision"
    assert row["gate_event"] == "pretooluse"
    assert row["blocked"] is True
    assert row["receipt_task_id"] == "T-9#gate:pretooluse:1700000000"
    assert "infra/prod.tf" in row["reason"]


def test_write_gate_decision_rejects_unsafe_session(tmp_path: Path) -> None:
    from bernstein.core.server.hooks_receiver import InvalidSessionIdError

    raised = False
    try:
        write_gate_decision_event(
            "../../etc",
            tmp_path,
            gate_event="pretooluse",
            blocked=True,
            reason="x",
            receipt_task_id="t",
        )
    except InvalidSessionIdError:
        raised = True
    assert raised
    assert _read_sidecar(tmp_path, "any") == []
