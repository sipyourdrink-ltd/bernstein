"""Tests for agent performance leaderboard (ROAD-036)."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.cli.leaderboard import (
    LeaderboardRecord,
    build_leaderboard,
    format_leaderboard,
    get_recommendation,
    load_leaderboard,
    save_leaderboard,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_HISTORY: list[dict[str, object]] = [
    {
        "adapter": "claude",
        "model": "sonnet",
        "task_type": "backend",
        "success": True,
        "cost_usd": 0.04,
        "duration_s": 20.0,
        "quality_pass": True,
    },
    {
        "adapter": "claude",
        "model": "sonnet",
        "task_type": "backend",
        "success": True,
        "cost_usd": 0.06,
        "duration_s": 30.0,
        "quality_pass": True,
    },
    {
        "adapter": "claude",
        "model": "sonnet",
        "task_type": "backend",
        "success": False,
        "cost_usd": 0.05,
        "duration_s": 40.0,
        "quality_pass": False,
    },
    {
        "adapter": "codex",
        "model": "gpt-5.4",
        "task_type": "backend",
        "success": True,
        "cost_usd": 0.10,
        "duration_s": 60.0,
        "quality_pass": True,
    },
    {
        "adapter": "claude",
        "model": "haiku",
        "task_type": "qa",
        "success": True,
        "cost_usd": 0.005,
        "duration_s": 5.0,
        "quality_pass": True,
    },
]


# ---------------------------------------------------------------------------
# build_leaderboard
# ---------------------------------------------------------------------------


class TestBuildLeaderboard:
    """Tests for build_leaderboard."""

    def test_empty_history(self) -> None:
        """Empty input produces an empty leaderboard."""
        lb = build_leaderboard([])
        assert lb.records == []
        assert lb.generated_at != ""

    def test_groups_by_adapter_model_task_type(self) -> None:
        """Records are grouped by (adapter, model, task_type)."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        keys = {(r.adapter, r.model, r.task_type) for r in lb.records}
        assert keys == {
            ("claude", "sonnet", "backend"),
            ("codex", "gpt-5.4", "backend"),
            ("claude", "haiku", "qa"),
        }

    def test_aggregation_counts(self) -> None:
        """Attempts and successes are counted correctly."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        sonnet_backend = next(r for r in lb.records if r.adapter == "claude" and r.model == "sonnet")
        assert sonnet_backend.attempts == 3
        assert sonnet_backend.successes == 2

    def test_avg_cost(self) -> None:
        """Average cost is computed from total cost / attempts."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        sonnet_backend = next(r for r in lb.records if r.adapter == "claude" and r.model == "sonnet")
        expected_avg = (0.04 + 0.06 + 0.05) / 3
        assert sonnet_backend.avg_cost_usd == pytest.approx(expected_avg)

    def test_avg_duration(self) -> None:
        """Average duration is computed correctly."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        sonnet_backend = next(r for r in lb.records if r.adapter == "claude" and r.model == "sonnet")
        expected_avg = (20.0 + 30.0 + 40.0) / 3
        assert sonnet_backend.avg_duration_s == pytest.approx(expected_avg)

    def test_quality_rate(self) -> None:
        """Quality rate = quality_passes / attempts."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        sonnet_backend = next(r for r in lb.records if r.adapter == "claude" and r.model == "sonnet")
        assert sonnet_backend.quality_rate == pytest.approx(2 / 3)

    def test_success_rate_property(self) -> None:
        """success_rate property computes successes / attempts."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        sonnet_backend = next(r for r in lb.records if r.adapter == "claude" and r.model == "sonnet")
        assert sonnet_backend.success_rate == pytest.approx(2 / 3)

    def test_success_rate_zero_attempts(self) -> None:
        """success_rate is 0.0 when attempts is 0."""
        rec = LeaderboardRecord(
            adapter="x",
            model="y",
            task_type="z",
            attempts=0,
            successes=0,
            avg_cost_usd=0.0,
            avg_duration_s=0.0,
            quality_rate=0.0,
        )
        assert rec.success_rate == pytest.approx(0.0)

    def test_role_fallback(self) -> None:
        """Uses 'role' key when 'task_type' is absent."""
        history = [
            {"adapter": "claude", "model": "opus", "role": "frontend", "success": True},
        ]
        lb = build_leaderboard(history)
        assert lb.records[0].task_type == "frontend"

    def test_missing_fields_default(self) -> None:
        """Missing optional fields default gracefully."""
        history = [{"adapter": "claude", "model": "sonnet", "success": True}]
        lb = build_leaderboard(history)
        rec = lb.records[0]
        assert rec.task_type == "unknown"
        assert rec.avg_cost_usd == pytest.approx(0.0)
        assert rec.avg_duration_s == pytest.approx(0.0)
        assert rec.quality_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# format_leaderboard
# ---------------------------------------------------------------------------


class TestFormatLeaderboard:
    """Tests for format_leaderboard."""

    def test_contains_header(self) -> None:
        """Output contains the table title."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        text = format_leaderboard(lb)
        assert "Agent Performance Leaderboard" in text

    def test_contains_adapter_names(self) -> None:
        """Output includes adapter names from records."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        text = format_leaderboard(lb)
        assert "claude" in text
        assert "codex" in text

    def test_sort_by_cost(self) -> None:
        """Sorting by cost puts cheapest first."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        text = format_leaderboard(lb, sort_by="cost")
        # haiku at $0.005 should appear before codex at $0.10
        haiku_pos = text.find("haiku")
        gpt4o_pos = text.find("gpt-5.4")
        assert haiku_pos < gpt4o_pos

    def test_empty_leaderboard(self) -> None:
        """Formatting an empty leaderboard does not crash."""
        lb = build_leaderboard([])
        text = format_leaderboard(lb)
        assert "Agent Performance Leaderboard" in text


