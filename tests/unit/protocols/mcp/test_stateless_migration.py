"""Tests for the stateless chain-anchored migration of the MCP wire paths.

Issue #2506. The stateless core primitives (issue #2307) already exist; these
tests pin the migration of the live wire paths onto them:

* the client attaches content-derived ``_meta`` to every request and never
  mints or round-trips a protocol session id;
* the remote transport serves every request from the body plus ``_meta``
  alone, with no server-side session store;
* the gateway correlates SSE responses per request via the content-derived
  span id rather than per-session queues;
* every served or proxied call is anchored as an ``mcp.stateless_call``
  chain entry, so call ordering reconstructs from the chain alone;
* legacy clients that still send ``Mcp-Session-Id`` keep working behind the
  compat shim for a bounded window and never receive a session header back.

All tests isolate state with ``tmp_path``; no sockets are opened.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest

from bernstein.core.protocols.mcp.mcp_client import (
    MCPClientSession,
    MCPSchemaViolation,
    RemoteServerConfig,
    RemoteTool,
)
from bernstein.core.protocols.mcp.stateless_core import (
    DEPRECATED_CAPABILITIES,
    DEPRECATION_DATE,
    LEGACY_SESSION_HEADER,
    REMOVAL_DATE,
    InputRequiredResult,
    anchor_stateless_call,
    compat_shim_active,
    legacy_session_header_value,
    months_since_deprecation,
    request_span_id,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.audit_chain import (
    EVENT_MCP_STATELESS_CALL,
    AuditChainStore,
    reconstruct_mcp_call_order,
)
from bernstein.mcp.remote_transport import RemoteMCPConfig, StreamableHTTPTransport

if TYPE_CHECKING:
    from pathlib import Path

_FAKE_REQUEST = httpx.Request("POST", "https://test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonrpc_response(result: dict[str, Any], request_id: int = 1) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={"jsonrpc": "2.0", "id": request_id, "result": result},
        headers={"content-type": "application/json"},
        request=_FAKE_REQUEST,
    )


class _RecordingClient:
    """Async httpx.AsyncClient stand-in that records every posted payload."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.posted: list[dict[str, Any]] = []
        self._responses = responses

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.posted.append({"url": url, "json": json, "headers": headers})
        return self._responses[min(len(self.posted) - 1, len(self._responses) - 1)]


def _config(name: str = "test-server") -> RemoteServerConfig:
    return RemoteServerConfig(name=name, url="https://test/mcp", retry_limit=1)


