"""Aider CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

# Map Bernstein short model names to aider model identifiers.
# Aider accepts provider-prefixed names (e.g. "openai/gpt-5.4", "anthropic/claude-3-5-sonnet").
# Short names are mapped to the most common aider-compatible IDs; unknown names pass through.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic/claude-opus-4-6",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
}


class AiderAdapter(CLIAdapter):
    """Spawn and monitor Aider CLI sessions.

    Aider runs in non-interactive mode via ``--message``, auto-confirms prompts
    with ``--yes``, and commits changes automatically. In a Bernstein worktree
    those commits stay isolated until the orchestrator merges the branch.
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
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "aider",
            "--model",
            model_id,
            "--message",
            prompt,
            "--yes",  # auto-confirm all prompts
            "--auto-commits",  # explicit: create a commit per change for clean worktree history
            "--map-tokens",
            "2048",  # larger repo map for better codebase navigation
            "--no-auto-lint",  # lint is orchestrator's job, not each agent's
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        # Aider supports both Anthropic and OpenAI models; include both API keys
        env = build_filtered_env(["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"])
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
                raise RuntimeError("aider not found in PATH. Install it with: pip install aider-chat") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing aider: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Aider"
