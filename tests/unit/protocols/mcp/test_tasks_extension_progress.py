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
    # A handle with no progress verifies with no authoritative vector supplied.
    ok, reason = verify_handle(handle, _events("run_started"))
    assert ok, reason
    assert wire["receiptHash"] == handle.receipt_hash


def test_progress_is_bound_to_the_receipt_hash() -> None:
    # The progress vector is part of the receipt pre-image: two handles that
    # differ only in their carried progress must have different receipt hashes,
    # so a client cannot swap the vector without invalidating the receipt.
    base = RunHandle.from_journal(task_id="t1", run_id="run-1", events=_events("run_started"), chain_head="c")
    withp = RunHandle.from_journal(
        task_id="t1",
        run_id="run-1",
        events=_events("run_started"),
        chain_head="c",
        progress=fold_progress(task_id="t1", ledger_phase="completed"),
    )
    assert base.receipt_hash != withp.receipt_hash


def test_verify_requires_authoritative_progress() -> None:
    authoritative = fold_progress(task_id="t1", ledger_phase="started", ledger_attempts=1)
    handle = RunHandle.from_journal(
        task_id="t1", run_id="run-1", events=_events("run_started"), chain_head="c", progress=authoritative
    )
    # Supplying the authoritative vector verifies.
    ok, reason = verify_handle(handle, _events("run_started"), progress=authoritative)
    assert ok, reason
    # A handle carrying progress cannot be verified without the authoritative one.
    ok, reason = verify_handle(handle, _events("run_started"))
    assert not ok
    assert reason is not None and "authoritative" in reason
    # A forged progress vector fails against the authoritative projection.
    forged = fold_progress(task_id="t1", ledger_phase="completed", ledger_attempts=99)
    ok, reason = verify_handle(handle, _events("run_started"), progress=forged)
    assert not ok