def _request(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


def _transport(
    tmp_path: Path,
    *,
    run_id: str = "run-2506",
    chain: AuditChainStore | None = None,
    today: date | None = None,
) -> StreamableHTTPTransport:
    cfg = RemoteMCPConfig(host="127.0.0.1", path="/mcp", auth_type="none")
    journal = EventJournal(run_id, tmp_path / run_id)
    return StreamableHTTPTransport(
        config=cfg,
        server_url="https://task-server:8052",
        journal=journal,
        audit_chain=chain,
        today=(lambda: today) if today is not None else None,
    )


# ---------------------------------------------------------------------------
# Phase 1: client emits _meta, no session ids
# ---------------------------------------------------------------------------


class TestClientStatelessMeta:
    @pytest.mark.anyio
    async def test_every_request_carries_meta_and_no_session_header(self) -> None:
        client = _RecordingClient([_jsonrpc_response({"tools": []})])
        session = MCPClientSession(_config())
        session._initialized = True
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
            await session.list_tools()

        assert client.posted, "no request captured"
        for posted in client.posted:
            params = posted["json"]["params"]
            meta = params["_meta"]
            assert "traceparent" in meta
            assert "client" in meta and "capabilities" in meta["client"]
            header_names = {k.lower() for k in posted["headers"]}
            assert LEGACY_SESSION_HEADER not in header_names

    @pytest.mark.anyio
    async def test_client_ignores_legacy_session_header_from_server(self) -> None:
        response = httpx.Response(
            status_code=200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            headers={"content-type": "application/json", "mcp-session-id": "sess-legacy"},
            request=_FAKE_REQUEST,
        )
        client = _RecordingClient([response, _jsonrpc_response({"tools": []}, request_id=2)])
        session = MCPClientSession(_config())
        session._initialized = True
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
            await session.list_tools()
            await session.list_tools()

        # The second request must not echo the header the server sent back.
        header_names = {k.lower() for k in client.posted[1]["headers"]}
        assert LEGACY_SESSION_HEADER not in header_names

    @pytest.mark.anyio
    async def test_two_replays_emit_byte_identical_meta(self) -> None:
        """AC2: replaying the same call sequence emits byte-identical _meta."""
        metas: list[list[str]] = []
        for _ in range(2):
            client = _RecordingClient(
                [
                    _jsonrpc_response({"tools": [{"name": "echo", "inputSchema": {}}]}),
                    _jsonrpc_response({"content": [{"type": "text", "text": "hi"}]}, request_id=2),
                ]
            )
            session = MCPClientSession(_config(), run_root_hash="a" * 64)
            session._initialized = True
            with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
                await session.list_tools()
                await session.call_tool("echo", {"input": "hi"})
            metas.append(
                [json.dumps(p["json"]["params"]["_meta"], sort_keys=True, separators=(",", ":")) for p in client.posted]
            )
        assert metas[0] == metas[1]

    @pytest.mark.anyio
    async def test_meta_span_ids_are_ordered_per_call(self) -> None:
        client = _RecordingClient(
            [
                _jsonrpc_response({"tools": []}),
                _jsonrpc_response({"tools": []}, request_id=2),
            ]
        )
        session = MCPClientSession(_config())
        session._initialized = True
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
            await session.list_tools()
            await session.list_tools()
        spans = [p["json"]["params"]["_meta"]["traceparent"].split("-")[2] for p in client.posted]
        assert spans[0] != spans[1]

    @pytest.mark.anyio
    async def test_baggage_call_index_only_advances_for_anchored_calls(self) -> None:
        """AC5: the baggage ``mcp.call_index`` advances only for anchor-eligible
        calls, so its claim matches a server's per-tools/call journal
        allocation rather than counting every message."""

        def _baggage_index(posted: dict[str, Any]) -> int:
            baggage = posted["json"]["params"]["_meta"]["baggage"]
            for item in baggage.split(","):
                key, _, value = item.partition("=")
                if key == "mcp.call_index":
                    return int(value)
            raise AssertionError(f"no mcp.call_index in baggage: {baggage!r}")

        client = _RecordingClient(
            [
                _jsonrpc_response({"tools": [{"name": "echo", "inputSchema": {}}]}),
                _jsonrpc_response({"content": [{"type": "text", "text": "a"}]}, request_id=2),
                _jsonrpc_response({"content": [{"type": "text", "text": "b"}]}, request_id=3),
            ]
        )
        session = MCPClientSession(_config())
        session._initialized = True
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
            await session.list_tools()  # tools/list -- not anchor-eligible
            await session.call_tool("echo", {"input": "1"})  # first anchored call
            await session.call_tool("echo", {"input": "2"})  # second anchored call

        methods = [p["json"]["params"].get("name") or p["json"]["method"] for p in client.posted]
        assert methods[0] == "tools/list"
        # The non-anchored tools/list does not consume an audit ordinal, and the
        # two tools/call requests claim a contiguous 0, 1.
        assert _baggage_index(client.posted[0]) == 0
        assert _baggage_index(client.posted[1]) == 0
        assert _baggage_index(client.posted[2]) == 1


class TestClientInputRequiredRetry:
    @pytest.mark.anyio
    async def test_input_required_result_surfaces_request_state(self) -> None:
        wire = InputRequiredResult(prompt="need host", request_state={"tool": "fetch"}).to_wire()
        client = _RecordingClient([_jsonrpc_response(wire)])
        session = MCPClientSession(_config())
        session._initialized = True
        session._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client):
            result = await session.call_tool("fetch", {})
        assert result.metadata["input_required"] is True
        assert result.metadata["request_state"] == wire["requestState"]
        assert result.metadata["prompt"] == "need host"

    @pytest.mark.anyio
    async def test_retry_resumes_on_a_fresh_client_instance(self) -> None:
        """AC: the retry needs only the echoed requestState; a client instance
        that never saw the original call re-submits it."""
        wire = InputRequiredResult(prompt="need host", request_state={"tool": "fetch"}).to_wire()

        first_client = _RecordingClient([_jsonrpc_response(wire)])
        first = MCPClientSession(_config())
        first._initialized = True
        first._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=first_client):
            pending = await first.call_tool("fetch", {})

        second_client = _RecordingClient([_jsonrpc_response({"content": [{"type": "text", "text": "resumed"}]})])
        second = MCPClientSession(_config())
        second._initialized = True
        second._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=second_client):
            result = await second.call_tool(
                "fetch",
                {"host": "example.test"},
                request_state=pending.metadata["request_state"],
            )

        assert result.content == "resumed"
        sent = second_client.posted[0]["json"]["params"]
        assert sent["requestState"] == pending.metadata["request_state"]
        assert sent["arguments"] == {"host": "example.test"}

    @pytest.mark.anyio
    async def test_retry_rejects_malformed_request_state(self) -> None:
        session = MCPClientSession(_config())
        session._initialized = True
        session._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with pytest.raises(ValueError, match="requestState"):
            await session.call_tool("fetch", {}, request_state="@@@not-base64@@@")

    @pytest.mark.anyio
    async def test_input_required_without_request_state_is_rejected(self) -> None:
        """AC7: an ``input_required`` result with no ``requestState`` cannot be
        resumed, so it is a schema violation and marks the server degraded."""
        client = _RecordingClient([_jsonrpc_response({"type": "input_required", "prompt": "need host"})])
        session = MCPClientSession(_config())
        session._initialized = True
        session._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with (
            patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client),
            pytest.raises(MCPSchemaViolation, match="requestState"),
        ):
            await session.call_tool("fetch", {})
        assert session.is_degraded

    @pytest.mark.anyio
    async def test_input_required_with_undecodable_request_state_is_rejected(self) -> None:
        """AC7: an ``input_required`` result whose ``requestState`` will not
        decode is rejected rather than surfaced as a resumable prompt."""
        wire = {"type": "input_required", "prompt": "need host", "requestState": "@@@not-base64@@@"}
        client = _RecordingClient([_jsonrpc_response(wire)])
        session = MCPClientSession(_config())
        session._initialized = True
        session._tools = [RemoteTool(name="fetch", description="", server_name="test-server")]
        with (
            patch("bernstein.core.protocols.mcp.mcp_client.httpx.AsyncClient", return_value=client),
            pytest.raises(MCPSchemaViolation, match="requestState"),
        ):
            await session.call_tool("fetch", {})
        assert session.is_degraded


