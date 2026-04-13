"""Tests for differential_privacy module — (ε, δ)-DP for telemetry export."""

from __future__ import annotations

import math
import statistics
from typing import Any

import pytest
from bernstein.core.differential_privacy import (
    DPConfig,
    GaussianMechanism,
    apply_dp_to_export,
)

# ---------------------------------------------------------------------------
# DPConfig defaults
# ---------------------------------------------------------------------------


def test_dp_config_defaults() -> None:
    """Default config uses ε=1.0, δ=1e-5."""
    cfg = DPConfig()
    assert cfg.epsilon == pytest.approx(1.0)
    assert cfg.delta == pytest.approx(1e-5)
    assert cfg.clip_min == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# GaussianMechanism — sigma formula
# ---------------------------------------------------------------------------


def test_gaussian_mechanism_sigma_formula() -> None:
    """σ = Δf * sqrt(2 * ln(1.25/δ)) / ε."""
    sensitivity = 1.0
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    mech = GaussianMechanism(sensitivity=sensitivity, config=cfg)
    expected_sigma = sensitivity * math.sqrt(2 * math.log(1.25 / cfg.delta)) / cfg.epsilon
    assert math.isclose(mech.sigma, expected_sigma, rel_tol=1e-9)


def test_gaussian_mechanism_sigma_scales_with_sensitivity() -> None:
    """Larger sensitivity → larger sigma."""
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    m1 = GaussianMechanism(sensitivity=1.0, config=cfg)
    m2 = GaussianMechanism(sensitivity=10.0, config=cfg)
    assert m2.sigma == pytest.approx(m1.sigma * 10.0)


def test_gaussian_mechanism_sigma_shrinks_with_larger_epsilon() -> None:
    """Larger ε (less privacy) → smaller sigma (less noise)."""
    cfg_strict = DPConfig(epsilon=0.1, delta=1e-5)
    cfg_loose = DPConfig(epsilon=10.0, delta=1e-5)
    m_strict = GaussianMechanism(sensitivity=1.0, config=cfg_strict)
    m_loose = GaussianMechanism(sensitivity=1.0, config=cfg_loose)
    assert m_strict.sigma > m_loose.sigma


# ---------------------------------------------------------------------------
# GaussianMechanism — add_noise
# ---------------------------------------------------------------------------


def test_gaussian_mechanism_adds_noise() -> None:
    """Over many samples the mean of noise is ~0 (unbiased)."""
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    mech = GaussianMechanism(sensitivity=1000.0, config=cfg)
    # Use a large value so clip_min never kicks in
    samples = [mech.add_noise(1e9) - 1e9 for _ in range(5000)]
    mean_noise = statistics.mean(samples)
    # Mean should be ~0 within 5-sigma / sqrt(N) (generous to avoid CI flakes)
    assert abs(mean_noise) < 5 * mech.sigma / math.sqrt(len(samples))


def test_gaussian_mechanism_clips_to_nonnegative() -> None:
    """Values are clamped to >= clip_min (default 0)."""
    cfg = DPConfig(epsilon=0.001, delta=1e-5)  # huge noise
    mech = GaussianMechanism(sensitivity=1.0, config=cfg)
    results = [mech.add_noise(0.0) for _ in range(500)]
    assert all(r >= 0.0 for r in results)


def test_gaussian_mechanism_custom_clip_min() -> None:
    """Custom clip_min is respected."""
    cfg = DPConfig(epsilon=0.001, delta=1e-5, clip_min=-1e6)
    mech = GaussianMechanism(sensitivity=1.0, config=cfg)
    # clip_min is very negative — allow negative results
    results = [mech.add_noise(0.0) for _ in range(200)]
    # Should still not go below clip_min
    assert all(r >= -1e6 for r in results)


# ---------------------------------------------------------------------------
# apply_dp_to_export — structure preservation
# ---------------------------------------------------------------------------


def _sample_export_data() -> dict[str, Any]:
    return {
        "exported_at": "2026-03-29T12:00:00",
        "summary": {
            "total_tasks": 42,
            "successful_tasks": 38,
            "failed_tasks": 4,
            "success_rate": 0.904,
            "janitor_pass_rate": 0.9,
            "total_agents": 5,
            "total_cost_usd": 1.23,
            "avg_completion_time_seconds": 65.0,
            "provider_stats": {},
            "provider_health": {},
            "quota_status": {},
        },
        "task_metrics": [
            {
                "task_id": "abc123",
                "role": "backend",
                "model": "claude-sonnet",
                "provider": "anthropic",
                "duration_seconds": 120.0,
                "success": True,
                "tokens_used": 8000,
                "cost_usd": 0.05,
                "error": None,
            }
        ],
        "agent_metrics": [
            {
                "agent_id": "agent-1",
                "role": "backend",
                "tasks_completed": 3,
                "tasks_failed": 0,
                "total_tokens": 25000,
                "total_cost_usd": 0.15,
            }
        ],
    }


def test_apply_dp_preserves_top_level_keys() -> None:
    """Output has the same top-level keys as input."""
    data = _sample_export_data()
    cfg = DPConfig()
    result = apply_dp_to_export(data, cfg)
    assert set(result.keys()) == set(data.keys())


