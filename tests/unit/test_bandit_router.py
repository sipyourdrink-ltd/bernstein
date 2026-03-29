"""Tests for BanditRouter — contextual bandit model routing.

Covers:
- TaskContext: feature extraction from Task metadata
- BanditPolicy: LinUCB arm selection and reward updates
- BanditRouter: cold-start fallback, warm-up, persistence
- compute_reward: composite quality × cost reward signal
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.models import Complexity, Scope, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
    model: str | None = None,
    effort: str | None = None,
    owned_files: list[str] | None = None,
    estimated_minutes: int = 30,
) -> Task:
    return Task(
        id="t1",
        title="Do something",
        description="desc",
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
        model=model,
        effort=effort,
        owned_files=owned_files or [],
        estimated_minutes=estimated_minutes,
    )


# ---------------------------------------------------------------------------
# TaskContext
# ---------------------------------------------------------------------------


class TestTaskContext:
    def test_from_task_extracts_role(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(role="backend"))
        assert ctx.role == "backend"

    def test_complexity_encoding_low_medium_high(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        assert TaskContext.from_task(_task(complexity=Complexity.LOW)).complexity_tier == 0
        assert TaskContext.from_task(_task(complexity=Complexity.MEDIUM)).complexity_tier == 1
        assert TaskContext.from_task(_task(complexity=Complexity.HIGH)).complexity_tier == 2

    def test_scope_encoding_small_medium_large(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        assert TaskContext.from_task(_task(scope=Scope.SMALL)).scope_tier == 0
        assert TaskContext.from_task(_task(scope=Scope.MEDIUM)).scope_tier == 1
        assert TaskContext.from_task(_task(scope=Scope.LARGE)).scope_tier == 2

    def test_priority_normalised(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        # priority 1 (critical) → 0.0, priority 2 (normal) → 0.5, priority 3 → 1.0
        assert TaskContext.from_task(_task(priority=1)).priority_norm == 0.0
        assert TaskContext.from_task(_task(priority=2)).priority_norm == pytest.approx(0.5)
        assert TaskContext.from_task(_task(priority=3)).priority_norm == 1.0

    def test_file_count_from_owned_files(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(owned_files=["a.py", "b.py", "c.py"]))
        assert ctx.file_count == 3

    def test_estimated_tokens_scales_with_minutes(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx_small = TaskContext.from_task(_task(estimated_minutes=10))
        ctx_large = TaskContext.from_task(_task(estimated_minutes=120))
        assert ctx_large.estimated_tokens > ctx_small.estimated_tokens

    def test_feature_vector_has_correct_length(self) -> None:
        from bernstein.core.bandit_router import FEATURE_DIM, TaskContext

        ctx = TaskContext.from_task(_task())
        assert len(ctx.to_vector()) == FEATURE_DIM

    def test_feature_vector_all_finite(self) -> None:
        import math

        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task())
        for v in ctx.to_vector():
            assert math.isfinite(v), f"Non-finite value in feature vector: {v}"

    def test_high_complexity_produces_higher_first_dim(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        low_vec = TaskContext.from_task(_task(complexity=Complexity.LOW)).to_vector()
        high_vec = TaskContext.from_task(_task(complexity=Complexity.HIGH)).to_vector()
        # First dimension encodes complexity (normalised)
        assert high_vec[0] > low_vec[0]


# ---------------------------------------------------------------------------
# BanditPolicy
# ---------------------------------------------------------------------------


class TestBanditPolicy:
    def test_selects_arm_from_candidates(self) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet", "opus"])
        ctx = TaskContext.from_task(_task())
        arm = policy.select(ctx)
        assert arm in ["haiku", "sonnet", "opus"]

    def test_select_consistent_before_any_updates(self) -> None:
        """Before any updates, policy must not crash and returns a valid arm."""
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        ctx = TaskContext.from_task(_task())
        for _ in range(20):
            assert policy.select(ctx) in ["haiku", "sonnet"]

    def test_update_increments_total_updates(self) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        ctx = TaskContext.from_task(_task())
        policy.update("haiku", ctx, reward=0.9)
        assert policy.total_updates == 1

    def test_update_does_not_raise_for_valid_arm(self) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "opus"])
        ctx = TaskContext.from_task(_task())
        policy.update("haiku", ctx, reward=1.0)
        policy.update("opus", ctx, reward=0.2)

    def test_learned_preference_haiku_over_opus(self) -> None:
        """After many high-reward haiku / low-reward opus updates, haiku wins."""
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "opus"], alpha=0.05)
        ctx = TaskContext.from_task(_task())
        for _ in range(60):
            policy.update("haiku", ctx, reward=1.0)
        for _ in range(60):
            policy.update("opus", ctx, reward=0.0)

        selections = [policy.select(ctx) for _ in range(30)]
        assert selections.count("haiku") > selections.count("opus")

    def test_save_creates_file(self, tmp_path: Path) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        ctx = TaskContext.from_task(_task())
        policy.update("haiku", ctx, reward=0.9)
        policy_file = tmp_path / "policy.json"
        policy.save(policy_file)
        assert policy_file.exists()

    def test_load_restores_total_updates(self, tmp_path: Path) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        ctx = TaskContext.from_task(_task())
        policy.update("haiku", ctx, reward=0.9)
        policy.update("sonnet", ctx, reward=0.5)
        policy_file = tmp_path / "policy.json"
        policy.save(policy_file)

        loaded = BanditPolicy.load(policy_file, arms=["haiku", "sonnet"])
        assert loaded.total_updates == 2

    def test_load_from_missing_file_returns_fresh(self, tmp_path: Path) -> None:
        from bernstein.core.bandit_router import BanditPolicy

        loaded = BanditPolicy.load(tmp_path / "nonexistent.json", arms=["haiku"])
        assert loaded.total_updates == 0
        assert loaded.arms == ["haiku"]


# ---------------------------------------------------------------------------
# BanditRouter
# ---------------------------------------------------------------------------


class TestBanditRouter:
    def test_cold_start_returns_valid_model(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=50)
        decision = router.select(_task())
        assert decision.model in ("haiku", "sonnet", "opus")

    def test_cold_start_sets_from_bandit_false(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=50)
        decision = router.select(_task())
        assert decision.from_bandit is False

    def test_cold_start_total_completions_is_zero(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=10)
        assert router.total_completions == 0

    def test_record_outcome_increments_total_completions(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=10)
        task = _task()
        router.record_outcome(task=task, model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        assert router.total_completions == 1

    def test_after_warmup_from_bandit_is_true(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=2)
        task = _task()
        router.record_outcome(task=task, model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        router.record_outcome(task=task, model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        decision = router.select(task)
        assert decision.from_bandit is True

    def test_decision_has_required_fields(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter()
        decision = router.select(_task())
        assert hasattr(decision, "model")
        assert hasattr(decision, "effort")
        assert hasattr(decision, "from_bandit")
        assert hasattr(decision, "reason")
        assert isinstance(decision.reason, str)
        assert len(decision.reason) > 0

    def test_exploration_rate_zero_before_warmup(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=100)
        assert router.exploration_rate == 0.0

    def test_exploration_rate_positive_after_warmup(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=1)
        router.record_outcome(task=_task(), model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        assert router.exploration_rate > 0.0

    def test_selection_frequency_tracks_counts(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=0)  # immediate bandit mode
        task = _task()
        for _ in range(5):
            router.select(task)
        freq = router.selection_frequency()
        assert sum(freq.values()) == 5

    def test_policy_persists_and_reloads_across_instances(self, tmp_path: Path) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router1 = BanditRouter(warmup_min=1, policy_dir=tmp_path)
        task = _task()
        router1.record_outcome(task=task, model="sonnet", effort="high", cost_usd=0.05, quality_score=1.0)
        router1.save()

        router2 = BanditRouter(warmup_min=1, policy_dir=tmp_path)
        assert router2.total_completions == 1

    def test_is_warmed_up_false_before_threshold(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=5)
        for _ in range(4):
            router.record_outcome(task=_task(), model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        assert router.is_warmed_up is False

    def test_is_warmed_up_true_at_threshold(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=5)
        for _ in range(5):
            router.record_outcome(task=_task(), model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)
        assert router.is_warmed_up is True

    def test_summary_contains_expected_keys(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter()
        summary = router.summary()
        assert "mode" in summary
        assert "total_completions" in summary
        assert "warmup_min" in summary
        assert "exploration_rate" in summary
        assert "selection_frequency" in summary

    def test_high_stakes_task_routes_to_sonnet_or_above(self) -> None:
        """High-stakes tasks must never be routed to haiku during cold-start."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=100)
        for role in ("manager", "architect", "security"):
            decision = router.select(_task(role=role))
            assert decision.model != "haiku", f"role={role} got haiku"

    def test_budget_ceiling_affects_reward(self) -> None:
        """Passing different budget ceilings should produce different rewards."""
        from bernstein.core.bandit_router import compute_reward

        r_tight = compute_reward(quality_score=1.0, cost_usd=0.5, budget_ceiling=1.0)
        r_loose = compute_reward(quality_score=1.0, cost_usd=0.5, budget_ceiling=10.0)
        assert r_loose > r_tight


