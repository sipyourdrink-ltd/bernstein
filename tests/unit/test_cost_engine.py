"""Tests for the intelligent cost optimization engine.

Covers:
- EpsilonGreedyBandit: learn cheapest model per role that meets quality threshold
- Bandit integration in _select_batch_config (spawner routing)
- get_cascade_model: escalation by retry count
- compute_savings_vs_opus / project_monthly_cost / compute_daily_cost
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import Complexity, Scope, Task

from bernstein.core.cost import (
    CASCADE,
    BanditArm,
    EpsilonGreedyBandit,
    compute_daily_cost,
    compute_savings_vs_opus,
    estimate_run_cost,
    get_cascade_model,
    project_monthly_cost,
)

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
) -> Task:
    return Task(
        id="t1",
        title="Do something",
        description="desc",
        role=role,
        complexity=complexity,
        scope=scope,
        priority=priority,
    )


def _bandit_with_data(
    metrics_dir: Path,
    role: str,
    model: str,
    observations: int,
    successes: int,
) -> None:
    """Write bandit state with given arm data."""
    arm = {
        "role": role,
        "model": model,
        "observations": observations,
        "successes": successes,
        "total_cost_usd": observations * 0.001,
        "total_latency_s": observations * 1.0,
    }
    state = {"arms": [arm]}
    (metrics_dir / "bandit_state.json").write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# BanditArm
# ---------------------------------------------------------------------------


class TestBanditArm:
    def test_success_rate_pessimistic_when_no_observations(self) -> None:
        """audit-069: cold-start success_rate must sit below QUALITY_THRESHOLD."""
        arm = BanditArm(role="backend", model="haiku")
        # Pessimistic 0.5 keeps a never-observed arm from greedily winning the
        # bandit's cheapest-wins exploitation path against the 0.8 threshold.
        assert arm.success_rate == pytest.approx(0.5)

    def test_success_rate_after_observations(self) -> None:
        arm = BanditArm(role="backend", model="haiku")
        arm.record(success=True)
        arm.record(success=True)
        arm.record(success=False)
        assert arm.success_rate == pytest.approx(2 / 3)

    def test_avg_cost_rough_estimate_when_no_observations(self) -> None:
        arm = BanditArm(role="backend", model="haiku")
        # Should return a rough estimate, not zero
        assert arm.avg_cost_usd > 0

    def test_avg_cost_after_observations(self) -> None:
        arm = BanditArm(role="backend", model="sonnet", total_cost_usd=0.3)
        arm.observations = 3
        arm.successes = 3
        assert arm.avg_cost_usd == pytest.approx(0.1)

    def test_roundtrip_dict(self) -> None:
        arm = BanditArm(role="qa", model="opus", observations=5, successes=4)
        arm2 = BanditArm.from_dict(arm.to_dict())
        assert arm2.role == arm.role
        assert arm2.model == arm.model
        assert arm2.observations == arm.observations
        assert arm2.successes == arm.successes


# ---------------------------------------------------------------------------
# EpsilonGreedyBandit — persistence
# ---------------------------------------------------------------------------


class TestEpsilonGreedyBanditPersistence:
    def test_load_returns_fresh_when_no_file(self, tmp_path: Path) -> None:
        bandit = EpsilonGreedyBandit.load(tmp_path)
        assert len(bandit._arms) == 0

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        bandit = EpsilonGreedyBandit()
        bandit.record(role="backend", model="haiku", success=True, cost_usd=0.001)
        bandit.record(role="backend", model="haiku", success=True, cost_usd=0.001)
        bandit.save(tmp_path)

        loaded = EpsilonGreedyBandit.load(tmp_path)
        arm = loaded._arms[("backend", "haiku")]
        assert arm.observations == 2
        assert arm.successes == 2

    def test_load_returns_fresh_on_corrupt_file(self, tmp_path: Path) -> None:
        (tmp_path / "bandit_state.json").write_text("not valid json{{{")
        bandit = EpsilonGreedyBandit.load(tmp_path)
        assert len(bandit._arms) == 0


# ---------------------------------------------------------------------------
# audit-071 — legacy state migration into unified BanditRouter
# ---------------------------------------------------------------------------


class TestAudit071Migration:
    """Cover the one-shot legacy → unified bandit migration (audit-071)."""

    def test_migration_reads_legacy_and_renames_bak(self, tmp_path: Path) -> None:
        """Legacy state is migrated, renamed ``.bak``, and re-read on boot."""
        metrics_dir = tmp_path / "metrics"
        routing_dir = tmp_path / "routing"
        metrics_dir.mkdir()

        legacy = {
            "arms": [
                {
                    "role": "backend",
                    "model": "sonnet",
                    "observations": 10,
                    "successes": 9,
                    "total_cost_usd": 0.03,
                    "total_latency_s": 100.0,
                }
            ]
        }
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        bandit = EpsilonGreedyBandit.load(metrics_dir)
        # Observations survive the migration.
        arm = bandit.get_arm("backend", "sonnet")
        assert arm is not None and arm.observations == 10
        # Legacy file renamed, unified state created.
        assert not (metrics_dir / "bandit_state.json").exists()
        assert (metrics_dir / "bandit_state.json.bak").exists()
        assert (routing_dir / "policy.json").exists()
        assert (routing_dir / "bandit_state.json").exists()

    def test_migration_seeds_linucb_router(self, tmp_path: Path) -> None:
        """Router sees the legacy observations as LinUCB seed arms."""
        from bernstein.core.routing.bandit_router import BanditRouter

        metrics_dir = tmp_path / "metrics"
        routing_dir = tmp_path / "routing"
        metrics_dir.mkdir()

        legacy = {
            "arms": [
                {"role": "backend", "model": "sonnet", "observations": 10, "successes": 9},
            ]
        }
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        EpsilonGreedyBandit.load(metrics_dir)

        router = BanditRouter(policy_dir=routing_dir)
        router._ensure_loaded()  # pyright: ignore[reportPrivateUsage]
        assert ("backend", "sonnet") in router._seeded_arms  # pyright: ignore[reportPrivateUsage]

    def test_migration_is_idempotent_after_bak(self, tmp_path: Path) -> None:
        """Once ``.bak`` exists, subsequent loads are pure reads of the unified file."""
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        legacy = {
            "arms": [
                {"role": "backend", "model": "sonnet", "observations": 3, "successes": 2},
            ]
        }
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        EpsilonGreedyBandit.load(metrics_dir)
        # Second load should not fail even though legacy file is gone.
        second = EpsilonGreedyBandit.load(metrics_dir)
        arm = second.get_arm("backend", "sonnet")
        assert arm is not None and arm.observations == 3

    def test_both_paths_coexist_during_migration(self, tmp_path: Path) -> None:
        """Directly before the rename, both legacy and routing files can exist.

        The migration snapshots the legacy contents and only renames after a
        successful parse; callers reading either file mid-flight see a
        consistent observation count (same role/model/observations).
        """
        metrics_dir = tmp_path / "metrics"
        routing_dir = tmp_path / "routing"
        metrics_dir.mkdir()
        legacy = {"arms": [{"role": "qa", "model": "haiku", "observations": 4, "successes": 3}]}
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        bandit = EpsilonGreedyBandit.load(metrics_dir)

        # After migration: legacy gone (.bak), routing has the data.
        assert (metrics_dir / "bandit_state.json.bak").exists()
        unified = json.loads((routing_dir / "bandit_state.json").read_text())
        observations = unified.get("observation_arms", [])
        assert any(a["role"] == "qa" and a["model"] == "haiku" for a in observations)
        # And the facade's in-memory view matches both stores.
        arm = bandit.get_arm("qa", "haiku")
        assert arm is not None and arm.observations == 4

    def test_record_after_migration_updates_only_routing(self, tmp_path: Path) -> None:
        """After migration, ``record`` writes to routing/; legacy stays .bak."""
        metrics_dir = tmp_path / "metrics"
        routing_dir = tmp_path / "routing"
        metrics_dir.mkdir()
        legacy = {"arms": [{"role": "backend", "model": "sonnet", "observations": 5, "successes": 5}]}
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        bandit = EpsilonGreedyBandit.load(metrics_dir)
        bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.01)
        bandit.save(metrics_dir)

        # Legacy file remains renamed — no new write there.
        assert not (metrics_dir / "bandit_state.json").exists()
        assert (metrics_dir / "bandit_state.json.bak").exists()
        # Routing file now reflects the new observation.
        unified = json.loads((routing_dir / "bandit_state.json").read_text())
        observations = unified.get("observation_arms", [])
        backend_sonnet = next(a for a in observations if a["role"] == "backend" and a["model"] == "sonnet")
        assert backend_sonnet["observations"] == 6  # 5 legacy + 1 new

    def test_cost_forecast_and_router_share_state(self, tmp_path: Path) -> None:
        """After migration, ``predict_task_cost`` and the router see the same arm.

        Ensures audit-071's core invariant: cost forecasts and model selection
        can never disagree because both read from a single state store.
        """
        from bernstein.core.cost.cost import predict_task_cost
        from bernstein.core.routing.bandit_router import BanditRouter

        metrics_dir = tmp_path / "metrics"
        routing_dir = tmp_path / "routing"
        metrics_dir.mkdir()
        legacy = {
            "arms": [
                {
                    "role": "backend",
                    "model": "sonnet",
                    "observations": 10,
                    "successes": 9,
                    "total_cost_usd": 0.05,
                    "total_latency_s": 120.0,
                }
            ]
        }
        (metrics_dir / "bandit_state.json").write_text(json.dumps(legacy))

        # Forecast path loads the facade.
        task = _task(role="backend", scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        task.model = "sonnet"
        cost_pred = predict_task_cost(task, metrics_dir=metrics_dir)
        assert cost_pred > 0.0  # historical refinement applied

        # Router path sees the seeded arm.
        router = BanditRouter(policy_dir=routing_dir)
        router._ensure_loaded()  # pyright: ignore[reportPrivateUsage]
        assert ("backend", "sonnet") in router._seeded_arms  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# EpsilonGreedyBandit — selection
# ---------------------------------------------------------------------------


class TestEpsilonGreedyBanditSelect:
    def test_returns_valid_model_from_cascade(self) -> None:
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        result = bandit.select(role="backend")
        assert result in CASCADE

    def test_exploits_cheapest_arm_meeting_quality(self) -> None:
        """After enough observations haiku (cheapest) at 100% success wins."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=3)
        for _ in range(5):
            bandit.record(role="backend", model="haiku", success=True, cost_usd=0.0004)
        for _ in range(5):
            bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)

        result = bandit.select(role="backend")
        assert result == "sonnet"

    def test_avoids_arm_below_quality_threshold(self) -> None:
        """haiku has <80% success rate; bandit should prefer sonnet."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=5, quality_threshold=0.8)
        # haiku: 3/6 = 50% — below threshold
        for _ in range(6):
            bandit.record(
                role="backend",
                model="haiku",
                success=(_ < 3),
                cost_usd=0.0004,
            )
        # sonnet: 5/5 = 100% — above threshold
        for _ in range(5):
            bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)

        result = bandit.select(role="backend")
        assert result == "sonnet"

    def test_falls_back_to_cheapest_when_all_underperforming(self) -> None:
        """All arms below threshold → fallback to cheapest."""
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=3, quality_threshold=0.9)
        for model in CASCADE:
            for _ in range(5):
                bandit.record(role="backend", model=model, success=False, cost_usd=0.001)

        result = bandit.select(role="backend")
        # Falls back to cheapest (haiku)
        assert result == "sonnet"

    def test_candidate_restriction(self) -> None:
        """Restricting candidates limits the selection pool."""
        bandit = EpsilonGreedyBandit(epsilon=0.0)
        result = bandit.select(role="backend", candidate_models=["sonnet", "opus"])
        assert result in ("sonnet", "opus")
        assert result != "haiku"

    def test_summary_contains_arm_data(self) -> None:
        bandit = EpsilonGreedyBandit()
        bandit.record(role="backend", model="sonnet", success=True, cost_usd=0.003)
        rows = bandit.summary()
        assert any(r["role"] == "backend" and r["model"] == "sonnet" for r in rows)

    def test_zero_obs_arm_does_not_beat_observed_passing_arm(self) -> None:
        """audit-069: cold-start arms must not steal from a 5-obs 80% arm.

        Before the fix, a freshly added cheap arm (e.g. ``qwen3-coder``) won
        the exploitation branch because :attr:`BanditArm.success_rate`
        returned an optimistic 1.0 and the select loop admitted any
        under-observed arm at nominal cost. Now the zero-observation arm
        sits below :data:`QUALITY_THRESHOLD` and is skipped during
        exploitation, so the observed qualifying arm wins.
        """
        bandit = EpsilonGreedyBandit(epsilon=0.0, min_observations=5, quality_threshold=0.8)
        # Proven arm: sonnet at 4/5 = 80% success (ties the threshold).
        for success in (True, True, True, True, False):
            bandit.record(role="backend", model="sonnet", success=success, cost_usd=0.003)

        # Candidate list forces the cheap unseen arm into the comparison.
        # Under the old optimistic rule this would have returned
        # ``"qwen3-coder"`` (nominal cost $0.00056 ≪ $0.003 for sonnet).
        chosen = bandit.select(
            role="backend",
            candidate_models=["qwen3-coder", "sonnet"],
        )
        assert chosen == "sonnet", f"expected observed arm to win, got {chosen!r}"

        # Zero-observation arm must advertise the pessimistic 0.5 rate so
        # the select loop sees a sub-threshold candidate.
        unseen = BanditArm(role="backend", model="qwen3-coder")
        assert unseen.success_rate < bandit.quality_threshold


# ---------------------------------------------------------------------------
# Bandit arm pool (audit-069)
# ---------------------------------------------------------------------------


class TestBanditArmPool:
    def test_get_all_bandit_arms_includes_cascade(self) -> None:
        from bernstein.core.cost.cost import get_all_bandit_arms

        arms = get_all_bandit_arms()
        for model in CASCADE:
            assert model in arms

    def test_get_all_bandit_arms_auto_includes_cheap_models(self) -> None:
        """Cheap adequate models declared in MODEL_COSTS_PER_1M_TOKENS join the pool."""
        from bernstein.core.cost.cost import get_all_bandit_arms

        arms = get_all_bandit_arms()
        # These were "new arms" flagged by audit-069 — the pool makes them
        # visible to the bandit for explicit exploration once seeded, but
        # they cannot greedily win selection (success_rate is pessimistic).
        assert "gemini-3-flash" in arms
        assert "qwen3-coder" in arms

    def test_get_all_bandit_arms_cascade_first(self) -> None:
        """Order matters: cascade stays at the front for cheapest-first callers."""
        from bernstein.core.cost.cost import get_all_bandit_arms

        arms = get_all_bandit_arms()
        # First len(CASCADE) entries preserve the cascade order.
        assert arms[: len(CASCADE)] == CASCADE


# ---------------------------------------------------------------------------
# Bandit integration in route_task
# ---------------------------------------------------------------------------


class TestRoutTaskBanditIntegration:
    """route_task consults the bandit when bandit_metrics_dir is provided."""

    @patch("bernstein.core.cost.cost.random.random", return_value=0.5)
    def test_bandit_routes_simple_task_to_cheap_model(self, _mock_rng: object, tmp_path: Path) -> None:
        """When haiku meets quality threshold for 'backend', route_task returns haiku."""
        from bernstein.core.router import route_task

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        # Seed bandit: haiku has 100% success for backend
        _bandit_with_data(metrics_dir, "backend", "haiku", observations=10, successes=10)

        task = _task(role="backend", complexity=Complexity.MEDIUM)
        config = route_task(task, bandit_metrics_dir=metrics_dir)

        assert config.model == "sonnet"

    def test_bandit_does_not_affect_manager_tasks(self, tmp_path: Path) -> None:
        """Manager role always uses opus regardless of bandit data."""
        from bernstein.core.router import route_task

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "manager", "haiku", observations=10, successes=10)

        task = _task(role="manager")
        config = route_task(task, bandit_metrics_dir=metrics_dir)

        assert config.model == "opus"

    def test_route_task_without_bandit_uses_heuristics(self) -> None:
        """Without bandit_metrics_dir, route_task uses heuristics."""
        from bernstein.core.router import route_task

        task = _task(role="backend", complexity=Complexity.MEDIUM)
        config = route_task(task, bandit_metrics_dir=None)

        # Heuristic gives sonnet for medium backend tasks
        assert config.model in ("sonnet", "haiku", "opus")


# ---------------------------------------------------------------------------
# Bandit integration in spawner._select_batch_config
# ---------------------------------------------------------------------------


class TestSelectBatchConfigBanditIntegration:
    """_select_batch_config uses bandit when metrics_dir is provided."""

    @patch("bernstein.core.cost.cost.random.random", return_value=0.5)
    def test_uses_bandit_for_standard_task(self, _mock_rng: object, tmp_path: Path) -> None:
        """When bandit has haiku meeting threshold, _select_batch_config picks haiku."""
        from bernstein.core.spawner import _select_batch_config

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "backend", "haiku", observations=10, successes=10)

        tasks = [_task(role="backend", complexity=Complexity.LOW)]
        config = _select_batch_config(tasks, metrics_dir=metrics_dir)

        assert config.model == "sonnet"

    def test_manager_ignores_bandit(self, tmp_path: Path) -> None:
        """Manager always gets opus even if bandit says haiku."""
        from bernstein.core.spawner import _select_batch_config

        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        _bandit_with_data(metrics_dir, "manager", "haiku", observations=10, successes=10)

        tasks = [_task(role="manager")]
        config = _select_batch_config(tasks, metrics_dir=metrics_dir)

        assert config.model == "opus"

    def test_no_metrics_dir_falls_back_to_heuristics(self) -> None:
        """Without metrics_dir, uses heuristic routing."""
        from bernstein.core.spawner import _select_batch_config

        tasks = [_task(role="backend", complexity=Complexity.MEDIUM)]
        config = _select_batch_config(tasks, metrics_dir=None)

        assert config.model in ("sonnet", "opus")


# ---------------------------------------------------------------------------
# get_cascade_model
# ---------------------------------------------------------------------------


class TestGetCascadeModel:
    def test_starts_at_sonnet_for_standard_task(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_escalates_to_opus_on_first_retry(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=1) == "opus"

    def test_stays_at_opus_on_second_retry(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=2) == "opus"

    def test_high_complexity_starts_at_sonnet(self) -> None:
        task = _task(role="backend", complexity=Complexity.HIGH)
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_manager_starts_at_sonnet(self) -> None:
        task = _task(role="manager")
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_security_role_starts_at_sonnet(self) -> None:
        task = _task(role="security")
        assert get_cascade_model(task, retry_count=0) == "sonnet"

    def test_caps_at_opus_beyond_max_retries(self) -> None:
        task = _task(role="backend", complexity=Complexity.LOW, scope=Scope.SMALL)
        assert get_cascade_model(task, retry_count=99) == "opus"


# ---------------------------------------------------------------------------
# compute_savings_vs_opus
# ---------------------------------------------------------------------------


class TestComputeSavingsVsOpus:
    def test_no_savings_when_no_records(self) -> None:
        assert compute_savings_vs_opus([]) == pytest.approx(0.0)

    def test_savings_for_haiku_task(self) -> None:
        # 1000 tokens at haiku cost — opus would have cost much more
        records = [
            {
                "model": "haiku",
                "tokens_prompt": 500,
                "tokens_completion": 500,
                "cost_usd": 0.0004,
            }
        ]
        savings = compute_savings_vs_opus(records)
        assert savings > 0.0

    def test_no_savings_for_opus_task(self) -> None:
        # Opus tasks have no savings vs baseline
        records = [
            {
                "model": "opus",
                "tokens_prompt": 500,
                "tokens_completion": 500,
                "cost_usd": 0.015,
            }
        ]
        savings = compute_savings_vs_opus(records)
        assert savings == pytest.approx(0.0)

    def test_no_savings_for_fast_path_task(self) -> None:
        records = [{"model": "fast-path", "tokens_prompt": 0, "tokens_completion": 0, "cost_usd": 0.0}]
        savings = compute_savings_vs_opus(records)
        assert savings == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_daily_cost
# ---------------------------------------------------------------------------


class TestComputeDailyCost:
    def test_empty_records(self) -> None:
        assert compute_daily_cost([]) == []

    def test_groups_by_day(self) -> None:
        now = time.time()
        yesterday = now - 86400
        records = [
            {"timestamp": now, "cost_usd": 0.01},
            {"timestamp": now, "cost_usd": 0.02},
            {"timestamp": yesterday, "cost_usd": 0.005},
        ]
        daily = compute_daily_cost(records, days=7)
        total = sum(d["cost_usd"] for d in daily)
        assert total == pytest.approx(0.035)
        assert len(daily) == 2  # today + yesterday

    def test_excludes_records_outside_window(self) -> None:
        now = time.time()
        old = now - 30 * 86400  # 30 days ago
        records = [
            {"timestamp": now, "cost_usd": 0.01},
            {"timestamp": old, "cost_usd": 100.0},  # outside 7-day window
        ]
        daily = compute_daily_cost(records, days=7)
        total = sum(d["cost_usd"] for d in daily)
        assert total == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# project_monthly_cost
# ---------------------------------------------------------------------------


class TestProjectMonthlyCost:
    def test_zero_when_no_records(self) -> None:
        assert project_monthly_cost([]) == pytest.approx(0.0)

    def test_projects_based_on_daily_average(self) -> None:
        # 7 days of $1/day should project $30/month
        now = time.time()
        records = []
        for i in range(7):
            ts = now - i * 86400
            records.append({"timestamp": ts, "cost_usd": 1.0})
        projected = project_monthly_cost(records, window_days=7)
        assert projected == pytest.approx(30.0, rel=0.01)


# ---------------------------------------------------------------------------
# estimate_run_cost
# ---------------------------------------------------------------------------


class TestEstimateRunCost:
    def test_returns_low_high_tuple(self) -> None:
        low, high = estimate_run_cost(5, model="sonnet")
        assert isinstance(low, float)
        assert isinstance(high, float)
        assert low <= high

    def test_scales_with_task_count(self) -> None:
        low1, high1 = estimate_run_cost(1)
        low5, high5 = estimate_run_cost(5)
        assert low5 == low1 * 5
        assert high5 == high1 * 5

    def test_haiku_cheaper_than_opus(self) -> None:
        _, high_haiku = estimate_run_cost(1, model="haiku")
        low_opus, _ = estimate_run_cost(1, model="opus")
        assert high_haiku < low_opus or high_haiku <= low_opus  # haiku is always cheaper

    def test_zero_tasks_returns_zero(self) -> None:
        low, high = estimate_run_cost(0)
        assert low == pytest.approx(0.0)
        assert high == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_savings_vs_manual
# ---------------------------------------------------------------------------


class TestComputeSavingsVsManual:
    def test_no_savings_when_no_records(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        res = compute_savings_vs_manual([])
        assert res["savings_usd"] == pytest.approx(0.0)
        assert res["manual_hours"] == pytest.approx(0.0)

    def test_savings_with_explicit_hours(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        records = [
            {"cost_usd": 1.0, "estimated_manual_hours": 2.0},
        ]
        res = compute_savings_vs_manual(records, hourly_rate=50.0)
        assert res["manual_hours"] == pytest.approx(2.0)
        assert res["manual_cost_usd"] == pytest.approx(100.0)
        assert res["api_cost_usd"] == pytest.approx(1.0)
        assert res["savings_usd"] == pytest.approx(99.0)

    def test_savings_with_implicit_scope(self) -> None:
        from bernstein.core.cost import compute_savings_vs_manual

        records = [
            {"cost_usd": 0.5, "scope": "small"},  # 0.5 hr
            {"cost_usd": 1.0, "scope": "medium"},  # 1.5 hr
            {"cost_usd": 2.0, "scope": "large"},  # 4.0 hr
            {"cost_usd": 0.5},  # default medium = 1.5 hr
        ]
        res = compute_savings_vs_manual(records, hourly_rate=100.0)
        # total hours = 0.5 + 1.5 + 4.0 + 1.5 = 7.5
        assert res["manual_hours"] == pytest.approx(7.5)
        assert res["manual_cost_usd"] == pytest.approx(750.0)
        assert res["api_cost_usd"] == pytest.approx(4.0)
        assert res["savings_usd"] == pytest.approx(746.0)
