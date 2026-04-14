"""Tests for the Cloudflare Workflow bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from bernstein.bridges.base import AgentState, BridgeConfig, BridgeError, SpawnRequest
from bernstein.bridges.cloudflare_workflow import (
    CloudflareWorkflowBridge,
    WorkflowConfig,
    WorkflowStatus,
    WorkflowStep,
)


def _make_config(**overrides: object) -> BridgeConfig:
    """Create a BridgeConfig suitable for CloudflareWorkflowBridge."""
    defaults: dict[str, object] = {
        "bridge_type": "cloudflare-workflow",
        "endpoint": "https://workers.example.com",
        "api_key": "test-token",
        "extra": {"account_id": "acct-123", "worker_name": "bernstein-agent"},
    }
    defaults.update(overrides)
    return BridgeConfig(**defaults)  # type: ignore[arg-type]


def _make_request(**overrides: object) -> SpawnRequest:
    """Create a SpawnRequest for testing."""
    defaults: dict[str, object] = {
        "agent_id": "agent-1",
        "image": "bernstein:latest",
        "command": ["run"],
        "prompt": "Fix the bug",
        "model": "claude-sonnet-4-20250514",
        "role": "backend",
    }
    defaults.update(overrides)
    return SpawnRequest(**defaults)  # type: ignore[arg-type]


def _mock_response(
    status_code: int = 200,
    json_data: dict | None = None,
    content: bytes = b"",
) -> httpx.Response:
    """Build a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data if json_data is not None else {},
        content=content if not json_data else None,
    )
    return resp


