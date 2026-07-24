"""x402 settlement wiring in the MCP gateway (issue #2528, phase 2 + AC1/AC4).

These tests drive :meth:`MCPGateway.handle_jsonrpc` against a hermetic fake
upstream that answers with a 402 challenge, exercising the settlement seam:

* AC1 -- with no x402 config a 402 surfaces as an ordinary tool error and no
  hook is looked up and no retry is sent.
* happy path -- an active config gates, settles, retries, and records a spend
  receipt bound to the retried WAL invocation.
* AC4 -- replay mode serves the recorded settled response without invoking the
  hook, so replay can never double-settle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.mcp_gateway import GatewayReplay, MCPGateway
from bernstein.core.wal import WALWriter

from bernstein.core.protocols.payments.mandates import IntentMandate
from bernstein.core.protocols.payments.x402 import (
    SettlementContext,
    X402Config,
    X402SettlementCoordinator,
    iter_spend_receipts,
)
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"x" * 32
_SERVER = "acme-data-api"
_TOOL = "fetch_dataset"


class _RecordingHook:
    def __init__(self, payment_ref: str | None = "pay-ref-0001") -> None:
        self._ref = payment_ref
        self.calls: list[dict[str, Any]] = []

    def settle(self, challenge: Any, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        self.calls.append({"server_name": server_name, "tool_name": tool_name, "amount_usd": amount_usd})
        return self._ref


def _challenge_response(req_id: int, amount_usd: float = 4.0) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": 402,
            "message": "Payment Required",
            "data": {
                "x402Version": 1,
                "accepts": [{"scheme": "exact", "maxAmountRequired": "4000000", "amountUsd": amount_usd}],
            },
        },
    }


def _settled_response(req_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "settled"}]}}


def _call_message(req_id: int = 7) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": _TOOL, "arguments": {"q": "widgets"}},
    }


def _signed_intent() -> IntentMandate:
    return IntentMandate(task_id="task-x402", allowed_tool_calls=(_TOOL,), spend_cap_usd=100.0).sign(_KEY)


def _coordinator(
    tmp_path: Path, config: X402Config, intent: IntentMandate | None, run_id: str
) -> X402SettlementCoordinator:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    ctx = SettlementContext(
        config=config,
        hmac_key=_KEY,
        workdir=tmp_path,
        lineage_root=sdd / "lineage",
        wal_sdd_dir=sdd,
        wal_run_id=run_id,
        intent=intent,
        meter=None,
        audit_chain=AuditChainStore(sdd / "audit", key=_KEY),
        now=lambda: 1_700_000_000,
    )
    return X402SettlementCoordinator(ctx)


def _make_writer(tmp_path: Path, run_id: str) -> WALWriter:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    return WALWriter(run_id=run_id, sdd_dir=sdd)


class TestDefaultOff:
    @pytest.mark.asyncio
    async def test_no_settlement_configured_surfaces_402(self, tmp_path: Any) -> None:
        writer = _make_writer(Path(tmp_path), "gw-off")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, server_name=_SERVER)

        sends: list[dict[str, Any]] = []

        async def _mock_send(msg: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            sends.append(msg)
            fut = gw._pending.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(_challenge_response(msg["id"]))

        from unittest.mock import patch

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            resp = await gw.handle_jsonrpc(_call_message())

        assert resp is not None
        assert resp["error"]["code"] == 402  # surfaces as an ordinary tool error
        assert len(sends) == 1  # no retry

    @pytest.mark.asyncio
    async def test_disabled_config_does_not_invoke_hook(self, tmp_path: Any) -> None:
        writer = _make_writer(Path(tmp_path), "gw-disabled")
        hook = _RecordingHook()
        coord = _coordinator(Path(tmp_path), X402Config(enabled=False, hook=hook), _signed_intent(), "gw-disabled")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, server_name=_SERVER, settlement=coord)

        sends: list[dict[str, Any]] = []

        async def _mock_send(msg: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            sends.append(msg)
            fut = gw._pending.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(_challenge_response(msg["id"]))

        from unittest.mock import patch

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            resp = await gw.handle_jsonrpc(_call_message())

        assert resp["error"]["code"] == 402
        assert hook.calls == []  # no hook lookup
        assert len(sends) == 1  # no retry


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_settles_and_retries(self, tmp_path: Any) -> None:
        writer = _make_writer(Path(tmp_path), "gw-live")
        hook = _RecordingHook()
        coord = _coordinator(Path(tmp_path), X402Config(enabled=True, hook=hook), _signed_intent(), "gw-live")
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, server_name=_SERVER, settlement=coord)

        sends: list[dict[str, Any]] = []

        async def _mock_send(msg: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            sends.append(msg)
            fut = gw._pending.get(msg.get("id"))
            if fut and not fut.done():
                paid = "_x402_payment" in msg.get("params", {}).get("arguments", {})
                fut.set_result(_settled_response(msg["id"]) if paid else _challenge_response(msg["id"]))

        from unittest.mock import patch

        with patch.object(gw, "_send_upstream", side_effect=_mock_send):
            resp = await gw.handle_jsonrpc(_call_message())

        assert resp["result"]["content"][0]["text"] == "settled"  # settled response returned
        assert len(hook.calls) == 1  # hook invoked once
        assert len(sends) == 2  # original + retry
        receipts = list(iter_spend_receipts(Path(tmp_path)))
        assert len(receipts) == 1
        assert receipts[0].server_name == _SERVER
        assert receipts[0].settlement_ref.amount_usd == pytest.approx(4.0)


class TestReplayNeverSettles:
    @pytest.mark.asyncio
    async def test_replay_serves_recorded_settled_response_without_hook(self, tmp_path: Any) -> None:
        sdd = Path(tmp_path) / ".sdd"
        sdd.mkdir(parents=True, exist_ok=True)
        # Record a settled tool call into a WAL to replay from.
        source = WALWriter(run_id="gw-src", sdd_dir=sdd)
        source.append(
            decision_type="mcp_tool_call",
            inputs={
                "method": "tools/call",
                "server_name": _SERVER,
                "tool_name": _TOOL,
                "arguments": {},
                "request_id": 1,
            },
            output={"result": {"content": [{"type": "text", "text": "settled"}]}, "error": None, "latency_ms": 2.0},
            actor="mcp_gateway",
        )

        hook = _RecordingHook()
        coord = _coordinator(Path(tmp_path), X402Config(enabled=True, hook=hook), _signed_intent(), "gw-replay")
        replay = GatewayReplay(run_id="gw-src", sdd_dir=sdd)
        writer = WALWriter(run_id="gw-replay", sdd_dir=sdd)
        gw = MCPGateway(upstream_cmd=[], wal_writer=writer, replay=replay, server_name=_SERVER, settlement=coord)

        resp = await gw.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": _TOOL, "arguments": {}}}
        )

        assert resp["result"]["content"][0]["text"] == "settled"
        assert hook.calls == []  # replay never invokes the hook
        assert list(iter_spend_receipts(Path(tmp_path))) == []  # and never settles
