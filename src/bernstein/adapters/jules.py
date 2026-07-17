"""Jules Tools CLI adapter (Google).

`Jules Tools <https://jules.google/docs/cli/reference/>`_ is the
command-line companion for Google's asynchronous coding agent Jules
(binary ``jules``). The ``remote new`` subcommand creates a session that
runs in Jules' cloud sandbox against a connected repository and returns
without an interactive TUI, which is the scriptable entry point:

    jules remote new --repo . --session "<prompt>"

``--repo .`` targets the repository in the spawn cwd (the Bernstein
worktree) and ``--session`` carries the task prompt. Jules executes
remotely in its own isolated VM, so there is no local permission gate.

Auth is via ``jules login`` or ``JULES_API_KEY``, which the adapter
forwards. Jules selects the model server-side, so no ``--model`` flag is
passed.

Note: unlike the local-subprocess adapters, Jules dispatches work to
Google's async runner rather than editing the local worktree in place;
this adapter drives that dispatch and journals its output.

Last verified against https://jules.google/docs/cli/reference/ and
https://developers.googleblog.com/en/meet-jules-tools-a-command-line-companion-for-googles-async-coding-agent/
on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class JulesAdapter(CLIAdapter):
    """Spawn and monitor Google Jules Tools (``jules``) sessions.

    ``jules remote new`` dispatches a task to Jules' async cloud runner
    against the repo in the spawn cwd; Bernstein reads the session output
    from the captured log.
    """

    #: Jules' API endpoint, declared so the spawn preflight honours a
    #: restrictive network policy.
    external_endpoints: tuple[tuple[str, int], ...] = (("jules.googleapis.com", 443),)

    registry_name = "jules"

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
        """Launch a one-shot Jules remote session.

        Args:
            prompt: Task prompt passed via ``--session``.
            workdir: Working directory; ``--repo .`` targets the repo here.
            model_config: Model selection (Jules resolves the model
                server-side, so it is carried for metadata only).
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused by Jules).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by Jules).
            budget_multiplier: Scope-budget multiplier (unused by Jules).
            system_addendum: Protocol-critical instructions (unused by Jules).
            multimodal_context: Optional multimodal attachments; refused.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``jules`` binary is missing from PATH or the
                current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["jules", "remote", "new", "--repo", ".", "--session", prompt]

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

        env = build_filtered_env(["JULES_API_KEY"])
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
                msg = "jules not found in PATH. Install: npm i -g @google/jules (see https://jules.google/docs/cli/)"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing jules: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Jules"
