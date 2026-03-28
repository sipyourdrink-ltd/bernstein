"""Automated quality gates: lint, type-check, and test gates after task completion.

Runs configurable code quality checks after a task agent finishes but before
the approval gate evaluates the work. Hard-blocks merge when enabled gates fail.
Records results to .sdd/metrics/quality_gates.jsonl for trend analysis.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualityGatesConfig:
    """Configuration for automated quality gates.

    Attributes:
        enabled: Master switch — when False, no gates run.
        lint: Run lint gate (ruff check by default).
        lint_command: Shell command for linting.
        type_check: Run type-check gate (pyright by default).
        type_check_command: Shell command for type checking.
        tests: Run test gate.
        test_command: Shell command for running tests.
        timeout_s: Per-gate command timeout in seconds.
    """

    enabled: bool = True
    lint: bool = True
    lint_command: str = "ruff check ."
    type_check: bool = False
    type_check_command: str = "pyright"
    tests: bool = False
    test_command: str = "uv run python scripts/run_tests.py -x"
    timeout_s: int = 120


@dataclass
class QualityGateCheckResult:
    """Result of a single quality gate check.

    Attributes:
        gate: Gate name (e.g. "lint", "type_check", "tests").
        passed: Whether the check passed.
        blocked: True if this is a hard block (merge must not proceed).
        detail: Human-readable description of findings (truncated at 2000 chars).
    """

    gate: str
    passed: bool
    blocked: bool
    detail: str


@dataclass
class QualityGatesResult:
    """Overall result of all quality gate checks for a task.

    Attributes:
        task_id: ID of the task checked.
        passed: True if all blocking gates passed (or no gates ran).
        gate_results: Per-gate results in run order.
    """

    task_id: str
    passed: bool
    gate_results: list[QualityGateCheckResult] = field(default_factory=list[QualityGateCheckResult])


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------


def _run_command(command: str, cwd: Path, timeout_s: int) -> tuple[bool, str]:
    """Run a shell command and return (success, output).

    Args:
        command: Shell command to run.
        cwd: Working directory for the subprocess.
        timeout_s: Timeout in seconds before the process is killed.

    Returns:
        Tuple of (exit_code_zero, combined_stdout_stderr_output).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = (proc.stdout + proc.stderr).strip()
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        return proc.returncode == 0, output or "(no output)"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout_s}s"
    except OSError as exc:
        return False, f"Command error: {exc}"


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_quality_gates(
    task: Task,
    run_dir: Path,
    workdir: Path,
    config: QualityGatesConfig,
) -> QualityGatesResult:
    """Run all enabled quality gates on a completed task's changes.

    Gates run in order: lint -> type_check -> tests. All enabled gates run
    even if an earlier gate fails, so the caller gets a full picture.
    A gate that is enabled and fails sets ``blocked=True`` on its result and
    causes the overall ``passed=False``.

    Args:
        task: The completed task being validated.
        run_dir: Directory to run gate commands in (agent worktree or workdir).
        workdir: Project root for writing metrics to .sdd/metrics/.
        config: Which gates to run and their command/timeout configuration.

    Returns:
        QualityGatesResult with per-gate outcomes and overall passed flag.
    """
    if not config.enabled:
        return QualityGatesResult(task_id=task.id, passed=True)

    results: list[QualityGateCheckResult] = []

    if config.lint:
        ok, detail = _run_command(config.lint_command, run_dir, config.timeout_s)
        check = QualityGateCheckResult(
            gate="lint",
            passed=ok,
            blocked=not ok,
            detail="no lint violations" if ok else detail,
        )
        results.append(check)
        _record_gate_event(task.id, "lint", _result_str(check), workdir)
        if not ok:
            logger.warning(
                "Quality gate [lint] failed for task %s: %s",
                task.id,
                detail[:200],
            )

    if config.type_check:
        ok, detail = _run_command(config.type_check_command, run_dir, config.timeout_s)
        check = QualityGateCheckResult(
            gate="type_check",
            passed=ok,
            blocked=not ok,
            detail="no type errors" if ok else detail,
        )
        results.append(check)
        _record_gate_event(task.id, "type_check", _result_str(check), workdir)
        if not ok:
            logger.warning(
                "Quality gate [type_check] failed for task %s: %s",
                task.id,
                detail[:200],
            )

    if config.tests:
        ok, detail = _run_command(config.test_command, run_dir, config.timeout_s)
        check = QualityGateCheckResult(
            gate="tests",
            passed=ok,
            blocked=not ok,
            detail="all tests passing" if ok else detail,
        )
        results.append(check)
        _record_gate_event(task.id, "tests", _result_str(check), workdir)
        if not ok:
            logger.warning(
                "Quality gate [tests] failed for task %s: %s",
                task.id,
                detail[:200],
            )

    overall_passed = all(not r.blocked for r in results)
    return QualityGatesResult(task_id=task.id, passed=overall_passed, gate_results=results)


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


def _result_str(check: QualityGateCheckResult) -> str:
    """Translate a QualityGateCheckResult to a metrics result string."""
    if check.passed:
        return "pass"
    if check.blocked:
        return "blocked"
    return "flagged"


def _record_gate_event(task_id: str, gate: str, result: str, workdir: Path) -> None:
    """Append a quality gate event to .sdd/metrics/quality_gates.jsonl.

    Args:
        task_id: ID of the task being checked.
        gate: Gate name (e.g. "lint").
        result: Outcome string: "pass", "blocked", or "flagged".
        workdir: Project root directory.
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "gate": gate,
        "result": result,
    }
    try:
        with open(metrics_dir / "quality_gates.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.debug("Could not write quality gate event: %s", exc)


def get_quality_gate_stats(workdir: Path) -> dict[str, Any]:
    """Read .sdd/metrics/quality_gates.jsonl and return aggregate stats.

    Returns a dict with:
      - total: total events recorded
      - blocked: events with result "blocked"
      - by_gate: per-gate breakdown {gate: {pass: N, blocked: N}}

    Args:
        workdir: Project root directory.
    """
    metrics_file = workdir / ".sdd" / "metrics" / "quality_gates.jsonl"
    if not metrics_file.exists():
        return {"total": 0, "blocked": 0, "by_gate": {}}

    total = blocked = 0
    by_gate: dict[str, dict[str, int]] = {}

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        gate = str(event.get("gate", "unknown"))
        result_val = str(event.get("result", "pass"))
        total += 1
        if result_val == "blocked":
            blocked += 1

        counts = by_gate.setdefault(gate, {"pass": 0, "blocked": 0})
        counts[result_val] = counts.get(result_val, 0) + 1

    return {"total": total, "blocked": blocked, "by_gate": by_gate}
