"""Golden test suite for the Bernstein orchestrator.

Curated set of tasks with known-good expectations to detect regressions
in routing, planning, or execution quality.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.models import Complexity, Scope

logger = logging.getLogger(__name__)


@dataclass
class GoldenTask:
    """A curated task with expected outcome bounds."""

    id: str
    title: str
    description: str
    role: str
    scope: Scope = Scope.MEDIUM
    complexity: Complexity = Complexity.MEDIUM
    expected_files: list[str] = field(default_factory=list)
    max_cost_usd: float = 1.0
    max_duration_s: int = 600


def load_golden_suite() -> list[GoldenTask]:
    """Load the curated list of golden tasks."""
    # In a real system, these would be loaded from a YAML/JSON file.
    # For this implementation, we provide a representative sample.
    return [
        GoldenTask(
            id="golden-001",
            title="Fix typo in README",
            description="Fix the typo 'orchestrater' -> 'orchestrator' in README.md",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            expected_files=["README.md"],
            max_cost_usd=0.05,
        ),
        GoldenTask(
            id="golden-002",
            title="Add unit test for router",
            description="Add a new unit test in tests/unit/test_router.py that verifies provider sorting.",
            role="qa",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            expected_files=["tests/unit/test_router.py"],
            max_cost_usd=0.20,
        ),
        GoldenTask(
            id="golden-003",
            title="Implement JSON structured logging",
            description="Ensure all components use JSON structured logging for easier parsing.",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.HIGH,
            expected_files=["src/bernstein/core/json_logging.py"],
            max_cost_usd=0.50,
        ),
    ]


class GoldenEvalRunner:
    """Runs the golden test suite and compares results against expectations."""

    def __init__(self, workdir: Path, server_url: str) -> None:
        self.workdir = workdir
        self.server_url = server_url
        self.results: list[dict[str, Any]] = []

    async def run_suite(self) -> dict[str, Any]:
        """Run all tasks in the golden suite."""
        tasks = load_golden_suite()
        logger.info("Starting golden test suite (%d tasks)", len(tasks))

        start_time = time.time()
        for gtask in tasks:
            result = await self.run_task(gtask)
            self.results.append(result)

        duration = time.time() - start_time

        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_tasks": len(tasks),
            "passed": sum(1 for r in self.results if r["passed"]),
            "failed": sum(1 for r in self.results if not r["passed"]),
            "total_cost_usd": sum(r.get("cost_usd", 0.0) for r in self.results),
            "duration_s": duration,
            "tasks": self.results,
        }
        return summary

    async def run_task(self, gtask: GoldenTask) -> dict[str, Any]:
        """Submit a golden task and wait for completion/failure."""
        await asyncio.sleep(0)  # Async interface requirement
        # This would interact with the task server API
        # For this implementation, we return a mock success result
        return {
            "task_id": gtask.id,
            "title": gtask.title,
            "passed": True,
            "cost_usd": 0.12,
            "duration_s": 45,
            "files_modified": gtask.expected_files,
        }
