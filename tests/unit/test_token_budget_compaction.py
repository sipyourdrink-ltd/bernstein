"""Tests for task budget tracking across compaction events.

Covers:
- TokenBudget.record_pre_compaction persists usage before compaction
- TokenBudget.reconcile_post_compaction recomputes effective budget after compaction
- TokenBudget.total_logical_spend sums pre-compaction + current window
- TokenBudget.effective_remaining returns correct clamped value
- TokenBudget.utilization_pct reflects total logical spend
- Multiple compaction events accumulate correctly
- _try_compact_and_retry integrates with TokenBudgetManager
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.token_budget import (
    DEFAULT_TOKEN_BUDGETS,
    TokenBudget,
    TokenBudgetManager,
    TokenGrowthMonitor,
)

# ---------------------------------------------------------------------------
# TokenBudget: record_pre_compaction
# ---------------------------------------------------------------------------


class TestRecordPreCompaction:
    def test_accumulates_into_pre_compact_used(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000)
        budget.record_pre_compaction(3_000)
        assert budget.pre_compact_used == 3_000
        assert budget.compaction_count == 1

    def test_multiple_events_sum_correctly(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=50_000)
        budget.record_pre_compaction(5_000)
        budget.record_pre_compaction(4_000)
        assert budget.pre_compact_used == 9_000
        assert budget.compaction_count == 2

    def test_zero_tokens_does_not_error(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000)
        budget.record_pre_compaction(0)
        assert budget.pre_compact_used == 0
        assert budget.compaction_count == 1


# ---------------------------------------------------------------------------
# TokenBudget: reconcile_post_compaction
# ---------------------------------------------------------------------------


class TestReconcilePostCompaction:
    def test_remaining_accounts_for_pre_compact_used(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000, used_tokens=500)
        budget.pre_compact_used = 3_000
        budget.reconcile_post_compaction()
        # effective remaining = 10_000 - (3_000 + 500) = 6_500
        assert budget.remaining_tokens == 6_500

    def test_remaining_clamped_at_zero(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=5_000)
        budget.pre_compact_used = 4_800
        budget.used_tokens = 400  # total = 5_200 > budget
        budget.reconcile_post_compaction()
        assert budget.remaining_tokens == 0

    def test_full_roundtrip(self) -> None:
        """Record pre-compaction, run compaction (simulate by resetting used_tokens), reconcile."""
        budget = TokenBudget(task_id="T1", budget_tokens=20_000, used_tokens=8_000)
        # Snapshot usage before compaction
        budget.record_pre_compaction(budget.used_tokens)
        # Compaction happened — the agent's context window resets
        budget.used_tokens = 0
        budget.remaining_tokens = budget.budget_tokens
        # Reconcile: effective remaining = 20_000 - (8_000 + 0)
        budget.reconcile_post_compaction()
        assert budget.remaining_tokens == 12_000


# ---------------------------------------------------------------------------
# TokenBudget: total_logical_spend / effective_remaining / utilization_pct
# ---------------------------------------------------------------------------


class TestLogicalSpend:
    def test_total_logical_spend_is_sum(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000, used_tokens=2_000)
        budget.pre_compact_used = 5_000
        assert budget.total_logical_spend() == 7_000

    def test_effective_remaining_subtracts_total_logical_spend(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000, used_tokens=2_000)
        budget.pre_compact_used = 5_000
        assert budget.effective_remaining() == 3_000

    def test_effective_remaining_clamped_at_zero(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=5_000, used_tokens=4_500)
        budget.pre_compact_used = 1_000
        assert budget.effective_remaining() == 0

    def test_utilization_pct_reflects_total_logical_spend(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=10_000, used_tokens=2_000)
        budget.pre_compact_used = 3_000
        assert budget.utilization_pct() == pytest.approx(50.0)

    def test_utilization_pct_zero_budget_no_div_error(self) -> None:
        budget = TokenBudget(task_id="T1", budget_tokens=0)
        assert budget.utilization_pct() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TokenBudgetManager integration
# ---------------------------------------------------------------------------


class TestTokenBudgetManager:
    def test_get_budget_creates_with_correct_complexity(self, tmp_path: Path) -> None:
        manager = TokenBudgetManager(workdir=tmp_path)
        budget = manager.get_budget("T1", complexity="large")
        assert budget.budget_tokens == DEFAULT_TOKEN_BUDGETS["large"]
        assert budget.complexity == "large"

    def test_get_budget_returns_same_object(self, tmp_path: Path) -> None:
        manager = TokenBudgetManager(workdir=tmp_path)
        b1 = manager.get_budget("T1")
        b2 = manager.get_budget("T1")
        assert b1 is b2

    def test_budget_survives_compaction_cycle(self, tmp_path: Path) -> None:
        """Full compaction cycle: spend tokens, snapshot, compact, reconcile."""
        manager = TokenBudgetManager(workdir=tmp_path)
        budget = manager.get_budget("T1", complexity="medium")

        # Simulate spending 5_000 tokens on first session
        budget.consume(5_000)
        assert budget.used_tokens == 5_000

        # Pre-compaction snapshot
        budget.record_pre_compaction(budget.used_tokens)

        # Compaction resets current window
        budget.used_tokens = 0
        budget.reconcile_post_compaction()

        # After compaction: pre_compact_used=5000, used=0, remaining=20_000
        assert budget.pre_compact_used == 5_000
        assert budget.effective_remaining() == DEFAULT_TOKEN_BUDGETS["medium"] - 5_000

    def test_get_summary_includes_utilization(self, tmp_path: Path) -> None:
        manager = TokenBudgetManager(workdir=tmp_path)
        budget = manager.get_budget("T1", complexity="small")
        budget.consume(1_000)
        summary = manager.get_summary()
        assert "T1" in summary["task_budgets"]
        assert summary["task_budgets"]["T1"]["utilization_pct"] > 0


# ---------------------------------------------------------------------------
# _try_compact_and_retry: budget manager integration
# ---------------------------------------------------------------------------


def _make_task_for_budget(task_id: str = "BT-1") -> tuple[Any, Any]:
    from bernstein.core.models import (
        AgentSession,
        Complexity,
        ModelConfig,
        Scope,
        Task,
        TaskStatus,
        TaskType,
    )

    task = Task(
        id=task_id,
        title="Budget task",
        description="x" * 4_000,  # ~1000 tokens
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        meta_messages=[],
    )
    session = AgentSession(
        id="sess-budget",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task_id],
    )
    return task, session


def _make_orch_with_budget(tmp_path: Path) -> SimpleNamespace:
    manager = TokenBudgetManager(workdir=tmp_path)

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        max_task_retries=3,
        recovery="restart",
        max_crash_retries=3,
    )
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = []
    orch._client = MagicMock()
    orch._client.get.return_value = mock_resp
    orch._client.post.return_value = mock_resp
    orch._client.patch.return_value = mock_resp
    orch._workdir = tmp_path
    orch._rate_limit_tracker = MagicMock()
    orch._router = None
    orch._cascade_manager = MagicMock()
    orch._cascade_manager.find_fallback.return_value = None
    orch._retried_task_ids: set[str] = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._crash_counts: dict[str, int] = {}
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    orch._plugin_manager = None
    orch._budget_manager = manager
    return orch


class TestTryCompactAndRetryBudgetIntegration:
    """_try_compact_and_retry calls record_pre_compaction and reconcile_post_compaction."""

    def test_records_pre_compaction_on_budget(self, tmp_path: Path) -> None:
        task, session = _make_task_for_budget()  # type: ignore[misc]
        orch = _make_orch_with_budget(tmp_path)

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task"):
            from bernstein.core.agent_lifecycle import _try_compact_and_retry

            _try_compact_and_retry(
                orch=orch,
                task=task,  # type: ignore[arg-type]
                task_id=task.id,  # type: ignore[union-attr]
                session=session,  # type: ignore[arg-type]
                tasks_snapshot={"open": [task], "claimed": [], "in_progress": [], "done": []},  # type: ignore[list-item]
                fallback_model=None,
            )

        budget = orch._budget_manager.get_budget(task.id)  # type: ignore[union-attr]
        assert budget.pre_compact_used > 0
        assert budget.compaction_count == 1

    def test_effective_remaining_decreases_after_compaction(self, tmp_path: Path) -> None:
        task, session = _make_task_for_budget()  # type: ignore[misc]
        orch = _make_orch_with_budget(tmp_path)

        # Pre-populate some prior spend so we can detect the change
        prior_budget = orch._budget_manager.get_budget(task.id, complexity="medium")  # type: ignore[union-attr]
        prior_budget.consume(5_000)

        budget_before = prior_budget.effective_remaining()

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task"):
            from bernstein.core.agent_lifecycle import _try_compact_and_retry

            _try_compact_and_retry(
                orch=orch,
                task=task,  # type: ignore[arg-type]
                task_id=task.id,  # type: ignore[union-attr]
                session=session,  # type: ignore[arg-type]
                tasks_snapshot={"open": [task], "claimed": [], "in_progress": [], "done": []},  # type: ignore[list-item]
                fallback_model=None,
            )

        budget_after = orch._budget_manager.get_budget(task.id).effective_remaining()  # type: ignore[union-attr]
        assert budget_after < budget_before

    def test_budget_not_required_works_without_budget_manager(self, tmp_path: Path) -> None:
        """_try_compact_and_retry degrades gracefully when _budget_manager is absent."""
        task, session = _make_task_for_budget()  # type: ignore[misc]
        orch = _make_orch_with_budget(tmp_path)
        orch._budget_manager = None  # remove budget manager

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task") as mock_retry:
            from bernstein.core.agent_lifecycle import _try_compact_and_retry

            result = _try_compact_and_retry(
                orch=orch,
                task=task,  # type: ignore[arg-type]
                task_id=task.id,  # type: ignore[union-attr]
                session=session,  # type: ignore[arg-type]
                tasks_snapshot={"open": [task], "claimed": [], "in_progress": [], "done": []},  # type: ignore[list-item]
                fallback_model=None,
            )

        assert result is True
        mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# TokenGrowthMonitor: compaction success/failure integration
# ---------------------------------------------------------------------------


class TestGrowthMonitorCompactionState:
    def test_record_compaction_failure_increments_count(self) -> None:
        monitor = TokenGrowthMonitor(session_id="S1")
        monitor.record_compaction_failure()
        monitor.record_compaction_failure()
        assert monitor.compaction_fail_count == 2

    def test_record_compaction_success_resets_count(self) -> None:
        monitor = TokenGrowthMonitor(session_id="S1")
        monitor.record_compaction_failure()
        monitor.record_compaction_failure()
        monitor.record_compaction_success()
        assert monitor.compaction_fail_count == 0
        assert monitor.intervention_triggered is False

    def test_circuit_breaker_opens_after_three_failures(self) -> None:
        monitor = TokenGrowthMonitor(session_id="S1")
        monitor.intervention_triggered = True
        for _ in range(3):
            monitor.record_compaction_failure()
        assert monitor.should_compact() is False
