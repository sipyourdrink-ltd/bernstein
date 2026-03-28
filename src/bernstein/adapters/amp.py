"""Amp CLI adapter."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

# Map Bernstein short model names to Amp model identifiers.
# Amp accepts provider-prefixed names (e.g. "anthropic:claude-sonnet-4-6", "openai:gpt-5.4").
# Short names are mapped to the most common Amp-compatible IDs; unknown names pass through.
_MODEL_MAP: dict[str, str] = {
    "opus": "anthropic:claude-opus-4-6",
    "sonnet": "anthropic:claude-sonnet-4-6",
    "haiku": "anthropic:claude-haiku-4-5-20251001",
    "gpt-5.4": "openai:gpt-5.4",
    "gpt-5.4-mini": "openai:gpt-5.4-mini",
    "o3": "openai:o3",
    "o4-mini": "openai:o4-mini",
}


class AmpAdapter(CLIAdapter):
    """Spawn and monitor Amp CLI sessions.

    Amp (by Sourcegraph) is a CLI coding agent that supports headless/non-interactive mode.
    It runs with --headless and accepts prompts via stdin or --prompt.
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        cmd = [
            "amp",
            "--model",
            model_id,
            "--prompt",
            prompt,
            "--headless",  # Run in non-interactive mode
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

        # Amp supports Anthropic and OpenAI models; SRC vars are for Sourcegraph auth
        env = build_filtered_env(["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SRC_ENDPOINT", "SRC_ACCESS_TOKEN"])
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
                msg = "amp not found in PATH. Install: brew install amp or see https://ampcode.com"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing amp: {exc}") from exc

        return SpawnResult(pid=proc.pid, log_path=log_path)

    def name(self) -> str:
        return "Amp"
