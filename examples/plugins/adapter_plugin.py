"""Example plugin: custom CLI adapter.

Demonstrates how to add a third-party CLI agent as a Bernstein adapter.
Install your package and declare it under the ``bernstein.adapters``
entry-point group — Bernstein auto-discovers it at runtime.

Example ``pyproject.toml`` entry:

    [project.entry-points."bernstein.adapters"]
    myagent = "mypackage.adapter:MyAgentAdapter"

Then ``bernstein run --cli myagent plan.yaml`` routes all tasks to your agent.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class ExampleAgentAdapter(CLIAdapter):
    """Adapter for a hypothetical "example-agent" CLI tool.

    Replace the spawn logic with whatever your agent's CLI expects.
    The only contract: return a SpawnResult with the process PID and log path.
    """

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict | None = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        """Launch example-agent with the given prompt and work directory."""
        log_path = workdir / f".bernstein-{session_id}.log"
        cmd = ["example-agent", "--prompt", prompt, "--workdir", str(workdir)]

        with log_path.open("w") as log_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=log_fh,
                stderr=log_fh,
            )

        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
