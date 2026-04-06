"""Tests for bernstein.core.claude_max_turns (CLAUDE-014)."""

from __future__ import annotations

from bernstein.core.claude_max_turns import (
    MaxTurnsConfig,
    MaxTurnsCoordinator,
    compute_max_turns,
)


class TestComputeMaxTurns:
    def test_medium_sonnet(self) -> None:
        cfg = compute_max_turns(complexity="medium", model="sonnet", timeout_s=1800)
        assert cfg.max_turns > 0
        assert cfg.complexity == "medium"
        assert cfg.model == "sonnet"

    def test_trivial_has_fewer_turns(self) -> None:
        trivial = compute_max_turns(complexity="trivial", model="sonnet")
        medium = compute_max_turns(complexity="medium", model="sonnet")
        assert trivial.max_turns <= medium.max_turns

    def test_critical_has_more_turns(self) -> None:
        medium = compute_max_turns(complexity="medium", model="sonnet")
        critical = compute_max_turns(complexity="critical", model="sonnet")
        assert critical.max_turns >= medium.max_turns

    def test_timeout_constrains_turns(self) -> None:
        short = compute_max_turns(complexity="critical", model="sonnet", timeout_s=60)
        long = compute_max_turns(complexity="critical", model="sonnet", timeout_s=3600)
        assert short.max_turns <= long.max_turns

    def test_very_short_timeout(self) -> None:
        cfg = compute_max_turns(complexity="critical", model="sonnet", timeout_s=30)
        assert cfg.constrained_by_timeout
        assert cfg.max_turns >= 5  # min_turns floor.

    def test_min_turns_floor(self) -> None:
        cfg = compute_max_turns(complexity="trivial", model="sonnet", timeout_s=10, min_turns=5)
        assert cfg.max_turns >= 5

    def test_max_turns_cap(self) -> None:
        cfg = compute_max_turns(complexity="critical", model="sonnet", timeout_s=99999, max_turns_cap=100)
        assert cfg.max_turns <= 100

    def test_opus_slower_than_haiku(self) -> None:
        opus = compute_max_turns(complexity="medium", model="opus", timeout_s=600)
        haiku = compute_max_turns(complexity="medium", model="haiku", timeout_s=600)
        # Haiku has higher TPM so should allow more turns in same timeout.
        assert haiku.turns_per_minute > opus.turns_per_minute

    def test_reasoning_non_empty(self) -> None:
        cfg = compute_max_turns(complexity="medium", model="sonnet")
        assert len(cfg.reasoning) > 0


class TestMaxTurnsConfig:
    def test_to_dict(self) -> None:
        cfg = MaxTurnsConfig(
            max_turns=40,
            complexity="medium",
            model="sonnet",
            timeout_s=1800,
            turns_per_minute=3.0,
            constrained_by_timeout=False,
            reasoning="test",
        )
        d = cfg.to_dict()
        assert d["max_turns"] == 40
        assert d["model"] == "sonnet"


class TestMaxTurnsCoordinator:
    def test_compute_for_task(self) -> None:
        coord = MaxTurnsCoordinator()
        cfg = coord.compute_for_task("s1", complexity="medium", model="sonnet")
        assert cfg.max_turns > 0

    def test_get_max_turns(self) -> None:
        coord = MaxTurnsCoordinator()
        coord.compute_for_task("s1", complexity="medium", model="sonnet")
        turns = coord.get_max_turns("s1")
        assert turns is not None
        assert turns > 0

    def test_get_max_turns_unknown(self) -> None:
        coord = MaxTurnsCoordinator()
        assert coord.get_max_turns("unknown") is None

    def test_summary(self) -> None:
        coord = MaxTurnsCoordinator()
        coord.compute_for_task("s1", complexity="low", model="sonnet")
        coord.compute_for_task("s2", complexity="high", model="opus")
        summary = coord.summary()
        assert summary["sessions"] == 2
        assert summary["min_turns"] <= summary["max_turns"]

    def test_summary_empty(self) -> None:
        coord = MaxTurnsCoordinator()
        summary = coord.summary()
        assert summary["sessions"] == 0

    def test_default_timeout(self) -> None:
        coord = MaxTurnsCoordinator(default_timeout_s=600)
        cfg = coord.compute_for_task("s1", complexity="medium")
        assert cfg.timeout_s == 600

    def test_explicit_timeout_overrides(self) -> None:
        coord = MaxTurnsCoordinator(default_timeout_s=600)
        cfg = coord.compute_for_task("s1", complexity="medium", timeout_s=300)
        assert cfg.timeout_s == 300
