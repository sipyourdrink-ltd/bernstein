"""Example plugin: custom quality gate.

Demonstrates how to run an additional automated check on every task's change
and block promotion when it fails.  The check here is a simple security scan
(`bandit -r . -ll -q`) but you can replace the command with anything: custom
linters, contract tests, licence checkers, etc.

`SecurityScanGate` is a `GatePlugin`, the same extension point the built-in
gates use.  A failed scan returns a ``fail`` result with ``blocked=True``, so
the task is not promoted.  The result is also appended to
`.sdd/metrics/custom_gates.jsonl`.

Usage - copy this file to `.bernstein/gates/security_scan_gate.py` (or register
the class in the `bernstein.gates` entry-point group), then add the gate to the
pipeline in bernstein.yaml:

    quality_gates:
      pipeline:
        - { name: "lint",              required: true, condition: "always" }
        - { name: "security_scan_cmd", required: true, condition: "python_changed" }

Set the command via environment variable to override the default:

    export BERNSTEIN_SECURITY_CMD="semgrep --config=auto ."
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from bernstein.core.quality.gate_plugins import GatePlugin
from bernstein.core.quality.gate_runner import GateResult

log = logging.getLogger(__name__)

DEFAULT_COMMAND = "bandit -r . -ll -q"


class SecurityScanGate(GatePlugin):
    """Runs a security scan on every task and blocks when it fails.

    The scan command defaults to ``bandit -r . -ll -q`` (medium+ severity).
    Override via the ``BERNSTEIN_SECURITY_CMD`` environment variable.

    A non-zero exit, a timeout or a command that cannot start returns a
    blocking result.  Results are appended to ``.sdd/metrics/custom_gates.jsonl``.
    """

    def __init__(self, command: str | None = None, timeout_s: int = 60) -> None:
        self._command: str = command or os.environ.get("BERNSTEIN_SECURITY_CMD", DEFAULT_COMMAND)
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        """Unique gate name; use it as the pipeline step name."""
        return "security_scan_cmd"

    def run(
        self,
        changed_files: list[str],
        run_dir: Path,
        task_title: str,
        task_description: str,
    ) -> GateResult:
        """Run the scan in ``run_dir`` and return a blocking result on failure."""
        started = time.monotonic()
        passed, output = self._run_scan(run_dir)
        self._record(run_dir, task_title, passed, output)
        if not passed:
            log.warning("SecurityScanGate: scan failed for task %r:\n%s", task_title, output[:500])
        return GateResult(
            name=self.name,
            status="pass" if passed else "fail",
            required=self.required,
            blocked=not passed,
            cached=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            details=output,
            metadata={"command": self._command},
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_scan(self, run_dir: Path) -> tuple[bool, str]:
        """Execute the scan command and return (passed, output)."""
        try:
            proc = subprocess.run(
                self._command,
                shell=True,
                cwd=run_dir,
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

    def _record(self, run_dir: Path, task_title: str, passed: bool, output: str) -> None:
        """Append result to .sdd/metrics/custom_gates.jsonl."""
        metrics_dir = run_dir / ".sdd" / "metrics"
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "gate": self.name,
            "task": task_title,
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
