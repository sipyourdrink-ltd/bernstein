"""Kimi CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class KimiAdapter(CLIAdapter):
    """Spawn and monitor Kimi CLI sessions.

    Kimi CLI is Moonshot's coding agent.  It is invoked with ``--yolo``
    (auto-approve tool calls) and ``-c`` (initial prompt flag).

    See https://www.kimi.com/code/docs/en/kimi-cli/guides/getting-started.html
    for installation and usage details.
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
        """Launch a Kimi CLI process with the given prompt.

        Args:
            prompt: The task prompt for the agent (passed via ``-c``).
            workdir: Working directory for the Kimi process.
            model_config: Model and effort configuration (unused — Kimi
                selects the model via its own configuration).
            session_id: Unique session identifier used for log naming.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope label (unused by this adapter).
            budget_multiplier: Retry budget multiplier (unused by this adapter).
            system_addendum: Protocol-critical instructions (unused — Kimi
                does not expose a separate system-prompt channel).

        Returns:
            SpawnResult describing the spawned process.

        Raises:
            RuntimeError: The ``kimi`` binary is missing from PATH or
                cannot be executed due to permissions.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # --yolo auto-approves tool calls; -c supplies the initial prompt.
        cmd = ["kimi", "--yolo", "-c", prompt]

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

        env = build_filtered_env(["KIMI_API_KEY", "MOONSHOT_API_KEY"])
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
                msg = (
                    "kimi not found in PATH. "
                    "Install: uv tool install kimi-cli "
                    "(see https://www.kimi.com/code/docs/en/kimi-cli/guides/getting-started.html)"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing kimi: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Kimi"
