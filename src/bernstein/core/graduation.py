"""Pilot-to-production graduation framework.

Structured graduation: sandbox (no real changes) → shadow (runs but doesn't commit) →
assisted (human approves each) → autonomous (batch review).

Tracks metrics at each level to drive evidence-based promotion decisions.
Addresses the deployment gap where 80% of experiments never reach production.

Stage semantics
---------------
- SANDBOX:   Agents spawn but make no real changes (dry_run=True).
- SHADOW:    Agents run and produce diffs; changes are applied locally but not committed.
- ASSISTED:  Changes committed; each task merge requires explicit human approval.
- AUTONOMOUS: Changes committed and auto-merged after batch review.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GraduationStage(Enum):
    """Operating stage for a Bernstein session.

    Stages form a linear progression from fully sandboxed to fully autonomous.
    Sessions start at a configured stage and graduate forward as metrics thresholds
    are met.
    """

    SANDBOX = "sandbox"
    SHADOW = "shadow"
    ASSISTED = "assisted"
    AUTONOMOUS = "autonomous"


_STAGE_ORDER: list[GraduationStage] = [
    GraduationStage.SANDBOX,
    GraduationStage.SHADOW,
    GraduationStage.ASSISTED,
    GraduationStage.AUTONOMOUS,
]


# ---------------------------------------------------------------------------
# Policy & metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagePolicy:
    """Minimum conditions required to graduate *from* this stage.

    Attributes:
        stage: The stage these thresholds apply to.
        min_tasks_completed: Minimum successful tasks at this stage before graduation.
        min_success_rate: Minimum success ratio (0.0-1.0) required to graduate.
        max_consecutive_failures: Graduation is blocked if this many failures occur
            in a row.  Resets to zero on the next success.
        min_hours: Minimum wall-clock hours spent at this stage before graduation
            is allowed (0.0 = no time requirement).
    """

    stage: GraduationStage
    min_tasks_completed: int = 5
    min_success_rate: float = 0.80
    max_consecutive_failures: int = 3
    min_hours: float = 0.0


def _default_policies() -> dict[str, StagePolicy]:
    """Return default graduation policies, keyed by stage value."""
    return {
        GraduationStage.SANDBOX.value: StagePolicy(
            stage=GraduationStage.SANDBOX,
            min_tasks_completed=3,
            min_success_rate=0.80,
            max_consecutive_failures=3,
            min_hours=0.0,
        ),
        GraduationStage.SHADOW.value: StagePolicy(
            stage=GraduationStage.SHADOW,
            min_tasks_completed=5,
            min_success_rate=0.85,
            max_consecutive_failures=2,
            min_hours=0.0,
        ),
        GraduationStage.ASSISTED.value: StagePolicy(
            stage=GraduationStage.ASSISTED,
            min_tasks_completed=10,
            min_success_rate=0.90,
            max_consecutive_failures=2,
            min_hours=0.0,
        ),
        # AUTONOMOUS is terminal — no outbound policy.
    }


@dataclass
class StageMetrics:
    """Accumulated metrics for a single graduation stage.

    Attributes:
        stage: The stage these metrics belong to.
        tasks_completed: Tasks that finished successfully.
        tasks_failed: Tasks that failed or exhausted retries.
        consecutive_failures: Current run of consecutive failures; resets on success.
        started_at: Unix timestamp when the session entered this stage.
        last_updated: Unix timestamp of the most recent task event.
        total_cost_usd: Cumulative agent spend at this stage.
        total_duration_s: Sum of all task durations at this stage (seconds).
    """

    stage: GraduationStage
    tasks_completed: int = 0
    tasks_failed: int = 0
    consecutive_failures: int = 0
    started_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    total_cost_usd: float = 0.0
    total_duration_s: float = 0.0

    @property
    def tasks_total(self) -> int:
        """Total tasks attempted at this stage."""
        return self.tasks_completed + self.tasks_failed

    @property
    def success_rate(self) -> float:
        """Ratio of successful tasks to total; 0.0 when no tasks attempted."""
        return self.tasks_completed / self.tasks_total if self.tasks_total > 0 else 0.0

    @property
    def hours_elapsed(self) -> float:
        """Wall-clock hours spent at this stage."""
        return (self.last_updated - self.started_at) / 3600.0

    @property
    def avg_duration_s(self) -> float:
        """Average task duration in seconds; 0.0 when no tasks attempted."""
        return self.total_duration_s / self.tasks_total if self.tasks_total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict, including computed properties."""
        return {
            "stage": self.stage.value,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_total": self.tasks_total,
            "consecutive_failures": self.consecutive_failures,
            "started_at": self.started_at,
            "last_updated": self.last_updated,
            "total_cost_usd": self.total_cost_usd,
            "total_duration_s": self.total_duration_s,
            "success_rate": round(self.success_rate, 4),
            "hours_elapsed": round(self.hours_elapsed, 4),
            "avg_duration_s": round(self.avg_duration_s, 2),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StageMetrics:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        return cls(
            stage=GraduationStage(d["stage"]),
            tasks_completed=d.get("tasks_completed", 0),
            tasks_failed=d.get("tasks_failed", 0),
            consecutive_failures=d.get("consecutive_failures", 0),
            started_at=d.get("started_at", time.time()),
            last_updated=d.get("last_updated", time.time()),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            total_duration_s=d.get("total_duration_s", 0.0),
        )


# ---------------------------------------------------------------------------
# Graduation record (per-session state)
# ---------------------------------------------------------------------------


@dataclass
class GraduationRecord:
    """Full graduation state for a Bernstein session.

    Attributes:
        session_id: Identifier for the run/session (e.g. orchestrator run ID).
        current_stage: The stage the session is currently operating at.
        stage_metrics: Per-stage accumulated metrics, keyed by stage value.
        promotion_log: Ordered list of promotion events (timestamp, reason, snapshot).
        created_at: Unix timestamp when this record was first created.
    """

    session_id: str
    current_stage: GraduationStage = GraduationStage.SANDBOX
    stage_metrics: dict[str, StageMetrics] = field(default_factory=dict)
    promotion_log: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def current_metrics(self) -> StageMetrics:
        """Return (and lazily create) metrics for the current stage."""
        key = self.current_stage.value
        if key not in self.stage_metrics:
            self.stage_metrics[key] = StageMetrics(stage=self.current_stage)
        return self.stage_metrics[key]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "session_id": self.session_id,
            "current_stage": self.current_stage.value,
            "stage_metrics": {k: v.to_dict() for k, v in self.stage_metrics.items()},
            "promotion_log": self.promotion_log,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GraduationRecord:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        stage_metrics = {k: StageMetrics.from_dict(v) for k, v in d.get("stage_metrics", {}).items()}
        return cls(
            session_id=d["session_id"],
            current_stage=GraduationStage(d.get("current_stage", "sandbox")),
            stage_metrics=stage_metrics,
            promotion_log=d.get("promotion_log", []),
            created_at=d.get("created_at", time.time()),
        )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class GraduationEvaluator:
    """Evaluates whether a session is ready to graduate to the next stage.

    Args:
        policies: Stage-keyed graduation policies.  Defaults to
            :func:`_default_policies` when omitted.
    """

    def __init__(self, policies: dict[str, StagePolicy] | None = None) -> None:
        self._policies = policies if policies is not None else _default_policies()

    def can_graduate(self, record: GraduationRecord) -> tuple[bool, str]:
        """Check whether *record* meets graduation criteria for its current stage.

        Args:
            record: The session's current graduation state.

        Returns:
            ``(True, reason)`` when criteria are met; ``(False, reason)`` otherwise.
        """
        stage = record.current_stage
        if stage == GraduationStage.AUTONOMOUS:
            return False, "already at terminal stage (autonomous)"

        policy = self._policies.get(stage.value)
        if policy is None:
            return False, f"no policy configured for stage {stage.value!r}"

        metrics = record.current_metrics()

        if metrics.tasks_completed < policy.min_tasks_completed:
            return False, (f"need {policy.min_tasks_completed} completed tasks, have {metrics.tasks_completed}")
        if metrics.success_rate < policy.min_success_rate:
            return False, (f"success rate {metrics.success_rate:.0%} below required {policy.min_success_rate:.0%}")
        if metrics.consecutive_failures >= policy.max_consecutive_failures:
            return False, (
                f"{metrics.consecutive_failures} consecutive failures (max {policy.max_consecutive_failures})"
            )
        if metrics.hours_elapsed < policy.min_hours:
            return False, (f"only {metrics.hours_elapsed:.1f}h at stage, need {policy.min_hours:.1f}h")

        next_st = self.next_stage(stage)
        return True, f"ready to graduate to {next_st.value}"

    @staticmethod
    def next_stage(current: GraduationStage) -> GraduationStage:
        """Return the stage that follows *current* in the progression.

        Args:
            current: The current stage.

        Returns:
            The next stage.

        Raises:
            ValueError: When *current* is already the terminal stage.
        """
        idx = _STAGE_ORDER.index(current)
        if idx + 1 >= len(_STAGE_ORDER):
            raise ValueError(f"no stage after {current.value!r}")
        return _STAGE_ORDER[idx + 1]

    def promote(
        self,
        record: GraduationRecord,
        *,
        reason: str = "auto",
        promoted_by: str = "system",
    ) -> GraduationRecord:
        """Advance *record* to the next graduation stage.

        Args:
            record: The graduation record to mutate.
            reason: Human-readable reason for the promotion.
            promoted_by: Who triggered the promotion (``"system"`` or a user ID).

        Returns:
            The same *record* instance, updated to the new stage.

        Raises:
            ValueError: When already at the terminal stage.
        """
        from_stage = record.current_stage
        to_stage = self.next_stage(from_stage)
        now = time.time()

        record.promotion_log.append(
            {
                "from_stage": from_stage.value,
                "to_stage": to_stage.value,
                "timestamp": now,
                "reason": reason,
                "promoted_by": promoted_by,
                "metrics_snapshot": record.current_metrics().to_dict(),
            }
        )
        record.current_stage = to_stage
        if to_stage.value not in record.stage_metrics:
            record.stage_metrics[to_stage.value] = StageMetrics(stage=to_stage, started_at=now)

        logger.info(
            "session %s graduated %s → %s (reason=%s, by=%s)",
            record.session_id,
            from_stage.value,
            to_stage.value,
            reason,
            promoted_by,
        )
        return record


# ---------------------------------------------------------------------------
# Store (file-based persistence)
# ---------------------------------------------------------------------------


class GraduationStore:
    """File-based persistence for graduation records and metrics.

    State files:
        ``.sdd/graduation/<session_id>.json``  — current stage and per-stage metrics.
        ``.sdd/metrics/graduation.jsonl``       — append-only event log.

    Args:
        sdd_dir: Path to the ``.sdd/`` directory.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._grad_dir = sdd_dir / "graduation"
        self._metrics_file = sdd_dir / "metrics" / "graduation.jsonl"

    def _ensure_dirs(self) -> None:
        self._grad_dir.mkdir(parents=True, exist_ok=True)
        self._metrics_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> GraduationRecord | None:
        """Load a graduation record by session ID, or return ``None`` if absent."""
        path = self._grad_dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return GraduationRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("failed to load graduation record %s: %s", session_id, exc)
            return None

    def save(self, record: GraduationRecord) -> None:
        """Persist a graduation record to disk."""
        self._ensure_dirs()
        path = self._grad_dir / f"{record.session_id}.json"
        path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")

    def get_or_create(
        self,
        session_id: str,
        initial_stage: GraduationStage = GraduationStage.SANDBOX,
    ) -> GraduationRecord:
        """Return an existing record or create a new one at *initial_stage*."""
        record = self.load(session_id)
        if record is None:
            record = GraduationRecord(
                session_id=session_id,
                current_stage=initial_stage,
            )
            record.stage_metrics[initial_stage.value] = StageMetrics(stage=initial_stage)
            self.save(record)
        return record

    def list_all(self) -> list[GraduationRecord]:
        """Return all tracked graduation records."""
        if not self._grad_dir.exists():
            return []
        records: list[GraduationRecord] = []
        for path in sorted(self._grad_dir.glob("*.json")):
            try:
                records.append(GraduationRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("skipping malformed graduation file %s: %s", path.name, exc)
        return records

    # ------------------------------------------------------------------
    # Event recording
    # ------------------------------------------------------------------

    def record_task_event(
        self,
        session_id: str,
        *,
        success: bool,
        task_id: str,
        duration_s: float = 0.0,
        cost_usd: float = 0.0,
        initial_stage: GraduationStage = GraduationStage.SANDBOX,
    ) -> GraduationRecord:
        """Update stage metrics after a task completes or fails.

        Args:
            session_id: The session that completed the task.
            success: Whether the task succeeded.
            task_id: Task identifier written to the audit log.
            duration_s: Task wall-clock duration in seconds.
            cost_usd: Task cost in USD.
            initial_stage: Stage to initialise the record at if not yet tracked.

        Returns:
            The updated graduation record.
        """
        record = self.get_or_create(session_id, initial_stage=initial_stage)
        metrics = record.current_metrics()
        now = time.time()

        if success:
            metrics.tasks_completed += 1
            metrics.consecutive_failures = 0
        else:
            metrics.tasks_failed += 1
            metrics.consecutive_failures += 1

        metrics.total_duration_s += duration_s
        metrics.total_cost_usd += cost_usd
        metrics.last_updated = now

        self.save(record)
        self._append_event(
            session_id=record.session_id,
            stage=record.current_stage.value,
            task_id=task_id,
            success=success,
            duration_s=duration_s,
            cost_usd=cost_usd,
        )
        return record

    def record_promotion(self, record: GraduationRecord) -> None:
        """Append the most recent promotion entry to the audit log."""
        if not record.promotion_log:
            return
        latest = record.promotion_log[-1]
        self._ensure_dirs()
        event: dict[str, Any] = {
            "ts": latest["timestamp"],
            "type": "promotion",
            "session_id": record.session_id,
            **latest,
        }
        with self._metrics_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def _append_event(
        self,
        *,
        session_id: str,
        stage: str,
        task_id: str,
        success: bool,
        duration_s: float,
        cost_usd: float,
    ) -> None:
        self._ensure_dirs()
        event: dict[str, Any] = {
            "ts": time.time(),
            "type": "task_event",
            "session_id": session_id,
            "stage": stage,
            "task_id": task_id,
            "success": success,
            "duration_s": duration_s,
            "cost_usd": cost_usd,
        }
        with self._metrics_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Stage → orchestrator config mapping
# ---------------------------------------------------------------------------


def stage_to_orchestrator_overrides(stage: GraduationStage) -> dict[str, Any]:
    """Return OrchestratorConfig overrides that enforce the given stage semantics.

    These can be merged into the orchestrator configuration at startup to apply
    the stage's operating constraints.

    Args:
        stage: The graduation stage to map.

    Returns:
        Dict of OrchestratorConfig field names → values.
    """
    match stage:
        case GraduationStage.SANDBOX:
            return {"dry_run": True, "approval": "auto", "merge_strategy": "none"}
        case GraduationStage.SHADOW:
            return {"dry_run": False, "approval": "auto", "merge_strategy": "none"}
        case GraduationStage.ASSISTED:
            return {"dry_run": False, "approval": "review", "merge_strategy": "pr"}
        case GraduationStage.AUTONOMOUS:
            return {"dry_run": False, "approval": "auto", "merge_strategy": "pr"}
