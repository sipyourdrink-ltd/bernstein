"""Auggie (Augment Code) CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class AuggieAdapter(CLIAdapter):
    """Spawn and monitor Auggie (Augment Code) CLI sessions.

    Auggie is Augment Code's CLI coding agent.  It is invoked with a
    positional prompt and the ``--allow-indexing`` flag, which is kept
    always-on so Auggie does not block the session waiting for an
    interactive indexing confirmation.

    See https://docs.augmentcode.com/cli/overview for details.
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
        """Launch an Auggie process with the given prompt.

        Args:
            prompt: The task prompt for the agent (passed positionally).
            workdir: Working directory for the Auggie process.
            model_config: Model and effort configuration (unused — Auggie
                selects the model via its own configuration).
            session_id: Unique session identifier used for log naming.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope label (unused by this adapter).
            budget_multiplier: Retry budget multiplier (unused by this adapter).
            system_addendum: Protocol-critical instructions (unused — Auggie
                accepts only a single positional prompt).

        Returns:
            SpawnResult describing the spawned process.

        Raises:
            RuntimeError: The ``auggie`` binary is missing from PATH or
                cannot be executed due to permissions.
        """
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # --allow-indexing is always-on; otherwise Auggie prompts each session.
        cmd = ["auggie", "--allow-indexing", prompt]

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

        env = build_filtered_env(["AUGMENT_API_KEY", "AUGMENT_TOKEN"])
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
                    "auggie not found in PATH. "
                    "Install: npm install -g @augmentcode/auggie "
                    "(see https://docs.augmentcode.com/cli/overview)"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing auggie: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Auggie"
