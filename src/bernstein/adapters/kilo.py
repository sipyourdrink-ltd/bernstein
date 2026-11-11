"""Kilo CLI adapter (Kilo Code)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

logger = logging.getLogger(__name__)


class KiloAdapter(CLIAdapter):
    """Spawn and monitor Kilo CLI sessions (Kilo Code).

    Kilo supports headless mode, ACP/MCP protocols, and session management.
    CLI invocation: ``kilo run --model <provider/model> --auto -- "<task>"``,
    from ``@kilocode/cli``.

    ``kilo run`` takes the message as a positional argument and auto-approves
    with ``--auto``. It has no ``--prompt``, ``--yes`` or ``--mcp``: given any
    of them, kilo 7.8 prints its usage and exits 1 before running anything.
    ``--`` ends the options, so a prompt that starts with ``-`` is still the
    message. MCP servers come from Kilo's own configuration (``kilo mcp``).
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
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "kilo",
            "run",
            "--model",
            model_config.model,
            "--auto",  # non-interactive: auto-approve permissions not explicitly denied
            "--",
            prompt,
        ]

        # `kilo run` has no flag that takes MCP servers; passing one made kilo
        # refuse the whole run. Say so rather than drop the servers silently.
        if mcp_config:
            logger.warning(
                "kilo takes MCP servers from its own configuration (`kilo mcp`), not from the "
                "command line; the %d server(s) passed for this task are not given to the agent",
                len(mcp_config.get("mcpServers", mcp_config)),
            )

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

        # Pass KILO_API_KEY if set; fall back to OAuth session in ~/.kilo/
        env = build_filtered_env(["KILO_API_KEY"])
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
                    "kilo not found in PATH. Install: npm install -g @kilocode/cli (see https://kilocode.ai)"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing kilo: {exc}") from exc

        # Pass proc through so downstream poll/wait works; see cursor.py.
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Kilo"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Kilo subscription tier from environment or local config.

        Kilo Code uses OAuth stored in ``~/.kilo/`` or an explicit
        ``KILO_API_KEY`` environment variable.  Since the CLI does not
        expose subscription details, we report PRO when any auth is
        detected (the most common paid tier).

        Returns:
            ApiTierInfo with detected tier and rate limits, or None if
            no authentication is found.
        """
        # Check explicit API key first
        api_key = os.environ.get("KILO_API_KEY", "")
        kilo_dir = Path.home() / ".kilo"

        if not api_key and not kilo_dir.exists():
            return None

        tier = ApiTier.PRO
        rate_limit = RateLimit(
            requests_per_minute=60,
            tokens_per_minute=20_000,
        )

        return ApiTierInfo(
            provider=ProviderType.KILO,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
