"""Tests for audit-057: bounded in-memory usage history in CostTracker.

Verifies that:
  * ``CostTracker._usages`` never exceeds its configured ring-buffer size.
  * Cumulative analytics (``spent_usd``, per-agent, per-model, totals,
    averages) remain exact across eviction boundaries.
  * ``_total_usages_recorded`` counts every record including evicted ones.
  * When ``rotation_dir`` is set, evicted rows are appended to a JSONL
    rotation file that is readable back as valid JSON lines.
  * Budget enforcement still works correctly when rows have been evicted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.cost.cost_tracker import (
    CostTracker,
    TokenUsage,
    estimate_cost,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Bounded buffer
# ---------------------------------------------------------------------------


class TestUsageBufferBounded:
    def test_buffer_respects_configured_size(self) -> None:
        """Pushing N > buffer_size records keeps the deque at buffer_size."""
        buffer_size = 16
        tracker = CostTracker(run_id="run-bounded", usage_buffer_size=buffer_size)
        total = buffer_size * 4  # way more than the cap
        for i in range(total):
            tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="haiku",
                input_tokens=10,
                output_tokens=10,
            )

        assert len(tracker.usages) == buffer_size
        # deque exposes its maxlen — verifies the ring buffer, not a list trim
        assert tracker._usages.maxlen == buffer_size  # pyright: ignore[reportPrivateUsage]
        # total ever appended is tracked independently of the in-memory cap
        assert tracker.total_usages_recorded == total

    def test_unbounded_mode_keeps_all_rows(self) -> None:
        """usage_buffer_size=0 disables the cap (legacy behaviour)."""
        tracker = CostTracker(run_id="run-unbounded", usage_buffer_size=0)
        for i in range(50):
            tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="haiku",
                input_tokens=1,
                output_tokens=1,
            )
        assert len(tracker.usages) == 50
        assert tracker._usages.maxlen is None  # pyright: ignore[reportPrivateUsage]

    def test_last_entry_is_the_most_recent_usage(self) -> None:
        """After eviction, usages[-1] is still the most recently recorded row."""
        buffer_size = 8
        tracker = CostTracker(run_id="run-last", usage_buffer_size=buffer_size)
        for i in range(buffer_size * 3):
            tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="haiku",
                input_tokens=1,
                output_tokens=1,
            )
        usages = tracker.usages
        assert len(usages) == buffer_size
        assert usages[-1].task_id == f"task-{buffer_size * 3 - 1}"
        # The first retained row should be the one right after the last eviction
        assert usages[0].task_id == f"task-{buffer_size * 2}"


# ---------------------------------------------------------------------------
# Aggregates survive eviction
# ---------------------------------------------------------------------------


class TestAggregatesSurviveEviction:
    def test_totals_and_averages_remain_correct(self) -> None:
        """Totals, per-agent and per-model spend are exact across eviction."""
        buffer_size = 4
        tracker = CostTracker(run_id="run-totals", usage_buffer_size=buffer_size)

        total_records = 25
        expected_cost = 0.0
        per_agent: dict[str, float] = {}
        per_model: dict[str, float] = {}
        models = ("haiku", "sonnet", "opus")
        agents = ("agent-a", "agent-b", "agent-c")
        for i in range(total_records):
            model = models[i % len(models)]
            agent = agents[i % len(agents)]
            cost = estimate_cost(model, input_tokens=100, output_tokens=50)
            tracker.record(
                agent_id=agent,
                task_id=f"task-{i}",
                model=model,
                input_tokens=100,
                output_tokens=50,
            )
            expected_cost += cost
            per_agent[agent] = per_agent.get(agent, 0.0) + cost
            per_model[model] = per_model.get(model, 0.0) + cost

        # Ring buffer bounded
        assert len(tracker.usages) == buffer_size
        assert tracker.total_usages_recorded == total_records

        # Cumulative total is exact
        assert tracker.spent_usd == _approx(expected_cost)

        # Per-agent and per-model spend survive eviction
        for agent_id, expected in per_agent.items():
            assert tracker.spent_for_agent(agent_id) == _approx(expected)
        by_model = tracker.spent_by_model()
        for model, expected in per_model.items():
            assert by_model[model] == _approx(expected)

        # Average per task computed from live totals (not the in-memory deque)
        projection = tracker.project(tasks_done=total_records, tasks_remaining=0)
        assert projection.avg_cost_per_task_usd == _approx(expected_cost / total_records)

        # Full report iterates the accumulators, not the deque
        report = tracker.report(tasks_done=total_records, tasks_remaining=0)
        assert report.total_spent_usd == _approx(expected_cost)
        assert sum(a.total_cost_usd for a in report.per_agent) == _approx(expected_cost)
        assert sum(m.total_cost_usd for m in report.per_model) == _approx(expected_cost)
        # Invocation counts add up to the full history
        assert sum(a.task_count for a in report.per_agent) == total_records
        assert sum(m.invocation_count for m in report.per_model) == total_records

    def test_budget_stop_still_fires_after_eviction(self) -> None:
        """Budget enforcement uses the accumulator, not the deque."""
        buffer_size = 2
        # Tiny budget so that a handful of records trip the hard-stop
        tracker = CostTracker(
            run_id="run-budget",
            budget_usd=0.01,
            usage_buffer_size=buffer_size,
        )
        status = None
        for i in range(20):
            status = tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="opus",
                input_tokens=1000,
                output_tokens=1000,
            )
        assert status is not None
        assert status.should_stop is True
        assert tracker.can_spawn() is False
        # The deque is still bounded even though the budget blew through
        assert len(tracker.usages) == buffer_size


# ---------------------------------------------------------------------------
# JSONL rotation
# ---------------------------------------------------------------------------


class TestJsonlRotation:
    def test_evicted_rows_are_rotated_to_jsonl(self, tmp_path: Path) -> None:
        """Evicted rows land in usages-{run_id}.jsonl and are valid JSON lines."""
        buffer_size = 4
        tracker = CostTracker(
            run_id="run-rot",
            usage_buffer_size=buffer_size,
            rotation_dir=tmp_path,
        )

        total = 10  # 10 - 4 = 6 evictions expected
        expected_evictions = total - buffer_size
        for i in range(total):
            tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="haiku",
                input_tokens=1,
                output_tokens=1,
            )

        rotation_file = tmp_path / f"usages-{tracker.run_id}.jsonl"
        assert rotation_file.exists()

        lines = [ln for ln in rotation_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == expected_evictions
        # Each line round-trips through TokenUsage.from_dict
        parsed = [TokenUsage.from_dict(json.loads(ln)) for ln in lines]
        # Evicted rows are the OLDEST: task-0 .. task-(total-buffer_size-1)
        assert [u.task_id for u in parsed] == [f"task-{i}" for i in range(expected_evictions)]
        # And the remaining in-memory rows are the most recent buffer_size
        in_memory_ids = [u.task_id for u in tracker.usages]
        assert in_memory_ids == [f"task-{i}" for i in range(expected_evictions, total)]

    def test_no_rotation_file_when_rotation_dir_is_none(self, tmp_path: Path) -> None:
        """Without rotation_dir, eviction drops rows silently (accumulators still carry stats)."""
        buffer_size = 2
        tracker = CostTracker(run_id="run-norot", usage_buffer_size=buffer_size)
        for i in range(buffer_size * 3):
            tracker.record(
                agent_id="agent-a",
                task_id=f"task-{i}",
                model="haiku",
                input_tokens=1,
                output_tokens=1,
            )

        # No JSONL file should have been created anywhere under tmp_path
        assert list(tmp_path.glob("usages-*.jsonl")) == []
        # But the per-agent accumulator still reflects every record
        summaries = tracker.agent_summaries()
        assert len(summaries) == 1
        assert summaries[0].task_count == buffer_size * 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approx(value: float) -> object:
    """Pytest-style approx wrapper kept local so imports stay minimal."""
    import pytest

    return pytest.approx(value, rel=1e-9, abs=1e-9)
