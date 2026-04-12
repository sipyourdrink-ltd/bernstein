"""Completion budget tracking for task lineages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task


@dataclass(frozen=True)
class BudgetStatus:
    """Completion budget status for a task lineage."""

    original_task_id: str
    total_attempts: int
    retry_count: int
    fix_count: int
    total_cost_usd: float
    budget_remaining: int
    is_exhausted: bool
    recommendation: str


class CompletionBudget:
    """Track and enforce completion budgets per task lineage."""

    MAX_TOTAL_ATTEMPTS = 5
    MAX_FIX_TASKS = 2

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._budget_path = workdir / ".sdd" / "runtime" / "completion_budgets.json"

    def check(self, task: Task) -> BudgetStatus:
        """Check the current budget state for a task lineage."""
        data = self._read()
        lineage = self._lineage_key(task.title)
        entry = data.get(lineage, {})
        total_attempts = int(entry.get("total_attempts", 0))
        retry_count = int(entry.get("retry_count", 0))
        fix_count = int(entry.get("fix_count", 0))
        total_cost_usd = float(entry.get("total_cost_usd", 0.0))
        budget_remaining = max(self.MAX_TOTAL_ATTEMPTS - total_attempts, 0)
        is_exhausted = total_attempts >= self.MAX_TOTAL_ATTEMPTS or fix_count >= self.MAX_FIX_TASKS
        recommendation = "continue"
        if total_attempts >= self.MAX_TOTAL_ATTEMPTS:
            recommendation = "abandon"
        elif fix_count >= self.MAX_FIX_TASKS:
            recommendation = "escalate_human"
        return BudgetStatus(
            original_task_id=lineage,
            total_attempts=total_attempts,
            retry_count=retry_count,
            fix_count=fix_count,
            total_cost_usd=total_cost_usd,
            budget_remaining=budget_remaining,
            is_exhausted=is_exhausted,
            recommendation=recommendation,
        )

    def record_attempt(self, task: Task, *, is_fix: bool = False, cost_usd: float = 0.0) -> None:
        """Record one attempt against a lineage."""
        data = self._read()
        lineage = self._lineage_key(task.title)
        entry = data.setdefault(
            lineage,
            {
                "total_attempts": 0,
                "retry_count": 0,
                "fix_count": 0,
                "total_cost_usd": 0.0,
            },
        )
        entry["total_attempts"] = int(entry.get("total_attempts", 0)) + 1
        if is_fix:
            entry["fix_count"] = int(entry.get("fix_count", 0)) + 1
        elif "[RETRY " in task.title:
            entry["retry_count"] = int(entry.get("retry_count", 0)) + 1
        entry["total_cost_usd"] = float(entry.get("total_cost_usd", 0.0)) + cost_usd
        self._write(data)

    def should_create_fix_task(self, task: Task) -> tuple[bool, str]:
        """Determine whether another fix task is allowed."""
        status = self.check(task)
        if status.total_attempts >= self.MAX_TOTAL_ATTEMPTS:
            return (False, "budget exhausted")
        if status.fix_count >= self.MAX_FIX_TASKS:
            return (False, "max fix tasks reached")
        return (True, "within budget")

    def list_statuses(self) -> list[BudgetStatus]:
        """Return budget status for all recorded lineages."""
        data = self._read()
        statuses: list[BudgetStatus] = []
        for lineage, entry in sorted(data.items()):
            total_attempts = int(entry.get("total_attempts", 0))
            fix_count = int(entry.get("fix_count", 0))
            statuses.append(
                BudgetStatus(
                    original_task_id=lineage,
                    total_attempts=total_attempts,
                    retry_count=int(entry.get("retry_count", 0)),
                    fix_count=fix_count,
                    total_cost_usd=float(entry.get("total_cost_usd", 0.0)),
                    budget_remaining=max(self.MAX_TOTAL_ATTEMPTS - total_attempts, 0),
                    is_exhausted=total_attempts >= self.MAX_TOTAL_ATTEMPTS or fix_count >= self.MAX_FIX_TASKS,
                    recommendation=(
                        "abandon"
                        if total_attempts >= self.MAX_TOTAL_ATTEMPTS
                        else "escalate_human"
                        if fix_count >= self.MAX_FIX_TASKS
                        else "continue"
                    ),
                )
            )
        return statuses

    def _read(self) -> dict[str, dict[str, Any]]:
        """Read budget state from disk."""
        if not self._budget_path.exists():
            return {}
        try:
            raw = json.loads(self._budget_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return cast("dict[str, dict[str, Any]]", raw) if isinstance(raw, dict) else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """Persist budget state to disk."""
        self._budget_path.parent.mkdir(parents=True, exist_ok=True)
        self._budget_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def _lineage_key(cls, title: str) -> str:
        """Normalize a task title into a stable lineage key."""
        normalized = title.strip()
        normalized = re.sub(r"^\[RETRY \d+\]\s*", "", normalized)
        normalized = re.sub(r"^\[FIX \d+\]\s*", "", normalized)
        normalized = re.sub(r"^Fix:\s*", "", normalized)
        normalized = re.sub(r"\s+\((?:janitor|judge retry \d+)\)$", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or title.strip()
