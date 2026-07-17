"""The MCP task handle carries the chain-computed progress vector (#2553)."""

from __future__ import annotations

from bernstein.core.protocols.mcp.tasks_extension import RunHandle, verify_handle
from bernstein.core.replay.progress import fold_progress


def _events(*types: str) -> list[dict[str, object]]:
    return [{"event": t, "event_hash": f"h{i}"} for i, t in enumerate(types)]


def test_handle_carries_progress_and_hash() -> None:
    vector = fold_progress(task_id="t1", ledger_phase="started", ledger_attempts=1)
    handle = RunHandle.from_journal(
        task_id="t1",
        run_id="run-1",
        events=_events("run_started"),
        chain_head="chain-head",
        progress=vector,
    )
    wire = handle.to_wire()
    assert wire["progressHash"] == vector.vector_hash()
    assert wire["progress"]["ledger_phase"] == "started"
    assert handle.progress_hash == vector.vector_hash()


def test_handle_without_progress_is_backward_compatible() -> None:
    handle = RunHandle.from_journal(
        task_id="t1",
        run_id="run-1",
        events=_events("run_started"),
        chain_head="chain-head",
    )
    wire = handle.to_wire()
    assert wire["progress"] is None
    assert wire["progressHash"] == ""
    # The receipt hash is unchanged by the additive progress field: verify still
    # holds and the pre-existing wire keys are intact.
    ok, reason = verify_handle(handle, _events("run_started"))
    assert ok, reason
    assert wire["receiptHash"] == handle.receipt_hash


def test_progress_does_not_change_receipt_hash() -> None:
    # Two handles identical but for the carried progress vector must share a
    # receipt hash: progress is additive, not part of the receipt preimage.
    base = RunHandle.from_journal(task_id="t1", run_id="run-1", events=_events("run_started"), chain_head="c")
    withp = RunHandle.from_journal(
        task_id="t1",
        run_id="run-1",
        events=_events("run_started"),
        chain_head="c",
        progress=fold_progress(task_id="t1", ledger_phase="completed"),
    )
    assert base.receipt_hash == withp.receipt_hash
