"""Aider CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

# Map Bernstein short model names to aider model identifiers.
# Aider accepts provider-prefixed names (e.g. "openai/gpt-4o", "anthropic/claude-3-5-sonnet").
# Short names are mapped to the most common aider-compatible IDs; unknown names pass through.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic/claude-opus-4-6",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-haiku-4-5-20251001",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4.1": "openai/gpt-4.1",
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
        timeout_seconds: int = 1800,
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
        ]

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
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
                    stdin=subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("aider not found in PATH. Install it with: pip install aider-chat") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing aider: {exc}") from exc

        timer = self._start_watchdog(proc, timeout_seconds=timeout_seconds, workdir=workdir, session_id=session_id)
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc, timer=timer)

    def name(self) -> str:
        return "Aider"
