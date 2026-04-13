"""Coverage delta gate implementation."""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bernstein.core.git_basic import run_git

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageEvaluation:
    """Outcome of a baseline-vs-current coverage comparison."""

    passed: bool
    baseline_pct: float
    current_pct: float
    delta_pct: float
    detail: str


class CoverageGate:
    """Block merge if test coverage decreases."""

    BASELINE_FILE = Path(".sdd/cache/coverage_baseline.json")
    DEFAULT_COMMAND = "uv run coverage run -m pytest tests/unit -q && uv run coverage json"

    def __init__(
        self,
        workdir: Path,
        run_dir: Path,
        *,
        base_ref: str = "main",
        coverage_command: str | None = None,
    ) -> None:
        self._workdir = workdir
        self._run_dir = run_dir
        self._base_ref = base_ref
        self._coverage_command = coverage_command or self.DEFAULT_COMMAND
        self._baseline_path = workdir / self.BASELINE_FILE

    def measure_baseline(self) -> float:
        """Measure coverage on the configured base ref in a temporary worktree."""
        temp_parent = self._workdir / ".sdd" / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="coverage-base-", dir=temp_parent) as temp_dir:
            temp_path = Path(temp_dir)
            add_result = run_git(
                ["worktree", "add", "--detach", str(temp_path), self._base_ref],
                self._workdir,
                timeout=60,
            )
            if not add_result.ok:
                raise RuntimeError(add_result.stderr.strip() or f"Failed to create worktree for {self._base_ref}")
            try:
                return self._run_measurement(temp_path)
            finally:
                remove_result = run_git(["worktree", "remove", "--force", str(temp_path)], self._workdir, timeout=60)
                if not remove_result.ok:
                    logger.warning(
                        "Failed to remove temporary worktree %s: %s",
                        temp_path,
                        remove_result.stderr.strip(),
                    )
                    shutil.rmtree(temp_path, ignore_errors=True)

    def measure_current(self) -> float:
        """Measure coverage on the current run directory."""
        return self._run_measurement(self._run_dir)

    def evaluate(self) -> CoverageEvaluation:
        """Compare current coverage to the cached or freshly measured baseline."""
        baseline = self._load_or_measure_baseline()
        current = self.measure_current()
        delta = round(current - baseline, 2)
        passed = delta >= 0.0
        detail = f"Coverage: {baseline:.1f}% -> {current:.1f}% (delta: {delta:+.1f}%)"
        return CoverageEvaluation(
            passed=passed,
            baseline_pct=baseline,
            current_pct=current,
            delta_pct=delta,
            detail=detail,
        )

    def _load_or_measure_baseline(self) -> float:
        """Load the cached baseline when compatible, else re-measure it."""
        if self._baseline_path.exists():
            try:
                raw: object = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            data = cast("dict[str, object] | None", raw if isinstance(raw, dict) else None)
            if (
                isinstance(data, dict)
                and data.get("base_ref") == self._base_ref
                and data.get("coverage_command") == self._coverage_command
            ):
                baseline_value = data.get("baseline_pct")
                if isinstance(baseline_value, (int, float, str)):
                    try:
                        return float(baseline_value)
                    except ValueError:
                        pass

        baseline = self.measure_baseline()
        self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
        self._baseline_path.write_text(
            json.dumps(
                {
                    "base_ref": self._base_ref,
                    "baseline_pct": baseline,
                    "coverage_command": self._coverage_command,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return baseline

    def _run_measurement(self, cwd: Path) -> float:
        """Execute the coverage command in ``cwd`` and parse ``coverage.json``."""
        result = subprocess.run(
            shlex.split(self._coverage_command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if result.returncode != 0:
            combined = (result.stdout + result.stderr).strip()
            raise RuntimeError(combined or "Coverage command failed")
        return self._parse_total_pct(cwd / "coverage.json")

    def _parse_total_pct(self, report_path: Path) -> float:
        """Parse total coverage percentage from a coverage JSON report."""
        if not report_path.exists():
            raise RuntimeError(f"Coverage report not found: {report_path}")
        try:
            raw: object = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Failed to read coverage report: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("Coverage report has invalid structure")
        data = cast("dict[str, object]", raw)
        totals = data.get("totals")
        if not isinstance(totals, dict):
            raise RuntimeError("Coverage report missing totals")
        totals_map = cast("dict[str, object]", totals)
        percent_covered = totals_map.get("percent_covered")
        if isinstance(percent_covered, (int, float, str)):
            try:
                return float(percent_covered)
            except ValueError as exc:
                raise RuntimeError("Coverage report missing percent_covered") from exc
        raise RuntimeError("Coverage report missing percent_covered")
