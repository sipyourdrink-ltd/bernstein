"""Mistral Vibe CLI adapter."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.plugin_sdk import AdapterCapability, AdapterPluginInfo

logger = logging.getLogger(__name__)


class MistralAdapter(CLIAdapter):
    """Spawn and monitor Mistral Vibe CLI sessions.

    Mistral Vibe is a CLI coding agent by Mistral.  It runs with
    ``--auto-approve`` for non-interactive execution and accepts the task
    prompt via the ``--prompt`` flag.
    """

    def plugin_info(self) -> AdapterPluginInfo:
        """Declare the sampling surface MistralAdapter genuinely wires.

        ``vibe`` accepts a ``--temperature`` flag. It has no documented
        ``--top-p``/``--top-k``/``--max-tokens`` flag, so only the narrow
        temperature capability is declared -
        :func:`bernstein.adapters.plugin_sdk.ensure_sampling_params_supported`
        refuses a spawn requesting the others rather than silently
        dropping them.
        """
        return AdapterPluginInfo(
            name="mistral",
            version="0.1.0",
            author="bernstein",
            description="Mistral Vibe CLI adapter",
            capabilities=(AdapterCapability.SUPPORTS_TEMPERATURE,),
        )

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
        """Spawn a Mistral Vibe session.

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
            RuntimeError: If the ``vibe`` binary cannot be found or executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "vibe",
            "--auto-approve",
            "--prompt",
            prompt,
        ]

        if mcp_config:
            temperature = mcp_config.get("temperature")
            if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
                logger.debug("mistral adapter: wiring --temperature=%s onto argv", temperature)
                cmd.extend(["--temperature", str(float(temperature))])
            for dropped_key in ("top_p", "top_k", "max_tokens"):
                if mcp_config.get(dropped_key) is not None:
                    logger.warning(
                        "mistral adapter: %s=%r requested but not wired (vibe CLI has no "
                        "matching flag) - ensure_sampling_params_supported should have refused "
                        "this spawn before reaching here",
                        dropped_key,
                        mcp_config.get(dropped_key),
                    )

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

        env = build_filtered_env(["MISTRAL_API_KEY"])
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
                msg = "vibe not found in PATH. Install: curl -LsSf https://mistral.ai/vibe/install.sh | bash"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing vibe: {exc}") from exc

        # ``proc`` MUST be threaded into SpawnResult - see cursor.py header
        # comment for the regression context (downstream needs poll/wait).
        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Mistral Vibe"
