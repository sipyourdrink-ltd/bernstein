"""Unit tests for CloudflareSandboxBridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.cloudflare_sandbox import (
    CloudflareSandboxBridge,
    NetworkAccess,
    SandboxConfig,
    SandboxInstance,
    SandboxType,
    _parse_sandbox_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    bridge_type: str = "cloudflare-sandbox",
    endpoint: str = "https://api.cloudflare.com",
    api_key: str = "cf-token-123",
    account_id: str = "acct-abc",
    sandbox_type: str = "isolate",
    max_memory_mb: int = 128,
    max_execution_seconds: int = 300,
    r2_bucket: str = "bernstein-workspaces",
) -> BridgeConfig:
    return BridgeConfig(
        bridge_type=bridge_type,
        endpoint=endpoint,
        api_key=api_key,
        extra={
            "account_id": account_id,
            "sandbox_type": sandbox_type,
            "max_memory_mb": max_memory_mb,
            "max_execution_seconds": max_execution_seconds,
            "r2_bucket": r2_bucket,
        },
    )


def _make_request(agent_id: str = "agent-1") -> SpawnRequest:
    return SpawnRequest(
        agent_id=agent_id,
        image="",
        command=["python", "main.py"],
        prompt="Fix the tests",
        model="gpt-4o",
        role="backend",
        effort="high",
        timeout_seconds=300,
        memory_mb=256,
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
# Enums
# ---------------------------------------------------------------------------


class TestSandboxType:
    def test_values(self) -> None:
        assert SandboxType.ISOLATE == "isolate"
        assert SandboxType.CONTAINER == "container"

    def test_membership(self) -> None:
        assert len(SandboxType) == 2


class TestNetworkAccess:
    def test_values(self) -> None:
        assert NetworkAccess.NONE == "none"
        assert NetworkAccess.RESTRICTED == "restricted"
        assert NetworkAccess.FULL == "full"

    def test_membership(self) -> None:
        assert len(NetworkAccess) == 3


# ---------------------------------------------------------------------------
# SandboxConfig
# ---------------------------------------------------------------------------


class TestSandboxConfig:
    def test_defaults(self) -> None:
        cfg = SandboxConfig()
        assert cfg.sandbox_type == SandboxType.ISOLATE
        assert cfg.max_memory_mb == 128
        assert cfg.max_execution_seconds == 300
        assert cfg.network_access == NetworkAccess.RESTRICTED
        assert "api.github.com" in cfg.allowed_domains
        assert cfg.r2_bucket == "bernstein-workspaces"

    def test_custom_values(self) -> None:
        cfg = SandboxConfig(
            sandbox_type=SandboxType.CONTAINER,
            max_memory_mb=512,
            max_execution_seconds=600,
            network_access=NetworkAccess.FULL,
            allowed_domains=("example.com",),
            r2_bucket="custom-bucket",
        )
        assert cfg.sandbox_type == SandboxType.CONTAINER
        assert cfg.max_memory_mb == 512
        assert cfg.r2_bucket == "custom-bucket"

    def test_frozen(self) -> None:
        cfg = SandboxConfig()
        with pytest.raises(AttributeError):
            cfg.max_memory_mb = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SandboxInstance
# ---------------------------------------------------------------------------


class TestSandboxInstance:
    def test_creation(self) -> None:
        inst = SandboxInstance(
            sandbox_id="sb-1",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.RUNNING,
            workspace_id="ws-abc",
            created_at=1700000000.0,
            cpu_time_ms=42.5,
            memory_used_mb=64.0,
            network_requests=3,
        )
        assert inst.sandbox_id == "sb-1"
        assert inst.sandbox_type == SandboxType.ISOLATE
        assert inst.state == AgentState.RUNNING
        assert inst.workspace_id == "ws-abc"
        assert inst.cpu_time_ms == 42.5
        assert inst.network_requests == 3

    def test_defaults(self) -> None:
        inst = SandboxInstance(
            sandbox_id="sb-2",
            sandbox_type=SandboxType.CONTAINER,
            state=AgentState.PENDING,
        )
        assert inst.workspace_id == ""
        assert inst.created_at == 0.0
        assert inst.cpu_time_ms == 0.0
        assert inst.memory_used_mb == 0.0
        assert inst.network_requests == 0

    def test_frozen(self) -> None:
        inst = SandboxInstance(
            sandbox_id="sb-3",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.PENDING,
        )
        with pytest.raises(AttributeError):
            inst.state = AgentState.RUNNING  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _parse_sandbox_state
# ---------------------------------------------------------------------------


class TestParseSandboxState:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("creating", AgentState.PENDING),
            ("pending", AgentState.PENDING),
            ("running", AgentState.RUNNING),
            ("succeeded", AgentState.COMPLETED),
            ("completed", AgentState.COMPLETED),
            ("failed", AgentState.FAILED),
            ("error", AgentState.FAILED),
            ("terminated", AgentState.CANCELLED),
            ("cancelled", AgentState.CANCELLED),
            ("canceled", AgentState.CANCELLED),
            ("RUNNING", AgentState.RUNNING),
            ("Failed", AgentState.FAILED),
            ("unknown_state", AgentState.PENDING),
            ("", AgentState.PENDING),
        ],
    )
    def test_state_mapping(self, raw: str, expected: AgentState) -> None:
        assert _parse_sandbox_state(raw) == expected


# ---------------------------------------------------------------------------
# __init__ validation
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeInit:
    def test_valid_config(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        assert bridge.name() == "cloudflare-sandbox"

    def test_wrong_bridge_type_raises(self) -> None:
        with pytest.raises(BridgeError, match="bridge_type='cloudflare-sandbox'"):
            CloudflareSandboxBridge(_make_config(bridge_type="wrong"))

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(BridgeError, match="non-empty api_key"):
            CloudflareSandboxBridge(_make_config(api_key=""))

    def test_missing_account_id_raises(self) -> None:
        with pytest.raises(BridgeError, match="account_id"):
            CloudflareSandboxBridge(
                BridgeConfig(
                    bridge_type="cloudflare-sandbox",
                    endpoint="https://api.cloudflare.com",
                    api_key="tok",
                    extra={},
                )
            )

    def test_sandbox_config_from_extras(self) -> None:
        bridge = CloudflareSandboxBridge(
            _make_config(
                sandbox_type="container",
                max_memory_mb=512,
                max_execution_seconds=600,
                r2_bucket="custom-bucket",
            )
        )
        assert bridge.sandbox_config.sandbox_type == SandboxType.CONTAINER
        assert bridge.sandbox_config.max_memory_mb == 512
        assert bridge.sandbox_config.max_execution_seconds == 600
        assert bridge.sandbox_config.r2_bucket == "custom-bucket"

    def test_invalid_sandbox_type_defaults_to_isolate(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config(sandbox_type="unknown"))
        assert bridge.sandbox_config.sandbox_type == SandboxType.ISOLATE


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeSpawn:
    @pytest.mark.asyncio
    async def test_spawn_success(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "result": {
                    "sandbox_id": "sb-123",
                    "state": "creating",
                    "workspace_id": "ws-abc",
                    "created_at": 1700000000.0,
                    "message": "Sandbox creating",
                },
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        status = await bridge.spawn(_make_request())
        assert status.agent_id == "agent-1"
        assert status.state == AgentState.PENDING
        assert status.metadata["sandbox_id"] == "sb-123"
        assert status.metadata["sandbox_type"] == "isolate"
        assert status.metadata["account_id"] == "acct-abc"
        bridge._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_stores_instance(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "result": {
                    "sandbox_id": "sb-456",
                    "state": "creating",
                },
            },
        )
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.spawn(_make_request("agent-x"))
        assert "agent-x" in bridge._instances
        assert bridge._instances["agent-x"].sandbox_id == "sb-456"

    @pytest.mark.asyncio
    async def test_spawn_http_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(BridgeError, match="Failed to create sandbox"):
            await bridge.spawn(_make_request())

    @pytest.mark.asyncio
    async def test_spawn_4xx_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(422, content=b"invalid payload")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="422"):
            await bridge.spawn(_make_request())

    @pytest.mark.asyncio
    async def test_spawn_caps_timeout_and_memory(self) -> None:
        """Spawn should cap request values to sandbox config limits."""
        bridge = CloudflareSandboxBridge(_make_config(max_memory_mb=64, max_execution_seconds=60))
        mock_resp = _mock_response(200, json_data={"result": {"sandbox_id": "sb-x", "state": "creating"}})
        bridge._client.post = AsyncMock(return_value=mock_resp)

        request = _make_request()
        await bridge.spawn(request)

        call_kwargs = bridge._client.post.call_args
        payload = (
            call_kwargs.kwargs.get("json") or call_kwargs.args[1]
            if len(call_kwargs.args) > 1
            else call_kwargs.kwargs["json"]
        )
        assert payload["timeout_seconds"] == 60
        assert payload["memory_mb"] == 64


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeStatus:
    @pytest.mark.asyncio
    async def test_status_success(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "result": {
                    "state": "running",
                    "started_at": 1700000000.0,
                    "cpu_time_ms": 150.0,
                    "memory_used_mb": 32.5,
                    "network_requests": 2,
                    "message": "executing",
                },
            },
        )
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.RUNNING
        assert status.message == "executing"

    @pytest.mark.asyncio
    async def test_status_uses_stored_sandbox_id(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._instances["agent-1"] = SandboxInstance(
            sandbox_id="sb-stored",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.RUNNING,
        )
        mock_resp = _mock_response(200, json_data={"result": {"state": "running"}})
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.metadata["sandbox_id"] == "sb-stored"
        # Verify the URL used the stored sandbox_id
        call_args = bridge._client.get.call_args
        assert "sb-stored" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_status_updates_instance(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._instances["agent-1"] = SandboxInstance(
            sandbox_id="sb-1",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.PENDING,
        )
        mock_resp = _mock_response(
            200,
            json_data={
                "result": {
                    "state": "running",
                    "cpu_time_ms": 100.0,
                    "memory_used_mb": 48.0,
                },
            },
        )
        bridge._client.get = AsyncMock(return_value=mock_resp)

        await bridge.status("agent-1")
        updated = bridge._instances["agent-1"]
        assert updated.state == AgentState.RUNNING
        assert updated.cpu_time_ms == 100.0
        assert updated.memory_used_mb == 48.0

    @pytest.mark.asyncio
    async def test_status_http_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))

        with pytest.raises(BridgeError, match="Failed to get sandbox status"):
            await bridge.status("agent-1")

    @pytest.mark.asyncio
    async def test_status_404(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(404, content=b"not found")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="404"):
            await bridge.status("agent-unknown")


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeCancel:
    @pytest.mark.asyncio
    async def test_cancel_success(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._instances["agent-1"] = SandboxInstance(
            sandbox_id="sb-1",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.RUNNING,
        )
        mock_resp = _mock_response(200)
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")
        bridge._client.post.assert_awaited_once()
        assert bridge._instances["agent-1"].state == AgentState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_without_stored_instance(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(200)
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")  # must not raise
        bridge._client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_http_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(BridgeError, match="Failed to terminate sandbox"):
            await bridge.cancel("agent-1")

    @pytest.mark.asyncio
    async def test_cancel_5xx_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(500, content=b"internal error")
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="500"):
            await bridge.cancel("agent-1")


# ---------------------------------------------------------------------------
# logs()
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeLogs:
    @pytest.mark.asyncio
    async def test_logs_success(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        log_content = b"line1\nline2\nline3\n"
        mock_resp = _mock_response(200, content=log_content)
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert result == log_content

    @pytest.mark.asyncio
    async def test_logs_with_tail(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(200, content=b"last line\n")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        await bridge.logs("agent-1", tail=5)
        call_kwargs = bridge._client.get.call_args
        assert call_kwargs.kwargs.get("params", {}).get("tail") == "5"

    @pytest.mark.asyncio
    async def test_logs_truncated_to_max_bytes(self) -> None:
        config = BridgeConfig(
            bridge_type="cloudflare-sandbox",
            endpoint="https://api.cloudflare.com",
            api_key="tok",
            max_log_bytes=10,
            extra={"account_id": "acct-abc"},
        )
        bridge = CloudflareSandboxBridge(config)
        large_content = b"x" * 100
        mock_resp = _mock_response(200, content=large_content)
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_logs_http_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(BridgeError, match="Failed to fetch logs"):
            await bridge.logs("agent-1")

    @pytest.mark.asyncio
    async def test_logs_uses_stored_sandbox_id(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._instances["agent-1"] = SandboxInstance(
            sandbox_id="sb-stored",
            sandbox_type=SandboxType.ISOLATE,
            state=AgentState.RUNNING,
        )
        mock_resp = _mock_response(200, content=b"logs here")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        await bridge.logs("agent-1")
        call_args = bridge._client.get.call_args
        assert "sb-stored" in call_args.args[0]


# ---------------------------------------------------------------------------
# download_artifacts()
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeArtifacts:
    @pytest.mark.asyncio
    async def test_download_artifacts_success(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(
            200,
            json_data={
                "result": {"files": ["src/main.py", "tests/test_main.py"]},
            },
        )
        bridge._client.get = AsyncMock(return_value=mock_resp)

        files = await bridge.download_artifacts("sb-123")
        assert files == ["src/main.py", "tests/test_main.py"]

    @pytest.mark.asyncio
    async def test_download_artifacts_empty(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(200, json_data={"result": {"files": []}})
        bridge._client.get = AsyncMock(return_value=mock_resp)

        files = await bridge.download_artifacts("sb-123")
        assert files == []

    @pytest.mark.asyncio
    async def test_download_artifacts_http_error(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(BridgeError, match="Failed to list artifacts"):
            await bridge.download_artifacts("sb-123")

    @pytest.mark.asyncio
    async def test_download_artifacts_4xx(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        mock_resp = _mock_response(404, content=b"not found")
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="404"):
            await bridge.download_artifacts("sb-123")


# ---------------------------------------------------------------------------
# _api_url / _headers / _map_state
# ---------------------------------------------------------------------------


class TestCloudflareSandboxBridgeHelpers:
    def test_api_url(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        url = bridge._api_url("/create")
        assert url == "https://api.cloudflare.com/client/v4/accounts/acct-abc/sandbox/create"

    def test_api_url_strips_trailing_slash(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config(endpoint="https://api.cloudflare.com/"))
        url = bridge._api_url("/status")
        assert "//" not in url.split("://")[1]

    def test_headers(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config(api_key="my-token"))
        headers = bridge._headers()
        assert headers["Authorization"] == "Bearer my-token"
        assert headers["Content-Type"] == "application/json"

    def test_map_state(self) -> None:
        bridge = CloudflareSandboxBridge(_make_config())
        assert bridge._map_state("running") == AgentState.RUNNING
        assert bridge._map_state("succeeded") == AgentState.COMPLETED
        assert bridge._map_state("terminated") == AgentState.CANCELLED
        assert bridge._map_state("gibberish") == AgentState.PENDING
