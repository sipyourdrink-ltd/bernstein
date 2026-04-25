"""Integration tests for the fleet aggregator with a fake task server."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from bernstein.core.fleet.aggregator import FleetAggregator, ProjectState
from bernstein.core.fleet.config import ProjectConfig
from bernstein.core.fleet.prometheus_proxy import merge_prometheus_metrics
from bernstein.core.fleet.web import build_fleet_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Handler(BaseHTTPRequestHandler):
    """Tiny stub of the Bernstein task server."""

    status_payload: dict[str, object] = {}
    bulletin_payload: list[dict[str, object]] = []
    metrics_body: str = "bernstein_tasks_total 0\n"
    sse_events: list[tuple[str, dict[str, object]]] = []

    def log_message(self, *args: object, **kwargs: object) -> None:
        return  # silence stderr in tests

    def do_GET(self) -> None:
        if self.path == "/status":
            self._json(200, self.__class__.status_payload)
        elif self.path == "/bulletin":
            self._json(200, self.__class__.bulletin_payload)
        elif self.path == "/metrics":
            body = self.__class__.metrics_body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                self.wfile.flush()
                for name, payload in list(self.__class__.sse_events):
                    line = (
                        f"event: {name}\n"
                        f"data: {json.dumps(payload)}\n\n"
                    ).encode()
                    self.wfile.write(line)
                    self.wfile.flush()
                # Keep connection alive briefly for the client to consume.
                time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _project(tmp_path: Path, name: str, port: int) -> ProjectConfig:
    return ProjectConfig(
        name=name,
        path=tmp_path,
        task_server_url=f"http://127.0.0.1:{port}",
        sdd_dir=tmp_path / ".sdd",
    )


@pytest.mark.asyncio
async def test_aggregator_against_real_http_server(tmp_path: Path) -> None:
    _Handler.status_payload = {
        "summary": {"cost_usd": 4.2, "agents": 1, "pending_approvals": 0},
        "agents": {"count": 1, "items": [{"role": "manager"}]},
        "runtime": {"state": "running", "head_sha": "abc12345"},
    }
    _Handler.sse_events = [("task.created", {"task_id": "t1", "title": "demo"})]
    with _serve(_Handler) as port:
        project = _project(tmp_path, "alpha", port)
        aggregator = FleetAggregator(
            [project], poll_interval_s=0.1, backoff_min_s=0.05, backoff_max_s=0.1
        )
        await aggregator.start()
        try:
            for _ in range(40):
                snap = aggregator.snapshot("alpha")
                if snap is not None and snap.state == ProjectState.ONLINE:
                    break
                await asyncio.sleep(0.05)
            snap = aggregator.snapshot("alpha")
            assert snap is not None
            assert snap.state == ProjectState.ONLINE
            assert snap.cost_usd == 4.2
            # Drain at least one event from the merged bus.
            event = None
            for _ in range(40):
                try:
                    event = aggregator._event_queue.get_nowait()
                    break
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)
            assert event is not None
            assert event.project == "alpha"
        finally:
            await aggregator.stop()


@pytest.mark.asyncio
async def test_offline_project_does_not_block_others(tmp_path: Path) -> None:
    """One offline project must not stall the rest of the fleet."""
    _Handler.status_payload = {
        "summary": {"cost_usd": 1.0, "agents": 0},
        "agents": {"count": 0, "items": []},
        "runtime": {"state": "idle"},
    }
    with _serve(_Handler) as port:
        online = _project(tmp_path, "alpha", port)
        offline = _project(tmp_path, "bravo", _free_port())  # no server bound
        aggregator = FleetAggregator(
            [online, offline],
            poll_interval_s=0.1,
            backoff_min_s=0.05,
            backoff_max_s=0.1,
        )
        await aggregator.start()
        try:
            for _ in range(40):
                a = aggregator.snapshot("alpha")
                b = aggregator.snapshot("bravo")
                if (
                    a is not None
                    and a.state == ProjectState.ONLINE
                    and b is not None
                    and b.state == ProjectState.OFFLINE
                ):
                    return
                await asyncio.sleep(0.05)
            pytest.fail(
                f"expected alpha=online, bravo=offline; got "
                f"{aggregator.snapshot('alpha')!s}, {aggregator.snapshot('bravo')!s}"
            )
        finally:
            await aggregator.stop()


@pytest.mark.asyncio
async def test_unified_metrics_endpoint(tmp_path: Path) -> None:
    """``merge_prometheus_metrics`` against a real HTTP server adds labels."""
    _Handler.metrics_body = "bernstein_tasks_total 7\n"
    with _serve(_Handler) as port:
        project = _project(tmp_path, "alpha", port)
        merge = await merge_prometheus_metrics([project])
        assert "alpha" in merge.ok_projects
        assert 'project="alpha"' in merge.body
        assert "bernstein_tasks_total" in merge.body


@pytest.mark.asyncio
async def test_web_app_projects_endpoint(tmp_path: Path) -> None:
    """The FastAPI app exposes ``/api/projects`` from the live aggregator."""
    _Handler.status_payload = {
        "summary": {"cost_usd": 0.5, "agents": 0},
        "agents": {"count": 0, "items": []},
        "runtime": {"state": "idle"},
    }
    with _serve(_Handler) as port:
        project = _project(tmp_path, "alpha", port)
        aggregator = FleetAggregator(
            [project], poll_interval_s=0.1, backoff_min_s=0.05, backoff_max_s=0.1
        )
        await aggregator.start()
        try:
            # Allow the poll to run at least once.
            for _ in range(40):
                snap = aggregator.snapshot("alpha")
                if snap is not None and snap.state == ProjectState.ONLINE:
                    break
                await asyncio.sleep(0.05)
            from bernstein.core.fleet.config import FleetConfig

            app = build_fleet_app(aggregator, FleetConfig(projects=[project]))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/projects")
                assert resp.status_code == 200
                payload = resp.json()
                assert payload["projects"][0]["name"] == "alpha"
                resp = await client.get("/healthz")
                assert resp.status_code == 200
        finally:
            await aggregator.stop()
