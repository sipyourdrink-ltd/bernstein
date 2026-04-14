"""Unit tests for CodexCloudflareAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bernstein.adapters.codex_cloudflare import (
    CodexCloudflareAdapter,
    CodexSandboxConfig,
    CodexSandboxResult,
)

# ---------------------------------------------------------------------------
# CodexSandboxConfig
# ---------------------------------------------------------------------------


class TestCodexSandboxConfig:
    """CodexSandboxConfig dataclass defaults and overrides."""

    def test_defaults(self) -> None:
        cfg = CodexSandboxConfig()
        assert cfg.cloudflare_account_id == ""
        assert cfg.cloudflare_api_token == ""
        assert cfg.openai_api_key == ""
        assert cfg.sandbox_image == "codex-sandbox:latest"
        assert cfg.max_execution_minutes == 30
        assert cfg.memory_mb == 512
        assert cfg.cpu_cores == 1.0
        assert cfg.network_access == "restricted"
        assert cfg.r2_bucket == "bernstein-workspaces"

    def test_custom_values(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-1",
            cloudflare_api_token="tok-abc",
            openai_api_key="sk-test",
            sandbox_image="custom:v2",
            max_execution_minutes=60,
            memory_mb=1024,
            cpu_cores=2.0,
            network_access="full",
            r2_bucket="my-bucket",
        )
        assert cfg.cloudflare_account_id == "acct-1"
        assert cfg.cloudflare_api_token == "tok-abc"
        assert cfg.openai_api_key == "sk-test"
        assert cfg.sandbox_image == "custom:v2"
        assert cfg.max_execution_minutes == 60
        assert cfg.memory_mb == 1024
        assert cfg.cpu_cores == 2.0
        assert cfg.network_access == "full"
        assert cfg.r2_bucket == "my-bucket"

    def test_frozen(self) -> None:
        cfg = CodexSandboxConfig()
        with pytest.raises(AttributeError):
            cfg.memory_mb = 2048  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CodexSandboxResult
# ---------------------------------------------------------------------------


class TestCodexSandboxResult:
    """CodexSandboxResult creation with all fields."""

    def test_creation_with_defaults(self) -> None:
        result = CodexSandboxResult(sandbox_id="sb-1", status="completed")
        assert result.sandbox_id == "sb-1"
        assert result.status == "completed"
        assert result.files_changed == []
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.execution_time_seconds == 0.0
        assert result.tokens_used == 0

    def test_creation_with_all_fields(self) -> None:
        result = CodexSandboxResult(
            sandbox_id="sb-2",
            status="failed",
            files_changed=["a.py", "b.py"],
            stdout="output",
            stderr="error",
            exit_code=1,
            execution_time_seconds=42.5,
            tokens_used=1500,
        )
        assert result.sandbox_id == "sb-2"
        assert result.status == "failed"
        assert result.files_changed == ["a.py", "b.py"]
        assert result.stdout == "output"
        assert result.stderr == "error"
        assert result.exit_code == 1
        assert result.execution_time_seconds == 42.5
        assert result.tokens_used == 1500


# ---------------------------------------------------------------------------
# CodexCloudflareAdapter — name
# ---------------------------------------------------------------------------


class TestCodexCloudflareAdapterName:
    def test_name_returns_codex_cloudflare(self) -> None:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig())
        assert adapter.name == "codex-cloudflare"


# ---------------------------------------------------------------------------
# _headers()
# ---------------------------------------------------------------------------


class TestHeaders:
    def test_includes_auth_token(self) -> None:
        cfg = CodexSandboxConfig(cloudflare_api_token="my-token")
        adapter = CodexCloudflareAdapter(cfg)
        headers = adapter._headers()
        assert headers["Authorization"] == "Bearer my-token"
        assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# _create_sandbox() — payload validation
# ---------------------------------------------------------------------------


def _mock_post_response(sandbox_id: str = "sb-test") -> httpx.Response:
    """Build a mock httpx.Response for sandbox creation."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"result": {"id": sandbox_id}}
    resp.raise_for_status = MagicMock()
    return resp


