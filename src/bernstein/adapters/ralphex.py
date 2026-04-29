"""Ralphex (umputun/ralphex) CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class RalphexAdapter(CLIAdapter):
    """Spawn umputun/ralphex as a single Bernstein-managed agent.

    Ralphex is itself a plan-walking orchestrator that runs Claude Code in
    repeated short-lived sessions, executing a markdown task plan and
    cycling through its own multi-phase code-review pipeline. Bernstein
    treats one ralphex invocation as a single leaf agent: we hand it a
    plan and observe only the final exit code and combined log output.
    Bernstein does not (and cannot) see the individual Claude sessions
    that ralphex spawns inside its own loop -- this is leaf-node
    delegation, not deep meta-orchestration.

    The CLI is non-interactive when given a positional plan-file
    argument, so we materialize the prompt as a markdown plan in
    ``.sdd/runtime/<session>-plan.md`` and invoke
    ``ralphex --no-color <plan-file>``. Ralphex does not expose a
    single-shot ``--goal``/``--prompt`` flag; ``--plan "<text>"`` exists
    but uses an interactive fzf/picker dialogue and is unsuitable for
    headless spawn. Auth flows entirely through Claude Code's normal
    credential discovery (``~/.claude/`` or ``ANTHROPIC_API_KEY``); ralphex
    itself takes no API key.
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
        """Launch a ralphex session over a synthesized plan file.

        Args:
            prompt: Task description; written verbatim into a generated
                markdown plan file that ralphex will consume.
            workdir: Working directory for the agent process; must be a
                git repository (ralphex requires this).
            model_config: Model and effort configuration; mapped to
                ralphex's ``--task-model model[:effort]`` flag.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused;
                ralphex configures Claude Code internally).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions
                (folded into the generated plan body).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``ralphex`` binary is missing from PATH
                or cannot be executed.
        """
        runtime_dir = workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = runtime_dir / f"{session_id}.log"

        plan_path = runtime_dir / f"{session_id}-plan.md"
        plan_body = prompt if not system_addendum else f"{system_addendum}\n\n{prompt}"
        plan_path.write_text(
            f"# Plan: {session_id}\n\n"
            "## Validation Commands\n\n"
            "### Task 1: Execute requested work\n\n"
            f"- [ ] {plan_body}\n",
            encoding="utf-8",
        )

        cmd = ["ralphex", "--no-color"]
        if model_config.model:
            spec = model_config.model
            if model_config.effort:
                spec = f"{spec}:{model_config.effort}"
            cmd.extend(["--task-model", spec])
        cmd.append(str(plan_path))

        pid_dir = runtime_dir / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(["ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "RALPHEX_CONFIG_DIR"])
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
                msg = "ralphex not found in PATH. Install: go install github.com/umputun/ralphex/cmd/ralphex@latest"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing ralphex: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Ralphex"
