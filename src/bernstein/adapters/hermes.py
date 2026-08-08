"""Hermes Agent (Nous Research) CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

#: The prompt travels attached to its flag, never as a separate argument.
#:
#: ``hermes`` has no top-level positional parameter - the only positional slot
#: is the subcommand, so a bare prompt is parsed as a command name and the
#: process exits 2 before a model is contacted. Using ``--oneshot=<prompt>``
#: rather than ``-z <prompt>`` additionally keeps a prompt that begins with a
#: dash from being read as a flag.
_ONESHOT_FLAG = "--oneshot"

#: Provider credentials forwarded into the spawned environment.
#:
#: Taken from the variables Hermes documents for its own providers - its
#: ``.env.example`` and the provider table in ``cli-config.yaml.example`` -
#: rather than inferred from the vendor's name. A credential missing from this
#: list is not an error the operator can see: the agent starts, fails to
#: authenticate, and the run looks like a model problem.
#:
#: ``NOUS_API_KEY`` is the documented key for the ``nous-api`` provider and is
#: forwarded for that reason. ``HERMES_API_KEY`` was previously forwarded and
#: is not: it appears in neither file, so nothing reads it.
#:
#: ``GITHUB_TOKEN`` is deliberately absent although the ``copilot`` provider
#: reads it. Forwarding a repository-scoped token to an agent whose approvals
#: are auto-bypassed is a separate decision from forwarding a model
#: credential, and not one to make implicitly here.
#:
#: ``HOME`` arrives via the base allowlist, which is what lets the operator's
#: own config resolve and carry whatever was configured interactively.
_PROVIDER_ENV_VARS = (
    "NOUS_API_KEY",
    "OPENROUTER_API_KEY",
    "FIREWORKS_API_KEY",
    "NOVITA_API_KEY",
    "DEEPINFRA_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "KIMI_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "HF_TOKEN",
    "NVIDIA_API_KEY",
    "ARCEEAI_API_KEY",
    "XIAOMI_API_KEY",
    "OLLAMA_API_KEY",
    "KILOCODE_API_KEY",
    "AI_GATEWAY_API_KEY",
    "LM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HERMES_HOME",
)


class HermesAdapter(CLIAdapter):
    """Spawn and monitor Hermes Agent CLI sessions.

    Hermes Agent is Nous Research's agent CLI. It is driven here through its
    one-shot mode, which runs a single prompt with approvals auto-bypassed,
    edits files in the process working directory, prints the final response
    and exits - the shape this orchestrator needs. The interactive TUI is
    never entered.

    Two properties of that mode decide how the process is spawned:

    * an empty prompt is not treated as an error. Dispatch tests the prompt
      for truthiness, so a blank one falls through to the interactive path,
      which then waits on stdin for the whole timeout. Both halves are
      guarded here - the prompt is rejected before spawning, and stdin is
      closed so an unforeseen interactive path fails fast instead of hanging.
    * one-shot suppresses tool previews and progress, so the log this returns
      holds the final response and little else. Progress is not pollable from
      it; the diff in the worktree is the outcome to inspect.
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
        """Spawn a Hermes Agent session.

        Args:
            prompt: The task prompt for the agent.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope label (unused).
            budget_multiplier: Budget multiplier (unused).
            system_addendum: Protocol-critical instructions (unused).

        Returns:
            A :class:`SpawnResult` describing the spawned process.

        Raises:
            ValueError: If *prompt* is empty or only whitespace.
            RuntimeError: If the ``hermes`` binary cannot be found or
                executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        if not prompt.strip():
            msg = (
                "hermes requires a non-empty prompt: one-shot mode dispatches on "
                "the prompt being truthy, so a blank one starts an interactive "
                "session that waits for input until the task times out"
            )
            raise ValueError(msg)

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["hermes", f"{_ONESHOT_FLAG}={prompt}"]

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

        env = build_filtered_env(_PROVIDER_ENV_VARS)
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                msg = (
                    "hermes not found in PATH. Install: curl -fsSL "
                    "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash "
                    "(see https://hermes-agent.nousresearch.com/docs/)"
                )
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing hermes: {exc}") from exc

        # Pass proc through so downstream poll/wait works; see cursor.py.
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Hermes Agent"
