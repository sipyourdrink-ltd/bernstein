"""Tests for agent performance profiling (agent-018)."""

from __future__ import annotations

import pytest

from bernstein.core.agent_profiling import (
    AgentProfile,
    aggregate_profiles,
    compute_profile,
    format_profile_table,
)

# ---------------------------------------------------------------------------
# AgentProfile creation
# ---------------------------------------------------------------------------


def test_agent_profile_is_frozen() -> None:
    """AgentProfile instances are immutable."""
    profile = AgentProfile(
        agent_id="a1",
        role="backend",
        model="sonnet",
        spawn_latency_s=1.5,
        tokens_per_minute=120.0,
        time_to_first_output_s=1.5,
        total_completion_s=60.0,
        task_count=3,
    )
    with pytest.raises(AttributeError):
        profile.spawn_latency_s = 2.0  # type: ignore[misc]


def test_agent_profile_to_dict() -> None:
    """to_dict produces a JSON-safe dict with rounded values."""
    profile = AgentProfile(
        agent_id="a1",
        role="frontend",
        model="haiku",
        spawn_latency_s=0.1234,
        tokens_per_minute=99.99,
        time_to_first_output_s=0.1234,
        total_completion_s=30.5678,
        task_count=2,
    )
    d = profile.to_dict()
    assert d["agent_id"] == "a1"
    assert d["role"] == "frontend"
    assert d["model"] == "haiku"
    assert d["spawn_latency_s"] == 0.123
    assert d["tokens_per_minute"] == 100.0
    assert d["time_to_first_output_s"] == 0.123
    assert d["total_completion_s"] == 30.568
    assert d["task_count"] == 2


# ---------------------------------------------------------------------------
# compute_profile
# ---------------------------------------------------------------------------


def test_compute_profile_normal_case() -> None:
    """compute_profile calculates spawn latency, token rate, and total time."""
    spawn_ts = 1000.0
    first_output_ts = 1002.5  # 2.5s after spawn
    end_ts = 1060.0  # 60s total
    total_tokens = 3000
    task_count = 2

    profile = compute_profile(
        agent_id="a1",
        role="backend",
        model="sonnet",
        spawn_ts=spawn_ts,
        first_output_ts=first_output_ts,
        end_ts=end_ts,
        total_tokens=total_tokens,
        task_count=task_count,
    )

    assert profile.agent_id == "a1"
    assert profile.role == "backend"
    assert profile.model == "sonnet"
    assert profile.spawn_latency_s == pytest.approx(2.5)
    assert profile.time_to_first_output_s == pytest.approx(2.5)
    assert profile.total_completion_s == pytest.approx(60.0)
    # 3000 tokens / 60 seconds * 60 = 3000 tokens/min
    assert profile.tokens_per_minute == pytest.approx(3000.0)
    assert profile.task_count == 2


def test_compute_profile_zero_duration() -> None:
    """When spawn, first output, and end are the same, rates are zero."""
    ts = 1000.0
    profile = compute_profile(
        agent_id="a2",
        role="qa",
        model="haiku",
        spawn_ts=ts,
        first_output_ts=ts,
        end_ts=ts,
        total_tokens=500,
        task_count=1,
    )

    assert profile.spawn_latency_s == pytest.approx(0.0)
    assert profile.tokens_per_minute == pytest.approx(0.0)
    assert profile.total_completion_s == pytest.approx(0.0)
    assert profile.task_count == 1


def test_compute_profile_clamps_negative_timestamps() -> None:
    """If first_output_ts < spawn_ts (clock skew), latency is clamped to 0."""
    profile = compute_profile(
        agent_id="a3",
        role="devops",
        model="opus",
        spawn_ts=1000.0,
        first_output_ts=999.0,  # before spawn (clock skew)
        end_ts=999.0,  # also before spawn
        total_tokens=100,
        task_count=1,
    )

    assert profile.spawn_latency_s == pytest.approx(0.0)
    assert profile.total_completion_s == pytest.approx(0.0)


def test_compute_profile_zero_tokens() -> None:
    """Zero tokens yields zero tokens_per_minute even with positive duration."""
    profile = compute_profile(
        agent_id="a4",
        role="docs",
        model="sonnet",
        spawn_ts=1000.0,
        first_output_ts=1001.0,
        end_ts=1060.0,
        total_tokens=0,
        task_count=1,
    )

    assert profile.tokens_per_minute == pytest.approx(0.0)
    assert profile.total_completion_s == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# format_profile_table
