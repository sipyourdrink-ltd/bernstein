"""Codex adapter for Cloudflare Sandbox execution.

Spawns OpenAI Codex agents inside Cloudflare sandboxes rather than
locally, leveraging the same infrastructure that Codex CLI uses
for cloud execution.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexSandboxConfig:
    """Configuration for Codex on Cloudflare sandbox."""

    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    openai_api_key: str = ""
    sandbox_image: str = "codex-sandbox:latest"
    max_execution_minutes: int = 30
    memory_mb: int = 512
    cpu_cores: float = 1.0
    network_access: str = "restricted"
    r2_bucket: str = "bernstein-workspaces"


@dataclass(frozen=True)
class CodexSandboxResult:
    """Result from a Codex sandbox execution."""

    sandbox_id: str
    status: str  # "completed", "failed", "timeout", "cancelled"
    files_changed: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time_seconds: float = 0.0
    tokens_used: int = 0


class CodexCloudflareAdapter:
    """Spawn Codex agents in Cloudflare sandboxes.

    Combines Codex CLI capabilities with Cloudflare's isolated
    sandbox infrastructure for secure, scalable code execution.

    Usage:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig(
            cloudflare_account_id="...",
            cloudflare_api_token="...",
            openai_api_key="...",
        ))
        result = await adapter.execute(
            prompt="Add input validation to all API endpoints",
            workspace_id="task-123",
        )
    """

    def __init__(self, config: CodexSandboxConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        """Return adapter name."""
        return "codex-cloudflare"

    async def execute(
        self,
        prompt: str,
        workspace_id: str,
        *,
        model: str = "codex-mini",
        timeout_minutes: int | None = None,
    ) -> CodexSandboxResult:
        """Execute Codex in a Cloudflare sandbox.

        1. Creates sandbox instance
        2. Syncs workspace from R2
        3. Runs Codex with prompt
        4. Collects results and modified files
        5. Syncs changes back to R2

        Args:
            prompt: The task prompt to send to Codex.
            workspace_id: Identifier for the workspace to sync from R2.
            model: The Codex model to use.
            timeout_minutes: Max execution time; defaults to config value.

        Returns:
            CodexSandboxResult with execution outcome.

        Raises:
            httpx.HTTPStatusError: If Cloudflare API calls fail.
        """
        timeout = timeout_minutes or self._config.max_execution_minutes
        sandbox_id = await self._create_sandbox(workspace_id, timeout)
        try:
            await self._inject_codex_command(sandbox_id, prompt, model)
            return await self._wait_for_completion(sandbox_id, timeout * 60)
        except Exception:
            await self._cleanup_sandbox(sandbox_id)
            raise

    async def get_status(self, sandbox_id: str) -> str:
        """Get current sandbox execution status.

        Args:
            sandbox_id: The sandbox instance identifier.

        Returns:
            Status string: "running", "completed", "failed", or "timeout".
        """
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._config.cloudflare_account_id}/sandbox/{sandbox_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        return str(data.get("result", {}).get("status", "unknown"))

    async def cancel(self, sandbox_id: str) -> None:
        """Cancel a running Codex sandbox execution.

        Args:
            sandbox_id: The sandbox instance identifier.
        """
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._config.cloudflare_account_id}/sandbox/{sandbox_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url, headers=self._headers())
            resp.raise_for_status()
        logger.info("Cancelled sandbox %s", sandbox_id)

    async def get_logs(self, sandbox_id: str) -> str:
        """Get stdout/stderr from sandbox.

        Args:
            sandbox_id: The sandbox instance identifier.

        Returns:
            Combined stdout and stderr output.
        """
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.cloudflare_account_id}/sandbox/{sandbox_id}/logs"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        return str(data.get("result", {}).get("output", ""))

    async def _create_sandbox(self, workspace_id: str, timeout_minutes: int) -> str:
        """Create a Cloudflare sandbox configured for Codex.

        Args:
            workspace_id: Workspace identifier for R2 sync.
            timeout_minutes: Sandbox timeout in minutes.

        Returns:
            The sandbox instance ID.
        """
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._config.cloudflare_account_id}/sandbox"
        payload: dict[str, Any] = {
            "image": self._config.sandbox_image,
            "memory_mb": self._config.memory_mb,
            "cpu_cores": self._config.cpu_cores,
            "timeout_seconds": timeout_minutes * 60,
            "network_access": self._config.network_access,
            "env": {
                "OPENAI_API_KEY": self._config.openai_api_key,
                "WORKSPACE_R2_BUCKET": self._config.r2_bucket,
                "WORKSPACE_ID": workspace_id,
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        sandbox_id = str(data.get("result", {}).get("id", ""))
        logger.info("Created sandbox %s for workspace %s", sandbox_id, workspace_id)
        return sandbox_id

    async def _inject_codex_command(self, sandbox_id: str, prompt: str, model: str) -> None:
        """Send Codex execution command to sandbox.

        Args:
            sandbox_id: The sandbox instance identifier.
            prompt: Task prompt for Codex.
            model: Codex model name.
        """
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.cloudflare_account_id}/sandbox/{sandbox_id}/exec"
        )
        payload = {
            "command": "codex",
            "args": ["exec", "--full-auto", "-m", model, prompt],
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()

    async def _wait_for_completion(self, sandbox_id: str, timeout_seconds: int) -> CodexSandboxResult:
        """Poll sandbox until execution completes or times out.

        Args:
            sandbox_id: The sandbox instance identifier.
            timeout_seconds: Maximum wait time in seconds.

        Returns:
            CodexSandboxResult with final execution state.
        """
        elapsed = 0.0
        poll_interval = 5.0
        while elapsed < timeout_seconds:
            status = await self.get_status(sandbox_id)
            if status in ("completed", "failed"):
                logs = await self.get_logs(sandbox_id)
                return CodexSandboxResult(
                    sandbox_id=sandbox_id,
                    status=status,
                    stdout=logs,
                    execution_time_seconds=elapsed,
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Timed out — cancel and return timeout result
        await self._cleanup_sandbox(sandbox_id)
        return CodexSandboxResult(
            sandbox_id=sandbox_id,
            status="timeout",
            execution_time_seconds=elapsed,
        )

    async def _cleanup_sandbox(self, sandbox_id: str) -> None:
        """Terminate and clean up sandbox resources.

        Args:
            sandbox_id: The sandbox instance identifier.
        """
        try:
            await self.cancel(sandbox_id)
        except Exception:
            logger.warning("Failed to clean up sandbox %s", sandbox_id, exc_info=True)

    def _headers(self) -> dict[str, str]:
        """Build Cloudflare API request headers.

        Returns:
            Dictionary with Authorization and Content-Type headers.
        """
        return {
            "Authorization": f"Bearer {self._config.cloudflare_api_token}",
            "Content-Type": "application/json",
        }
