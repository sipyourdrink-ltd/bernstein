"""Journal-fold progress notifications on the poll tool (#3085).

An hours-long run shows an in-band signal only when the request asked for
one (a ``progressToken``), the signal's every value is a pure fold of the
journal (no wall clock, no model output), a tick that does not strictly
advance the previous tick is suppressed, and a failure to emit never
reaches the tool result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bernstein.core.replay.progress import ProgressVector, fold_progress
from bernstein.mcp.server import (
    _PROGRESS_TICKS,
    _maybe_emit_progress,
    _progress_notification_payload,
    _run_status_impl,
    _should_emit_progress,
)

_ROWS = [
    {"event": "retry.checkpoint"},
    {"event": "task_diff_captured"},
    {"event": "task_review_decision"},
    {"event": "artifact_posted"},
]


@pytest.fixture(autouse=True)
def _reset_tick_cache() -> Any:
    _PROGRESS_TICKS.clear()
    yield
    _PROGRESS_TICKS.clear()


def _ctx(token: str | None) -> Any:
    meta = {"progress_token": token}
    rc = SimpleNamespace(meta=meta)
    return SimpleNamespace(request_context=rc, report_progress=AsyncMock())


def _vector(**overrides: Any) -> ProgressVector:
    base: dict[str, Any] = {
        "task_id": "run-1",
        "journal_rows": _ROWS,
        "ledger_phase": "started",
        "ledger_attempts": 1,
        "evidence_declared": 4,
        "evidence_passed": 2,
    }
    base.update(overrides)
    return fold_progress(**base)


# ---------------------------------------------------------------------------
# Payload shaping: journal-derived only, byte-identical across folds
# ---------------------------------------------------------------------------


def test_two_folds_produce_byte_identical_notification_payloads() -> None:
    first = _progress_notification_payload(_vector())
    second = _progress_notification_payload(_vector())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_payload_fields_come_from_the_fold_alone() -> None:
    vector = _vector()
    payload = _progress_notification_payload(vector)
    assert payload["progress"] == vector.earned_steps
    assert payload["total"] == vector.evidence_declared
    # The message renders fold counters only - no timestamps, no free text.
    assert payload["message"] == "phase=started checkpoints=1 diffs=1 gates=1 evidence=2/4"


def test_total_is_omitted_when_no_evidence_is_declared() -> None:
    payload = _progress_notification_payload(_vector(evidence_declared=0, evidence_passed=0))
    assert payload["total"] is None


# ---------------------------------------------------------------------------
# Monotone suppression via strictly_advances
# ---------------------------------------------------------------------------


def test_first_tick_always_emits() -> None:
    assert _should_emit_progress(None, _vector()) is True


def test_unchanged_tick_is_suppressed() -> None:
    assert _should_emit_progress(_vector(), _vector()) is False


def test_regressed_tick_is_suppressed() -> None:
    advanced = _vector()
    regressed = _vector(journal_rows=_ROWS[:1])
    assert _should_emit_progress(advanced, regressed) is False


def test_strictly_advancing_tick_emits() -> None:
    previous = _vector(journal_rows=_ROWS[:1])
    current = _vector()
    assert _should_emit_progress(previous, current) is True


# ---------------------------------------------------------------------------
# Emission wiring on the poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_means_no_notification(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(token=None)
    await _maybe_emit_progress(ctx, tmp_path / ".sdd", "run-1")
    ctx.report_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_emits_one_tick_and_suppresses_the_unchanged_repoll(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bernstein.core.replay.progress as progress_module

    monkeypatch.setattr(progress_module, "project_task_progress", lambda *_a, **_k: _vector())
    ctx = _ctx(token="tok-1")
    await _maybe_emit_progress(ctx, tmp_path / ".sdd", "run-1")
    assert ctx.report_progress.await_count == 1
    kwargs = ctx.report_progress.await_args.kwargs
    assert kwargs["progress"] == float(_vector().earned_steps)
    assert kwargs["total"] == float(_vector().evidence_declared)

    # The same fold again does not strictly advance: suppressed.
    ctx2 = _ctx(token="tok-2")
    await _maybe_emit_progress(ctx2, tmp_path / ".sdd", "run-1")
    ctx2.report_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_advancing_fold_emits_again(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.core.replay.progress as progress_module

    monkeypatch.setattr(progress_module, "project_task_progress", lambda *_a, **_k: _vector(journal_rows=_ROWS[:1]))
    ctx = _ctx(token="tok-1")
    await _maybe_emit_progress(ctx, tmp_path / ".sdd", "run-1")
    assert ctx.report_progress.await_count == 1

    monkeypatch.setattr(progress_module, "project_task_progress", lambda *_a, **_k: _vector())
    ctx2 = _ctx(token="tok-2")
    await _maybe_emit_progress(ctx2, tmp_path / ".sdd", "run-1")
    assert ctx2.report_progress.await_count == 1


@pytest.mark.asyncio
async def test_emission_failure_never_reaches_the_tool_result(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.replay.journal import EventJournal

    monkeypatch.chdir(tmp_path)
    journal = EventJournal("run-3085", tmp_path / ".sdd")
    journal.record("run_started", goal="g")

    ctx = _ctx(token="tok-1")
    ctx.report_progress = AsyncMock(side_effect=RuntimeError("stream gone"))
    out = await _run_status_impl("run-3085", ".", ctx)
    body = json.loads(out)
    assert body["runId"] == "run-3085"
    assert "error" not in body


@pytest.mark.asyncio
async def test_no_token_poll_result_is_unchanged(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients that send no progress token see exactly the old behaviour."""
    from bernstein.core.replay.journal import EventJournal

    monkeypatch.chdir(tmp_path)
    journal = EventJournal("run-3085", tmp_path / ".sdd")
    journal.record("run_started", goal="g")

    without_ctx = json.loads(await _run_status_impl("run-3085", "."))
    with_tokenless_ctx = json.loads(await _run_status_impl("run-3085", ".", _ctx(token=None)))
    assert without_ctx == with_tokenless_ctx


