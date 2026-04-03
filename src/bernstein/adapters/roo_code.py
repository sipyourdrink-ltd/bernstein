"""Roo Code CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

# Map Bernstein short model names to Roo Code model identifiers.
# Roo Code uses provider-prefixed or full model IDs accepted by the underlying LLM API.
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "gpt-4o": "gpt-4o",
    "gpt-4.1": "gpt-4.1",
}


class RooCodeAdapter(CLIAdapter):
    """Spawn and monitor Roo Code CLI sessions.

    Roo Code is a VS Code AI coding extension (fork of Cline) with a headless
    CLI mode. It accepts ``--task`` for the prompt and ``--model`` for the
    model identifier, and outputs JSON-structured results via ``--output-format json``.
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
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "roo-code",
            "--model",
            model_id,
            "--task",
            prompt,
            "--output-format",
            "json",
        ]

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

        env = build_filtered_env(["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
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
                raise RuntimeError("roo-code not found in PATH. Install it with: npm install -g @roo-code/cli") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing roo-code: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Roo Code"
