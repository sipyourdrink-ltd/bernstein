"""Example plugin: custom quality gate.

Demonstrates how to run an additional automated check after every task
completes.  The check here is a simple security scan (`bandit -r .`) but
you can replace the command with anything: custom linters, contract tests,
licence checkers, etc.

The gate result is written to `.sdd/metrics/custom_gates.jsonl` and also
logged at WARNING level when the check fails.

Usage — add to bernstein.yaml:

    plugins:
      - examples.plugins.quality_gate_plugin:SecurityScanGate

Set the command via environment variable to override the default:

    export BERNSTEIN_SECURITY_CMD="semgrep --config=auto ."
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)

DEFAULT_COMMAND = "bandit -r . -ll -q"


class SecurityScanGate:
    """Runs a security scan after every task completes.

    The scan command defaults to ``bandit -r . -ll -q`` (medium+ severity).
    Override via the ``BERNSTEIN_SECURITY_CMD`` environment variable.

    Results are written to ``.sdd/metrics/custom_gates.jsonl``.
    A failed scan is logged as a warning but does NOT block the orchestrator
    — you can promote it to a hard block by raising an exception here, though
    note that exceptions from plugins are caught and discarded.  For hard
    blocking, wire your gate into the quality_gates config instead.
    """

    def __init__(
        self,
        command: str | None = None,
        workdir: Path | str | None = None,
        timeout_s: int = 60,
    ) -> None:
        self._command = command or os.getenv("BERNSTEIN_SECURITY_CMD", DEFAULT_COMMAND)
        self._workdir = Path(workdir) if workdir else Path.cwd()
        self._timeout_s = timeout_s

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Run the security scan after every task completes."""
        passed, output = self._run_scan()
        self._record(task_id, passed, output)
        if not passed:
            log.warning(
                "SecurityScanGate: scan failed after task %s (%s):\n%s",
                task_id,
                role,
                output[:500],
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_scan(self) -> tuple[bool, str]:
        """Execute the scan command and return (passed, output)."""
        try:
            proc = subprocess.run(
                self._command,
                shell=True,
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
            out = (proc.stdout + proc.stderr).strip()
            if len(out) > 2000:
                out = out[:2000] + "\n... (truncated)"
            return proc.returncode == 0, out or "(no output)"
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {self._timeout_s}s"
        except OSError as exc:
            return False, f"Command error: {exc}"

    def _record(self, task_id: str, passed: bool, output: str) -> None:
        """Append result to .sdd/metrics/custom_gates.jsonl."""
        metrics_dir = self._workdir / ".sdd" / "metrics"
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "gate": "security_scan",
            "task_id": task_id,
            "command": self._command,
            "passed": passed,
            "output": output[:500],
        }
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            with (metrics_dir / "custom_gates.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("SecurityScanGate: could not write result: %s", exc)
