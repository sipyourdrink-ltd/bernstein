"""MCP Gateway Proxy - transparent recording and replay.

Intercepts all JSON-RPC MCP traffic between clients and upstream servers,
recording each tool call to the WAL and supporting offline replay.

Architecture:
- MCPGateway: spawns upstream process, proxies JSON-RPC bidirectionally
- GatewayReplay: serves recorded responses from WAL (no upstream needed)
- ToolMetrics: per-tool call/latency/error tracking
- create_gateway_sse_app: FastAPI SSE server for MCP SSE transport

The SSE app is stateless (issue #2506): responses are correlated to their
requests by the content-derived span id (from the request ``_meta`` when
present, otherwise a deterministic projection of the request content), so
any gateway instance can serve any request with no per-client state. When a
run journal / audit chain is wired in, every proxied call is anchored as an
``mcp.stateless_call`` chain entry.

WAL decision_type: "mcp_tool_call"
WAL inputs:  {method, server_name, tool_name, arguments, request_id}
WAL output:  {result, error, latency_ms}
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.mcp.stateless_core import anchor_stateless_call, request_span_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.protocols.payments.x402 import X402SettlementCoordinator
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.wal import WALEntry, WALWriter


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
            # Malformed or partially-written WAL - load what was indexed so far
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
        journal: EventJournal | None = None,
        audit_chain: AuditChainStore | None = None,
        settlement: X402SettlementCoordinator | None = None,
    ) -> None:
        self._upstream_cmd = upstream_cmd
        self._wal_writer = wal_writer
        self._replay = replay
        self._server_name = server_name.strip() or "unknown"
        self._journal = journal
        self._audit_chain = audit_chain
        # x402 settlement coordinator (issue #2528). ``None`` keeps the gateway
        # byte-identical to the pre-settlement proxy; a 402 then surfaces as an
        # ordinary tool error. The coordinator gates against a spending mandate
        # before any payment. The settlement seam lives on the live-proxy
        # branch only, so replay can never invoke the hook or double-settle.
        self._settlement = settlement
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
            # Upstream died - fail all pending futures
            for fut in self._pending.values():
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

    def _handle_replay(
        self, method: str, params: dict[str, Any], req_id: Any, is_notification: bool
    ) -> dict[str, Any] | None:
        """Handle a message in replay mode."""
        assert self._replay is not None
        if is_notification:
            return None
        recorded = self._replay.find_response(method, params)
        if recorded is not None:
            return {"jsonrpc": "2.0", "id": req_id, "result": recorded.get("result")}
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": "No recorded response for this call"},
        }

    def _record_wal_and_metrics(
        self, method: str, params: dict[str, Any], req_id: Any, response: dict[str, Any], latency_ms: float
    ) -> WALEntry:
        """Write WAL record and update per-tool metrics; return the WAL entry.

        The returned entry's ``entry_hash`` is the WAL invocation digest an
        x402 spend receipt binds (issue #2528), so a settlement is provably
        tied to the exact recorded call it paid for.
        """
        tool_name = str(params.get("name", "")) if method == "tools/call" else ""
        has_error = response.get("error") is not None
        entry = self._wal_writer.append(
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
        metric_key = f"tools/call:{tool_name}" if tool_name else method
        if metric_key not in self._metrics:
            self._metrics[metric_key] = ToolMetrics(tool_name=metric_key)
        self._metrics[metric_key].record(latency_ms, error=has_error)
        return entry

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

        if self._replay is not None:
            return self._handle_replay(method, params, req_id, is_notification)

        if is_notification:
            if self._proc:
                await self._send_upstream(message)
            return None

        response, latency_ms = await self._send_request(message, req_id)
        self._record_wal_and_metrics(method, params, req_id, response, latency_ms)
        self._anchor_proxied_call(method, params)

        if self._settlement is not None and method == "tools/call":
            response = await self._maybe_settle(message, params, response)

        return response

    async def _send_request(self, message: dict[str, Any], req_id: Any) -> tuple[dict[str, Any], float]:
        """Send one JSON-RPC request upstream and await its response.

        Returns the response and the round-trip latency in milliseconds. The
        pending-future dance is factored out here so the x402 retry (issue
        #2528) reuses the identical send/await path as the original call.
        """
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        t0 = time.monotonic()
        try:
            await self._send_upstream(message)
            response: dict[str, Any] = await asyncio.wait_for(asyncio.shield(fut), timeout=30.0)
        finally:
            self._pending.pop(req_id, None)
        return response, (time.monotonic() - t0) * 1000.0

    async def _maybe_settle(
        self, message: dict[str, Any], params: dict[str, Any], response: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the x402 settlement flow when a proxied call answers with a 402.

        Detects an x402 challenge, gates it against the active spending mandate,
        invokes the operator settlement hook, retries the call with the payment
        reference, records the retried invocation to the WAL, and emits a
        chain-anchored spend receipt binding the WAL invocation digest, the
        challenge, the payment reference, the retried request, and the mandate.

        A non-402 response, a disabled config, or a refused/declined settlement
        returns the original response unchanged -- so the 402 surfaces as an
        ordinary tool error and Bernstein never pays outside a mandate.
        """
        from bernstein.core.protocols.payments.x402 import (
            SettlementStatus,
            build_retry_request,
            parse_challenge,
        )

        assert self._settlement is not None
        challenge = parse_challenge(response)
        if challenge is None:
            return response

        tool_name = str(params.get("name", ""))
        pre = self._settlement.pre_authorize(challenge, server_name=self._server_name, tool_name=tool_name)
        if pre.status is not SettlementStatus.AUTHORIZED or not pre.payment_ref:
            # SKIPPED (disabled) or REFUSED (fail closed): the original 402
            # surfaces as an ordinary tool error; a refusal is already anchored.
            return response

        retried = build_retry_request(message, pre.payment_ref)
        retried_id = retried.get("id")
        retried_params: dict[str, Any] = retried.get("params") or {}
        settled, latency_ms = await self._send_request(retried, retried_id)
        wal_entry = self._record_wal_and_metrics("tools/call", retried_params, retried_id, settled, latency_ms)
        self._anchor_proxied_call("tools/call", retried_params)

        try:
            self._settlement.record_settlement(
                challenge,
                server_name=self._server_name,
                tool_name=tool_name,
                payment_ref=pre.payment_ref,
                amount_usd=pre.amount_usd,
                retried_request=retried,
                wal_entry=wal_entry,
            )
        except Exception:
            # A recording failure must not swallow the settled response the
            # operator already paid for; it surfaces in the log and as a
            # missing receipt a verifier can detect.
            logger.exception("x402: failed to record settlement receipt for %s", tool_name)

        return settled

    def _anchor_proxied_call(self, method: str, params: dict[str, Any]) -> None:
        """Anchor a proxied call into the run journal and audit chain.

        Mirrors the WAL record with a chain-anchored continuity record
        (issue #2506): the call's content-derived ids bind to the journal
        head, so a verifier reconstructs the proxied call ordering from
        chain entries alone. Anchoring failures never take the proxy down:
        a missing anchor is visible to a verifier as a call-index gap.
        """
        if self._journal is None:
            return
        try:
            anchor_stateless_call(
                journal=self._journal,
                method=method,
                params=params,
                chain=self._audit_chain,
            )
        except Exception:
            # Anchoring stays non-fatal -- a missing anchor is visible to a
            # verifier as a call-index gap -- but the failure is surfaced in
            # the log rather than silently swallowed (AC4).
            logger.exception("Failed to anchor proxied mcp.stateless_call for %s", method)

    # ------------------------------------------------------------------
    # Transport runners
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        """Run as a stdio proxy. Reads from stdin, writes to stdout. Blocks until EOF."""
        loop = asyncio.get_running_loop()
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

    Implements a minimal, stateless MCP SSE transport (issue #2506):

    - ``GET /sse``     - opens an SSE stream; the first event names the POST
                         endpoint (no per-client token in the URL).
    - ``POST /message`` - accepts JSON-RPC, forwards through the gateway,
                          and returns the response in the POST body. The
                          response is correlated to its request by the
                          content-derived span id (``X-Bernstein-Span-Id``
                          header and the SSE event ``id`` line), so a client
                          holding several requests in flight matches each
                          response without any server-held state.

    Every response is also fanned out to the open SSE streams tagged with
    its span id, keeping SSE-consuming hosts working. Because correlation is
    a pure function of the request content, consecutive requests may be
    served by different app instances with no shared memory.

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

    # Open SSE consumer queues. This is fan-out plumbing for live streams on
    # this instance, not continuity state: correlation lives in the span id.
    _streams: set[asyncio.Queue[tuple[str, str] | None]] = set()

    def sse_endpoint(request: Any) -> Any:
        """Open an SSE stream; responses arrive tagged with their span id."""
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        _streams.add(queue)

        async def _event_stream() -> Any:
            # MCP SSE spec: first event tells client where to POST.
            yield "event: endpoint\ndata: /message\n\n"
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
                    span_id, payload = item
                    yield f"id: {span_id}\ndata: {payload}\n\n"
            finally:
                _streams.discard(queue)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Bernstein-Run-ID": run_id,
            },
        )

    async def message_endpoint(request: Any) -> Any:
        """Serve a JSON-RPC request, correlated by content-derived span id."""
        body: dict[str, Any] = await request.json()
        span_id = request_span_id(body)

        response = await gateway.handle_jsonrpc(body)

        if response is None:
            # Notification: acknowledged, nothing to correlate.
            return JSONResponse({"status": "accepted"}, headers={"X-Bernstein-Span-Id": span_id})

        payload = json.dumps(response, separators=(",", ":"))
        for queue in list(_streams):
            await queue.put((span_id, payload))
        return JSONResponse(response, headers={"X-Bernstein-Span-Id": span_id})

    # ``from __future__ import annotations`` stringifies the endpoint
    # annotations and FastAPI resolves them against module globals, where the
    # lazily imported fastapi names do not exist. Bind real annotation
    # objects so the Request parameter is injected rather than treated as a
    # required query field.
    sse_endpoint.__annotations__ = {"request": Request, "return": StreamingResponse}
    message_endpoint.__annotations__ = {"request": Request, "return": JSONResponse}
    app.get("/sse")(sse_endpoint)
    app.post("/message")(message_endpoint)

    @app.get("/gateway/metrics")
    def metrics_endpoint() -> Any:
        """Return current per-tool metrics for this gateway process."""
        return JSONResponse(
            {
                "run_id": run_id,
                "metrics": gateway.get_metrics(),
            }
        )

    return app
