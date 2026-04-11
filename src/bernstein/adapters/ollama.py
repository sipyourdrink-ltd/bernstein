"""Ollama local LLM adapter — run coding agents without cloud API keys.

Uses Aider as the coding frontend with Ollama as the local LLM backend.
This enables full code editing capabilities in air-gapped, privacy-sensitive,
or cost-zero environments.

Prerequisites:
    - Ollama: https://ollama.ai  (brew install ollama)
    - Aider: pip install aider-chat
    - A pulled model: ollama pull qwen2.5-coder:7b
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

# Default Ollama API endpoint
OLLAMA_BASE_URL = "http://localhost:11434"

# Maps Bernstein abstract model names to Ollama model IDs.
# Users can also pass native ollama model IDs directly (e.g. "qwen2.5-coder:32b").
_MODEL_MAP: dict[str, str] = {
    # Bernstein tiers → sensible local defaults
    "opus": "deepseek-r1:70b",
    "sonnet": "qwen2.5-coder:32b",
    "haiku": "qwen2.5-coder:7b",
    # Common coding-focused models
    "codellama": "codellama",
    "deepseek-coder": "deepseek-coder-v2",
    "deepseek-r1": "deepseek-r1",
    "qwen2.5-coder": "qwen2.5-coder",
    "qwen3-coder": "qwen3-coder",
    "llama3.1": "llama3.1",
    "llama3.2": "llama3.2",
    "gemma3": "gemma3",
    "phi4": "phi4",
    "mistral": "mistral",
    "starcoder2": "starcoder2",
}


class OllamaAdapter(CLIAdapter):
    """Spawn coding agent sessions using local Ollama LLMs.

    Uses Aider as the coding agent with Ollama as the LLM provider, giving
    full file-editing capabilities without any cloud API keys.

    Model selection:
        - Pass a Bernstein tier name (opus/sonnet/haiku) → maps to a capable local model
        - Pass a native Ollama model ID (e.g. "qwen2.5-coder:7b") → used as-is
        - Override OLLAMA_BASE_URL env var to point at a remote Ollama instance

    Args:
        base_url: Ollama API base URL. Defaults to http://localhost:11434.
    """

    def __init__(self, *, base_url: str = OLLAMA_BASE_URL) -> None:
        super().__init__()
        self._base_url = base_url

    def _resolve_model(self, model_name: str) -> str:
        """Map Bernstein model name to Ollama model ID."""
        return _MODEL_MAP.get(model_name, model_name)

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
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        ollama_model = self._resolve_model(model_config.model)

        # aider supports ollama via litellm: --model ollama/<model>
        # Smaller repo map keeps local model context usage manageable.
        cmd = [
            "aider",
            "--model",
            f"ollama/{ollama_model}",
            "--message",
            prompt,
            "--yes",
            "--auto-commits",
            "--map-tokens",
            "1024",
            "--no-auto-lint",
        ]

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=f"ollama/{ollama_model}",
        )

        # Pass OLLAMA_API_BASE so aider/litellm finds the Ollama server.
        # Strip cloud API keys so the agent doesn't accidentally use them.
        env = build_filtered_env(["OLLAMA_API_BASE", "OLLAMA_HOST"])
        env["OLLAMA_API_BASE"] = self._base_url
        env["OLLAMA_HOST"] = self._base_url

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
                raise RuntimeError(
                    "aider not found in PATH. Install with: pip install aider-chat\n"
                    "Also ensure Ollama is running: ollama serve"
                ) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing aider: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        return "Ollama (local)"
