"""Coverage delta gate implementation.

The coverage gate compares the current branch's measured coverage against a
baseline captured from the configured base ref (usually ``main``). Measuring
baseline synchronously during a task completion is expensive — it requires a
full test run against a freshly checked-out worktree and historically blocked
agent progress for 5+ minutes (see audit-032).

Behavior now:

* The default command runs tests via ``scripts/run_tests.py``, the
  isolated-per-file runner the project mandates. The previous default invoked
  ``pytest tests/unit`` directly, which violated CLAUDE.md and leaked 100+ GB of
  RAM on this repo.
* ``evaluate()`` is non-blocking with respect to baseline measurement. When the
  cached baseline is missing or stale it returns a ``skipped`` evaluation with
  a warning rather than kicking off a multi-minute worktree + pytest run on the
  hot path. Callers are expected to refresh the baseline out-of-band (for
  example via a merge-to-main hook that invokes :meth:`refresh_baseline`).
* The cache schema includes a ``measured_at`` timestamp. Baselines older than
  ``baseline_ttl_seconds`` (default 7 days) are considered stale.
* :meth:`refresh_baseline` is the explicit entry point for background jobs
  that want to rebuild the cache.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bernstein.core.git_basic import run_git

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageEvaluation:
    """Outcome of a baseline-vs-current coverage comparison.

    Attributes:
        passed: ``True`` when the gate should not block. A skipped evaluation
            (no baseline available yet) also reports ``passed=True`` — the gate
            is not authoritative until a baseline exists.
        baseline_pct: Baseline coverage percentage. ``0.0`` when skipped.
        current_pct: Current coverage percentage. ``0.0`` when skipped.
        delta_pct: ``current_pct - baseline_pct`` rounded to two decimals.
        detail: Human-readable summary suitable for gate output.
        status: ``"ok"`` for a real comparison, ``"skipped"`` when the gate
            short-circuited (missing/stale baseline), or ``"regressed"`` on
            regression.
        stale: ``True`` when the baseline cache is missing or expired.
    """

    passed: bool
    baseline_pct: float
    current_pct: float
    delta_pct: float
    detail: str
    status: str = "ok"
    stale: bool = False


class CoverageGate:
    """Compare current coverage against a cached base-ref baseline.

    The gate never kicks off a synchronous baseline measurement during
    ``evaluate`` — long worktree + pytest runs belong to a background refresh
    job. When no fresh baseline is cached the gate reports a non-blocking
    ``skipped`` outcome so task completion is not stalled.
    """

    BASELINE_FILE = Path(".sdd/cache/coverage_baseline.json")
    # Use the project's isolated per-file runner with --coverage so we don't
    # violate CLAUDE.md's "never run pytest tests/ -x -q" rule.
    DEFAULT_COMMAND = "uv run python scripts/run_tests.py --coverage"
    # Baselines older than this are treated as stale and trigger a warning.
    DEFAULT_BASELINE_TTL_SECONDS = 7 * 24 * 60 * 60

    def __init__(
        self,
        workdir: Path,
        run_dir: Path,
        *,
        base_ref: str = "main",
        coverage_command: str | None = None,
        baseline_ttl_seconds: int | None = None,
    ) -> None:
        """Initialize the gate.

        Args:
            workdir: Repository root (source of truth for git + cache).
            run_dir: Directory in which to measure current coverage.
            base_ref: Base ref whose coverage is the baseline.
            coverage_command: Shell command that produces ``coverage.json``.
                Defaults to the isolated runner.
            baseline_ttl_seconds: Maximum age of a cached baseline. ``None``
                uses :attr:`DEFAULT_BASELINE_TTL_SECONDS`.
        """
        self._workdir = workdir
        self._run_dir = run_dir
        self._base_ref = base_ref
        self._coverage_command = coverage_command or self.DEFAULT_COMMAND
        self._baseline_path = workdir / self.BASELINE_FILE
        self._baseline_ttl_seconds = (
            baseline_ttl_seconds if baseline_ttl_seconds is not None else self.DEFAULT_BASELINE_TTL_SECONDS
        )

    # -- public API ---------------------------------------------------------

    def measure_baseline(self) -> float:
        """Measure coverage on the configured base ref in a temporary worktree.

        This is the expensive path; it should only be called from background
        refresh jobs, never from a task-completion gate.
        """
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

    def refresh_baseline(self) -> float:
        """Re-measure the baseline and persist it to the cache.

        Intended for background jobs (e.g. post-merge-to-main hook). Returns
        the freshly measured percentage.
        """
        baseline = self.measure_baseline()
        self._write_baseline(baseline)
        return baseline

    def evaluate(self) -> CoverageEvaluation:
        """Compare current coverage to the cached baseline.

        Never measures the baseline synchronously. When no valid baseline is
        cached the evaluation short-circuits with ``passed=True`` and
        ``stale=True`` so task completion is not blocked by a 5+ minute
        worktree + test run.
        """
        cached = self._load_cached_baseline()
        if cached is None:
            detail = (
                "Coverage baseline not available — gate skipped. "
                f"Run CoverageGate(base_ref={self._base_ref!r}).refresh_baseline() "
                "to populate the cache."
            )
            logger.warning("coverage_gate: %s", detail)
            return CoverageEvaluation(
                passed=True,
                baseline_pct=0.0,
                current_pct=0.0,
                delta_pct=0.0,
                detail=detail,
                status="skipped",
                stale=True,
            )

        baseline_pct, measured_at = cached
        stale = self._is_stale(measured_at)
        current = self.measure_current()
        delta = round(current - baseline_pct, 2)
        passed = delta >= 0.0
        age_suffix = ""
        if stale:
            age_suffix = f" [baseline stale: measured {self._format_age(measured_at)} ago]"
        detail = f"Coverage: {baseline_pct:.1f}% -> {current:.1f}% (delta: {delta:+.1f}%){age_suffix}"
        if stale:
            logger.warning(
                "coverage_gate: cached baseline for %s is stale (%s old); schedule a refresh",
                self._base_ref,
                self._format_age(measured_at),
            )
        return CoverageEvaluation(
            passed=passed,
            baseline_pct=baseline_pct,
            current_pct=current,
            delta_pct=delta,
            detail=detail,
            status="ok" if passed else "regressed",
            stale=stale,
        )

    # -- baseline cache helpers --------------------------------------------

    def _load_cached_baseline(self) -> tuple[float, float | None] | None:
        """Return ``(baseline_pct, measured_at)`` if the cache is valid.

        ``measured_at`` is ``None`` for legacy cache entries without a
        timestamp. Returns ``None`` when the cache is missing, malformed, or
        written for a different base ref / coverage command.
        """
        if not self._baseline_path.exists():
            return None
        try:
            raw: object = json.loads(self._baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        data = cast("dict[str, object]", raw)
        if data.get("base_ref") != self._base_ref:
            return None
        if data.get("coverage_command") != self._coverage_command:
            return None
        baseline_value = data.get("baseline_pct")
        if not isinstance(baseline_value, (int, float, str)):
            return None
        try:
            baseline_pct = float(baseline_value)
        except ValueError:
            return None
        measured_at_raw = data.get("measured_at")
        measured_at = float(measured_at_raw) if isinstance(measured_at_raw, (int, float)) else None
        return baseline_pct, measured_at

    def _load_or_measure_baseline(self) -> float:
        """Backwards-compatible helper retained for existing tests/callers.

        Prefer :meth:`evaluate` (non-blocking) or :meth:`refresh_baseline`
        (explicit background refresh). This path still short-circuits to the
        cache first; a synchronous re-measure only happens when a caller
        explicitly requests it.
        """
        cached = self._load_cached_baseline()
        if cached is not None:
            return cached[0]
        baseline = self.measure_baseline()
        self._write_baseline(baseline)
        return baseline

    def _write_baseline(self, baseline: float) -> None:
        """Persist ``baseline`` to the cache alongside timing metadata."""
        self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_ref": self._base_ref,
            "baseline_pct": baseline,
            "coverage_command": self._coverage_command,
            "measured_at": time.time(),
        }
        self._baseline_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _is_stale(self, measured_at: float | None) -> bool:
        """Return ``True`` when the cache is older than the configured TTL."""
        if measured_at is None:
            # Legacy entries without timestamps are treated as stale.
            return True
        if self._baseline_ttl_seconds <= 0:
            return False
        age = time.time() - measured_at
        return age > self._baseline_ttl_seconds

    @staticmethod
    def _format_age(measured_at: float | None) -> str:
        """Format cache age as a short human-readable string."""
        if measured_at is None:
            return "unknown"
        age = max(0.0, time.time() - measured_at)
        if age < 60:
            return f"{age:.0f}s"
        if age < 3600:
            return f"{age / 60:.0f}m"
        if age < 86400:
            return f"{age / 3600:.1f}h"
        return f"{age / 86400:.1f}d"

    # -- measurement internals ---------------------------------------------

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