@pytest.mark.asyncio
async def test_native_sdk_client_receives_only_advancing_journal_progress(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise SDK metadata normalization and actual notification delivery."""
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.tasks.checkpoint_retry import task_run_id
    from bernstein.mcp.server import create_mcp_server
    from tests.support.mcp_wire import sdk_mcp_session

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_MCP_COST_METER", "0")
    run_id = "run-native-3085"
    journal = EventJournal(run_id, tmp_path / ".sdd")
    journal.record("run_started", goal="offline notification regression")
    task_journal = EventJournal(task_run_id(run_id), tmp_path / ".sdd")
    notifications: list[tuple[float, float | None, str | None]] = []

    async def progress(progress: float, total: float | None, message: str | None) -> None:
        notifications.append((progress, total, message))

    async with sdk_mcp_session(create_mcp_server()) as client:
        arguments = {"run_id": run_id, "workdir": str(tmp_path)}
        plain = await client.call_tool("bernstein_run_status", arguments)
        assert not plain.is_error
        assert run_id not in _PROGRESS_TICKS

        first = await client.call_tool("bernstein_run_status", arguments, progress_callback=progress)
        assert not first.is_error
        assert len(notifications) == 1
        vector = _PROGRESS_TICKS[run_id]
        payload = _progress_notification_payload(vector)
        assert notifications[0] == (float(payload["progress"]), payload["total"], payload["message"])

        repeated = await client.call_tool("bernstein_run_status", arguments, progress_callback=progress)
        assert not repeated.is_error
        assert len(notifications) == 1

        task_journal.record("retry.checkpoint")
        advanced = await client.call_tool("bernstein_run_status", arguments, progress_callback=progress)
        assert not advanced.is_error
        assert len(notifications) == 2
        assert _PROGRESS_TICKS[run_id].strictly_advances(vector)
