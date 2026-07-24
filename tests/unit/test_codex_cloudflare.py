"""Unit tests for CodexCloudflareAdapter.

The adapter is experimental and non-functional (issue #2783): its target
Cloudflare sandbox REST API does not exist, so every public operation refuses
with a clear error instead of issuing a request Cloudflare cannot route.
"""

from __future__ import annotations

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
        assert cfg.cpu_cores == pytest.approx(1.0)
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
        assert cfg.cpu_cores == pytest.approx(2.0)
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
        assert result.execution_time_seconds == pytest.approx(0.0)
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
        assert result.execution_time_seconds == pytest.approx(42.5)
        assert result.tokens_used == 1500


# ---------------------------------------------------------------------------
# CodexCloudflareAdapter - name
# ---------------------------------------------------------------------------


class TestCodexCloudflareAdapterName:
    def test_name_returns_codex_cloudflare(self) -> None:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig())
        assert adapter.name == "codex-cloudflare"


# ---------------------------------------------------------------------------
# Refusal: the target Cloudflare sandbox REST API does not exist (issue #2783)
# ---------------------------------------------------------------------------


def _adapter() -> CodexCloudflareAdapter:
    return CodexCloudflareAdapter(CodexSandboxConfig(cloudflare_account_id="a", cloudflare_api_token="t"))


class TestRefusal:
    @pytest.mark.asyncio
    async def test_execute_refuses(self) -> None:
        with pytest.raises(RuntimeError, match="experimental"):
            await _adapter().execute("do stuff", "ws-1", model="codex-mini")

    @pytest.mark.asyncio
    async def test_get_status_refuses(self) -> None:
        with pytest.raises(RuntimeError, match="experimental"):
            await _adapter().get_status("sb-1")

    @pytest.mark.asyncio
    async def test_cancel_refuses(self) -> None:
        with pytest.raises(RuntimeError, match="experimental"):
            await _adapter().cancel("sb-1")

    @pytest.mark.asyncio
    async def test_get_logs_refuses(self) -> None:
        with pytest.raises(RuntimeError, match="experimental"):
            await _adapter().get_logs("sb-1")

    @pytest.mark.asyncio
    async def test_refusal_message_is_actionable(self) -> None:
        with pytest.raises(RuntimeError) as exc:
            await _adapter().execute("do stuff", "ws-1")
        message = str(exc.value)
        assert "#2783" in message
        assert "CloudflareBridge" in message
