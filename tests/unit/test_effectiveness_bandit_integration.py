"""Tests for unified effectiveness scorer + bandit learning integration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.models import Scope

from bernstein.core.cost import EpsilonGreedyBandit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_effectiveness_records(
    workdir: Path,
    role: str,
    model: str,
    totals: list[int],
    effort: str = "high",
) -> None:
    """Write synthetic effectiveness records to the JSONL history file."""
    history_path = workdir / ".sdd" / "metrics" / "effectiveness.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        for i, total in enumerate(totals):
            if total >= 90:
                grade = "A"
            elif total >= 80:
                grade = "B"
            else:
                grade = "C"
            record = {
                "session_id": f"s-{model}-{i}",
                "task_id": f"t-{model}-{i}",
                "role": role,
                "model": model,
                "effort": effort,
                "time_score": 80,
                "quality_score": total,
                "efficiency_score": 70,
                "retry_score": 100,
                "completion_score": 100,
                "total": total,
                "grade": grade,
                "wall_time_s": 120.0,
                "estimated_time_s": 180.0,
                "tokens_used": 5000,
                "retry_count": 0,
                "fix_count": 0,
                "gate_pass_rate": 1.0,
                "timestamp": time.time(),
            }
            f.write(json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# EffectivenessScorer.export_for_bandit
# ---------------------------------------------------------------------------


def test_export_for_bandit_empty(tmp_path: Path) -> None:
    """export_for_bandit returns empty dict when no history exists."""
    scorer = EffectivenessScorer(tmp_path)
    assert scorer.export_for_bandit("backend") == {}


def test_export_for_bandit_insufficient_data(tmp_path: Path) -> None:
    """Models with fewer than 3 observations are excluded."""
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 85])
    scorer = EffectivenessScorer(tmp_path)
    result = scorer.export_for_bandit("backend")
    assert result == {}


def test_export_for_bandit_success_rate(tmp_path: Path) -> None:
    """Success rate counts sessions with total >= 80 as successes."""
    # 4 sessions above 80, 1 below => 80% success rate
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 85, 82, 95, 60])
    scorer = EffectivenessScorer(tmp_path)
    result = scorer.export_for_bandit("backend")
    assert "sonnet" in result
    assert abs(result["sonnet"] - 0.8) < 0.01


def test_export_for_bandit_multiple_models(tmp_path: Path) -> None:
    """Exports data for all models with sufficient observations."""
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 85, 82, 95])
    _write_effectiveness_records(tmp_path, "backend", "opus", [95, 92, 88])
    _write_effectiveness_records(tmp_path, "backend", "haiku", [50, 60])  # too few
    scorer = EffectivenessScorer(tmp_path)
    result = scorer.export_for_bandit("backend")
    assert "sonnet" in result
    assert "opus" in result
    assert "haiku" not in result


def test_export_for_bandit_filters_by_role(tmp_path: Path) -> None:
    """Only returns data for the requested role."""
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 85, 82])
    _write_effectiveness_records(tmp_path, "qa", "sonnet", [70, 65, 60])
    scorer = EffectivenessScorer(tmp_path)
    backend_result = scorer.export_for_bandit("backend")
    qa_result = scorer.export_for_bandit("qa")
    assert backend_result["sonnet"] > qa_result["sonnet"]


# ---------------------------------------------------------------------------
# EpsilonGreedyBandit.seed_arm
# ---------------------------------------------------------------------------


def test_seed_arm_creates_arm() -> None:
    """seed_arm creates a new arm with virtual observations."""
    bandit = EpsilonGreedyBandit()
    bandit.seed_arm("backend", "sonnet", success_rate=0.9, virtual_observations=10)
    arm = bandit.get_arm("backend", "sonnet")
    assert arm is not None
    assert arm.observations == 10
    assert arm.successes == 9
    assert abs(arm.success_rate - 0.9) < 0.01


def test_seed_arm_skips_existing_arm() -> None:
    """seed_arm does not overwrite an arm that already has real observations."""
    bandit = EpsilonGreedyBandit()
    bandit.record("backend", "sonnet", success=True, cost_usd=0.01)
    bandit.seed_arm("backend", "sonnet", success_rate=0.5, virtual_observations=10)
    arm = bandit.get_arm("backend", "sonnet")
    assert arm is not None
    # Should still have the single real observation, not the 10 virtual ones
    assert arm.observations == 1
    assert arm.successes == 1


def test_seed_arm_clamps_rate() -> None:
    """seed_arm clamps success_rate to [0.0, 1.0]."""
    bandit = EpsilonGreedyBandit()
    bandit.seed_arm("backend", "sonnet", success_rate=1.5)
    arm = bandit.get_arm("backend", "sonnet")
    assert arm is not None
    assert arm.successes == arm.observations  # clamped to 1.0


def test_seed_arm_zero_rate() -> None:
    """seed_arm with 0.0 rate creates arm with zero successes."""
    bandit = EpsilonGreedyBandit()
    bandit.seed_arm("backend", "sonnet", success_rate=0.0)
    arm = bandit.get_arm("backend", "sonnet")
    assert arm is not None
    assert arm.successes == 0
    assert arm.observations == 5  # default virtual_observations


def test_seed_arm_persists(tmp_path: Path) -> None:
    """Seeded arms survive save/load cycle."""
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    bandit = EpsilonGreedyBandit()
    bandit.seed_arm("backend", "sonnet", success_rate=0.8, virtual_observations=10)
    bandit.save(metrics_dir)

    loaded = EpsilonGreedyBandit.load(metrics_dir)
    arm = loaded.get_arm("backend", "sonnet")
    assert arm is not None
    assert arm.observations == 10
    assert arm.successes == 8


# ---------------------------------------------------------------------------
# Integration: effectiveness seeds bandit selection
# ---------------------------------------------------------------------------


def test_seeded_bandit_exploits_best_model(tmp_path: Path) -> None:
    """When seeded with effectiveness data, bandit exploits the best-performing model."""
    # sonnet has high success rate, opus has low
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 92, 88, 85, 95])
    _write_effectiveness_records(tmp_path, "backend", "opus", [50, 45, 60, 55, 40])

    scorer = EffectivenessScorer(tmp_path)
    effectiveness_data = scorer.export_for_bandit("backend")

    # Seed a bandit with epsilon=0 (pure exploit mode)
    bandit = EpsilonGreedyBandit(epsilon=0.0)
    for model, rate in effectiveness_data.items():
        bandit.seed_arm("backend", model, rate)

    # With pure exploit and sonnet having higher success rate + lower cost,
    # bandit should always pick sonnet
    selections = [bandit.select("backend", candidate_models=["sonnet", "opus"]) for _ in range(10)]
    assert all(s == "sonnet" for s in selections)


def test_seeded_bandit_respects_quality_threshold(tmp_path: Path) -> None:
    """Seeded arm below quality threshold is not exploited."""
    # haiku has low success rate (below 0.80 threshold)
    _write_effectiveness_records(tmp_path, "backend", "haiku", [60, 55, 70])
    # sonnet has high success rate
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 92, 88])

    scorer = EffectivenessScorer(tmp_path)
    effectiveness_data = scorer.export_for_bandit("backend")

    bandit = EpsilonGreedyBandit(epsilon=0.0)
    for model, rate in effectiveness_data.items():
        bandit.seed_arm("backend", model, rate)

    # haiku has ~0.33 success rate which is below 0.80 threshold
    # Bandit in exploit mode should prefer sonnet
    selections = [bandit.select("backend", candidate_models=["haiku", "sonnet"]) for _ in range(10)]
    assert all(s == "sonnet" for s in selections)


def test_real_observations_override_seed() -> None:
    """Real observations after seeding shift the arm statistics."""
    bandit = EpsilonGreedyBandit()
    # Seed with 90% success rate (9/10)
    bandit.seed_arm("backend", "sonnet", success_rate=0.9, virtual_observations=10)

    # Record 5 failures
    for _ in range(5):
        bandit.record("backend", "sonnet", success=False)

    arm = bandit.get_arm("backend", "sonnet")
    assert arm is not None
    # 9 successes out of 15 total observations = 60%
    assert arm.observations == 15
    assert arm.successes == 9
    assert abs(arm.success_rate - 0.6) < 0.01


# ---------------------------------------------------------------------------
# Integration: route_task with effectiveness seeding
# ---------------------------------------------------------------------------


def test_route_task_seeds_bandit_from_effectiveness(tmp_path: Path, make_task: Any) -> None:
    """route_task seeds the bandit with effectiveness data when workdir is provided."""
    from bernstein.core.router import route_task

    # Write effectiveness data showing sonnet is very good for backend
    _write_effectiveness_records(tmp_path, "backend", "sonnet", [90, 92, 88, 95, 91])

    # Set up metrics dir
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    task = make_task(role="backend", scope=Scope.SMALL)

    # Call route_task with workdir to trigger seeding
    route_task(task, bandit_metrics_dir=metrics_dir, workdir=tmp_path)

    # The bandit should now have state persisted
    bandit = EpsilonGreedyBandit.load(metrics_dir)
    arm = bandit.get_arm("backend", "sonnet")
    # The arm should exist (seeded from effectiveness data)
    assert arm is not None
    assert arm.observations > 0


def test_route_task_without_workdir_still_works(make_task: Any, tmp_path: Path) -> None:
    """route_task works without workdir (backward compatible)."""
    from bernstein.core.router import route_task

    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()

    task = make_task(role="backend", scope=Scope.SMALL)
    config = route_task(task, bandit_metrics_dir=metrics_dir)
    assert config.model is not None
    assert config.effort is not None
