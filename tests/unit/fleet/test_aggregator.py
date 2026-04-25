"""Tests for the fleet aggregator fan-out and SSE reconnect."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from bernstein.core.fleet.aggregator import (
    FleetAggregator,
    ProjectState,
    _extract_snapshot_fields,
)
from bernstein.core.fleet.config import ProjectConfig


def _project(tmp_path: Path, name: str, port: int = 8052) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        path=tmp_path,
        task_server_url=f"http://127.0.0.1:{port}",
        sdd_dir=tmp_path / ".sdd",
    )


def test_extract_snapshot_fields_handles_minimum() -> None:
    """A barebones status payload still yields a usable snapshot."""
    fields = _extract_snapshot_fields(
        {
            "summary": {"cost_usd": 1.5, "agents": 2},
            "agents": {"count": 2, "items": [{"role": "backend"}, {"role": "qa"}]},
            "runtime": {"state": "running", "head_sha": "abcdef1234567890"},
        }
    )
    assert fields["agents"] == 2
    assert fields["active_agents_roles"] == ["backend", "qa"]
    assert fields["last_sha"].startswith("abcdef")
    assert fields["cost_usd"] == 1.5
    assert fields["run_state"] == "running"


@pytest.mark.asyncio
async def test_poll_once_updates_snapshot(tmp_path: Path) -> None:
    """A successful ``/status`` response transitions a row to ONLINE."""
    project = _project(tmp_path, "alpha")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(
            200,
            json={
                "summary": {"cost_usd": 7.0, "agents": 1, "pending_approvals": 2},
                "agents": {"count": 1, "items": [{"role": "manager"}]},
                "runtime": {"state": "running", "head_sha": "deadbeef1234"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    aggregator = FleetAggregator([project], client=client, poll_interval_s=0.01)
    await aggregator._poll_once(project)
    snap = aggregator.snapshot("alpha")
    assert snap is not None
    assert snap.state == ProjectState.ONLINE
    assert snap.cost_usd == 7.0
    assert snap.last_sha.startswith("deadbeef")
    assert snap.cost_history[-1] == 7.0
    await client.aclose()


@pytest.mark.asyncio
async def test_offline_marker_on_connection_error(tmp_path: Path) -> None:
    """A poll failure flips the row to OFFLINE without crashing."""
    project = _project(tmp_path, "down")

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    aggregator = FleetAggregator([project], client=client)
    with pytest.raises(httpx.ConnectError):
        await aggregator._poll_once(project)
    aggregator._mark_offline("down", "refused")
    snap = aggregator.snapshot("down")
    assert snap is not None
    assert snap.state == ProjectState.OFFLINE
    assert snap.offline_since is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_sse_emit_handles_well_formed_event(tmp_path: Path) -> None:
    """``_emit`` decodes JSON payloads and tags them with the project name."""
    project = _project(tmp_path, "sse")
    aggregator = FleetAggregator([project])
    try:
        await aggregator._emit(
            "sse",
            "cost.update",
            json.dumps({"total_usd": 1.23, "agent": "manager"}),
        )
        event = aggregator._event_queue.get_nowait()
    finally:
        await aggregator.stop()
    assert event.event == "cost.update"
    assert event.project == "sse"
    assert event.data["total_usd"] == 1.23


@pytest.mark.asyncio
async def test_sse_emit_drops_oldest_when_full(tmp_path: Path) -> None:
    """When the bus is full the oldest event is dropped, never the newest."""
    project = _project(tmp_path, "alpha")
    aggregator = FleetAggregator([project])
    aggregator._event_queue = asyncio.Queue(maxsize=2)
    try:
        await aggregator._emit("alpha", "a", "{}")
        await aggregator._emit("alpha", "b", "{}")
        await aggregator._emit("alpha", "c", "{}")
        events = []
        while True:
            try:
                events.append(aggregator._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        await aggregator.stop()
    names = [e.event for e in events]
    assert names[-1] == "c"
    assert "a" not in names  # oldest dropped


@pytest.mark.asyncio
async def test_offline_recovery_to_online(tmp_path: Path) -> None:
    """An OFFLINE row can be brought back to ONLINE by a successful poll."""
    project = _project(tmp_path, "alpha")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"summary": {"cost_usd": 1.0}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    aggregator = FleetAggregator([project], client=client)
    aggregator._mark_offline("alpha", "boom")
    snap = aggregator.snapshot("alpha")
    assert snap is not None and snap.state == ProjectState.OFFLINE
    await aggregator._poll_once(project)
    snap = aggregator.snapshot("alpha")
    assert snap is not None and snap.state == ProjectState.ONLINE
    await client.aclose()


@pytest.mark.asyncio
async def test_sse_reconnect_uses_backoff(tmp_path: Path) -> None:
    """``_sse_loop`` retries on errors with bounded backoff and respects stop."""
    project = _project(tmp_path, "sse")
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("nope")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    aggregator = FleetAggregator(
        [project],
        client=client,
        backoff_min_s=0.01,
        backoff_max_s=0.02,
    )
    task = asyncio.create_task(aggregator._sse_loop(project))
    try:
        await asyncio.sleep(0.1)
    finally:
        aggregator._stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await client.aclose()
    assert attempts["n"] >= 2  # multiple retries occurred under backoff
    snap = aggregator.snapshot("sse")
    assert snap is not None
    assert snap.state == ProjectState.OFFLINE
