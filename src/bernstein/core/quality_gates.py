"""Automated quality gates: lint, type-check, test, and mutation testing gates.

Runs configurable code quality checks after a task agent finishes but before
the approval gate evaluates the work. Hard-blocks merge when enabled gates fail.
Records results to .sdd/metrics/quality_gates.jsonl for trend analysis.
"""

from __future__ import annotations

import json
import logging
import re
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
        mutation_testing: Run mutation testing gate (mutmut by default).
        mutation_command: Shell command that runs mutation tests and prints results.
        mutation_threshold: Minimum required mutation score (0.0-1.0). Blocks if below.
        mutation_timeout_s: Timeout for mutation testing (longer than other gates).
    """

    enabled: bool = True
    lint: bool = True
    lint_command: str = "ruff check ."
    type_check: bool = False
    type_check_command: str = "pyright"
    tests: bool = False
    test_command: str = "uv run python scripts/run_tests.py -x"
    timeout_s: int = 120
    mutation_testing: bool = False
    mutation_command: str = "uv run mutmut run"
    mutation_threshold: float = 0.50
    mutation_timeout_s: int = 600


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
# Mutation testing helpers
# ---------------------------------------------------------------------------


def _parse_mutation_score(output: str) -> float | None:
    """Extract a mutation score (0.0-1.0) from mutation tool output.

    Supports output formats from mutmut and mutatest. Returns None when the
    output cannot be parsed (caller should fall back to exit-code semantics).

    Args:
        output: Combined stdout/stderr from the mutation testing command.

    Returns:
        Float mutation score in [0.0, 1.0], or None if unparseable.
    """
    # mutmut run: "🎉 42/100  🤔 0  🙁 58  🔇 0"
    # The first "killed/total" fraction appears as digits-slash-digits.
    m = re.search(r"\b(\d+)/(\d+)\b", output)
    if m:
        killed, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            return killed / total
        return None  # zero mutants — can't produce a meaningful score

    # mutatest / generic:
    #   "Killed: 42\nSurvived: 58"   (keyword before number)
    #   "42 killed, 58 survived"      (number before keyword)
    # Use [ \t]+ (horizontal whitespace only) for the N-before-keyword form so
    # a newline between a digit and the next keyword on a new line is not matched.
    killed_m = re.search(r"(?:[Kk]illed[:\s]+(\d+)|(\d+)[ \t]+[Kk]illed)", output)
    survived_m = re.search(r"(?:[Ss]urvived[:\s]+(\d+)|(\d+)[ \t]+[Ss]urvived)", output)
    if killed_m and survived_m:
        killed = int(next(g for g in killed_m.groups() if g is not None))
        survived = int(next(g for g in survived_m.groups() if g is not None))
        total = killed + survived
        if total > 0:
            return killed / total
        return None

    return None


def _run_mutation_gate(config: QualityGatesConfig, run_dir: Path) -> tuple[bool, str, float | None]:
    """Run mutation testing and compare score against the configured threshold.

    Args:
        config: Quality gates configuration.
        run_dir: Directory to run the mutation command in.

    Returns:
        Tuple of (passed, detail_message, score_or_None).
        ``passed`` is True when the mutation score meets the threshold.
        ``score_or_None`` is the parsed float score, or None when unparseable.
    """
    _ok, output = _run_command(config.mutation_command, run_dir, config.mutation_timeout_s)

    score = _parse_mutation_score(output)

    if score is not None:
        passed = score >= config.mutation_threshold
        status = "\u2265" if passed else "<"
        detail = f"Mutation score: {score:.1%} ({status} threshold {config.mutation_threshold:.1%})\n{output}"
        return passed, detail, score

    # Could not parse a numeric score — treat non-zero exit as failure.
    passed = _ok
    detail = (
        f"Could not parse mutation score (threshold {config.mutation_threshold:.1%}). "
        f"Exit: {'0' if _ok else 'non-zero'}\n{output}"
    )
    return passed, detail, None


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

    if config.mutation_testing:
        ok, detail, score = _run_mutation_gate(config, run_dir)
        check = QualityGateCheckResult(
            gate="mutation_testing",
            passed=ok,
            blocked=not ok,
            detail=detail,
        )
        results.append(check)
        extra: dict[str, Any] | None = {"mutation_score": round(score, 4)} if score is not None else None
        _record_gate_event(task.id, "mutation_testing", _result_str(check), workdir, extra=extra)
        if not ok:
            logger.warning(
                "Quality gate [mutation_testing] failed for task %s: %s",
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


def _record_gate_event(
    task_id: str,
    gate: str,
    result: str,
    workdir: Path,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a quality gate event to .sdd/metrics/quality_gates.jsonl.

    Args:
        task_id: ID of the task being checked.
        gate: Gate name (e.g. "lint").
        result: Outcome string: "pass", "blocked", or "flagged".
        workdir: Project root directory.
        extra: Optional extra fields merged into the event (e.g. mutation_score).
    """
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "gate": gate,
        "result": result,
    }
    if extra:
        event.update(extra)
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