# ---------------------------------------------------------------------------
# get_recommendation
# ---------------------------------------------------------------------------


class TestGetRecommendation:
    """Tests for get_recommendation."""

    def test_returns_best_for_task_type(self) -> None:
        """Returns the record with highest success_rate for the task type."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        rec = get_recommendation("backend", lb)
        assert rec is not None
        # codex/gpt-5.4 has 100% success rate vs claude/sonnet at 66%
        assert rec.adapter == "codex"
        assert rec.model == "gpt-5.4"

    def test_tiebreak_by_cost(self) -> None:
        """When success rates are equal, cheaper wins."""
        history = [
            {"adapter": "a", "model": "m1", "task_type": "x", "success": True, "cost_usd": 0.10},
            {"adapter": "b", "model": "m2", "task_type": "x", "success": True, "cost_usd": 0.01},
        ]
        lb = build_leaderboard(history)
        rec = get_recommendation("x", lb)
        assert rec is not None
        assert rec.adapter == "b"  # cheaper

    def test_no_match_returns_none(self) -> None:
        """Returns None when no records match the task type."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        rec = get_recommendation("nonexistent", lb)
        assert rec is None

    def test_empty_leaderboard_returns_none(self) -> None:
        """Returns None for an empty leaderboard."""
        lb = build_leaderboard([])
        assert get_recommendation("backend", lb) is None


# ---------------------------------------------------------------------------
# save / load roundtrip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundtrip:
    """Tests for save_leaderboard / load_leaderboard."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Save then load produces identical records."""
        lb = build_leaderboard(_SAMPLE_HISTORY)
        path = tmp_path / "lb.json"
        save_leaderboard(lb, path)

        loaded = load_leaderboard(path)
        assert loaded is not None
        assert loaded.generated_at == lb.generated_at
        assert len(loaded.records) == len(lb.records)
        for orig, loaded_rec in zip(lb.records, loaded.records, strict=True):
            assert orig.adapter == loaded_rec.adapter
            assert orig.model == loaded_rec.model
            assert orig.task_type == loaded_rec.task_type
            assert orig.attempts == loaded_rec.attempts
            assert orig.successes == loaded_rec.successes
            assert orig.avg_cost_usd == pytest.approx(loaded_rec.avg_cost_usd)
            assert orig.avg_duration_s == pytest.approx(loaded_rec.avg_duration_s)
            assert orig.quality_rate == pytest.approx(loaded_rec.quality_rate)

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Loading a non-existent file returns None."""
        result = load_leaderboard(tmp_path / "nope.json")
        assert result is None

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        """Loading corrupt JSON returns None."""
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        result = load_leaderboard(path)
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        """save_leaderboard creates parent directories."""
        path = tmp_path / "sub" / "dir" / "lb.json"
        lb = build_leaderboard([])
        save_leaderboard(lb, path)
        assert path.exists()
