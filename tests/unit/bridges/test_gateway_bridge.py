"""Tests for the API gateway bridge (#6564)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.gateway import GatewayBridge


def _make_config(**kw) -> BridgeConfig:
    defaults = {
        "bridge_type": "gateway",
        "endpoint": "https://gw.example.com",
        "api_key": "test-key",
        "timeout_seconds": 10,
    }
    defaults.update(kw)
    return BridgeConfig(**defaults)


def _make_request(**kw) -> SpawnRequest:
    defaults = {
        "agent_id": "agent-1",
        "image": "",
        "command": [],
        "prompt": "implement feature X",
        "model": "coding-smart",
        "role": "backend",
        "effort": "high",
    }
    defaults.update(kw)
    return SpawnRequest(**defaults)


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = b""
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


class TestGatewayBridgeInit:
    def test_requires_endpoint(self):
        config = BridgeConfig(bridge_type="gateway", endpoint="")
        with pytest.raises(BridgeError, match="requires an endpoint"):
            GatewayBridge(config)

    def test_name(self):
        bridge = GatewayBridge(_make_config())
        assert bridge.name() == "gateway"


class TestGatewayBridgeSpawn:
    @pytest.mark.anyio
    async def test_spawn_success(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200, {
            "status": "pending",
            "routing": {
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "cost_usd": 0.003,
                "latency_ms": 120.0,
            },
        })
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        status = await bridge.spawn(_make_request())
        assert status.agent_id == "agent-1"
        assert status.state == AgentState.PENDING

        meta = bridge.routing_metadata("agent-1")
        assert meta is not None
        assert meta.provider == "anthropic"
        assert meta.cost_usd == 0.003

    @pytest.mark.anyio
    async def test_spawn_503_with_fallback(self):
        config = _make_config(extra={"fallback_model": "fleet-hard"})
        bridge = GatewayBridge(config)

        fail_resp = _mock_response(503)
        ok_resp = _mock_response(200, {"status": "pending"})
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(side_effect=[fail_resp, ok_resp])

        status = await bridge.spawn(_make_request(model="coding-smart"))
        assert status.state == AgentState.PENDING
        assert bridge._client.post.call_count == 2

    @pytest.mark.anyio
    async def test_spawn_503_no_fallback_raises(self):
        bridge = GatewayBridge(_make_config())
        fail_resp = _mock_response(503)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=fail_resp)

        with pytest.raises(BridgeError, match="unavailable"):
            await bridge.spawn(_make_request())

    @pytest.mark.anyio
    async def test_spawn_400_raises(self):
        bridge = GatewayBridge(_make_config())
        fail_resp = _mock_response(400)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=fail_resp)

        with pytest.raises(BridgeError, match="spawn failed"):
            await bridge.spawn(_make_request())


class TestGatewayBridgeStatus:
    @pytest.mark.anyio
    async def test_status_completed(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200, {
            "status": "completed",
            "exit_code": 0,
            "routing": {"provider": "openai", "model": "gpt-5.5"},
        })
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.COMPLETED
        assert status.exit_code == 0

    @pytest.mark.anyio
    async def test_status_running(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200, {"status": "running"})
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.RUNNING


class TestGatewayBridgeCancel:
    @pytest.mark.anyio
    async def test_cancel_success(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")

    @pytest.mark.anyio
    async def test_cancel_404_ignored(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(404)
        mock_resp.raise_for_status = MagicMock()
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")


class TestGatewayBridgeLogs:
    @pytest.mark.anyio
    async def test_logs_returns_bytes(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200)
        mock_resp.content = b"log line 1\nlog line 2\n"
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        logs = await bridge.logs("agent-1")
        assert b"log line 1" in logs

    @pytest.mark.anyio
    async def test_logs_with_tail(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200)
        mock_resp.content = b"last line\n"
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        await bridge.logs("agent-1", tail=10)
        bridge._client.get.assert_called_once()
        call_kwargs = bridge._client.get.call_args
        assert call_kwargs[1].get("params", {}).get("tail") == 10


class TestRoutingMetadata:
    def test_no_metadata_returns_none(self):
        bridge = GatewayBridge(_make_config())
        assert bridge.routing_metadata("nonexistent") is None

    @pytest.mark.anyio
    async def test_metadata_cached_from_spawn(self):
        bridge = GatewayBridge(_make_config())
        mock_resp = _mock_response(200, {
            "status": "pending",
            "routing": {
                "provider": "anthropic",
                "model": "claude-opus-4",
                "cost_usd": 0.01,
                "latency_ms": 250.0,
                "fallback_used": True,
                "cache_hit": False,
                "region": "us-east-1",
            },
        })
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.spawn(_make_request())
        meta = bridge.routing_metadata("agent-1")
        assert meta is not None
        assert meta.provider == "anthropic"
        assert meta.fallback_used is True
        assert meta.extra.get("region") == "us-east-1"
