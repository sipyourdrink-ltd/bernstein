"""Tests for BanditRouter — contextual bandit model routing.

Covers:
- TaskContext: feature extraction from Task metadata
- BanditPolicy: LinUCB arm selection and reward updates
- BanditRouter: cold-start fallback, warm-up, persistence
- compute_reward: composite quality × cost reward signal
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import Complexity, Scope, Task, TaskType

if TYPE_CHECKING:
    from pathlib import Path

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
    task_type: TaskType = TaskType.STANDARD,
    metadata: dict[str, object] | None = None,
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
        task_type=task_type,
        metadata=metadata or {},
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
        assert TaskContext.from_task(_task(priority=1)).priority_norm == pytest.approx(0.0)
        assert TaskContext.from_task(_task(priority=2)).priority_norm == pytest.approx(0.5)
        assert TaskContext.from_task(_task(priority=3)).priority_norm == pytest.approx(1.0)

    def test_repo_size_falls_back_to_owned_file_count(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(owned_files=["a.py", "b.py", "c.py"]))
        assert ctx.repo_size == 3

    def test_repo_size_uses_metadata_when_available(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(owned_files=["a.py"], metadata={"repo_size": 1234}))
        assert ctx.repo_size == 1234

    def test_language_inferred_from_owned_files(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(owned_files=["src/app.py", "tests/test_app.py", "README.md"]))
        assert ctx.language == "python"

    def test_task_type_included_in_context(self) -> None:
        from bernstein.core.bandit_router import TaskContext

        ctx = TaskContext.from_task(_task(task_type=TaskType.FIX))
        assert ctx.task_type == TaskType.FIX.value

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
    def test_default_alpha_is_conservative(self) -> None:
        from bernstein.core.bandit_router import BanditPolicy

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        assert policy.alpha == pytest.approx(0.3)

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

    def test_score_breakdown_contains_exploit_explore_and_total(self) -> None:
        from bernstein.core.bandit_router import BanditPolicy, TaskContext

        policy = BanditPolicy(arms=["haiku", "sonnet"])
        ctx = TaskContext.from_task(_task())
        scores = policy.score(ctx)
        assert scores[0].arm in {"haiku", "sonnet"}
        assert scores[0].total == pytest.approx(scores[0].exploit + scores[0].explore)

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

    def test_load_resets_incompatible_feature_schema(self, tmp_path: Path) -> None:
        import json

        from bernstein.core.bandit_router import BanditPolicy

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(
            json.dumps(
                {
                    "arms": ["haiku"],
                    "alpha": 1.0,
                    "feature_schema_version": -1,
                    "feature_dim": 1,
                    "total_updates": 10,
                    "A": {"haiku": [[1.0]]},
                    "b": {"haiku": [1.0]},
                }
            )
        )
        loaded = BanditPolicy.load(policy_file, arms=["haiku"])
        assert loaded.total_updates == 0
        assert loaded.alpha == pytest.approx(0.3)

    def test_sherman_morrison_matches_full_inverse_update(self) -> None:
        from bernstein.core.bandit_router import TaskContext, _identity, _inv, _sherman_morrison_update

        ctx = TaskContext.from_task(_task())
        x = ctx.to_vector()
        identity = _identity(len(x))
        updated = _sherman_morrison_update(identity, x)

        expected = _inv([[identity[row][col] + (x[row] * x[col]) for col in range(len(x))] for row in range(len(x))])
        for row_index, row in enumerate(updated):
            for col_index, value in enumerate(row):
                assert value == pytest.approx(expected[row_index][col_index], abs=1e-8)

    def test_load_converts_legacy_a_matrix_and_rewrites_new_format(self, tmp_path: Path) -> None:
        import json

        from bernstein.core.bandit_router import FEATURE_DIM, BanditPolicy

        identity = [[1.0 if i == j else 0.0 for j in range(FEATURE_DIM)] for i in range(FEATURE_DIM)]
        vector = [0.0] * FEATURE_DIM
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(
            json.dumps(
                {
                    "arms": ["haiku"],
                    "alpha": 0.3,
                    "feature_schema_version": 2,
                    "feature_dim": FEATURE_DIM,
                    "total_updates": 3,
                    "A": {"haiku": identity},
                    "b": {"haiku": vector},
                }
            ),
            encoding="utf-8",
        )

        loaded = BanditPolicy.load(policy_file, arms=["haiku"])

        assert loaded.total_updates == 3
        rewritten = json.loads(policy_file.read_text(encoding="utf-8"))
        assert rewritten["matrix_storage"] == "A_inv"
        assert "A_inv" in rewritten
        assert "A" not in rewritten


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
        assert router.exploration_rate == pytest.approx(0.0)

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
        assert "exploration_stats" in summary
        assert "shadow_stats" in summary

    def test_high_stakes_task_routes_to_sonnet_or_above(self) -> None:
        """High-stakes tasks must never be routed to haiku during cold-start."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=100)
        for role in ("manager", "architect", "security"):
            decision = router.select(_task(role=role))
            assert decision.model != "haiku", f"role={role} got haiku"

    def test_high_stakes_guardrail_applies_after_warmup(self) -> None:
        """Bandit mode must still keep high-stakes tasks off haiku."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=1)
        normal_task = _task()
        router.record_outcome(task=normal_task, model="haiku", effort="low", cost_usd=0.0, quality_score=1.0)
        decision = router.select(_task(priority=1))
        assert decision.model != "haiku"
        assert decision.from_bandit is False

    def test_bandit_reason_includes_score_breakdown(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=1)
        task = _task()
        router.record_outcome(task=task, model="sonnet", effort="high", cost_usd=0.0, quality_score=1.0)
        decision = router.select(task)
        assert "exploit=" in decision.reason
        assert "explore=" in decision.reason
        assert "total=" in decision.reason

    def test_bandit_selection_is_deterministic_after_warmup(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=1)
        task = _task()
        router.record_outcome(task=task, model="sonnet", effort="high", cost_usd=0.0, quality_score=1.0)
        selections = [router.select(task).model for _ in range(5)]
        assert len(set(selections)) == 1

    def test_exploration_stats_window_is_bounded(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=1)
        task = _task()
        router.record_outcome(task=task, model="haiku", effort="low", cost_usd=0.01, quality_score=1.0)

        for _ in range(130):
            router.select(task)

        stats = router.summary()["exploration_stats"]
        assert stats["haiku"]["samples"] <= 100

    def test_shadow_outcomes_aggregate_observed_reward_only(self, tmp_path: Path) -> None:
        import json

        from bernstein.core.bandit_router import BanditRouter, BanditRoutingDecision, compute_reward

        router = BanditRouter(warmup_min=0, policy_dir=tmp_path)
        task = _task()
        router.record_shadow_decision(
            task=task,
            decision=BanditRoutingDecision(
                model="sonnet",
                effort="high",
                from_bandit=True,
                reason="test shadow",
            ),
            executed_model="haiku",
            executed_effort="low",
        )

        router.record_outcome(
            task=task, model="haiku", effort="low", cost_usd=0.2, quality_score=1.0, budget_ceiling=1.0
        )
        router.save()

        shadow_stats = router.summary()["shadow_stats"]
        expected_reward = compute_reward(quality_score=1.0, cost_usd=0.2, budget_ceiling=1.0)
        assert shadow_stats["matched_outcomes"] == 1
        assert shadow_stats["disagreement_count"] == 1
        assert shadow_stats["avg_executed_reward_when_disagree"] == pytest.approx(expected_reward)
        assert shadow_stats["pending_outcomes"] == 0

        outcome_path = tmp_path / "shadow_outcomes.jsonl"
        payload = json.loads(outcome_path.read_text(encoding="utf-8").splitlines()[0])
        assert payload["observed_reward"] == pytest.approx(expected_reward, abs=1e-6)
        assert "uplift" not in payload

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


# ---------------------------------------------------------------------------
# EffortBandit (audit-111)
# ---------------------------------------------------------------------------


class TestEffortBandit:
    """UCB1 effort bandit learns optimal effort per (task_type, model) key."""

    def test_cold_start_pulls_each_arm_at_least_once(self) -> None:
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit()
        seen: set[str] = set()
        for _ in range(len(bandit.arms)):
            arm = bandit.select("standard", "sonnet")
            seen.add(arm)
            # Simulate observing reward 0.5 so arm counts advance.
            bandit.update("standard", "sonnet", arm, 0.5)
        assert seen == set(bandit.arms)

    def test_not_warmed_up_below_threshold(self) -> None:
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit(min_pulls_per_key=6)
        for _ in range(5):
            bandit.update("standard", "sonnet", "high", 1.0)
        assert bandit.is_warmed_up("standard", "sonnet") is False

    def test_warmed_up_at_threshold(self) -> None:
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit(min_pulls_per_key=6)
        for _ in range(6):
            bandit.update("standard", "sonnet", "high", 1.0)
        assert bandit.is_warmed_up("standard", "sonnet") is True

    def test_ignores_unknown_effort_arm(self) -> None:
        """Unknown effort strings (e.g. 'medium') must not poison counters."""
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit()
        bandit.update("standard", "sonnet", "medium", 1.0)
        assert bandit.total_pulls("standard", "sonnet") == 0

    def test_convergence_prefers_high_reward_effort(self) -> None:
        """Feed synthetic rewards: 'high' is best for sonnet — bandit learns it."""
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit(c=0.2, min_pulls_per_key=3)
        ground_truth = {"low": 0.2, "high": 0.9, "max": 0.4}

        for _ in range(200):
            arm = bandit.select("standard", "sonnet")
            bandit.update("standard", "sonnet", arm, ground_truth[arm])

        final_selections = [bandit.select("standard", "sonnet") for _ in range(50)]
        # Ground-truth best arm ("high") should dominate steady-state selections.
        assert final_selections.count("high") > final_selections.count("low")
        assert final_selections.count("high") > final_selections.count("max")
        # Mean rewards should reflect the ground truth ordering.
        means = bandit.mean_rewards("standard", "sonnet")
        assert means["high"] > means["max"] > means["low"]

    def test_keys_are_isolated_per_task_type_and_model(self) -> None:
        """Rewards for one (task_type, model) key must not leak into another."""
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit()
        # Fix bug tasks: max is best.
        fix_truth = {"low": 0.1, "high": 0.3, "max": 0.95}
        # Standard tasks: low is best.
        std_truth = {"low": 0.9, "high": 0.3, "max": 0.1}

        for _ in range(200):
            arm_fix = bandit.select("fix", "sonnet")
            bandit.update("fix", "sonnet", arm_fix, fix_truth[arm_fix])
            arm_std = bandit.select("standard", "sonnet")
            bandit.update("standard", "sonnet", arm_std, std_truth[arm_std])

        fix_selections = [bandit.select("fix", "sonnet") for _ in range(40)]
        std_selections = [bandit.select("standard", "sonnet") for _ in range(40)]
        assert fix_selections.count("max") >= fix_selections.count("low")
        assert std_selections.count("low") >= std_selections.count("max")

    def test_roundtrip_to_dict_from_dict(self) -> None:
        from bernstein.core.bandit_router import EffortBandit

        bandit = EffortBandit()
        for _ in range(4):
            bandit.update("standard", "haiku", "low", 0.8)
            bandit.update("standard", "haiku", "high", 0.3)

        restored = EffortBandit.from_dict(bandit.to_dict())
        assert restored.total_pulls("standard", "haiku") == 8
        means = restored.mean_rewards("standard", "haiku")
        assert means["low"] == pytest.approx(0.8)
        assert means["high"] == pytest.approx(0.3)

    def test_from_dict_tolerates_malformed_payload(self) -> None:
        from bernstein.core.bandit_router import EffortBandit

        assert isinstance(EffortBandit.from_dict(None), EffortBandit)
        assert isinstance(EffortBandit.from_dict({"pulls": "nope"}), EffortBandit)


# ---------------------------------------------------------------------------
# BanditRouter effort-learning wiring (audit-111)
# ---------------------------------------------------------------------------


class TestBanditRouterEffortLearning:
    """Router-level wiring: effort rewards flow into EffortBandit, selection
    prefers learned arm once warmed up."""

    def test_record_outcome_feeds_effort_bandit(self) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=0)
        task = _task(task_type=TaskType.STANDARD)
        router.record_outcome(task=task, model="sonnet", effort="high", cost_usd=0.0, quality_score=1.0)
        summary = router.summary()
        pulls = summary["effort_bandit"]["pulls"]["standard|sonnet"]
        assert pulls["high"] == 1

    def test_select_uses_learned_effort_after_warmup(self) -> None:
        """Once a (task_type, model) key is warmed up, effort comes from bandit."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=0)
        task = _task(task_type=TaskType.STANDARD)

        # Ground truth: max is the best effort for (standard, sonnet).
        ground_truth = {"low": 0.1, "high": 0.3, "max": 0.95}
        # Feed enough outcomes so the effort bandit converges and model-arm
        # learning prefers "sonnet".
        for _ in range(80):
            for effort, reward in ground_truth.items():
                router.record_outcome(
                    task=task,
                    model="sonnet",
                    effort=effort,
                    cost_usd=0.0,
                    quality_score=reward,
                )

        # Force the model arm to "sonnet" for deterministic effort inspection:
        # because we can't directly steer the LinUCB model choice here, we
        # instead check the effort bandit's select directly (this mirrors
        # what the router does internally).
        chosen_effort = router._effort_bandit.select("standard", "sonnet")
        assert chosen_effort == "max"

    def test_explicit_task_effort_overrides_bandit(self) -> None:
        """Manager-specified task.effort must always win, even with a hot bandit."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=0)
        # Saturate the bandit toward "max".
        task = _task(task_type=TaskType.STANDARD, effort=None)
        for _ in range(50):
            router.record_outcome(task=task, model="sonnet", effort="max", cost_usd=0.0, quality_score=1.0)

        override_task = _task(task_type=TaskType.STANDARD, effort="low")
        decision = router.select(override_task)
        assert decision.effort == "low"

    def test_effort_state_persists_across_router_instances(self, tmp_path: Path) -> None:
        from bernstein.core.bandit_router import BanditRouter

        router1 = BanditRouter(warmup_min=0, policy_dir=tmp_path)
        task = _task(task_type=TaskType.STANDARD)
        for _ in range(5):
            router1.record_outcome(task=task, model="sonnet", effort="high", cost_usd=0.0, quality_score=1.0)
        router1.save()

        router2 = BanditRouter(warmup_min=0, policy_dir=tmp_path)
        summary = router2.summary()
        pulls = summary["effort_bandit"]["pulls"]["standard|sonnet"]
        assert pulls["high"] == 5

    def test_cold_start_effort_uses_static_heuristic(self) -> None:
        """Below the per-key threshold, router falls back to the static heuristic."""
        from bernstein.core.bandit_router import BanditRouter

        router = BanditRouter(warmup_min=100)  # stay in cold-start forever
        task = _task(task_type=TaskType.STANDARD)
        decision = router.select(task)
        # No pulls recorded → fallback heuristic for the selected model.
        assert decision.effort in {"low", "high", "max"}
        # Effort bandit must not have been consulted (no pulls yet).
        assert router._effort_bandit.total_pulls("standard", decision.model) == 0
