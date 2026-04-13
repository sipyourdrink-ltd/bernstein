"""<YourCLI> adapter for Bernstein.

Replace every occurrence of:
  - ``YourCLI``        → the CLI tool name (e.g. ``Cursor``, ``OpenCode``)
  - ``your-cli``       → the adapter key used in registry + bernstein.yaml
  - ``your-cli-bin``   → the actual binary name on PATH (e.g. ``cursor``, ``opencode``)

Then delete these comments and open a PR!
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult, build_worker_cmd

# ---------------------------------------------------------------------------
# Model mapping
# ---------------------------------------------------------------------------
# Map Bernstein short model names ("opus", "sonnet", "haiku") to the model
# identifiers accepted by your CLI tool.
#
# Examples:
#   Aider uses provider-prefixed names: "anthropic/claude-opus-4-6"
#   Claude Code uses full model IDs:    "claude-opus-4-6"
#   Some tools just take "opus" as-is — in that case, keep this dict empty
#   and pass ``model_config.model`` directly.
_MODEL_MAP: dict[str, str] = {
    "opus": "TODO: map to your CLI's opus model ID",
    "sonnet": "TODO: map to your CLI's sonnet model ID",
    "haiku": "TODO: map to your CLI's haiku model ID",
    # Add other models your CLI supports, e.g.:
    # "gpt-5.4": "openai/gpt-5.4",
    # "gpt-5.4-mini": "openai/gpt-5.4-mini",
}


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------


class YourCLIAdapter(CLIAdapter):
    """Spawn and monitor <YourCLI> sessions.

    <One sentence describing how this CLI tool operates in non-interactive mode,
    e.g. "Cursor runs in headless mode via --prompt, auto-accepts changes.">
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        """Launch a <YourCLI> agent process.

        Args:
            prompt: The task description passed to the agent.
            workdir: Working directory (project root).
            model_config: Model and effort settings chosen by the orchestrator.
            session_id: Unique identifier for this agent session.
            mcp_config: Optional MCP server configuration (ignored if the CLI
                doesn't support MCP).

        Returns:
            SpawnResult with the process PID and log file path.

        Raises:
            RuntimeError: If the CLI binary is not found or lacks execute permission.
        """
        # 1. Set up the log file path — Bernstein reads this for `bernstein logs`
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # 2. Resolve the model ID
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)

        # 3. Build the CLI command
        # Replace with the actual flags for your CLI tool
        cmd = [
            "your-cli-bin",  # binary name on PATH
            "--model",
            model_id,  # model flag (if supported)
            "--message",
            prompt,  # how to pass the prompt
            "--yes",  # auto-accept all prompts (non-interactive)
            # Add any other required flags here
        ]

        # Optional: map Bernstein effort levels to CLI-specific flags
        # effort = getattr(model_config, "effort", "high")
        # if effort == "max":
        #     cmd += ["--thinking", "extended"]

        # Optional: pass MCP config if your CLI supports it
        # if mcp_config:
        #     import json
        #     cmd += ["--mcp-config", json.dumps(mcp_config)]

        # 4. Wrap with bernstein-worker for `bernstein ps` visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],  # e.g. "qa" from "qa-abc12345"
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        # 5. Launch the process, redirecting stdout+stderr to the log file
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,  # merge stderr into the same log
                    start_new_session=True,  # detach from terminal signals
                )
            except FileNotFoundError as exc:
                raise RuntimeError("your-cli-bin not found in PATH. Install it: <link to installation docs>") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing your-cli-bin: {exc}") from exc

        return SpawnResult(pid=proc.pid, log_path=log_path)

    def name(self) -> str:
        """Human-readable name shown in `bernstein ps` and logs."""
        return "YourCLI"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
# Add this adapter to src/bernstein/adapters/registry.py:
#
#   from bernstein.adapters.yourcli import YourCLIAdapter
#   _ADAPTERS["your-cli"] = YourCLIAdapter
#
# Then users can select it with:
#   bernstein run --adapter your-cli
# or in bernstein.yaml:
#   adapter: your-cli
