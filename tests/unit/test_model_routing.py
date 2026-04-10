"""Unit tests for multi-model conversation routing."""

from __future__ import annotations

import pytest

from bernstein.core.model_routing import (
    DEFAULT_ROUTING,
    ConversationPhase,
    ModelRoutingStrategy,
    PhaseModelConfig,
    detect_phase,
    get_phase_config,
    load_routing_strategy,
)

# ---------------------------------------------------------------------------
# ConversationPhase enum
# ---------------------------------------------------------------------------


class TestConversationPhase:
    """ConversationPhase has the expected members."""

    def test_members(self) -> None:
        assert ConversationPhase.PLANNING.value == "planning"
        assert ConversationPhase.IMPLEMENTATION.value == "implementation"
        assert ConversationPhase.REVIEW.value == "review"
        assert ConversationPhase.CLEANUP.value == "cleanup"

    def test_count(self) -> None:
        assert len(ConversationPhase) == 4


# ---------------------------------------------------------------------------
# PhaseModelConfig
# ---------------------------------------------------------------------------


class TestPhaseModelConfig:
    """PhaseModelConfig creation and immutability."""

    def test_create(self) -> None:
        cfg = PhaseModelConfig(
            phase=ConversationPhase.PLANNING,
            model="opus",
            max_turns=5,
            effort="high",
        )
        assert cfg.phase == ConversationPhase.PLANNING
        assert cfg.model == "opus"
        assert cfg.max_turns == 5
        assert cfg.effort == "high"

    def test_frozen(self) -> None:
        cfg = PhaseModelConfig(
            phase=ConversationPhase.PLANNING,
            model="opus",
            max_turns=5,
            effort="high",
        )
        with pytest.raises(AttributeError):
            cfg.model = "sonnet"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DEFAULT_ROUTING
# ---------------------------------------------------------------------------


class TestDefaultRouting:
    """DEFAULT_ROUTING covers all four phases with sensible values."""

    def test_covers_all_phases(self) -> None:
        phases = {cfg.phase for cfg in DEFAULT_ROUTING}
        assert phases == set(ConversationPhase)

    def test_length(self) -> None:
        assert len(DEFAULT_ROUTING) == 4

    def test_planning_defaults(self) -> None:
        planning = next(c for c in DEFAULT_ROUTING if c.phase == ConversationPhase.PLANNING)
        assert planning.model == "opus"
        assert planning.max_turns == 5

    def test_implementation_defaults(self) -> None:
        impl = next(c for c in DEFAULT_ROUTING if c.phase == ConversationPhase.IMPLEMENTATION)
        assert impl.model == "sonnet"
        assert impl.max_turns == 30

    def test_cleanup_defaults(self) -> None:
        cleanup = next(c for c in DEFAULT_ROUTING if c.phase == ConversationPhase.CLEANUP)
        assert cleanup.model == "haiku"


# ---------------------------------------------------------------------------
# load_routing_strategy
# ---------------------------------------------------------------------------


class TestLoadRoutingStrategy:
    """load_routing_strategy parses task dicts correctly."""

    def test_returns_none_without_model_routing(self) -> None:
        task = {"id": "t-1", "goal": "do stuff"}
        assert load_routing_strategy(task) is None

    def test_returns_none_for_empty_phases(self) -> None:
        task = {"id": "t-1", "model_routing": {"phases": []}}
        assert load_routing_strategy(task) is None

    def test_returns_none_for_non_dict_routing(self) -> None:
        task = {"id": "t-1", "model_routing": "auto"}
        assert load_routing_strategy(task) is None

    def test_parses_valid_routing(self) -> None:
        task = {
            "id": "task-42",
            "model_routing": {
                "phases": [
                    {"phase": "planning", "model": "opus", "max_turns": 3, "effort": "high"},
                    {"phase": "implementation", "model": "sonnet", "max_turns": 20, "effort": "high"},
                ]
            },
        }
        strategy = load_routing_strategy(task)
        assert strategy is not None
        assert strategy.task_id == "task-42"
        assert len(strategy.phases) == 2
        assert strategy.phases[0].phase == ConversationPhase.PLANNING
        assert strategy.phases[0].model == "opus"
        assert strategy.phases[1].phase == ConversationPhase.IMPLEMENTATION

    def test_skips_unknown_phases(self) -> None:
        task = {
            "id": "t-1",
            "model_routing": {
                "phases": [
                    {"phase": "planning", "model": "opus", "max_turns": 5, "effort": "high"},
                    {"phase": "unknown_phase", "model": "haiku", "max_turns": 1, "effort": "low"},
                ]
            },
        }
        strategy = load_routing_strategy(task)
        assert strategy is not None
        assert len(strategy.phases) == 1
        assert strategy.phases[0].phase == ConversationPhase.PLANNING

    def test_returns_none_when_all_phases_invalid(self) -> None:
        task = {
            "id": "t-1",
            "model_routing": {
                "phases": [
                    {"phase": "bogus", "model": "opus", "max_turns": 5, "effort": "high"},
                ]
            },
        }
        assert load_routing_strategy(task) is None

    def test_defaults_for_missing_fields(self) -> None:
        task = {
            "id": "t-1",
            "model_routing": {
                "phases": [
                    {"phase": "review"},
                ]
            },
        }
        strategy = load_routing_strategy(task)
        assert strategy is not None
        cfg = strategy.phases[0]
        assert cfg.model == "sonnet"
        assert cfg.max_turns == 10
        assert cfg.effort == "medium"