# ---------------------------------------------------------------------------
# Phase 2: transport has no session store; multi-instance continuity
# ---------------------------------------------------------------------------


class TestTransportStateless:
    @pytest.mark.anyio
    async def test_no_session_header_in_responses(self, tmp_path: Path) -> None:
        transport = _transport(tmp_path)
        _, headers, _ = await transport.handle_request("POST", "/mcp", {}, _request("ping"))
        assert LEGACY_SESSION_HEADER not in {k.lower() for k in headers}

    @pytest.mark.anyio
    async def test_multi_instance_continuity(self, tmp_path: Path) -> None:
        """AC1: consecutive calls served by two instances with no shared
        memory produce results identical to the single-instance run."""
        listing = _request("tools/list", req_id=1)
        health = _request("tools/call", {"name": "bernstein_health", "arguments": {}}, req_id=2)

        single = _transport(tmp_path / "single")
        s1, _, single_list = await single.handle_request("POST", "/mcp", {}, listing)
        s2, _, single_call = await single.handle_request("POST", "/mcp", {}, health)
        assert s1 == s2 == 200

        instance_a = _transport(tmp_path / "a")
        instance_b = _transport(tmp_path / "b")
        a_status, _, a_body = await instance_a.handle_request("POST", "/mcp", {}, listing)
        b_status, _, b_body = await instance_b.handle_request("POST", "/mcp", {}, health)
        assert a_status == b_status == 200

        def _stable(raw: bytes) -> dict[str, Any]:
            parsed = json.loads(raw)
            # The cost-meter envelope carries wall-clock fields; compare the
            # protocol-visible result shape.
            return {"id": parsed.get("id"), "keys": sorted(parsed.get("result", {}).keys())}

        assert _stable(a_body) == _stable(single_list)
        assert _stable(b_body) == _stable(single_call)
        assert json.loads(a_body)["result"] == json.loads(single_list)["result"]

    @pytest.mark.anyio
    async def test_legacy_header_accepted_as_noop_while_shim_active(self, tmp_path: Path) -> None:
        """AC4: legacy clients succeed during the shim window and get no
        session header back."""
        transport = _transport(tmp_path, today=DEPRECATION_DATE)
        status, headers, body = await transport.handle_request(
            "POST", "/mcp", {LEGACY_SESSION_HEADER: "legacy-1"}, _request("ping")
        )
        assert status == 200
        assert json.loads(body)["result"] == {}
        assert LEGACY_SESSION_HEADER not in {k.lower() for k in headers}

    @pytest.mark.anyio
    async def test_legacy_header_refused_after_removal_date(self, tmp_path: Path) -> None:
        transport = _transport(tmp_path, today=REMOVAL_DATE)
        status, _, body = await transport.handle_request(
            "POST", "/mcp", {LEGACY_SESSION_HEADER: "legacy-1"}, _request("ping")
        )
        assert status == 400
        assert b"removed" in body

    @pytest.mark.anyio
    async def test_legacy_delete_is_a_noop_while_shim_active(self, tmp_path: Path) -> None:
        transport = _transport(tmp_path, today=DEPRECATION_DATE)
        status, _, _ = await transport.handle_request("DELETE", "/mcp", {LEGACY_SESSION_HEADER: "legacy-1"}, b"")
        assert status == 200

    @pytest.mark.anyio
    async def test_legacy_delete_refused_after_removal_date(self, tmp_path: Path) -> None:
        transport = _transport(tmp_path, today=REMOVAL_DATE)
        status, _, _ = await transport.handle_request("DELETE", "/mcp", {}, b"")
        assert status == 405


