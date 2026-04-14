"""Cloudflare Agents SDK adapter for local wrangler dev or deployed worker trigger."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

logger = logging.getLogger(__name__)

# Environment variables forwarded to the wrangler/worker subprocess.
_CF_ENV_KEYS: list[str] = [
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_EMAIL",
    "CF_ACCOUNT_ID",
    "CF_API_TOKEN",
    "WRANGLER_SEND_METRICS",
]


class CloudflareAgentsAdapter(CLIAdapter):
    """Spawn agents via Cloudflare Workers using ``npx wrangler dev`` locally.

    The adapter launches a local wrangler dev server that hosts a Cloudflare
    Agents SDK worker.  The prompt is passed as a CLI argument to the worker
    entry-point.

    Required environment variables:
        CLOUDFLARE_ACCOUNT_ID: Cloudflare account identifier.
        CLOUDFLARE_API_TOKEN: API token with Workers permissions.
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        """Launch a Cloudflare Agents worker via ``npx wrangler dev``.

        Args:
            prompt: Task prompt for the agent.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope for budget caps.
            budget_multiplier: Retry budget multiplier.
            system_addendum: System-prompt instructions to inject.

        Returns:
            SpawnResult with process metadata.

        Raises:
            RuntimeError: If ``npx`` is not found or permission is denied.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID")
        api_token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")
        if not account_id:
            logger.warning("CloudflareAgentsAdapter: CLOUDFLARE_ACCOUNT_ID is not set — spawn may fail")
        if not api_token:
            logger.warning("CloudflareAgentsAdapter: CLOUDFLARE_API_TOKEN is not set — spawn may fail")

        full_prompt = f"{prompt}\n\n{system_addendum}".strip() if system_addendum else prompt

        cmd = [
            "npx",
            "wrangler",
            "dev",
            "--var",
            f"AGENT_PROMPT:{full_prompt}",
            "--var",
            f"AGENT_MODEL:{model_config.model}",
            "--var",
            f"AGENT_SESSION:{session_id}",
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(_CF_ENV_KEYS)
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=self._get_preexec_fn(),
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "npx not found in PATH. Install Node.js and wrangler: npm install -g wrangler"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing npx wrangler: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="cloudflare-agents")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Cloudflare Agents"
