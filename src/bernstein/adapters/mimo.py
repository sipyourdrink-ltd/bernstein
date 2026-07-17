"""MiMo Code CLI adapter (Xiaomi).

`MiMo Code <https://github.com/XiaomiMiMo/MiMo-Code>`_ is Xiaomi's
open-source terminal coding agent (binary ``mimo``), built on the
OpenCode core with persistent memory and autonomous loops. Its
non-interactive surface follows the OpenCode ``run`` shape with a
skip-permissions flag:

    mimo run --model <id> --dangerously-skip-permissions "<prompt>"

``run`` executes a single task and exits;
``--dangerously-skip-permissions`` runs unattended without the
interactive permission gate.

MiMo Code keeps OpenCode's multi-provider support, so the adapter
forwards ``MIMO_API_KEY`` plus the common provider keys
(``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``).

Last verified against https://github.com/XiaomiMiMo/MiMo-Code on
2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class MimoAdapter(CLIAdapter):
    """Spawn and monitor Xiaomi MiMo Code CLI sessions.

    MiMo Code is OpenCode-shaped: ``mimo run`` for a one-shot task and
    ``--dangerously-skip-permissions`` to run unattended. Bernstein spawns
    each session fresh and reads its output from the captured log.
    """

    registry_name = "mimo"

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
        """Launch a one-shot MiMo Code task.

        Args:
            prompt: Task prompt passed positionally to ``mimo run``.
            workdir: Working directory; MiMo treats the spawn cwd as the
                project root.
            model_config: Model selection forwarded via ``--model`` when set.
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused; MiMo has
                its own MCP wiring).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by MiMo).
            budget_multiplier: Scope-budget multiplier (unused by MiMo).
            system_addendum: Protocol-critical instructions (unused by MiMo).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``mimo`` binary is missing from PATH or the
                current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["mimo", "run"]
        if model_config.model:
            cmd.extend(["--model", model_config.model])
        cmd.extend(["--dangerously-skip-permissions", prompt])

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

        env = build_filtered_env(["MIMO_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
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
                msg = "mimo not found in PATH. Install: see https://github.com/XiaomiMiMo/MiMo-Code"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing mimo: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "MiMo Code"