# ---------------------------------------------------------------------------
# Phase 3: chain anchoring of served calls
# ---------------------------------------------------------------------------


class TestServedCallAnchoring:
    @pytest.mark.anyio
    async def test_served_call_records_journal_and_chain_entry(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        transport = _transport(tmp_path, chain=chain)
        status, _, _ = await transport.handle_request(
            "POST",
            "/mcp",
            {},
            _request("tools/call", {"name": "bernstein_health", "arguments": {}}),
        )
        assert status == 200

        rows = [r for r in load_events(transport._journal.path) if r["event"] == "mcp.stateless_call"]
        assert len(rows) == 1
        events = chain.query(event_type=EVENT_MCP_STATELESS_CALL)
        assert len(events) == 1
        assert events[0].details["span_id"] == rows[0]["span_id"]
        assert events[0].details["journal_head"] == transport._journal.head()

    @pytest.mark.anyio
    async def test_replays_emit_identical_chain_entry_identity(self, tmp_path: Path) -> None:
        """AC2: two replays of the same run record identical chain-entry
        identity fields (trace id, span id, call index, journal head)."""
        identities: list[dict[str, Any]] = []
        for run in ("x", "y"):
            chain = AuditChainStore(tmp_path / run / "audit", key=b"k" * 32)
            transport = _transport(tmp_path / run, chain=chain)
            await transport.handle_request(
                "POST",
                "/mcp",
                {},
                _request("tools/call", {"name": "bernstein_health", "arguments": {}}),
            )
            event = chain.query(event_type=EVENT_MCP_STATELESS_CALL)[0]
            identities.append(
                {k: event.details[k] for k in ("run_id", "method", "call_index", "trace_id", "span_id", "journal_head")}
            )
        assert identities[0] == identities[1]

    @pytest.mark.anyio
    async def test_meta_carried_ids_are_anchored_verbatim(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        transport = _transport(tmp_path, chain=chain)
        # The claimed call index matches the journal-allocated index (0 for the
        # first served call), so the client-derived trace / span ids are
        # anchored verbatim.
        meta = {
            "traceparent": f"00-{'a' * 32}-{'b' * 16}-01",
            "baggage": "mcp.method=tools/call,mcp.call_index=0",
        }
        await transport.handle_request(
            "POST",
            "/mcp",
            {},
            _request("tools/call", {"name": "bernstein_health", "arguments": {}, "_meta": meta}),
        )
        event = chain.query(event_type=EVENT_MCP_STATELESS_CALL)[0]
        assert event.details["trace_id"] == "a" * 32
        assert event.details["span_id"] == "b" * 16
        assert event.details["call_index"] == 0


class TestCallIndexAllocation:
    """AC1: the audit call index is allocated from the journal, and the client
    baggage is only a claim that must match the allocation."""

    @staticmethod
    def _claim_params(index: int) -> dict[str, Any]:
        return {
            "name": "echo",
            "arguments": {},
            "_meta": {"baggage": f"mcp.method=tools/call,mcp.call_index={index}"},
        }

    def test_absent_and_matching_claims_allocate_contiguously(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        journal = EventJournal("run-alloc", tmp_path / "journal")
        # No baggage -> allocated 0.
        anchor_stateless_call(
            journal=journal, method="tools/call", params={"name": "echo", "arguments": {}}, chain=chain
        )
        # Baggage claims the correct next index (1) -> accepted.
        anchor_stateless_call(journal=journal, method="tools/call", params=self._claim_params(1), chain=chain)
        calls = reconstruct_mcp_call_order(chain=chain, run_id="run-alloc")
        assert [c["call_index"] for c in calls] == [0, 1]

    def test_mismatching_claim_is_rejected(self, tmp_path: Path) -> None:
        journal = EventJournal("run-alloc", tmp_path / "journal")
        # The first call's allocated index is 0; a claim of 7 is a lie.
        with pytest.raises(ValueError, match="call_index claim 7"):
            anchor_stateless_call(journal=journal, method="tools/call", params=self._claim_params(7), chain=None)
        # The rejected call left no journal entry behind.
        assert [r for r in load_events(journal.path) if r["event"] == "mcp.stateless_call"] == []

    @pytest.mark.anyio
    async def test_transport_survives_mismatched_claim_without_anchoring(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        transport = _transport(tmp_path, chain=chain)
        meta = {"baggage": "mcp.method=tools/call,mcp.call_index=7"}
        status, _, _ = await transport.handle_request(
            "POST",
            "/mcp",
            {},
            _request("tools/call", {"name": "bernstein_health", "arguments": {}, "_meta": meta}),
        )
        # Serving stays up (anchoring is non-fatal) but the lying claim is not
        # anchored -- the gap is visible to a verifier.
        assert status == 200
        assert chain.query(event_type=EVENT_MCP_STATELESS_CALL) == []


def _held_against_other_threads(lock: threading.Lock | threading.RLock) -> bool:
    """Return whether ``lock`` would block an acquire from a *different* thread.

    The append lock is re-entrant, so the owning thread can always re-acquire
    it and ``locked()`` is not part of the re-entrant lock's public surface.
    Probing from another thread states the guarantee the test is really about:
    a concurrent writer is kept out for the whole critical section.
    """
    outcome: dict[str, bool] = {}

    def _probe() -> None:
        acquired = lock.acquire(blocking=False)
        outcome["acquired"] = acquired
        if acquired:
            lock.release()

    prober = threading.Thread(target=_probe)
    prober.start()
    prober.join()
    return not outcome["acquired"]


class TestChainReconstruction:
    def _seed(self, tmp_path: Path, count: int = 3) -> AuditChainStore:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        journal = EventJournal("run-2506", tmp_path)
        for i in range(count):
            anchor_stateless_call(
                journal=journal,
                method="tools/call",
                params={"name": "echo", "arguments": {"i": i}},
                chain=chain,
            )
        return chain

    def test_ordering_reconstructs_from_chain_alone(self, tmp_path: Path) -> None:
        """AC3: the full MCP call ordering of a run rebuilds purely from
        verified chain entries."""
        chain = self._seed(tmp_path)
        calls = reconstruct_mcp_call_order(chain=chain, run_id="run-2506")
        assert [c["call_index"] for c in calls] == [0, 1, 2]
        assert all(c["span_id"] for c in calls)

    def test_tampering_one_entry_fails_at_exactly_that_entry(self, tmp_path: Path) -> None:
        """AC3: flipping a byte in one mcp.stateless_call entry fails
        verification at exactly that entry."""
        chain = self._seed(tmp_path)
        log_file = next((tmp_path / "audit").glob("*.jsonl"))
        lines = log_file.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["details"]["call_index"] = 99  # tamper the middle entry
        lines[1] = json.dumps(entry, sort_keys=True)
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(ValueError, match=rf"{log_file.name}:2"):
            reconstruct_mcp_call_order(chain=chain, run_id="run-2506")

    def test_ordering_gap_is_rejected(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        from bernstein.core.security.audit_chain import record_mcp_stateless_call

        for index in (0, 2):  # skip 1
            record_mcp_stateless_call(
                chain=chain,
                run_id="run-2506",
                method="tools/call",
                call_index=index,
                trace_id="a" * 32,
                span_id=f"{index:016x}",
                journal_head="f" * 64,
            )
        with pytest.raises(ValueError, match="call_index"):
            reconstruct_mcp_call_order(chain=chain, run_id="run-2506")

    def test_verify_and_query_reads_one_locked_snapshot(self, tmp_path: Path) -> None:
        """AC6: verify() and query() run under one lock, so the projected
        events are exactly the events that were verified (no TOCTOU gap)."""
        chain = self._seed(tmp_path)
        ok, errors, events = chain.verify_and_query(event_type=EVENT_MCP_STATELESS_CALL)
        assert ok, errors
        # Same projection a plain query would return, but read atomically with
        # the verification verdict.
        assert [e.details["call_index"] for e in events] == [0, 1, 2]
        assert events == chain.query(event_type=EVENT_MCP_STATELESS_CALL)

    def test_verify_and_query_holds_the_append_lock(self, tmp_path: Path) -> None:
        """AC6: an append cannot interleave between the verify and the query --
        the operation holds the store's append lock across both reads, so a
        concurrent ``log_with_prev_digest`` would block."""
        chain = self._seed(tmp_path)
        observed: dict[str, bool] = {}
        real_verify = chain._log.verify

        def _spy_verify() -> tuple[bool, list[str]]:
            observed["locked_during_verify"] = _held_against_other_threads(chain._append_lock)
            return real_verify()

        chain._log.verify = _spy_verify  # type: ignore[method-assign]
        ok, _, _ = chain.verify_and_query(event_type=EVENT_MCP_STATELESS_CALL)
        assert ok
        assert observed["locked_during_verify"] is True
        # The lock is released once the operation returns.
        assert not _held_against_other_threads(chain._append_lock)


# ---------------------------------------------------------------------------
# Gateway: per-request span-id correlation
# ---------------------------------------------------------------------------


class TestRequestSpanId:
    def test_meta_traceparent_wins(self) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "_meta": {"traceparent": f"00-{'a' * 32}-{'c' * 16}-01"}},
        }
        assert request_span_id(message) == "c" * 16

    def test_content_derived_fallback_is_deterministic(self) -> None:
        message = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        first = request_span_id(message)
        second = request_span_id(dict(message))
        assert first == second
        assert len(first) == 16

    def test_different_requests_get_different_span_ids(self) -> None:
        a = request_span_id({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        b = request_span_id({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert a != b


class TestGatewaySSECorrelation:
    @pytest.mark.anyio
    async def test_post_returns_response_correlated_by_span_id(self, tmp_path: Path) -> None:
        from bernstein.core.wal import WALWriter

        from bernstein.core.protocols.mcp.mcp_gateway import GatewayReplay, MCPGateway, create_gateway_sse_app

        run_id = "gw-2506"
        writer = WALWriter(run_id=run_id, sdd_dir=tmp_path)
        writer.append(
            decision_type="mcp_tool_call",
            inputs={"method": "tools/list", "server_name": "s", "tool_name": "", "arguments": {}, "request_id": 1},
            output={"result": {"tools": []}, "error": None, "latency_ms": 1.0},
            actor="mcp_gateway",
        )
        replay = GatewayReplay(run_id=run_id, sdd_dir=tmp_path)
        gateway = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay)
        app = create_gateway_sse_app(gateway, run_id=run_id)

        from fastapi.testclient import TestClient

        with TestClient(app) as http:
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            resp = http.post("/message", json=message)
            assert resp.status_code == 200
            body = resp.json()
            assert body["result"] == {"tools": []}
            assert resp.headers["x-bernstein-span-id"] == request_span_id(message)

    def test_sse_module_has_no_session_queues(self) -> None:
        import inspect

        from bernstein.core.protocols.mcp import mcp_gateway

        source = inspect.getsource(mcp_gateway)
        assert "sessionId" not in source
        assert "session_id" not in source


# ---------------------------------------------------------------------------
# Compat shim window
# ---------------------------------------------------------------------------


class TestShimWindow:
    def test_sessions_are_a_deprecated_capability(self) -> None:
        assert "sessions" in DEPRECATED_CAPABILITIES

    def test_removal_date_is_twelve_months_after_deprecation(self) -> None:
        assert date(2026, 7, 28) == DEPRECATION_DATE
        assert date(2027, 7, 28) == REMOVAL_DATE

    def test_months_since_deprecation_clamps_to_zero_before_the_date(self) -> None:
        assert months_since_deprecation(date(2026, 7, 16)) == 0

    def test_months_since_deprecation_counts_whole_months(self) -> None:
        assert months_since_deprecation(date(2026, 8, 27)) == 0
        assert months_since_deprecation(date(2026, 8, 28)) == 1
        assert months_since_deprecation(REMOVAL_DATE) == 12

    def test_shim_expires_at_removal_date(self) -> None:
        assert compat_shim_active("sessions", months_since_deprecation=11)
        assert not compat_shim_active("sessions", months_since_deprecation=months_since_deprecation(REMOVAL_DATE))

    def test_legacy_header_lookup_is_case_insensitive(self) -> None:
        assert legacy_session_header_value({"Mcp-Session-Id": "x"}) == "x"
        assert legacy_session_header_value({"mcp-session-id": "y"}) == "y"
        assert legacy_session_header_value({"content-type": "application/json"}) is None


# ---------------------------------------------------------------------------
# Phase 4: deprecated capability advertisement
# ---------------------------------------------------------------------------


class TestNegotiationDeprecationGate:
    def test_sampling_advertised_only_while_shim_active(self) -> None:
        from bernstein.core.protocols.protocol_negotiation import get_supported_versions

        active = get_supported_versions("mcp", months_since_deprecation=0)
        assert any("sampling" in v.capabilities for v in active)

        expired = get_supported_versions("mcp", months_since_deprecation=12)
        for version in expired:
            assert not (version.capabilities & DEPRECATED_CAPABILITIES)

    def test_non_deprecated_capabilities_survive_expiry(self) -> None:
        from bernstein.core.protocols.protocol_negotiation import get_supported_versions

        expired = get_supported_versions("mcp", months_since_deprecation=24)
        merged: set[str] = set()
        for version in expired:
            merged |= version.capabilities
        assert {"tools", "resources", "prompts"} <= merged

    def test_other_protocols_are_untouched(self) -> None:
        from bernstein.core.protocols.protocol_negotiation import get_supported_versions

        a2a = get_supported_versions("a2a", months_since_deprecation=24)
        assert any("streaming" in v.capabilities for v in a2a)


# ---------------------------------------------------------------------------
# Phase 3: auth lifecycle keyed by server name only
# ---------------------------------------------------------------------------


class TestAuthLifecycleServerNameKeyed:
    def test_refresh_is_keyed_by_server_name_only(self) -> None:
        """Two managers with the same registration behave identically: the
        refresh path consults nothing beyond the server-name keyed session."""
        import time

        from bernstein.core.protocols.mcp.mcp_auth_lifecycle import (
            AuthLifecycleManager,
            AuthSession,
            RefreshResult,
        )

        outcomes = []
        for _ in range(2):
            manager = AuthLifecycleManager(token_refresher=lambda s: ("tok-new", time.time() + 3600))
            manager.register_session(
                "github",
                AuthSession(server_name="github", access_token="old", refresh_token="r"),
            )
            outcomes.append(manager.handle_auth_failure("github", 401))
        assert all(o.result == RefreshResult.SUCCESS for o in outcomes)
        assert all(o.new_token == "tok-new" for o in outcomes)
