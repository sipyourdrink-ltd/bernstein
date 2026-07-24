"""Codex adapter for Cloudflare Sandbox execution (experimental, non-functional).

This adapter targeted ``https://api.cloudflare.com/client/v4/accounts/{id}/sandbox/...``,
a REST route family that does not exist: an authenticated request returns HTTP
400 with Cloudflare errors 7000/7003 ("No route for that URI"). Cloudflare's real
sandbox/container product runs inside a Worker/Durable Object (the
``@cloudflare/sandbox`` SDK), not a ``client/v4`` REST surface (issue #2783).

Because the target API does not resolve to a route, no operation could ever
populate a result. Rather than pretend, every public method refuses with an
actionable error. A future implementation would drive a deployed worker (the
pattern ``bernstein.bridges.cloudflare.CloudflareBridge`` already uses).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_UNAVAILABLE_MSG = (
    "Codex-on-Cloudflare adapter is experimental and currently non-functional: "
    "it targets a Cloudflare sandbox REST API "
    "(client/v4/accounts/{id}/sandbox/...) that does not exist, so no operation "
    "can run or return a result (issue #2783). Cloudflare's real sandbox product "
    "runs inside a Worker/Durable Object; drive a deployed worker via "
    "`bernstein.bridges.cloudflare.CloudflareBridge`, or run Codex locally with "
    "the `codex` adapter."
)


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
    """Experimental Codex-on-Cloudflare adapter that refuses every operation.

    The adapter's target REST API does not exist, so it cannot create a sandbox,
    inject a command, poll status, or collect results. Every public method raises
    a clear ``RuntimeError`` instead of issuing a request that Cloudflare cannot
    route (issue #2783).
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
        """Refuse to execute: the Cloudflare sandbox REST API does not exist.

        Args:
            prompt: The task prompt (unused).
            workspace_id: Workspace identifier (unused).
            model: The Codex model to use (unused).
            timeout_minutes: Max execution time (unused).

        Raises:
            RuntimeError: Always, because the adapter is non-functional.
        """
        raise RuntimeError(_UNAVAILABLE_MSG)

    async def get_status(self, sandbox_id: str) -> str:
        """Refuse: the sandbox status route does not exist.

        Args:
            sandbox_id: The sandbox instance identifier (unused).

        Raises:
            RuntimeError: Always, because the adapter is non-functional.
        """
        raise RuntimeError(_UNAVAILABLE_MSG)

    async def cancel(self, sandbox_id: str) -> None:
        """Refuse: the sandbox cancel route does not exist.

        Args:
            sandbox_id: The sandbox instance identifier (unused).

        Raises:
            RuntimeError: Always, because the adapter is non-functional.
        """
        raise RuntimeError(_UNAVAILABLE_MSG)

    async def get_logs(self, sandbox_id: str) -> str:
        """Refuse: the sandbox logs route does not exist.

        Args:
            sandbox_id: The sandbox instance identifier (unused).

        Raises:
            RuntimeError: Always, because the adapter is non-functional.
        """
        raise RuntimeError(_UNAVAILABLE_MSG)
