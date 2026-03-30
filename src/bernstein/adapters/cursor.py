"""Cursor Agent CLI adapter."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.adapters.base import CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit


class CursorAdapter(CLIAdapter):
    """Spawn and monitor Cursor Agent CLI sessions."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["cursor", "agent"]

        # Session isolation via separate user data directory
        data_dir = workdir / ".sdd" / "runtime" / "cursor" / session_id
        data_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--user-data-dir", str(data_dir)]

        # MCP config injection
        if mcp_config:
            cmd += ["--add-mcp", json.dumps(mcp_config)]

        # Prompt via positional argument (cursor agent reads it directly)
        cmd.append(prompt)

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            model=model_config.model,
        )

        # Cursor inherits OAuth session from ~/.cursor/ — no extra env keys needed
        env = build_filtered_env([])
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "cursor not found in PATH. Install Cursor from https://www.cursor.com and ensure the CLI is on PATH."
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing cursor: {exc}") from exc

        return SpawnResult(pid=proc.pid, log_path=log_path)

    def name(self) -> str:
        return "Cursor"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Cursor subscription tier from ~/.cursor/ config.

        Cursor subscription tiers:
        - Free: 50 slow requests/month
        - Pro: $20/mo, 500 fast requests + unlimited slow
        - Business: $40/mo, unlimited fast requests

        Since Cursor does not expose subscription tier via CLI, we check
        for the presence of the auth directory as a proxy for being logged in.
        The tier is reported as PRO when logged in (most common paid tier).

        Returns:
            ApiTierInfo with detected tier and rate limits, or None if not logged in.
        """
        from pathlib import Path

        cursor_dir = Path.home() / ".cursor"
        if not cursor_dir.exists():
            return None

        # Cursor Pro is the most common paid tier — conservative estimate
        tier = ApiTier.PRO
        rate_limit = RateLimit(
            requests_per_minute=50,   # 500 fast req/month ≈ ~50/min burst
            tokens_per_minute=20_000,
        )

        return ApiTierInfo(
            provider=ProviderType.CURSOR,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