# ---------------------------------------------------------------------------
# compute_reward
# ---------------------------------------------------------------------------


class TestComputeReward:
    def test_perfect_quality_zero_cost(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        assert compute_reward(quality_score=1.0, cost_usd=0.0, budget_ceiling=1.0) == pytest.approx(1.0)

    def test_zero_quality_any_cost(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        assert compute_reward(quality_score=0.0, cost_usd=0.5, budget_ceiling=1.0) == pytest.approx(0.0)

    def test_reward_decreases_with_cost(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        cheap = compute_reward(quality_score=1.0, cost_usd=0.1, budget_ceiling=1.0)
        expensive = compute_reward(quality_score=1.0, cost_usd=0.9, budget_ceiling=1.0)
        assert cheap > expensive

    def test_reward_clamped_to_zero_one(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        assert 0.0 <= compute_reward(quality_score=1.5, cost_usd=0.0, budget_ceiling=1.0) <= 1.0
        assert 0.0 <= compute_reward(quality_score=0.0, cost_usd=5.0, budget_ceiling=1.0) <= 1.0

    def test_cost_exceeding_ceiling_gives_zero_reward(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        # Cost >= ceiling → normalized_cost = 1.0 → reward = quality * 0 = 0.0
        assert compute_reward(quality_score=1.0, cost_usd=2.0, budget_ceiling=1.0) == pytest.approx(0.0)

    def test_zero_budget_ceiling_does_not_divide_by_zero(self) -> None:
        from bernstein.core.bandit_router import compute_reward

        reward = compute_reward(quality_score=1.0, cost_usd=0.5, budget_ceiling=0.0)
        assert 0.0 <= reward <= 1.0
