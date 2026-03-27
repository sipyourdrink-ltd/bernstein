"""Claude Code CLI adapter."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit

# Map short model names to Claude Code CLI model IDs
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class ClaudeCodeAdapter(CLIAdapter):
    """Spawn and monitor Claude Code CLI sessions."""

    # Track Popen objects for reliable is_alive() via poll()
    _procs: dict[int, subprocess.Popen[bytes]] = {}

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        # Map effort to max-turns: more effort = more turns allowed
        effort = getattr(model_config, "effort", "high")
        max_turns = {"max": 100, "high": 50, "normal": 25}.get(effort, 50)

        cmd = [
            "claude",
            "--model", model_id,
            "--dangerously-skip-permissions",
            "--max-turns", str(max_turns),
            "-p", prompt,
        ]

        log_file = log_path.open("w")  # noqa: SIM115
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()

        self._procs[proc.pid] = proc
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def is_alive(self, pid: int) -> bool:
        # Use poll() to detect zombies — os.kill(pid, 0) can't
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Fallback for processes we didn't spawn
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
        # Clean up proc reference
        self._procs.pop(pid, None)

    def name(self) -> str:
        return "Claude Code"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Claude API tier based on environment and API key type.

        Checks ANTHROPIC_API_KEY prefix to determine tier:
        - sk-ant-api03... = Pro tier
        - sk-ant-api01... = Plus tier
        - Other = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            return None

        # Determine tier from API key prefix
        if api_key.startswith("sk-ant-api03"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=50000,
            )
        elif api_key.startswith("sk-ant-api01"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