class TestCreateSandbox:
    @pytest.mark.asyncio
    async def test_sends_correct_payload(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-42",
            cloudflare_api_token="tok-x",
            openai_api_key="sk-key",
            sandbox_image="img:v1",
            memory_mb=1024,
            cpu_cores=2.0,
            network_access="full",
            r2_bucket="bucket-1",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = _mock_post_response("sb-created")
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            sandbox_id = await adapter._create_sandbox("ws-123", timeout_minutes=10)

        assert sandbox_id == "sb-created"
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["image"] == "img:v1"
        assert payload["memory_mb"] == 1024
        assert payload["cpu_cores"] == 2.0
        assert payload["timeout_seconds"] == 600
        assert payload["network_access"] == "full"
        assert payload["env"]["OPENAI_API_KEY"] == "sk-key"
        assert payload["env"]["WORKSPACE_R2_BUCKET"] == "bucket-1"
        assert payload["env"]["WORKSPACE_ID"] == "ws-123"

    @pytest.mark.asyncio
    async def test_sends_to_correct_url(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-99",
            cloudflare_api_token="tok-z",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = _mock_post_response()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            await adapter._create_sandbox("ws-1", timeout_minutes=5)

        url = mock_client.post.call_args[0][0]
        assert "acct-99" in url
        assert url.endswith("/sandbox")


# ---------------------------------------------------------------------------
# execute() — happy path
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_happy_path(self) -> None:
        """execute() creates sandbox, injects command, waits, returns result."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-1",
            cloudflare_api_token="tok-1",
            openai_api_key="sk-1",
        )
        adapter = CodexCloudflareAdapter(cfg)

        expected_result = CodexSandboxResult(
            sandbox_id="sb-exec",
            status="completed",
            stdout="done",
            execution_time_seconds=5.0,
        )
        with (
            patch.object(adapter, "_create_sandbox", new_callable=AsyncMock, return_value="sb-exec"),
            patch.object(adapter, "_inject_codex_command", new_callable=AsyncMock) as mock_inject,
            patch.object(adapter, "_wait_for_completion", new_callable=AsyncMock, return_value=expected_result),
        ):
            result = await adapter.execute("do stuff", "ws-1", model="codex-mini")

        assert result.sandbox_id == "sb-exec"
        assert result.status == "completed"
        mock_inject.assert_awaited_once_with("sb-exec", "do stuff", "codex-mini")

    @pytest.mark.asyncio
    async def test_execute_uses_config_timeout(self) -> None:
        """execute() defaults to config max_execution_minutes."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="a",
            cloudflare_api_token="t",
            max_execution_minutes=15,
        )
        adapter = CodexCloudflareAdapter(cfg)

        with (
            patch.object(adapter, "_create_sandbox", new_callable=AsyncMock, return_value="sb-t"),
            patch.object(adapter, "_inject_codex_command", new_callable=AsyncMock),
            patch.object(
                adapter,
                "_wait_for_completion",
                new_callable=AsyncMock,
                return_value=CodexSandboxResult(sandbox_id="sb-t", status="completed"),
            ) as mock_wait,
        ):
            await adapter.execute("prompt", "ws-1")

        # timeout_minutes=15 => timeout_seconds=900
        mock_wait.assert_awaited_once_with("sb-t", 900)

    @pytest.mark.asyncio
    async def test_execute_custom_timeout(self) -> None:
        """execute() uses explicit timeout_minutes when provided."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="a",
            cloudflare_api_token="t",
            max_execution_minutes=30,
        )
        adapter = CodexCloudflareAdapter(cfg)

        with (
            patch.object(adapter, "_create_sandbox", new_callable=AsyncMock, return_value="sb-t2"),
            patch.object(adapter, "_inject_codex_command", new_callable=AsyncMock),
            patch.object(
                adapter,
                "_wait_for_completion",
                new_callable=AsyncMock,
                return_value=CodexSandboxResult(sandbox_id="sb-t2", status="completed"),
            ) as mock_wait,
        ):
            await adapter.execute("prompt", "ws-1", timeout_minutes=5)

        mock_wait.assert_awaited_once_with("sb-t2", 300)


# ---------------------------------------------------------------------------
# execute() — cleanup on failure
# ---------------------------------------------------------------------------


class TestExecuteCleanupOnFailure:
    @pytest.mark.asyncio
    async def test_cleanup_called_on_inject_failure(self) -> None:
        """execute() calls _cleanup_sandbox when _inject_codex_command fails."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="a",
            cloudflare_api_token="t",
        )
        adapter = CodexCloudflareAdapter(cfg)

        with (
            patch.object(adapter, "_create_sandbox", new_callable=AsyncMock, return_value="sb-fail"),
            patch.object(
                adapter,
                "_inject_codex_command",
                new_callable=AsyncMock,
                side_effect=httpx.HTTPStatusError(
                    "server error",
                    request=MagicMock(),
                    response=MagicMock(status_code=500),
                ),
            ),
            patch.object(adapter, "_cleanup_sandbox", new_callable=AsyncMock) as mock_cleanup,
            pytest.raises(httpx.HTTPStatusError),
        ):
            await adapter.execute("prompt", "ws-1")

        mock_cleanup.assert_awaited_once_with("sb-fail")

    @pytest.mark.asyncio
    async def test_cleanup_called_on_wait_failure(self) -> None:
        """execute() calls _cleanup_sandbox when _wait_for_completion fails."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="a",
            cloudflare_api_token="t",
        )
        adapter = CodexCloudflareAdapter(cfg)

        with (
            patch.object(adapter, "_create_sandbox", new_callable=AsyncMock, return_value="sb-fail2"),
            patch.object(adapter, "_inject_codex_command", new_callable=AsyncMock),
            patch.object(
                adapter,
                "_wait_for_completion",
                new_callable=AsyncMock,
                side_effect=RuntimeError("poll failed"),
            ),
            patch.object(adapter, "_cleanup_sandbox", new_callable=AsyncMock) as mock_cleanup,
            pytest.raises(RuntimeError, match="poll failed"),
        ):
            await adapter.execute("prompt", "ws-1")

        mock_cleanup.assert_awaited_once_with("sb-fail2")


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_status_string(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-s",
            cloudflare_api_token="tok-s",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.json.return_value = {"result": {"status": "running"}}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            status = await adapter.get_status("sb-123")

        assert status == "running"

    @pytest.mark.asyncio
    async def test_returns_unknown_on_missing_status(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-s",
            cloudflare_api_token="tok-s",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.json.return_value = {"result": {}}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            status = await adapter.get_status("sb-empty")

        assert status == "unknown"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_sends_delete_request(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-c",
            cloudflare_api_token="tok-c",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.delete.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            await adapter.cancel("sb-cancel")

        url = mock_client.delete.call_args[0][0]
        assert "sb-cancel" in url
        assert "acct-c" in url


# ---------------------------------------------------------------------------
# get_logs()
# ---------------------------------------------------------------------------


class TestGetLogs:
    @pytest.mark.asyncio
    async def test_returns_log_output(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-l",
            cloudflare_api_token="tok-l",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.json.return_value = {"result": {"output": "hello world\n"}}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            logs = await adapter.get_logs("sb-logs")

        assert logs == "hello world\n"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_create_sandbox_api_failure(self) -> None:
        """_create_sandbox raises on HTTP error."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-e",
            cloudflare_api_token="tok-e",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=MagicMock(),
            response=mock_resp,
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await adapter._create_sandbox("ws-err", timeout_minutes=5)

    @pytest.mark.asyncio
    async def test_get_status_api_failure(self) -> None:
        """get_status raises on HTTP error."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-e",
            cloudflare_api_token="bad-token",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden",
            request=MagicMock(),
            response=mock_resp,
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await adapter.get_status("sb-err")

    @pytest.mark.asyncio
    async def test_cleanup_sandbox_swallows_errors(self) -> None:
        """_cleanup_sandbox does not raise when cancel fails."""
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-e",
            cloudflare_api_token="tok-e",
        )
        adapter = CodexCloudflareAdapter(cfg)

        with patch.object(
            adapter,
            "cancel",
            new_callable=AsyncMock,
            side_effect=httpx.HTTPStatusError(
                "gone",
                request=MagicMock(),
                response=MagicMock(status_code=410),
            ),
        ):
            # Should not raise
            await adapter._cleanup_sandbox("sb-gone")


# ---------------------------------------------------------------------------
# _inject_codex_command()
# ---------------------------------------------------------------------------


class TestInjectCodexCommand:
    @pytest.mark.asyncio
    async def test_sends_exec_payload(self) -> None:
        cfg = CodexSandboxConfig(
            cloudflare_account_id="acct-i",
            cloudflare_api_token="tok-i",
        )
        adapter = CodexCloudflareAdapter(cfg)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("bernstein.adapters.codex_cloudflare.httpx.AsyncClient", return_value=mock_client):
            await adapter._inject_codex_command("sb-inj", "fix bugs", "o3-mini")

        call_kwargs = mock_client.post.call_args
        url = call_kwargs[0][0]
        assert "sb-inj/exec" in url
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["command"] == "codex"
        assert "fix bugs" in payload["args"]
        assert "-m" in payload["args"]
        assert "o3-mini" in payload["args"]