def test_apply_dp_preserves_categorical_task_fields() -> None:
    """task_id, role, model, provider, error, success are not perturbed."""
    data = _sample_export_data()
    cfg = DPConfig()
    result = apply_dp_to_export(data, cfg)
    task_in = data["task_metrics"][0]
    task_out = result["task_metrics"][0]
    assert task_out["task_id"] == task_in["task_id"]
    assert task_out["role"] == task_in["role"]
    assert task_out["model"] == task_in["model"]
    assert task_out["provider"] == task_in["provider"]
    assert task_out["success"] == task_in["success"]
    assert task_out["error"] == task_in["error"]


def test_apply_dp_preserves_categorical_agent_fields() -> None:
    """agent_id and role are not perturbed."""
    data = _sample_export_data()
    cfg = DPConfig()
    result = apply_dp_to_export(data, cfg)
    agent_in = data["agent_metrics"][0]
    agent_out = result["agent_metrics"][0]
    assert agent_out["agent_id"] == agent_in["agent_id"]
    assert agent_out["role"] == agent_in["role"]


def test_apply_dp_perturbs_numeric_task_fields() -> None:
    """tokens_used, cost_usd, duration_seconds are perturbed (not identical) with high probability."""
    data = _sample_export_data()
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    # Run many times — probability that all are identical is astronomically small
    changed = set()
    for _ in range(20):
        result = apply_dp_to_export(data, cfg)
        task = result["task_metrics"][0]
        if task["tokens_used"] != data["task_metrics"][0]["tokens_used"]:
            changed.add("tokens_used")
        if task["cost_usd"] != data["task_metrics"][0]["cost_usd"]:
            changed.add("cost_usd")
        if task["duration_seconds"] != data["task_metrics"][0]["duration_seconds"]:
            changed.add("duration_seconds")
    assert "tokens_used" in changed
    assert "cost_usd" in changed
    assert "duration_seconds" in changed


def test_apply_dp_perturbs_numeric_agent_fields() -> None:
    """tasks_completed, total_tokens, total_cost_usd are perturbed."""
    data = _sample_export_data()
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    changed = set()
    for _ in range(20):
        result = apply_dp_to_export(data, cfg)
        agent = result["agent_metrics"][0]
        if agent["tasks_completed"] != data["agent_metrics"][0]["tasks_completed"]:
            changed.add("tasks_completed")
        if agent["total_tokens"] != data["agent_metrics"][0]["total_tokens"]:
            changed.add("total_tokens")
        if agent["total_cost_usd"] != data["agent_metrics"][0]["total_cost_usd"]:
            changed.add("total_cost_usd")
    assert "tasks_completed" in changed
    assert "total_tokens" in changed
    assert "total_cost_usd" in changed


def test_apply_dp_all_numeric_values_nonnegative() -> None:
    """All perturbed numeric values are >= 0."""
    data = _sample_export_data()
    cfg = DPConfig(epsilon=0.001, delta=1e-5)  # very noisy to stress-test clipping
    for _ in range(50):
        result = apply_dp_to_export(data, cfg)
        for task in result["task_metrics"]:
            if task["duration_seconds"] is not None:
                assert task["duration_seconds"] >= 0.0
            assert task["tokens_used"] >= 0.0
            assert task["cost_usd"] >= 0.0
        for agent in result["agent_metrics"]:
            assert agent["tasks_completed"] >= 0.0
            assert agent["tasks_failed"] >= 0.0
            assert agent["total_tokens"] >= 0.0
            assert agent["total_cost_usd"] >= 0.0


def test_apply_dp_perturbs_summary_numeric_fields() -> None:
    """Summary counts and rates are perturbed."""
    data = _sample_export_data()
    cfg = DPConfig(epsilon=1.0, delta=1e-5)
    changed = set()
    for _ in range(20):
        result = apply_dp_to_export(data, cfg)
        s = result["summary"]
        if s["total_tasks"] != data["summary"]["total_tasks"]:
            changed.add("total_tasks")
        if s["total_cost_usd"] != data["summary"]["total_cost_usd"]:
            changed.add("total_cost_usd")
    assert "total_tasks" in changed
    assert "total_cost_usd" in changed


def test_apply_dp_preserves_exported_at() -> None:
    """exported_at timestamp is not modified."""
    data = _sample_export_data()
    result = apply_dp_to_export(data, DPConfig())
    assert result["exported_at"] == data["exported_at"]


def test_apply_dp_does_not_mutate_input() -> None:
    """Original data dict is not modified in place."""
    data = _sample_export_data()
    original_tokens = data["task_metrics"][0]["tokens_used"]
    apply_dp_to_export(data, DPConfig())
    assert data["task_metrics"][0]["tokens_used"] == original_tokens


def test_apply_dp_handles_none_duration() -> None:
    """None duration_seconds (task still running) is left as None."""
    data = _sample_export_data()
    data["task_metrics"][0]["duration_seconds"] = None
    result = apply_dp_to_export(data, DPConfig())
    assert result["task_metrics"][0]["duration_seconds"] is None


def test_apply_dp_handles_empty_metrics() -> None:
    """Empty task_metrics and agent_metrics lists are handled gracefully."""
    data = _sample_export_data()
    data["task_metrics"] = []
    data["agent_metrics"] = []
    result = apply_dp_to_export(data, DPConfig())
    assert result["task_metrics"] == []
    assert result["agent_metrics"] == []
