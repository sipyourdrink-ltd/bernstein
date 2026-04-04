"""A/B model testing: deterministic 50/50 split across tasks with cost/quality comparison.

When ``--ab-test`` is active the spawner routes tasks to one of two models
(e.g. opus vs sonnet) based on a deterministic hash of the task-id.  Results
are written to ``.sdd/metrics/ab_test_results.jsonl`` so a post-run report
can compare quality (files changed) and cost (tokens) between the two models.

Design:
- **Routing**: ``model_for_task(task_id, model_a, model_b)`` assigns each task
  to exactly one model — no extra agents are spawned.  The split is 50/50
  across a large number of tasks, and each individual task always gets the
  same model (deterministic).
- **Recording**: ``record_ab_outcome()`` appends one JSON line per completed
  task to ``.sdd/metrics/ab_test_results.jsonl``.
- **Reporting**: ``generate_ab_report()`` reads the file, groups by model, and
  returns a human-readable summary with per-model averages and a winner call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RESULTS_FILENAME = "ab_test_results.jsonl"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ABTestRecord:
    """One completed task in an A/B model test.

    Attributes:
        task_id: The task that was executed.
        task_title: Human-readable task title.
        model: Model that ran this task (e.g. ``"opus"`` or ``"sonnet"``).
        session_id: Agent session that executed the task.
        tokens_used: Total input+output tokens consumed.
        files_changed: Number of files modified.
        status: ``"completed"`` or ``"failed"``.
        duration_s: Wall-clock seconds from spawn to finish.
        recorded_at: Unix epoch when this record was written.
    """

    task_id: str
    task_title: str
    model: str
    session_id: str
    tokens_used: int
    files_changed: int
    status: str  # "completed" | "failed"
    duration_s: float
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ABTestRecord:
        """Deserialise from a stored dict."""
        return cls(
            task_id=str(d["task_id"]),
            task_title=str(d["task_title"]),
            model=str(d["model"]),
            session_id=str(d["session_id"]),
            tokens_used=int(d["tokens_used"]),
            files_changed=int(d["files_changed"]),
            status=str(d["status"]),
            duration_s=float(d["duration_s"]),
            recorded_at=float(d.get("recorded_at", 0.0)),
        )


@dataclass
class ModelStats:
    """Aggregated statistics for one model across all A/B test tasks.

    Attributes:
        model: Model name.
        task_count: Number of tasks assigned to this model.
        completed: Number of tasks that completed successfully.
        failed: Number of tasks that failed.
        avg_tokens: Average tokens per task (0 when task_count is 0).
        avg_files_changed: Average files changed per completed task.
        avg_duration_s: Average wall-clock seconds per task.
        total_tokens: Total tokens across all tasks.
    """

    model: str
    task_count: int
    completed: int
    failed: int
    avg_tokens: float
    avg_files_changed: float
    avg_duration_s: float
    total_tokens: int


@dataclass
class ABTestReport:
    """Comparison report for a completed A/B model test run.

    Attributes:
        model_a: Stats for the first model.
        model_b: Stats for the second model.
        winner: Model with higher completion rate and fewer tokens, or
            ``"tie"`` when both are equal, or ``"insufficient_data"``
            when fewer than 2 tasks per model were recorded.
        summary: Human-readable one-paragraph summary.
    """

    model_a: ModelStats
    model_b: ModelStats
    winner: str
    summary: str


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def model_for_task(task_id: str, model_a: str, model_b: str) -> str:
    """Deterministically assign a task to one of two models (50/50 split).

    Uses the first byte of the SHA-256 digest of ``task_id`` to select the
    model.  Even bytes → ``model_a``; odd bytes → ``model_b``.

    Args:
        task_id: Unique task identifier.
        model_a: First candidate model (e.g. ``"sonnet"``).
        model_b: Second candidate model (e.g. ``"opus"``).

    Returns:
        Either ``model_a`` or ``model_b``.
    """
    digest_byte = hashlib.sha256(task_id.encode()).digest()[0]
    return model_a if digest_byte % 2 == 0 else model_b


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class ABTestStore:
    """Append-only store for A/B test task records.

    Records are written as JSON lines to
    ``{workdir}/.sdd/metrics/ab_test_results.jsonl``.

    Args:
        workdir: Project root directory.
    """

    def __init__(self, workdir: Path) -> None:
        self._path = workdir / ".sdd" / "metrics" / _RESULTS_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ABTestRecord) -> None:
        """Append one record to the results file.

        Args:
            record: Completed task result to persist.
        """
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("ABTestStore: failed to write record: %s", exc)

    def load(self) -> list[ABTestRecord]:
        """Load all records from the results file.

        Returns:
            List of records in append order; empty list when the file does not
            exist or is unreadable.
        """
        if not self._path.exists():
            return []
        records: list[ABTestRecord] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(ABTestRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    logger.debug("ABTestStore: skipping malformed line: %s", exc)
        except OSError as exc:
            logger.warning("ABTestStore: failed to read results: %s", exc)
        return records


# ---------------------------------------------------------------------------
# Recording helper
# ---------------------------------------------------------------------------


def record_ab_outcome(
    workdir: Path,
    *,
    task_id: str,
    task_title: str,
    model: str,
    session_id: str,
    tokens_used: int,
    files_changed: int,
    status: str,
    duration_s: float,
) -> None:
    """Persist one A/B test task outcome.

    Args:
        workdir: Project root directory.
        task_id: Task that was executed.
        task_title: Human-readable task title.
        model: Model that ran the task.
        session_id: Agent session that executed the task.
        tokens_used: Total tokens consumed by the agent.
        files_changed: Number of files modified by the agent.
        status: ``"completed"`` or ``"failed"``.
        duration_s: Wall-clock seconds from spawn to finish.
    """
    record = ABTestRecord(
        task_id=task_id,
        task_title=task_title,
        model=model,
        session_id=session_id,
        tokens_used=tokens_used,
        files_changed=files_changed,
        status=status,
        duration_s=duration_s,
    )
    ABTestStore(workdir).append(record)
    logger.info(
        "A/B TEST outcome recorded: task=%s model=%s status=%s tokens=%d files=%d",
        task_id,
        model,
        status,
        tokens_used,
        files_changed,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _compute_model_stats(records: list[ABTestRecord], model: str) -> ModelStats:
    """Compute aggregated stats for one model from a list of records."""
    subset = [r for r in records if r.model == model]
    if not subset:
        return ModelStats(
            model=model,
            task_count=0,
            completed=0,
            failed=0,
            avg_tokens=0.0,
            avg_files_changed=0.0,
            avg_duration_s=0.0,
            total_tokens=0,
        )
    completed = [r for r in subset if r.status == "completed"]
    failed = len(subset) - len(completed)
    total_tokens = sum(r.tokens_used for r in subset)
    avg_tokens = total_tokens / len(subset)
    avg_files = sum(r.files_changed for r in completed) / max(1, len(completed))
    avg_dur = sum(r.duration_s for r in subset) / len(subset)
    return ModelStats(
        model=model,
        task_count=len(subset),
        completed=len(completed),
        failed=failed,
        avg_tokens=avg_tokens,
        avg_files_changed=avg_files,
        avg_duration_s=avg_dur,
        total_tokens=total_tokens,
    )


def generate_ab_report(workdir: Path) -> ABTestReport:
    """Load A/B test records and produce a comparison report.

    Args:
        workdir: Project root directory.

    Returns:
        :class:`ABTestReport` comparing all models found in the results file.
        When fewer than two distinct models have been recorded, the winner is
        ``"insufficient_data"``.
    """
    store = ABTestStore(workdir)
    records = store.load()

    if not records:
        empty = ModelStats("(none)", 0, 0, 0, 0.0, 0.0, 0.0, 0)
        return ABTestReport(
            model_a=empty,
            model_b=empty,
            winner="insufficient_data",
            summary="No A/B test records found.",
        )

    models = sorted({r.model for r in records})
    if len(models) < 2:
        stats = _compute_model_stats(records, models[0])
        empty = ModelStats("(none)", 0, 0, 0, 0.0, 0.0, 0.0, 0)
        return ABTestReport(
            model_a=stats,
            model_b=empty,
            winner="insufficient_data",
            summary=f"Only one model ({models[0]}) has records — need at least 2 to compare.",
        )

    model_a, model_b = models[0], models[1]
    stats_a = _compute_model_stats(records, model_a)
    stats_b = _compute_model_stats(records, model_b)

    # Determine winner: higher completion rate wins; on tie, fewer tokens wins.
    min_tasks = 2
    if stats_a.task_count < min_tasks or stats_b.task_count < min_tasks:
        winner = "insufficient_data"
    else:
        rate_a = stats_a.completed / stats_a.task_count
        rate_b = stats_b.completed / stats_b.task_count
        if rate_a > rate_b:
            winner = model_a
        elif rate_b > rate_a:
            winner = model_b
        elif stats_a.avg_tokens <= stats_b.avg_tokens:
            winner = model_a
        else:
            winner = model_b

    lines = [
        "## A/B Model Test Report",
        "",
        f"{'Model':<20} {'Tasks':>6} {'Done':>6} {'Fail':>6} {'Avg Tokens':>12} {'Avg Files':>10} {'Avg Dur(s)':>11}",
        "-" * 75,
    ]
    for st in (stats_a, stats_b):
        lines.append(
            f"{st.model:<20} {st.task_count:>6} {st.completed:>6} {st.failed:>6} "
            f"{st.avg_tokens:>12.0f} {st.avg_files_changed:>10.1f} {st.avg_duration_s:>11.1f}"
        )
    lines += [
        "",
        f"Winner: **{winner}**",
    ]
    if winner not in ("tie", "insufficient_data"):
        loser = model_b if winner == model_a else model_a
        winner_stats = stats_a if winner == model_a else stats_b
        loser_stats = stats_b if winner == model_a else stats_a
        token_diff_pct = (loser_stats.avg_tokens - winner_stats.avg_tokens) / max(1, loser_stats.avg_tokens) * 100
        lines.append(f"  {winner} used {token_diff_pct:.0f}% fewer tokens on average vs {loser}")
    summary = "\n".join(lines)

    return ABTestReport(model_a=stats_a, model_b=stats_b, winner=winner, summary=summary)