# ---------------------------------------------------------------------------
# Init validation
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for CloudflareWorkflowBridge.__init__ validation."""

    def test_wrong_bridge_type_raises(self) -> None:
        config = _make_config(bridge_type="wrong")
        with pytest.raises(BridgeError, match="Expected bridge_type='cloudflare-workflow'"):
            CloudflareWorkflowBridge(config)

    def test_missing_account_id_raises(self) -> None:
        config = _make_config(extra={"worker_name": "w"})
        with pytest.raises(BridgeError, match="Missing required config: extra.account_id"):
            CloudflareWorkflowBridge(config)

    def test_empty_account_id_raises(self) -> None:
        config = _make_config(extra={"account_id": ""})
        with pytest.raises(BridgeError, match="Missing required config: extra.account_id"):
            CloudflareWorkflowBridge(config)

    def test_valid_config_creates_bridge(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge.name() == "cloudflare-workflow"

    def test_default_worker_name(self) -> None:
        config = _make_config(extra={"account_id": "acct-1"})
        bridge = CloudflareWorkflowBridge(config)
        assert bridge._worker_name == "bernstein-agent"

    def test_custom_worker_name(self) -> None:
        config = _make_config(extra={"account_id": "acct-1", "worker_name": "custom-w"})
        bridge = CloudflareWorkflowBridge(config)
        assert bridge._worker_name == "custom-w"


# ---------------------------------------------------------------------------
# name()
# ---------------------------------------------------------------------------


class TestName:
    """Tests for name() method."""

    def test_returns_cloudflare_workflow(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge.name() == "cloudflare-workflow"


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


class TestSpawn:
    """Tests for spawn() dispatching workflows."""

    @pytest.mark.asyncio
    async def test_spawn_dispatches_workflow(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(
            200,
            {"result": {"id": "wf-123", "started_at": 1000.0, "message": "ok"}},
        )
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        status = await bridge.spawn(_make_request())

        assert status.agent_id == "agent-1"
        assert status.state == AgentState.PENDING
        assert status.metadata["workflow_id"] == "wf-123"
        assert status.metadata["worker"] == "bernstein-agent"

    @pytest.mark.asyncio
    async def test_spawn_api_error_raises(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(500, {"error": "internal"})
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow dispatch returned 500"):
            await bridge.spawn(_make_request())

    @pytest.mark.asyncio
    async def test_spawn_http_error_raises(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("timeout"))

        with pytest.raises(BridgeError, match="Failed to dispatch workflow"):
            await bridge.spawn(_make_request())


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for status() mapping workflow steps."""

    @pytest.mark.asyncio
    async def test_status_running(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(
            200,
            {
                "result": {
                    "id": "wf-1",
                    "current_step": "execute",
                    "status": "running",
                    "started_at": 1000.0,
                }
            },
        )
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.RUNNING
        assert status.metadata["current_step"] == "execute"

    @pytest.mark.asyncio
    async def test_status_completed(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(
            200,
            {
                "result": {
                    "id": "wf-1",
                    "current_step": "complete",
                    "status": "complete",
                    "exit_code": 0,
                    "finished_at": 2000.0,
                }
            },
        )
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        status = await bridge.status("agent-1")
        assert status.state == AgentState.COMPLETED
        assert status.exit_code == 0

    @pytest.mark.asyncio
    async def test_status_api_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(404, {"error": "not found"})
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow status returned 404"):
            await bridge.status("agent-x")

    @pytest.mark.asyncio
    async def test_status_http_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(side_effect=httpx.ConnectError("down"))

        with pytest.raises(BridgeError, match="Failed to get workflow status"):
            await bridge.status("agent-x")


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCancel:
    """Tests for cancel() sending cancel request."""

    @pytest.mark.asyncio
    async def test_cancel_success(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(200)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.cancel("agent-1")  # should not raise

    @pytest.mark.asyncio
    async def test_cancel_api_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(500)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow cancel returned 500"):
            await bridge.cancel("agent-1")

    @pytest.mark.asyncio
    async def test_cancel_http_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("nope"))

        with pytest.raises(BridgeError, match="Failed to cancel workflow"):
            await bridge.cancel("agent-1")


# ---------------------------------------------------------------------------
# logs()
# ---------------------------------------------------------------------------


class TestLogs:
    """Tests for logs() retrieval."""

    @pytest.mark.asyncio
    async def test_logs_returns_bytes(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(200, content=b"line1\nline2\n")
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert result == b"line1\nline2\n"

    @pytest.mark.asyncio
    async def test_logs_with_tail(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(200, content=b"last line\n")
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1", tail=5)
        assert result == b"last line\n"
        # Verify tail param was passed
        call_kwargs = bridge._client.get.call_args
        assert call_kwargs.kwargs.get("params", {}).get("tail") == "5"

    @pytest.mark.asyncio
    async def test_logs_truncates_to_max_bytes(self) -> None:
        config_obj = BridgeConfig(
            bridge_type="cloudflare-workflow",
            endpoint="https://w.example.com",
            api_key="tok",
            max_log_bytes=10,
            extra={"account_id": "a"},
        )
        bridge = CloudflareWorkflowBridge(config_obj)
        mock_resp = _mock_response(200, content=b"x" * 100)
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        result = await bridge.logs("agent-1")
        assert len(result) == 10

    @pytest.mark.asyncio
    async def test_logs_api_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(500)
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow logs returned 500"):
            await bridge.logs("agent-1")


# ---------------------------------------------------------------------------
# approve()
# ---------------------------------------------------------------------------


class TestApprove:
    """Tests for approve() sending approval."""

    @pytest.mark.asyncio
    async def test_approve_success(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(200)
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        await bridge.approve("wf-1")  # should not raise

    @pytest.mark.asyncio
    async def test_approve_api_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(400, {"error": "not waiting"})
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow approval returned 400"):
            await bridge.approve("wf-1")

    @pytest.mark.asyncio
    async def test_approve_http_error(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        bridge._client = AsyncMock()
        bridge._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))

        with pytest.raises(BridgeError, match="Failed to approve workflow"):
            await bridge.approve("wf-1")


# ---------------------------------------------------------------------------
# _map_step_to_state()
# ---------------------------------------------------------------------------


class TestMapStepToState:
    """Tests for _map_step_to_state() covering all combinations."""

    def test_complete_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("execute", "complete") == AgentState.COMPLETED

    def test_succeeded_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("merge", "succeeded") == AgentState.COMPLETED

    def test_failed_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("verify", "failed") == AgentState.FAILED

    def test_errored_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("spawn", "errored") == AgentState.FAILED

    def test_cancelled_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("execute", "cancelled") == AgentState.CANCELLED

    def test_approval_step_pending(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("approval", "running") == AgentState.PENDING

    def test_running_default(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("execute", "running") == AgentState.RUNNING

    def test_unknown_status_running(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("claim", "pending") == AgentState.RUNNING

    def test_terminal_overrides_approval(self) -> None:
        """Terminal states take priority even for the approval step."""
        bridge = CloudflareWorkflowBridge(_make_config())
        assert bridge._map_step_to_state("approval", "failed") == AgentState.FAILED
        assert bridge._map_step_to_state("approval", "complete") == AgentState.COMPLETED


# ---------------------------------------------------------------------------
# get_workflow_status()
# ---------------------------------------------------------------------------


class TestGetWorkflowStatus:
    """Tests for get_workflow_status() detailed status."""

    @pytest.mark.asyncio
    async def test_returns_workflow_status(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(
            200,
            {
                "result": {
                    "id": "wf-1",
                    "current_step": "execute",
                    "status": "running",
                    "started_at": 1000.0,
                    "updated_at": 1100.0,
                    "retries_used": 1,
                    "error": "",
                    "metadata": {"foo": "bar"},
                }
            },
        )
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        ws = await bridge.get_workflow_status("wf-1")
        assert isinstance(ws, WorkflowStatus)
        assert ws.workflow_id == "wf-1"
        assert ws.current_step == WorkflowStep.EXECUTE
        assert ws.state == AgentState.RUNNING
        assert ws.retries_used == 1
        assert ws.metadata == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_unknown_step_defaults_to_claim(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(
            200,
            {"result": {"current_step": "unknown-step", "status": "running"}},
        )
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        ws = await bridge.get_workflow_status("wf-1")
        assert ws.current_step == WorkflowStep.CLAIM

    @pytest.mark.asyncio
    async def test_api_error_raises(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        mock_resp = _mock_response(404)
        bridge._client = AsyncMock()
        bridge._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(BridgeError, match="Workflow status returned 404"):
            await bridge.get_workflow_status("wf-x")


# ---------------------------------------------------------------------------
# Dataclass creation
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Tests for WorkflowConfig and WorkflowStatus creation."""

    def test_workflow_config_defaults(self) -> None:
        cfg = WorkflowConfig(account_id="a", api_token="t")
        assert cfg.worker_name == "bernstein-agent"
        assert cfg.max_retries == 3
        assert cfg.spawn_timeout_minutes == 30
        assert cfg.execute_timeout_minutes == 120
        assert cfg.verify_timeout_minutes == 15
        assert cfg.require_approval is False

    def test_workflow_config_custom(self) -> None:
        cfg = WorkflowConfig(
            account_id="a",
            api_token="t",
            worker_name="custom",
            max_retries=5,
            require_approval=True,
        )
        assert cfg.worker_name == "custom"
        assert cfg.max_retries == 5
        assert cfg.require_approval is True

    def test_workflow_status_defaults(self) -> None:
        ws = WorkflowStatus(
            workflow_id="wf-1",
            current_step=WorkflowStep.CLAIM,
            state=AgentState.PENDING,
        )
        assert ws.started_at == 0.0
        assert ws.retries_used == 0
        assert ws.error_message == ""
        assert ws.metadata == {}

    def test_workflow_status_frozen(self) -> None:
        ws = WorkflowStatus(
            workflow_id="wf-1",
            current_step=WorkflowStep.EXECUTE,
            state=AgentState.RUNNING,
        )
        with pytest.raises(AttributeError):
            ws.state = AgentState.FAILED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WorkflowStep enum
# ---------------------------------------------------------------------------


class TestWorkflowStep:
    """Tests for WorkflowStep enum values."""

    def test_all_steps(self) -> None:
        assert WorkflowStep.CLAIM == "claim"
        assert WorkflowStep.SPAWN == "spawn"
        assert WorkflowStep.EXECUTE == "execute"
        assert WorkflowStep.VERIFY == "verify"
        assert WorkflowStep.APPROVAL == "approval"
        assert WorkflowStep.MERGE == "merge"
        assert WorkflowStep.COMPLETE == "complete"

    def test_step_count(self) -> None:
        assert len(WorkflowStep) == 7


# ---------------------------------------------------------------------------
# API URL construction
# ---------------------------------------------------------------------------


class TestApiUrl:
    """Tests for _api_url() construction."""

    def test_url_format(self) -> None:
        bridge = CloudflareWorkflowBridge(_make_config())
        url = bridge._api_url("/instances")
        assert url == ("https://api.cloudflare.com/client/v4/accounts/acct-123/workflows/bernstein-agent/instances")

    def test_url_with_custom_worker(self) -> None:
        config = _make_config(extra={"account_id": "acct-1", "worker_name": "my-w"})
        bridge = CloudflareWorkflowBridge(config)
        url = bridge._api_url("/instances/wf-1")
        assert "my-w/instances/wf-1" in url
