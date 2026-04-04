"""Tests for task budget tracking across compaction events.

Covers:
- TokenBudget pre-compaction usage persistence
- TokenBudget post-compaction effective budget reconciliation
- Total logical spend calculation across multiple compaction windows
- Integration: _try_compact_and_retry records budget when _budget_manager present
- Integration: budget meta-message injected into retry task patch
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.agent_lifecycle import _try_compact_and_retry
from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType
from bernstein.core.token_budget import TokenBudget, TokenBudgetManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_budget(
    task_id: str = "T-1",
    budget: int = 25_000,
    used: int = 0,
    complexity: str = "medium",
) -> TokenBudget:
    b = TokenBudget(task_id=task_id, budget_tokens=budget, used_tokens=used, complexity=complexity)
    b.remaining_tokens = budget - used
    return b


def _make_task(task_id: str = "T-413") -> Task:
    return Task(
        id=task_id,
        title="Implement feature",
        description="Write the code for the new feature module.\n" * 20,
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        meta_messages=[],
    )


def _make_session(task_id: str = "T-413") -> AgentSession:
    return AgentSession(
        id="sess-413",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task_id],
    )


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = []
    return response


def _make_orch(tmp_path: Path, budget_manager: TokenBudgetManager | None = None) -> SimpleNamespace:
    tracker = MagicMock()
    tracker.detect_failure_type.return_value = "context_overflow"
    tracker.throttle_summary.return_value = {}
    tracker.is_throttled.return_value = False

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        max_task_retries=3,
        recovery="restart",
        max_crash_retries=3,
    )
    orch._client = MagicMock()
    orch._client.patch.return_value = _ok_response()
    orch._client.post.return_value = _ok_response()
    orch._client.get.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = tracker
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
    orch._budget_manager = budget_manager
    return orch


def _snapshot(task: Task) -> dict[str, list[Task]]:
    return {"open": [task], "claimed": [], "in_progress": [], "done": []}


# ---------------------------------------------------------------------------
# TokenBudget: pre-compaction persistence
# ---------------------------------------------------------------------------


class TestRecordPreCompaction:
    """record_pre_compaction accumulates historical usage correctly."""

    def test_initial_state_has_no_history(self) -> None:
        b = _make_budget()
        assert b.pre_compact_used == 0
        assert b.compaction_count == 0

    def test_records_single_pre_compact_event(self) -> None:
        b = _make_budget(budget=25_000)
        b.record_pre_compaction(5_000)
        assert b.pre_compact_used == 5_000
        assert b.compaction_count == 1

    def test_accumulates_across_multiple_events(self) -> None:
        b = _make_budget(budget=100_000)
        b.record_pre_compaction(20_000)
        b.record_pre_compaction(15_000)
        assert b.pre_compact_used == 35_000
        assert b.compaction_count == 2

    def test_does_not_alter_used_tokens(self) -> None:
        """record_pre_compaction should not modify used_tokens."""
        b = _make_budget(budget=50_000, used=10_000)
        b.record_pre_compaction(10_000)
        assert b.used_tokens == 10_000


# ---------------------------------------------------------------------------
# TokenBudget: post-compaction reconciliation
# ---------------------------------------------------------------------------


class TestReconcilePostCompaction:
    """reconcile_post_compaction recomputes effective remaining correctly."""

    def test_remaining_reflects_pre_compact_spend(self) -> None:
        b = _make_budget(budget=25_000)
        b.record_pre_compaction(10_000)   # spent 10K before compaction
        b.reconcile_post_compaction()      # compaction happened; current window resets
        # Effective remaining = 25K - 10K (pre) - 0 (current) = 15K
        assert b.effective_remaining() == 15_000

    def test_remaining_accounts_for_current_window_too(self) -> None:
        b = _make_budget(budget=50_000, used=5_000)
        b.record_pre_compaction(20_000)
        b.reconcile_post_compaction()
        # Effective remaining = 50K - 20K (pre) - 5K (current) = 25K
        assert b.effective_remaining() == 25_000

    def test_remaining_clamps_to_zero(self) -> None:
        b = _make_budget(budget=10_000)
        b.record_pre_compaction(8_000)
        b.used_tokens = 5_000
        b.reconcile_post_compaction()
        assert b.effective_remaining() == 0

    def test_reconcile_is_idempotent(self) -> None:
        b = _make_budget(budget=25_000)
        b.record_pre_compaction(10_000)
        b.reconcile_post_compaction()
        first = b.effective_remaining()
        b.reconcile_post_compaction()
        assert b.effective_remaining() == first


# ---------------------------------------------------------------------------
# TokenBudget: total logical spend
# ---------------------------------------------------------------------------


class TestTotalLogicalSpend:
    """total_logical_spend returns cumulative spend across all windows."""

    def test_without_compaction_equals_used_tokens(self) -> None:
        b = _make_budget(budget=25_000, used=3_000)
        assert b.total_logical_spend() == 3_000

    def test_sums_pre_compact_and_current(self) -> None:
        b = _make_budget(budget=100_000, used=15_000)
        b.pre_compact_used = 30_000
        assert b.total_logical_spend() == 45_000

    def test_multiple_compaction_windows(self) -> None:
        b = _make_budget(budget=200_000)

        # Window 1: spent 50K, then compacted
        b.consume(50_000)
        b.record_pre_compaction(50_000)
        b.reconcile_post_compaction()

        # Window 2: agent re-runs, spends 40K, then compacted again
        b.used_tokens = 40_000
        b.record_pre_compaction(40_000)
        b.reconcile_post_compaction()

        # Window 3: agent re-runs, current spend is 20K
        b.used_tokens = 20_000

        # Total logical spend = 50K + 40K + 20K = 110K
        assert b.total_logical_spend() == 110_000
        assert b.effective_remaining() == 90_000


# ---------------------------------------------------------------------------
# TokenBudget: utilization_pct uses total logical spend
# ---------------------------------------------------------------------------


class TestUtilizationPct:
    def test_reflects_pre_compact_spend(self) -> None:
        b = _make_budget(budget=100_000)
        b.pre_compact_used = 60_000
        b.used_tokens = 10_000
        assert b.utilization_pct() == pytest.approx(70.0)

    def test_zero_budget_returns_zero(self) -> None:
        b = TokenBudget(task_id="T", budget_tokens=0)
        assert b.utilization_pct() == 0.0


# ---------------------------------------------------------------------------
# TokenBudgetManager: get_budget returns existing with accumulated history
# ---------------------------------------------------------------------------


class TestTokenBudgetManagerCompaction:
    def test_same_budget_object_returned(self, tmp_path: Path) -> None:
        mgr = TokenBudgetManager(tmp_path)
        b1 = mgr.get_budget("T-1", complexity="medium")
        b1.record_pre_compaction(5_000)
        b2 = mgr.get_budget("T-1", complexity="medium")
        assert b2.pre_compact_used == 5_000

    def test_effective_remaining_visible_via_manager(self, tmp_path: Path) -> None:
        mgr = TokenBudgetManager(tmp_path)
        b = mgr.get_budget("T-2", complexity="medium")  # budget = 25_000
        b.record_pre_compaction(10_000)
        b.reconcile_post_compaction()
        assert mgr.get_budget("T-2").effective_remaining() == 15_000


# ---------------------------------------------------------------------------
# Integration: _try_compact_and_retry records budget when manager is present
# ---------------------------------------------------------------------------


class TestTryCompactAndRetryBudgetIntegration:
    """Verify budget manager is called during reactive compaction."""

    def test_records_pre_compaction_on_budget_manager(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        mgr = TokenBudgetManager(tmp_path)
        orch = _make_orch(tmp_path, budget_manager=mgr)

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task"):
            _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        budget = mgr.get_budget(task.id, complexity="medium")
        assert budget.compaction_count == 1
        assert budget.pre_compact_used > 0

    def test_budget_meta_injected_when_manager_present(self, tmp_path: Path) -> None:
        """When _budget_manager is on orch, retry patch includes a budget nudge."""
        task = _make_task()
        session = _make_session()
        mgr = TokenBudgetManager(tmp_path)
        orch = _make_orch(tmp_path, budget_manager=mgr)

        retry_task_resp = MagicMock()
        retry_task_resp.raise_for_status.return_value = None
        retry_task_resp.json.return_value = [
            {"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"},
        ]
        orch._client.get.return_value = retry_task_resp

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task"):
            _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        patch_calls = [c for c in orch._client.patch.call_args_list if "T-retry-1" in str(c)]
        assert len(patch_calls) == 1
        meta = patch_calls[0][1]["json"]["meta_messages"]
        # Must still contain the compaction nudge
        assert any("CONTEXT COMPACTION" in m for m in meta)
        # Must also contain the budget hint
        assert any("BUDGET EFFECTIVE REMAINING" in m for m in meta)

    def test_no_budget_meta_without_manager(self, tmp_path: Path) -> None:
        """When no _budget_manager is present, no budget nudge is injected."""
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path, budget_manager=None)

        retry_task_resp = MagicMock()
        retry_task_resp.raise_for_status.return_value = None
        retry_task_resp.json.return_value = [
            {"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"},
        ]
        orch._client.get.return_value = retry_task_resp

        with patch("bernstein.core.agent_lifecycle.retry_or_fail_task"):
            _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        patch_calls = [c for c in orch._client.patch.call_args_list if "T-retry-1" in str(c)]
        if patch_calls:
            meta = patch_calls[0][1]["json"]["meta_messages"]
            assert not any("BUDGET EFFECTIVE REMAINING" in m for m in meta)
