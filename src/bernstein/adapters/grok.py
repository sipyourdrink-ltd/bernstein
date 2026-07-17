"""Grok CLI adapter (xAI Grok Build).

`Grok Build <https://docs.x.ai/build/overview>`_ is xAI's terminal coding
agent, distributed as the ``grok`` binary. It exposes a first-class
headless surface for scripting and CI:

    grok -p "<prompt>" --output-format json --always-approve --no-auto-update

``-p`` sends one prompt and exits (no interactive TUI), ``--always-approve``
auto-approves tool executions so the run never blocks on a permission
prompt, ``--output-format json`` emits a machine-readable result, and
``--no-auto-update`` suppresses the background update check that would
otherwise perturb an unattended run.

Auth is by ``XAI_API_KEY`` (``GROK_API_KEY`` is accepted as an alias);
with the key set, Grok Build authenticates without opening a browser, so
headless use needs no interactive login.

Last verified against the xAI Grok Build CLI headless reference
(https://docs.x.ai/build/cli/headless-scripting and
https://docs.x.ai/build/cli/reference) on 2026-07-17.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env


class GrokAdapter(CLIAdapter):
    """Spawn and monitor xAI Grok Build CLI sessions.

    Grok Build supports a native headless mode driven entirely by flags,
    so Bernstein spawns each session non-interactively and reads the
    JSON result from the captured log.
    """

    #: xAI's Grok API endpoint, declared so the spawn preflight can honour
    #: a restrictive network policy.
    external_endpoints: tuple[tuple[str, int], ...] = (("api.x.ai", 443),)

    registry_name = "grok"

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
        """Launch a one-shot Grok Build headless session.

        Args:
            prompt: Task prompt passed via ``-p``.
            workdir: Working directory for the agent process.
            model_config: Model selection (Grok Build resolves the model
                from its own configuration, so it is carried for metadata
                only).
            session_id: Unique session identifier used for log and PID files.
            mcp_config: Optional MCP server definitions (unused by Grok).
            timeout_seconds: Seconds before the watchdog sends SIGTERM.
            task_scope: Task scope label (unused by Grok).
            budget_multiplier: Scope-budget multiplier (unused by Grok).
            system_addendum: Protocol-critical instructions (unused by Grok).
            multimodal_context: Optional multimodal attachments; refused
                because Grok's headless surface is text-only here.

        Returns:
            A :class:`SpawnResult` with the child PID and log path.

        Raises:
            RuntimeError: The ``grok`` binary is missing from PATH or the
                current user lacks permission to execute it.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "grok",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--always-approve",  # auto-approve tool executions (dangerous mode)
            "--no-auto-update",  # never phone home for updates mid-run
        ]

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

        env = build_filtered_env(["XAI_API_KEY", "GROK_API_KEY"])
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
                msg = "grok not found in PATH. Install: see https://docs.x.ai/build/overview"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing grok: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Grok"
