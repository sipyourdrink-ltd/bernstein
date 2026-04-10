"""MCP Gateway Proxy — transparent recording and replay.

Intercepts all JSON-RPC MCP traffic between clients and upstream servers,
recording each tool call to the WAL and supporting offline replay.

Architecture:
- MCPGateway: spawns upstream process, proxies JSON-RPC bidirectionally
- GatewayReplay: serves recorded responses from WAL (no upstream needed)
- ToolMetrics: per-tool call/latency/error tracking
- create_gateway_sse_app: FastAPI SSE server for MCP SSE transport

WAL decision_type: "mcp_tool_call"
WAL inputs:  {method, server_name, tool_name, arguments, request_id}
WAL output:  {result, error, latency_ms}
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.wal import WALWriter


# ---------------------------------------------------------------------------
# ToolMetrics
# ---------------------------------------------------------------------------


@dataclass
class ToolMetrics:
    """Per-tool call metrics accumulated during a gateway session."""

    tool_name: str
    total_calls: int = 0
    error_count: int = 0
    latency_samples: list[float] = field(default_factory=list)

    def record(self, latency_ms: float, *, error: bool = False) -> None:
        """Record one call."""
        self.total_calls += 1
        self.latency_samples.append(latency_ms)
        if error:
            self.error_count += 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize metrics to a JSON-compatible dict."""
        samples = sorted(self.latency_samples)
        n = len(samples)

        def _pct(p: float) -> float:
            return round(samples[int(n * p)] if n else 0.0, 2)

        return {
            "tool_name": self.tool_name,
            "total_calls": self.total_calls,
            "error_count": self.error_count,
            "error_rate": round(self.error_count / self.total_calls, 4) if self.total_calls else 0.0,
            "latency_p50_ms": _pct(0.5),
            "latency_p90_ms": _pct(0.9),
            "latency_p99_ms": _pct(0.99),
        }


# ---------------------------------------------------------------------------
# GatewayReplay
# ---------------------------------------------------------------------------


class GatewayReplay:
    """Serves recorded MCP responses from the WAL for offline replay.

    Builds an in-memory index of method:tool_name → last recorded output
    on construction, so replay is O(1) per lookup.
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        from bernstein.core.wal import WALReader

        self._reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)
        self._index: dict[str, dict[str, Any]] = {}
        self._build_index()

    def _build_index(self) -> None:
        """Index all mcp_tool_call entries by method:tool_name."""
        try:
            for entry in self._reader.iter_entries():
                if entry.decision_type == "mcp_tool_call":
                    key = self._make_key(
                        entry.inputs.get("method", ""),
                        entry.inputs.get("tool_name", ""),
                    )
                    self._index[key] = entry.output
        except FileNotFoundError:
            pass  # No cache file yet; start with empty index
        except Exception:
            # Malformed or partially-written WAL — load what was indexed so far
            # and continue without crashing.  This is intentionally broad: any
            # corruption in the WAL file must not take down the gateway process.
            pass

    @staticmethod
    def _make_key(method: str, tool_name: str) -> str:
        return f"{method}:{tool_name}" if tool_name else method

    def find_response(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return the recorded output for this method/tool, or None if not found."""
        tool_name = str(params.get("name", "")) if method == "tools/call" else ""
        return self._index.get(self._make_key(method, tool_name))

    @property
    def indexed_count(self) -> int:
        """Number of distinct call patterns indexed."""
        return len(self._index)


# ---------------------------------------------------------------------------
# MCPGateway
# ---------------------------------------------------------------------------