# ---------------------------------------------------------------------------
# get_phase_config
# ---------------------------------------------------------------------------


class TestGetPhaseConfig:
    """get_phase_config looks up the strategy then falls back to defaults."""

    @pytest.fixture()
    def strategy(self) -> ModelRoutingStrategy:
        return ModelRoutingStrategy(
            task_id="t-1",
            phases=(
                PhaseModelConfig(
                    phase=ConversationPhase.PLANNING,
                    model="opus",
                    max_turns=3,
                    effort="high",
                ),
                PhaseModelConfig(
                    phase=ConversationPhase.IMPLEMENTATION,
                    model="sonnet",
                    max_turns=25,
                    effort="high",
                ),
            ),
        )

    def test_returns_strategy_config(self, strategy: ModelRoutingStrategy) -> None:
        cfg = get_phase_config(strategy, ConversationPhase.PLANNING)
        assert cfg.model == "opus"
        assert cfg.max_turns == 3

    def test_falls_back_to_default(self, strategy: ModelRoutingStrategy) -> None:
        cfg = get_phase_config(strategy, ConversationPhase.REVIEW)
        # Strategy has no REVIEW phase, so we get the default.
        default = next(c for c in DEFAULT_ROUTING if c.phase == ConversationPhase.REVIEW)
        assert cfg == default

    def test_falls_back_cleanup(self, strategy: ModelRoutingStrategy) -> None:
        cfg = get_phase_config(strategy, ConversationPhase.CLEANUP)
        default = next(c for c in DEFAULT_ROUTING if c.phase == ConversationPhase.CLEANUP)
        assert cfg == default


# ---------------------------------------------------------------------------
# detect_phase
# ---------------------------------------------------------------------------


class TestDetectPhase:
    """detect_phase returns the right phase based on turn position."""

    @pytest.fixture()
    def strategy(self) -> ModelRoutingStrategy:
        return ModelRoutingStrategy(task_id="t-1", phases=tuple(DEFAULT_ROUTING))

    # ---- boundary tests (100 total turns) --------------------------------

    def test_turn_0_is_planning(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(0, 100, strategy) == ConversationPhase.PLANNING

    def test_turn_19_is_planning(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(19, 100, strategy) == ConversationPhase.PLANNING

    def test_turn_20_is_implementation(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(20, 100, strategy) == ConversationPhase.IMPLEMENTATION

    def test_turn_79_is_implementation(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(79, 100, strategy) == ConversationPhase.IMPLEMENTATION

    def test_turn_80_is_review(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(80, 100, strategy) == ConversationPhase.REVIEW

    def test_turn_94_is_review(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(94, 100, strategy) == ConversationPhase.REVIEW

    def test_turn_95_is_cleanup(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(95, 100, strategy) == ConversationPhase.CLEANUP

    def test_turn_99_is_cleanup(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(99, 100, strategy) == ConversationPhase.CLEANUP

    # ---- edge cases -------------------------------------------------------

    def test_zero_total_turns(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(0, 0, strategy) == ConversationPhase.PLANNING

    def test_negative_total_turns(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(5, -1, strategy) == ConversationPhase.PLANNING

    def test_single_turn(self, strategy: ModelRoutingStrategy) -> None:
        # With 1 total turn, turn 0 fraction = 0.0 -> PLANNING.
        assert detect_phase(0, 1, strategy) == ConversationPhase.PLANNING

    def test_negative_turn_clamped(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(-5, 100, strategy) == ConversationPhase.PLANNING

    def test_oversized_turn_clamped(self, strategy: ModelRoutingStrategy) -> None:
        # turn_number beyond total_turns clamps to last turn -> CLEANUP.
        assert detect_phase(200, 100, strategy) == ConversationPhase.CLEANUP

    # ---- small session (10 turns) ----------------------------------------

    def test_small_session_planning(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(0, 10, strategy) == ConversationPhase.PLANNING
        assert detect_phase(1, 10, strategy) == ConversationPhase.PLANNING

    def test_small_session_implementation(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(2, 10, strategy) == ConversationPhase.IMPLEMENTATION
        assert detect_phase(7, 10, strategy) == ConversationPhase.IMPLEMENTATION

    def test_small_session_review(self, strategy: ModelRoutingStrategy) -> None:
        assert detect_phase(8, 10, strategy) == ConversationPhase.REVIEW

    def test_small_session_cleanup(self, strategy: ModelRoutingStrategy) -> None:
        # 9/10 = 0.9 -> still REVIEW (< 0.95). Last fraction before cleanup.
        assert detect_phase(9, 10, strategy) == ConversationPhase.REVIEW