# ---------------------------------------------------------------------------


def test_format_profile_table_produces_string() -> None:
    """format_profile_table returns a non-empty string with table content."""
    profiles = [
        AgentProfile(
            agent_id="a1",
            role="backend",
            model="sonnet",
            spawn_latency_s=1.2,
            tokens_per_minute=500.0,
            time_to_first_output_s=1.2,
            total_completion_s=45.0,
            task_count=3,
        ),
        AgentProfile(
            agent_id="a2",
            role="frontend",
            model="haiku",
            spawn_latency_s=0.8,
            tokens_per_minute=800.0,
            time_to_first_output_s=0.8,
            total_completion_s=30.0,
            task_count=2,
        ),
    ]
    output = format_profile_table(profiles)

    assert isinstance(output, str)
    assert len(output) > 0
    assert "Agent Performance Profiles" in output
    assert "a1" in output
    assert "a2" in output
    assert "backend" in output
    assert "frontend" in output


def test_format_profile_table_empty_list() -> None:
    """format_profile_table handles an empty profile list gracefully."""
    output = format_profile_table([])

    assert isinstance(output, str)
    assert "Agent Performance Profiles" in output


# ---------------------------------------------------------------------------
# aggregate_profiles
# ---------------------------------------------------------------------------


def test_aggregate_profiles_empty() -> None:
    """Aggregating an empty list returns zeroed stats."""
    result = aggregate_profiles([])

    assert result["count"] == 0
    assert result["avg_spawn_latency_s"] == 0.0
    assert result["avg_tokens_per_minute"] == 0.0
    assert result["avg_time_to_first_output_s"] == 0.0
    assert result["avg_total_completion_s"] == 0.0
    assert result["total_tasks"] == 0
    assert result["min_tokens_per_minute"] == 0.0
    assert result["max_tokens_per_minute"] == 0.0


def test_aggregate_profiles_single() -> None:
    """Aggregating a single profile returns its own values as averages."""
    profile = AgentProfile(
        agent_id="a1",
        role="backend",
        model="sonnet",
        spawn_latency_s=2.0,
        tokens_per_minute=600.0,
        time_to_first_output_s=2.0,
        total_completion_s=90.0,
        task_count=3,
    )
    result = aggregate_profiles([profile])

    assert result["count"] == 1
    assert result["avg_spawn_latency_s"] == pytest.approx(2.0)
    assert result["avg_tokens_per_minute"] == pytest.approx(600.0)
    assert result["avg_time_to_first_output_s"] == pytest.approx(2.0)
    assert result["avg_total_completion_s"] == pytest.approx(90.0)
    assert result["total_tasks"] == 3
    assert result["min_tokens_per_minute"] == pytest.approx(600.0)
    assert result["max_tokens_per_minute"] == pytest.approx(600.0)


def test_aggregate_profiles_multiple() -> None:
    """Aggregating multiple profiles computes correct averages and extremes."""
    profiles = [
        AgentProfile(
            agent_id="a1",
            role="backend",
            model="sonnet",
            spawn_latency_s=1.0,
            tokens_per_minute=400.0,
            time_to_first_output_s=1.0,
            total_completion_s=60.0,
            task_count=2,
        ),
        AgentProfile(
            agent_id="a2",
            role="frontend",
            model="haiku",
            spawn_latency_s=3.0,
            tokens_per_minute=800.0,
            time_to_first_output_s=3.0,
            total_completion_s=120.0,
            task_count=4,
        ),
    ]
    result = aggregate_profiles(profiles)

    assert result["count"] == 2
    assert result["avg_spawn_latency_s"] == pytest.approx(2.0)
    assert result["avg_tokens_per_minute"] == pytest.approx(600.0)
    assert result["avg_time_to_first_output_s"] == pytest.approx(2.0)
    assert result["avg_total_completion_s"] == pytest.approx(90.0)
    assert result["total_tasks"] == 6
    assert result["min_tokens_per_minute"] == pytest.approx(400.0)
    assert result["max_tokens_per_minute"] == pytest.approx(800.0)
