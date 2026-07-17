"""Warp Agent CLI adapter (``oz``).

`Warp <https://docs.warp.dev/reference/cli/>`_ ships its agent CLI as the
``oz`` binary (the older ``warp-cli`` is deprecated). The ``agent run``
subcommand executes a task non-interactively, printing tool calls and
responses as it works and exiting when done:

    oz agent run --prompt "<prompt>" --model <id>

The agent runs in the current working directory by default, so Bernstein
lets the child inherit the worktree cwd rather than passing ``-C``.

Permission behaviour is governed by the selected Warp *agent profile*
(``--profile``), not by a per-run CLI flag: to run fully unattended the
operator configures an allow-all profile in Warp and selects it as the
account default. Because there is no single skip-permissions flag, the
adapter declares its dangerous-mode strategy as unsupported (see
``STRATEGY_MATRIX``) and relies on the profile for autonomy.

Auth is via ``oz login`` (device auth cached under ``$HOME``), so no
provider key is forwarded on argv.

Last verified against https://docs.warp.dev/reference/cli/ and
https://www.warp.dev/agents on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class WarpAdapter(CLIAdapter):
    """Spawn and monitor Warp Agent CLI (``oz``) sessions.

    ``oz agent run`` is a one-shot non-interactive agent run; Bernstein
    spawns it in the worktree cwd and reads its output from the captured
    log. Unattended permission handling is delegated to a pre-configured
    Warp agent profile.
    """

    registry_name = "warp"

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
        """Launch a one-shot Warp ``oz agent run`` session.

        Args:
            prompt: Task prompt passed via ``--prompt``.
            workdir: Working directory; ``oz agent run`` uses the spawn
                cwd as its working directory.
            model_config: Model selection forwarded via ``--model`` when set.
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused; Warp wires
                MCP through ``--mcp`` / profiles).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by Warp).
            budget_multiplier: Scope-budget multiplier (unused by Warp).
            system_addendum: Protocol-critical instructions (unused by Warp).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``oz`` binary is missing from PATH or the
                current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["oz", "agent", "run", "--prompt", prompt]
        if model_config.model:
            cmd.extend(["--model", model_config.model])

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

        # Warp authenticates via `oz login` (cached under $HOME), so no
        # provider secret is threaded onto argv or the child env allow-list.
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
                msg = "oz (Warp Agent CLI) not found in PATH. Install: see https://docs.warp.dev/reference/cli/"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing oz: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Warp"
