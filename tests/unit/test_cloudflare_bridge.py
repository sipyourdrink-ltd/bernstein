"""Unit tests for CloudflareBridge spawn/status/cancel/logs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.cloudflare import CloudflareBridge, _parse_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    endpoint: str = "https://my-worker.example.workers.dev",
    api_key: str = "cf-token-123",
    account_id: str = "acct-abc",
    worker_name: str = "bernstein-agent",
) -> BridgeConfig:
    return BridgeConfig(
        bridge_type="cloudflare",
        endpoint=endpoint,
        api_key=api_key,
        extra={"account_id": account_id, "worker_name": worker_name},
    )


def _make_request(agent_id: str = "agent-1") -> SpawnRequest:
    return SpawnRequest(
        agent_id=agent_id,
        image="",
        command=[],
        prompt="Fix the tests",
        model="gpt-4o",
        role="backend",
        effort="high",
        timeout_seconds=300,
    )


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    content: bytes = b"",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if content else ""
    return resp


# ---------------------------------------------------------------------------
# __init__ validation
# ---------------------------------------------------------------------------


class TestCloudflareBridgeInit:
    def test_valid_config(self) -> None:
        bridge = CloudflareBridge(_make_config())
        assert bridge.name() == "cloudflare"

    def test_missing_endpoint_raises(self) -> None:
        with pytest.raises(BridgeError, match="non-empty endpoint"):
            CloudflareBridge(_make_config(endpoint=""))

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(BridgeError, match="non-empty api_key"):
            CloudflareBridge(_make_config(api_key=""))

    def test_missing_account_id_raises(self) -> None:
        with pytest.raises(BridgeError, match="account_id"):
            CloudflareBridge(
                BridgeConfig(
                    bridge_type="cloudflare",
                    endpoint="https://example.com",
                    api_key="tok",
                    extra={},
                )
            )


# ---------------------------------------------------------------------------
# _parse_state
# ---------------------------------------------------------------------------


class TestParseState:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pending", AgentState.PENDING),
            ("running", AgentState.RUNNING),
            ("completed", AgentState.COMPLETED),
            ("complete", AgentState.COMPLETED),
            ("failed", AgentState.FAILED),
            ("error", AgentState.FAILED),
            ("cancelled", AgentState.CANCELLED),
            ("canceled", AgentState.CANCELLED),
            ("RUNNING", AgentState.RUNNING),
            ("unknown", AgentState.PENDING),
        ],
    )
    def test_state_mapping(self, raw: str, expected: AgentState) -> None:
        assert _parse_state(raw) == expected


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


class TestCloudflareBridgeSpawn:
    @pytest.mark.asyncio
    async def test_spawn_success(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={"state": "running", "started_at": 1700000000.0, "message": "OK"},
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        status = await bridge.spawn(_make_request())
        assert status.agent_id == "agent-1"
        assert status.state == AgentState.RUNNING
        assert status.metadata["worker"] == "bernstein-agent"
        bridge._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_http_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(BridgeError, match="Failed to spawn"):
            await bridge.spawn(_make_request())

    @pytest.mark.asyncio
    async def test_spawn_4xx_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(422, content=b"invalid payload")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="422"):
            await bridge.spawn(_make_request())


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestCloudflareBridgeStatus:
    @pytest.mark.asyncio
    async def test_status_success(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "state": "completed",
                "exit_code": 0,
                "started_at": 1700000000.0,
                "finished_at": 1700000060.0,
                "message": "done",
            },
        )
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.COMPLETED
        assert status.exit_code == 0

    @pytest.mark.asyncio
    async def test_status_http_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))

        with pytest.raises(BridgeError, match="Failed to get status"):
            await bridge.status("agent-1")

    @pytest.mark.asyncio
    async def test_status_404(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(404, content=b"not found")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="404"):
            await bridge.status("agent-unknown")


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCloudflareBridgeCancel:
    @pytest.mark.asyncio
    async def test_cancel_success(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(200)
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")  # must not raise
        bridge._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_http_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(BridgeError, match="Failed to cancel"):
            await bridge.cancel("agent-1")

    @pytest.mark.asyncio
    async def test_cancel_5xx_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(500, content=b"internal error")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="500"):
            await bridge.cancel("agent-1")


# ---------------------------------------------------------------------------
# logs()
# ---------------------------------------------------------------------------


class TestCloudflareBridgeLogs:
    @pytest.mark.asyncio
    async def test_logs_success(self) -> None:
        bridge = CloudflareBridge(_make_config())
        log_content = b"line1\nline2\nline3\n"
        mock_resp = _mock_response(200, content=log_content)
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert result == log_content

    @pytest.mark.asyncio
    async def test_logs_with_tail(self) -> None:
        bridge = CloudflareBridge(_make_config())
        mock_resp = _mock_response(200, content=b"last line\n")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        await bridge.logs("agent-1", tail=5)
        call_kwargs = bridge._client.get.call_args
        assert call_kwargs.kwargs.get("params", {}).get("tail") == "5"

    @pytest.mark.asyncio
    async def test_logs_truncated_to_max_bytes(self) -> None:
        config = _make_config()
        config_small = BridgeConfig(
            bridge_type="cloudflare",
            endpoint=config.endpoint,
            api_key=config.api_key,
            max_log_bytes=10,
            extra=config.extra,
        )
        bridge = CloudflareBridge(config_small)
        large_content = b"x" * 100
        mock_resp = _mock_response(200, content=large_content)
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_logs_http_error(self) -> None:
        bridge = CloudflareBridge(_make_config())
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(BridgeError, match="Failed to fetch logs"):
            await bridge.logs("agent-1")
