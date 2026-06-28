"""GitHub Copilot CLI adapter.

Last verified against GitHub Copilot CLI 1.0.65 on 2026-06-28.
Install: ``npm install -g @github/copilot``.

The harness path is the CLI's non-interactive ("print") mode:
``copilot -p "<prompt>" -s --allow-all-tools --no-ask-user [--model <model>]``.
``-p/--prompt`` runs a single prompt and exits when done, ``-s/--silent`` trims
stdout to the agent's final response (clean for the session log), and
``--allow-all-tools`` together with ``--no-ask-user`` make the run autonomous so
it never blocks on a permission prompt or a clarifying question.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Copilot accepts ``auto`` to let its own router pick the best available model.
# The batch / heuristic selectors emit Claude cascade tier names (opus / sonnet /
# haiku) that are not valid Copilot model ids, so map any that reach the adapter
# to ``auto`` rather than passing ``--model sonnet`` (which Copilot rejects). The
# real selection fix lives in the spawner; this is a last-resort safety net.
_DEFAULT_COPILOT_MODEL = "auto"
_CLAUDE_TIER_MODELS = frozenset({"opus", "sonnet", "haiku"})


def _copilot_model(model: str) -> str:
    """Map a Claude cascade tier name to Copilot's ``auto``; pass others through."""
    if model in _CLAUDE_TIER_MODELS:
        logger.warning(
            "CopilotAdapter: model %r is a Claude tier name Copilot cannot run; using %r "
            "instead. Set role_model_policy.<role>.model or default_model to a Copilot "
            "model (e.g. gpt-5.4 or claude-sonnet-4.5) to choose explicitly.",
            model,
            _DEFAULT_COPILOT_MODEL,
        )
        return _DEFAULT_COPILOT_MODEL
    return model


class CopilotAdapter(CLIAdapter):
    """Spawn and monitor GitHub Copilot CLI sessions.

    The CLI runs in non-interactive print mode:
    ``copilot -p <prompt> -s --allow-all-tools --no-ask-user --model <model>``.
    ``-p/--prompt`` supplies the prompt and exits when done, ``-s/--silent``
    trims stdout to the agent's final response, and ``--allow-all-tools`` plus
    ``--no-ask-user`` make the run autonomous so it never blocks on a
    permission prompt or a clarifying question.
    """

    registry_name = "copilot"
    # Default model when no operator-pinned model reaches this adapter. Read by
    # the spawner to substitute Claude tier names for non-Claude adapters.
    default_model = _DEFAULT_COPILOT_MODEL
    # GitHub Copilot surfaces 429s when the underlying account hits the
    # Copilot quota; we record under the Copilot label so the panel
    # attributes pressure to the right surface.
    rate_limit_provider = "github_copilot"

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
        """Launch a GitHub Copilot CLI session in non-interactive print mode.

        Args:
            prompt: The prompt supplied via ``-p``; Copilot exits when done.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration. ``model`` is passed
                through via ``--model``; Claude tier names are mapped to
                Copilot's ``auto`` router.
            session_id: Unique session identifier. A deterministic id derived
                from it is pinned via ``--session-id`` for replay isolation.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by Copilot).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``copilot`` binary is missing from PATH
                or cannot be executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model = _copilot_model(model_config.model)
        cmd = [
            "copilot",
            "-p",
            prompt,
            "-s",
            "--allow-all-tools",
            "--no-ask-user",
            "--model",
            model,
        ]
        # Pin a deterministic session id so a replay of this run reaches the
        # same conversation slot and parallel copilot sessions in the same
        # worktree do not collide. Copilot's ``--session-id`` sets the UUID for
        # a new session (and resumes an existing one by id), which is the
        # harness-preferred control surface. When the copilot contract declares
        # no session-id flag this is an empty list and the argv is unchanged.
        cmd.extend(self.session_id_args(session_id))

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model,
        )

        env = build_filtered_env(["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"])
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
                msg = "copilot not found in PATH. Install: npm install -g @github/copilot"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing copilot: {exc}") from exc

        # Detect spawn-time 429s; updates the meter through the base hook.
        self._probe_fast_exit(proc, log_path, provider_name="copilot")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "GitHub Copilot"
