"""Tests for the Prefect workflow scheduler bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.prefect import _PREFECT_STATE_MAP, PrefectBridge


def _make_config(**overrides: object) -> BridgeConfig:
    defaults: dict[str, object] = {
        "bridge_type": "prefect",
        "endpoint": "http://localhost:4200",
        "api_key": "",
    }
    defaults.update(overrides)
    return BridgeConfig(**defaults)  # type: ignore[arg-type]


def _make_request(**overrides: object) -> SpawnRequest:
    defaults: dict[str, object] = {
        "agent_id": "agent-1",
        "image": "acc-store/sync-inventory",
        "command": ["run"],
        "prompt": "Sync inventory from provider",
        "env": {"provider": "lolz", "batch_size": "50"},
    }
    defaults.update(overrides)
    return SpawnRequest(**defaults)  # type: ignore[arg-type]


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data if json_data is not None else {},
        request=httpx.Request("GET", "http://test"),
    )


# ── Init ──


class TestPrefectBridgeInit:
    def test_name(self) -> None:
        bridge = PrefectBridge(_make_config())
        assert bridge.name() == "prefect"

    def test_auth_headers_empty_key(self) -> None:
        assert PrefectBridge._auth_headers("") == {}

    def test_auth_headers_with_key(self) -> None:
        headers = PrefectBridge._auth_headers("pnu_abc123")
        assert headers == {"Authorization": "Bearer pnu_abc123"}


# ── Spawn ──


class TestPrefectBridgeSpawn:
    @pytest.mark.anyio()
    async def test_spawn_creates_flow_run(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._deployment_cache["acc-store/sync-inventory"] = "dep-uuid-1"
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(
            return_value=_mock_response(json_data={"id": "run-uuid-1", "state": {"type": "SCHEDULED"}})
        )

        result = await bridge.spawn(_make_request())

        assert result.state == AgentState.PENDING
        assert result.metadata["prefect_run_id"] == "run-uuid-1"
        assert result.metadata["deployment_id"] == "dep-uuid-1"
        bridge._client.post.assert_called_once()
        call_args = bridge._client.post.call_args
        assert "deployments/dep-uuid-1/create_flow_run" in call_args[0][0]

    @pytest.mark.anyio()
    async def test_spawn_passes_parameters(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._deployment_cache["acc-store/sync-inventory"] = "dep-uuid-1"
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=_mock_response(json_data={"id": "run-uuid-2"}))

        await bridge.spawn(_make_request())

        call_kwargs = bridge._client.post.call_args[1]
        body = call_kwargs["json"]
        assert body["parameters"]["provider"] == "lolz"
        assert body["parameters"]["prompt"] == "Sync inventory from provider"

    @pytest.mark.anyio()
    async def test_spawn_http_error_raises_bridge_error(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._deployment_cache["acc-store/sync-inventory"] = "dep-uuid-1"
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=_mock_response(status_code=422, json_data={"detail": "bad"}))

        with pytest.raises(BridgeError, match="422"):
            await bridge.spawn(_make_request())


# ── Status ──


class TestPrefectBridgeStatus:
    @pytest.mark.anyio()
    async def test_status_maps_running(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(
            return_value=_mock_response(
                json_data={
                    "id": "run-uuid-1",
                    "state": {"type": "RUNNING", "message": "executing step 2"},
                    "start_time": "2026-10-16T10:00:00Z",
                    "flow_id": "flow-abc",
                }
            )
        )

        result = await bridge.status("run-uuid-1")

        assert result.state == AgentState.RUNNING
        assert result.message == "executing step 2"
        assert result.started_at is not None

    @pytest.mark.anyio()
    async def test_status_maps_completed(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(
            return_value=_mock_response(
                json_data={
                    "id": "run-uuid-1",
                    "state": {"type": "COMPLETED", "message": "done"},
                    "start_time": "2026-10-16T10:00:00Z",
                    "end_time": "2026-10-16T10:05:00Z",
                }
            )
        )

        result = await bridge.status("run-uuid-1")

        assert result.state == AgentState.COMPLETED
        assert result.exit_code == 0
        assert result.finished_at is not None

    @pytest.mark.anyio()
    async def test_status_maps_failed(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(
            return_value=_mock_response(
                json_data={
                    "id": "run-uuid-1",
                    "state": {"type": "CRASHED", "message": "OOM"},
                }
            )
        )

        result = await bridge.status("run-uuid-1")

        assert result.state == AgentState.FAILED
        assert result.exit_code == 1

    @pytest.mark.anyio()
    async def test_status_not_found_raises(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=_mock_response(status_code=404, json_data={"detail": "not found"}))

        with pytest.raises(BridgeError, match="404"):
            await bridge.status("nonexistent")


# ── Cancel ──


class TestPrefectBridgeCancel:
    @pytest.mark.anyio()
    async def test_cancel_sends_cancelling_state(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=_mock_response(json_data={"status": "ok"}))

        await bridge.cancel("run-uuid-1")

        call_args = bridge._client.post.call_args
        assert "set_state" in call_args[0][0]
        body = call_args[1]["json"]
        assert body["state"]["type"] == "CANCELLING"

    @pytest.mark.anyio()
    async def test_cancel_ignores_409(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=_mock_response(status_code=409, json_data={"detail": "conflict"}))

        await bridge.cancel("run-uuid-1")


# ── Logs ──


class TestPrefectBridgeLogs:
    @pytest.mark.anyio()
    async def test_logs_returns_formatted_lines(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(
            return_value=_mock_response(
                json_data=[
                    {"timestamp": "2026-10-16T10:00:00Z", "level": "INFO", "message": "started"},
                    {"timestamp": "2026-10-16T10:00:01Z", "level": "INFO", "message": "done"},
                ]
            )
        )

        result = await bridge.logs("run-uuid-1")

        assert b"started" in result
        assert b"done" in result


# ── Pool management ──


class TestPrefectBridgePools:
    @pytest.mark.anyio()
    async def test_list_pools(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()

        async def mock_post(url: str, **kwargs: object) -> httpx.Response:
            if "workers/filter" in url:
                return _mock_response(json_data=[{"status": "ONLINE"}, {"status": "OFFLINE"}])
            if "get_runs" in url:
                return _mock_response(json_data=[{"id": "r1"}, {"id": "r2"}])
            if "work_pools/filter" in url:
                return _mock_response(
                    json_data=[
                        {"name": "bernstein-vps", "type": "process", "is_paused": True, "concurrency_limit": 4},
                        {"name": "default", "type": "process", "is_paused": False, "concurrency_limit": None},
                    ]
                )
            return _mock_response(json_data=[])

        bridge._client.post = AsyncMock(side_effect=mock_post)

        pools = await bridge.list_pools()

        assert len(pools) == 2
        assert pools[0].name == "bernstein-vps"
        assert pools[0].is_paused is True
        assert pools[0].workers_online == 1
        assert pools[0].pending_runs == 2

    @pytest.mark.anyio()
    async def test_set_pool_paused(self) -> None:
        bridge = PrefectBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.patch = AsyncMock(
            return_value=_mock_response(json_data={"name": "bernstein-vps", "is_paused": True})
        )

        await bridge.set_pool_paused("bernstein-vps", paused=True)

        bridge._client.patch.assert_called_once()
        call_kwargs = bridge._client.patch.call_args[1]
        assert call_kwargs["json"]["is_paused"] is True


# ── State mapping coverage ──


class TestPrefectStateMap:
    @pytest.mark.parametrize(
        ("prefect_state", "expected"),
        [
            ("SCHEDULED", AgentState.PENDING),
            ("PENDING", AgentState.PENDING),
            ("RUNNING", AgentState.RUNNING),
            ("COMPLETED", AgentState.COMPLETED),
            ("FAILED", AgentState.FAILED),
            ("CRASHED", AgentState.FAILED),
            ("CANCELLED", AgentState.CANCELLED),
            ("CANCELLING", AgentState.RUNNING),
            ("LATE", AgentState.RUNNING),
            ("PAUSED", AgentState.PENDING),
        ],
    )
    def test_state_mapping(self, prefect_state: str, expected: AgentState) -> None:
        assert _PREFECT_STATE_MAP[prefect_state] == expected