class MCPGateway:
    """Transparent MCP JSON-RPC proxy with WAL recording and optional replay.

    Spawns an upstream MCP server as a subprocess (stdio transport) and
    intercepts all JSON-RPC traffic, recording every request/response pair
    to the WAL with ``decision_type="mcp_tool_call"``.

    In replay mode (``replay`` is not None), serves recorded responses from
    the WAL without connecting to any upstream process.

    Usage (stdio proxy)::

        writer = WALWriter(run_id="gw-abc123", sdd_dir=Path(".sdd"))
        gw = MCPGateway(upstream_cmd=["uvx", "mcp-server-git"], wal_writer=writer)
        await gw.start()
        await gw.run_stdio()   # blocks until stdin EOF

    Usage (replay)::

        replay = GatewayReplay(run_id="gw-abc123", sdd_dir=Path(".sdd"))
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay)
        await gw.start()       # no-op in replay mode
        await gw.run_stdio()
    """

    def __init__(
        self,
        upstream_cmd: list[str],
        wal_writer: WALWriter,
        replay: GatewayReplay | None = None,
        *,
        server_name: str = "unknown",
    ) -> None:
        self._upstream_cmd = upstream_cmd
        self._wal_writer = wal_writer
        self._replay = replay
        self._server_name = server_name.strip() or "unknown"
        self._metrics: dict[str, ToolMetrics] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the upstream subprocess (no-op in replay mode)."""
        if self._replay:
            return
        if not self._upstream_cmd:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._upstream_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_upstream_loop())

    async def stop(self) -> None:
        """Terminate the upstream process gracefully."""
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                self._proc.kill()

    # ------------------------------------------------------------------
    # Upstream I/O
    # ------------------------------------------------------------------

    async def _read_upstream_loop(self) -> None:
        """Read JSON-RPC responses from upstream stdout and dispatch to waiters."""
        assert self._proc and self._proc.stdout
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                req_id = msg.get("id")
                if req_id is not None:
                    fut = self._pending.get(req_id)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except Exception:
            # Upstream died — fail all pending futures
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("Upstream process died"))

    async def _send_upstream(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC line to the upstream subprocess stdin."""
        assert self._proc and self._proc.stdin
        line = json.dumps(message, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    # ------------------------------------------------------------------
    # Core proxy
    # ------------------------------------------------------------------

    async def handle_jsonrpc(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message, recording to WAL.

        Args:
            message: Parsed JSON-RPC request or notification.

        Returns:
            Response dict for requests (``id`` present), ``None`` for notifications.
        """
        method = str(message.get("method", ""))
        params: dict[str, Any] = message.get("params") or {}
        req_id = message.get("id")
        is_notification = "id" not in message

        # ------------------------------------------------------------------
        # Replay mode
        # ------------------------------------------------------------------
        if self._replay is not None:
            if is_notification:
                return None
            recorded = self._replay.find_response(method, params)
            if recorded is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": recorded.get("result"),
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "No recorded response for this call"},
            }

        # ------------------------------------------------------------------
        # Live proxy mode
        # ------------------------------------------------------------------
        if is_notification:
            if self._proc:
                await self._send_upstream(message)
            return None

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        t0 = time.monotonic()
        try:
            await self._send_upstream(message)
            response: dict[str, Any] = await asyncio.wait_for(asyncio.shield(fut), timeout=30.0)
        finally:
            self._pending.pop(req_id, None)

        latency_ms = (time.monotonic() - t0) * 1000.0
        tool_name = str(params.get("name", "")) if method == "tools/call" else ""
        has_error = response.get("error") is not None

        # WAL record
        self._wal_writer.append(
            decision_type="mcp_tool_call",
            inputs={
                "method": method,
                "server_name": self._server_name,
                "tool_name": tool_name,
                "arguments": params.get("arguments", {}),
                "request_id": req_id,
            },
            output={
                "result": response.get("result"),
                "error": response.get("error"),
                "latency_ms": round(latency_ms, 2),
            },
            actor="mcp_gateway",
        )

        # Metrics
        metric_key = f"tools/call:{tool_name}" if tool_name else method
        if metric_key not in self._metrics:
            self._metrics[metric_key] = ToolMetrics(tool_name=metric_key)
        self._metrics[metric_key].record(latency_ms, error=has_error)

        return response

    # ------------------------------------------------------------------
    # Transport runners
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        """Run as a stdio proxy. Reads from stdin, writes to stdout. Blocks until EOF."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            response = await self.handle_jsonrpc(message)
            if response is not None:
                out = json.dumps(response, separators=(",", ":")) + "\n"
                sys.stdout.buffer.write(out.encode())
                sys.stdout.buffer.flush()

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """Return current per-tool metrics as a JSON-serializable dict."""
        return {key: m.to_dict() for key, m in self._metrics.items()}


# ---------------------------------------------------------------------------
# SSE gateway app
# ---------------------------------------------------------------------------


def create_gateway_sse_app(gateway: MCPGateway, *, run_id: str) -> Any:
    """Create a FastAPI SSE app that proxies MCP over HTTP.

    Implements a minimal MCP SSE transport:
    - ``GET /sse``     — opens SSE stream; sends session endpoint URL as first event
    - ``POST /message`` — accepts JSON-RPC, forwards through gateway, pushes
                         response back via SSE

    Args:
        gateway: Configured MCPGateway (already started).
        run_id: Current WAL run ID, included in response headers for tracing.

    Returns:
        A FastAPI application instance.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app: Any = FastAPI(title="bernstein-mcp-gateway", version="1.0.0")
    app.state.gateway = gateway
    app.state.run_id = run_id

    # Per-session SSE queues: session_id → asyncio.Queue
    _sessions: dict[str, asyncio.Queue[str | None]] = {}

    @app.get("/sse")
    def sse_endpoint(request: Request) -> StreamingResponse:  # type: ignore[misc]
        """Open an SSE stream and receive an endpoint URL for sending requests."""
        session_id = uuid.uuid4().hex
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        _sessions[session_id] = queue

        async def _event_stream() -> Any:
            # MCP SSE spec: first event tells client where to POST
            yield f"event: endpoint\ndata: /message?sessionId={session_id}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        break
                    yield f"data: {item}\n\n"
            finally:
                _sessions.pop(session_id, None)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Bernstein-Run-ID": run_id,
            },
        )

    @app.post("/message")
    async def message_endpoint(request: Request) -> JSONResponse:  # type: ignore[misc]
        """Accept a JSON-RPC request and push the response to the SSE stream."""
        session_id = request.query_params.get("sessionId", "")
        body: dict[str, Any] = await request.json()

        response = await gateway.handle_jsonrpc(body)

        if response is not None and session_id in _sessions:
            await _sessions[session_id].put(json.dumps(response, separators=(",", ":")))

        return JSONResponse({"status": "accepted"})

    @app.get("/gateway/metrics")
    def metrics_endpoint(request: Request) -> JSONResponse:  # type: ignore[misc]
        """Return current per-tool metrics for this gateway session."""
        return JSONResponse(
            {
                "run_id": run_id,
                "metrics": gateway.get_metrics(),
            }
        )

    return app
