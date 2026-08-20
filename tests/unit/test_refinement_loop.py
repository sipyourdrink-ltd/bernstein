"""Tests for the iterative self-refinement orchestration loop.

Covers the four mandated categories:

* Unit tests (round counting, plateau detection, threshold gate,
  budget exhaustion, adversary veto, gate halt, mutual exclusion).
* Property tests (monotone-in-budget, deterministic-with-seed,
  round-count invariants).
* Integration tests using a mock adapter that runs the full
  drafter→critic→refiner loop end-to-end.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.orchestration.refinement_loop import (
    DEFAULT_SCORE_THRESHOLD,
    MAX_REFINEMENT_ROUNDS,
    MIN_REFINEMENT_ROUNDS,
    PLATEAU_WINDOW,
    RefinementLoopRunner,
    RoundArtefact,
    clamp_rounds,
    detect_plateau,
    is_refinement,
    parse_refine_spec,
    task_rounds,
)
from bernstein.core.orchestration.refinement_schemas import (
    Critique,
    CritiqueIssue,
    clamp_score,
)
from bernstein.core.tasks.models import (
    Complexity,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)

# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


def _task(
    *,
    task_id: str = "trend-1403",
    refinement_rounds: int | None = 3,
    best_of_n: int | None = None,
    role: str = "backend",
) -> Task:
    """Build a Task with the refinement field wired up."""
    return Task(
        id=task_id,
        title="iterative refinement",
        description="trend-1403 spec",
        role=role,
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        refinement_rounds=refinement_rounds,
        best_of_n=best_of_n,
    )


@dataclass
class _ScriptedCritic:
    """Critic stub that returns scores from a predetermined script.

    The runner appends one critique per round; this lets tests assert
    early-stop behaviour against an explicit score curve.
    """

    scores: list[float]
    veto_rounds: set[int] = field(default_factory=set[int])
    issues_per_round: list[list[CritiqueIssue]] | None = None
    calls: int = 0

    def __call__(self, task: Task, artefact: RoundArtefact, round_index: int) -> Critique:
        idx = self.calls
        self.calls += 1
        score = self.scores[idx] if idx < len(self.scores) else self.scores[-1]
        issues: list[CritiqueIssue] = []
        if self.issues_per_round is not None and idx < len(self.issues_per_round):
            issues = self.issues_per_round[idx].copy()
        veto = round_index in self.veto_rounds
        return Critique(score=score, issues=issues, veto=veto, rationale=f"round-{round_index}")


@dataclass
class _CountingDrafter:
    """Drafter stub that yields a fixed cost per round."""

    cost_per_round: float = 0.10
    calls: int = 0

    def __call__(self, task: Task) -> RoundArtefact:
        self.calls += 1
        return RoundArtefact(content="draft", cost_usd=self.cost_per_round)


@dataclass
class _CountingRefiner:
    """Refiner stub that bumps the artefact content per round."""

    cost_per_round: float = 0.10
    calls: int = 0

    def __call__(self, task: Task, prior: RoundArtefact, critique: Critique) -> RoundArtefact:
        self.calls += 1
        return RoundArtefact(content=f"{prior.content}+{critique.score:.3f}", cost_usd=self.cost_per_round)


def _runner(
    *,
    critic: Callable[[Task, RoundArtefact, int], Critique],
    drafter: _CountingDrafter | None = None,
    refiner: _CountingRefiner | None = None,
    gate_runner: Callable[[Task, RoundArtefact, int], bool] | None = None,
    budget_usd: float | None = None,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    plateau_window: int = PLATEAU_WINDOW,
    seed: int | None = None,
) -> RefinementLoopRunner:
    return RefinementLoopRunner(
        drafter=drafter or _CountingDrafter(),
        refiner=refiner or _CountingRefiner(),
        critic=critic,
        gate_runner=gate_runner,
        budget_usd=budget_usd,
        score_threshold=score_threshold,
        plateau_window=plateau_window,
        seed=seed,
    )


@pytest.fixture(autouse=True)
def _disable_observability(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Silence decision-log and calibration writers during tests."""
    monkeypatch.setenv("BERNSTEIN_DECISION_LOG", "0")
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_clamp_score_below_zero() -> None:
    assert clamp_score(-1.0) == 0.0


def test_clamp_score_above_one() -> None:
    assert clamp_score(2.5) == 1.0


def test_clamp_score_passthrough() -> None:
    assert clamp_score(0.42) == 0.42


def test_critique_round_trip_dict() -> None:
    original = Critique(
        score=0.7,
        issues=[CritiqueIssue(severity="high", message="m", suggestion="s")],
        veto=False,
        rationale="r",
    )
    restored = Critique.from_dict(original.to_dict())
    assert restored == original


def test_critique_from_dict_missing_fields_uses_defaults() -> None:
    restored = Critique.from_dict({})
    assert restored.score == 0.0
    assert restored.issues == []
    assert restored.veto is False
    assert restored.rationale == ""


def test_critique_from_dict_clamps_score() -> None:
    restored = Critique.from_dict({"score": 7.5})
    assert restored.score == 1.0


def test_critique_issue_from_dict_defaults() -> None:
    issue = CritiqueIssue.from_dict({})
    assert issue.severity == "low"
    assert issue.message == ""
    assert issue.suggestion == ""


def test_critique_from_dict_drops_non_dict_issues() -> None:
    restored = Critique.from_dict({"issues": [{"severity": "low"}, "garbage", 42]})
    assert len(restored.issues) == 1


def test_critique_to_dict_clamps_negative_score() -> None:
    assert Critique(score=-3.0).to_dict()["score"] == 0.0


def test_critique_veto_persists_through_dict() -> None:
    restored = Critique.from_dict({"veto": True, "score": 0.4})
    assert restored.veto is True


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_clamp_rounds_below_min_collapses_to_one() -> None:
    assert clamp_rounds(0) == 1
    assert clamp_rounds(1) == 1


def test_clamp_rounds_in_range() -> None:
    assert clamp_rounds(3) == 3


def test_clamp_rounds_above_max_is_capped() -> None:
    assert clamp_rounds(99) == MAX_REFINEMENT_ROUNDS


def test_is_refinement_for_opted_in_task() -> None:
    assert is_refinement(_task(refinement_rounds=3)) is True


def test_is_refinement_for_none_returns_false() -> None:
    assert is_refinement(_task(refinement_rounds=None)) is False


def test_is_refinement_for_below_minimum_returns_false() -> None:
    assert is_refinement(_task(refinement_rounds=1)) is False


def test_task_rounds_returns_one_for_legacy_task() -> None:
    assert task_rounds(_task(refinement_rounds=None)) == 1


def test_task_rounds_clamps_high_values() -> None:
    assert task_rounds(_task(refinement_rounds=99)) == MAX_REFINEMENT_ROUNDS


def test_task_rounds_returns_configured_value() -> None:
    assert task_rounds(_task(refinement_rounds=4)) == 4


def test_detect_plateau_empty_returns_false() -> None:
    assert detect_plateau([]) is False


def test_detect_plateau_short_returns_false() -> None:
    assert detect_plateau([0.5]) is False


def test_detect_plateau_strict_decline() -> None:
    assert detect_plateau([0.5, 0.4, 0.3]) is True


def test_detect_plateau_flat() -> None:
    assert detect_plateau([0.5, 0.5, 0.5]) is True


def test_detect_plateau_still_climbing() -> None:
    assert detect_plateau([0.1, 0.3, 0.5]) is False


def test_detect_plateau_rejects_window_zero() -> None:
    with pytest.raises(ValueError):
        detect_plateau([0.5, 0.5], window=0)


def test_detect_plateau_custom_window() -> None:
    assert detect_plateau([0.1, 0.5, 0.4, 0.4, 0.4], window=3) is True
    # Pivot=0.05; last three include a value > pivot → not a plateau.
    assert detect_plateau([0.05, 0.1, 0.4, 0.4, 0.45], window=3) is False


# ---------------------------------------------------------------------------
# Spec parser unit tests
# ---------------------------------------------------------------------------


def test_parse_refine_spec_full() -> None:
    spec = parse_refine_spec("rounds:3,critic:adversary,stop:plateau,threshold:0.8")
    assert spec.rounds == 3
    assert spec.critic == "adversary"
    assert spec.stop == "plateau"
    assert spec.score_threshold == 0.8


def test_parse_refine_spec_partial_defaults() -> None:
    spec = parse_refine_spec("rounds:2")
    assert spec.rounds == 2
    assert spec.critic == "adversary"
    assert spec.stop == "rounds"
    assert spec.score_threshold == DEFAULT_SCORE_THRESHOLD


def test_parse_refine_spec_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("")


def test_parse_refine_spec_whitespace_only_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("   ")


def test_parse_refine_spec_missing_colon_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("rounds3")


def test_parse_refine_spec_rounds_below_min_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("rounds:1")


def test_parse_refine_spec_rounds_above_max_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec(f"rounds:{MAX_REFINEMENT_ROUNDS + 1}")


def test_parse_refine_spec_rounds_non_integer_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("rounds:two")


def test_parse_refine_spec_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("bogus:42")


def test_parse_refine_spec_unknown_stop_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("stop:explode")


def test_parse_refine_spec_threshold_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("threshold:1.5")


def test_parse_refine_spec_threshold_non_float_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("threshold:abc")


def test_parse_refine_spec_empty_critic_raises() -> None:
    with pytest.raises(ValueError):
        parse_refine_spec("critic:")


def test_parse_refine_spec_trims_whitespace() -> None:
    spec = parse_refine_spec("  rounds : 4 , critic : adversary  ")
    assert spec.rounds == 4
    assert spec.critic == "adversary"


def test_parse_refine_spec_skips_empty_entries() -> None:
    spec = parse_refine_spec("rounds:3,,critic:reviewer,")
    assert spec.rounds == 3
    assert spec.critic == "reviewer"


# ---------------------------------------------------------------------------
# Mutual-exclusion unit tests
# ---------------------------------------------------------------------------


def test_mutual_exclusion_raises_when_both_set() -> None:
    runner = _runner(critic=_ScriptedCritic(scores=[0.5, 0.6]))
    task = _task(refinement_rounds=2, best_of_n=3)
    with pytest.raises(ValueError):
        runner.run(task)


def test_mutual_exclusion_allows_only_refinement() -> None:
    runner = _runner(critic=_ScriptedCritic(scores=[1.0]))
    task = _task(refinement_rounds=2, best_of_n=None)
    runner.run(task)


def test_mutual_exclusion_allows_only_best_of_n() -> None:
    runner = _runner(critic=_ScriptedCritic(scores=[1.0]))
    task = _task(refinement_rounds=None, best_of_n=3)
    # When refinement is not opted in, the runner refuses with a clearer
    # error than the mutual-exclusion path.
    with pytest.raises(ValueError):
        runner.run(task)


def test_run_below_minimum_rounds_raises() -> None:
    runner = _runner(critic=_ScriptedCritic(scores=[1.0]))
    task = _task(refinement_rounds=1)
    with pytest.raises(ValueError):
        runner.run(task)


def test_run_with_invalid_plateau_window_raises() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.1, 0.1])
    runner = _runner(critic=critic, plateau_window=0)
    with pytest.raises(ValueError):
        runner.run(_task(refinement_rounds=3))


# ---------------------------------------------------------------------------
# Round-counting unit tests
# ---------------------------------------------------------------------------


def test_runs_full_budget_when_no_early_stop() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.rounds_run == 3
    assert report.early_stop_reason == "rounds"
    assert len(report.per_round_critique) == 3
    assert len(report.per_round_cost) == 3
    assert len(report.per_round_quality_score) == 3


def test_per_round_cost_records_each_round() -> None:
    drafter = _CountingDrafter(cost_per_round=0.05)
    refiner = _CountingRefiner(cost_per_round=0.07)
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert report.per_round_cost == [0.05, 0.07, 0.07]
    assert math.isclose(report.cumulative_cost_usd, 0.19, rel_tol=1e-6)


def test_critic_call_count_matches_rounds_run() -> None:
    critic = _ScriptedCritic(scores=[0.5, 0.5, 0.5, 0.5])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=10)
    runner.run(_task(refinement_rounds=4))
    assert critic.calls == 4


def test_refiner_invoked_once_less_than_rounds() -> None:
    drafter = _CountingDrafter()
    refiner = _CountingRefiner()
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        score_threshold=1.0,
        plateau_window=10,
    )
    runner.run(_task(refinement_rounds=3))
    assert drafter.calls == 1
    assert refiner.calls == 2  # rounds-1: refiner is not called after the last round


def test_final_artefact_is_last_round_artefact() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert "draft" in report.final_artefact.content


# ---------------------------------------------------------------------------
# Plateau early-stop unit tests
# ---------------------------------------------------------------------------


def test_plateau_stops_on_flat_scores() -> None:
    critic = _ScriptedCritic(scores=[0.5, 0.5, 0.5, 0.5, 0.5])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=2)
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "plateau"
    assert report.rounds_run == 3


def test_plateau_stops_on_declining_scores() -> None:
    critic = _ScriptedCritic(scores=[0.6, 0.5, 0.4])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=2)
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "plateau"
    assert report.rounds_run == 3


def test_plateau_not_triggered_when_still_climbing() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.3, 0.5])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=2)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "rounds"


# ---------------------------------------------------------------------------
# Threshold early-stop unit tests
# ---------------------------------------------------------------------------


def test_threshold_stops_loop_when_score_hits_gate() -> None:
    critic = _ScriptedCritic(scores=[0.5, 0.95, 0.99])
    runner = _runner(critic=critic, score_threshold=0.9, plateau_window=10)
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "threshold"
    assert report.rounds_run == 2


def test_threshold_at_first_round_short_circuits() -> None:
    critic = _ScriptedCritic(scores=[0.99])
    runner = _runner(critic=critic, score_threshold=0.9, plateau_window=10)
    report = runner.run(_task(refinement_rounds=5))
    assert report.rounds_run == 1
    assert report.early_stop_reason == "threshold"


def test_threshold_inclusive_boundary() -> None:
    critic = _ScriptedCritic(scores=[0.9])
    runner = _runner(critic=critic, score_threshold=0.9, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "threshold"


def test_threshold_not_hit_runs_full_budget() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(critic=critic, score_threshold=0.99, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "rounds"


# ---------------------------------------------------------------------------
# Budget early-stop unit tests
# ---------------------------------------------------------------------------


def test_budget_circuit_breaker_stops_when_cap_reached() -> None:
    drafter = _CountingDrafter(cost_per_round=1.0)
    refiner = _CountingRefiner(cost_per_round=1.0)
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3, 0.4])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        budget_usd=2.0,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=4))
    assert report.early_stop_reason == "budget"
    assert report.rounds_run == 2


def test_budget_none_disables_breaker() -> None:
    drafter = _CountingDrafter(cost_per_round=100.0)
    refiner = _CountingRefiner(cost_per_round=100.0)
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        budget_usd=None,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "rounds"


def test_budget_zero_disables_breaker() -> None:
    drafter = _CountingDrafter(cost_per_round=10.0)
    refiner = _CountingRefiner(cost_per_round=10.0)
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        budget_usd=0.0,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "rounds"


def test_budget_with_negative_cost_clamped_to_zero() -> None:
    @dataclass
    class _NegDrafter:
        def __call__(self, task: Task) -> RoundArtefact:
            return RoundArtefact(content="d", cost_usd=-5.0)

    @dataclass
    class _NegRefiner:
        def __call__(self, task: Task, prior: RoundArtefact, critique: Critique) -> RoundArtefact:
            return RoundArtefact(content="r", cost_usd=-5.0)

    critic = _ScriptedCritic(scores=[0.1, 0.2])
    runner = RefinementLoopRunner(
        drafter=_NegDrafter(),
        refiner=_NegRefiner(),
        critic=critic,
        budget_usd=1.0,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=2))
    # Negative costs clamped to zero  -  budget never trips.
    assert report.early_stop_reason == "rounds"
    assert report.cumulative_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Gate early-stop unit tests
# ---------------------------------------------------------------------------


def _gate_fail_on_round_two(task: Task, artefact: RoundArtefact, round_index: int) -> bool:
    return round_index != 2


def _gate_always_pass(task: Task, artefact: RoundArtefact, round_index: int) -> bool:
    return True


def _gate_always_fail(task: Task, artefact: RoundArtefact, round_index: int) -> bool:
    return False


def test_gate_halts_loop_on_failure() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.5, 0.9])
    runner = _runner(critic=critic, gate_runner=_gate_fail_on_round_two, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "gate"
    assert report.gate_failed_round == 2
    assert report.rounds_run == 2


def test_gate_pass_lets_loop_continue() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(critic=critic, gate_runner=_gate_always_pass, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "rounds"
    assert report.gate_failed_round is None


def test_gate_first_round_failure_short_circuits() -> None:
    critic = _ScriptedCritic(scores=[0.5])
    runner = _runner(critic=critic, gate_runner=_gate_always_fail, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.rounds_run == 1
    assert report.gate_failed_round == 1


# ---------------------------------------------------------------------------
# Adversary-veto unit tests
# ---------------------------------------------------------------------------


def test_adversary_veto_short_circuits() -> None:
    critic = _ScriptedCritic(scores=[0.5, 0.9, 0.99], veto_rounds={2})
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "adversary_veto"
    assert report.rounds_run == 2


def test_adversary_veto_at_round_one() -> None:
    critic = _ScriptedCritic(scores=[0.99], veto_rounds={1})
    runner = _runner(critic=critic, score_threshold=0.5, plateau_window=10)
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "adversary_veto"
    assert report.rounds_run == 1


def test_adversary_veto_takes_precedence_over_threshold() -> None:
    critic = _ScriptedCritic(scores=[0.99], veto_rounds={1})
    runner = _runner(critic=critic, score_threshold=0.5, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.early_stop_reason == "adversary_veto"


def test_critic_returns_out_of_range_score_clamped() -> None:
    @dataclass
    class _BadCritic:
        def __call__(self, t: Task, a: RoundArtefact, r: int) -> Critique:
            return Critique(score=2.5)

    runner = _runner(critic=_BadCritic(), score_threshold=0.9, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    # Clamped to 1.0 → threshold trips on round 1.
    assert report.early_stop_reason == "threshold"
    assert report.per_round_quality_score[0] == 1.0


# ---------------------------------------------------------------------------
# Report structure unit tests
# ---------------------------------------------------------------------------


def test_report_is_frozen_dataclass() -> None:
    from dataclasses import FrozenInstanceError

    critic = _ScriptedCritic(scores=[1.0])
    runner = _runner(critic=critic, score_threshold=0.5, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    with pytest.raises(FrozenInstanceError):
        report.rounds_run = 99  # type: ignore[misc]


def test_report_cumulative_cost_is_sum_of_per_round() -> None:
    drafter = _CountingDrafter(cost_per_round=0.1)
    refiner = _CountingRefiner(cost_per_round=0.2)
    critic = _ScriptedCritic(scores=[0.1, 0.2, 0.3])
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert math.isclose(report.cumulative_cost_usd, sum(report.per_round_cost))


def test_report_per_round_critique_count_matches_rounds_run() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=10)
    report = runner.run(_task(refinement_rounds=2))
    assert len(report.per_round_critique) == report.rounds_run


def test_report_score_list_matches_critique_scores() -> None:
    critic = _ScriptedCritic(scores=[0.2, 0.5, 0.9])
    runner = _runner(critic=critic, score_threshold=0.99, plateau_window=10)
    report = runner.run(_task(refinement_rounds=3))
    assert report.per_round_quality_score == [c.score for c in report.per_round_critique]


# ---------------------------------------------------------------------------
# Seeded determinism unit tests
# ---------------------------------------------------------------------------


def test_seeded_runner_uses_local_rng() -> None:
    critic = _ScriptedCritic(scores=[0.1])
    runner = _runner(critic=critic, seed=42, score_threshold=0.5, plateau_window=10)
    runner.run(_task(refinement_rounds=2))
    # ``_rng`` is set as an attribute after run()  -  type ignore because the
    # dataclass does not declare it.  Functional contract is that the seed
    # is used; we assert the attribute exists.
    assert hasattr(runner, "_rng")


def test_unseeded_runner_still_runs() -> None:
    critic = _ScriptedCritic(scores=[0.1, 0.2])
    runner = _runner(critic=critic, seed=None, score_threshold=0.5, plateau_window=10)
    report = runner.run(_task(refinement_rounds=2))
    assert report.rounds_run == 2


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


@settings(
    derandomize=True,
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
    threshold=st.floats(min_value=0.0, max_value=1.0),
)
def test_property_rounds_run_never_exceeds_budget(rounds: int, threshold: float) -> None:
    critic = _ScriptedCritic(scores=[0.0] * rounds)
    runner = _runner(critic=critic, score_threshold=threshold, plateau_window=rounds + 10)
    report = runner.run(_task(refinement_rounds=rounds))
    assert report.rounds_run <= rounds


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
    score=st.floats(min_value=0.0, max_value=1.0).filter(lambda x: not math.isnan(x)),
)
def test_property_threshold_invariant_score_above_stops(rounds: int, score: float) -> None:
    critic = _ScriptedCritic(scores=[score] * rounds)
    threshold = 0.5
    runner = _runner(critic=critic, score_threshold=threshold, plateau_window=rounds + 10)
    report = runner.run(_task(refinement_rounds=rounds))
    if score >= threshold:
        assert report.early_stop_reason == "threshold"
        assert report.rounds_run == 1


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    budget=st.floats(min_value=0.01, max_value=10.0),
    per_round_cost=st.floats(min_value=0.01, max_value=2.0),
)
def test_property_monotone_in_budget(budget: float, per_round_cost: float) -> None:
    """Increasing the budget never reduces rounds_run."""
    drafter = _CountingDrafter(cost_per_round=per_round_cost)
    refiner = _CountingRefiner(cost_per_round=per_round_cost)
    critic = _ScriptedCritic(scores=[0.1] * MAX_REFINEMENT_ROUNDS)
    runner_small = _runner(
        critic=_ScriptedCritic(scores=[0.1] * MAX_REFINEMENT_ROUNDS),
        drafter=_CountingDrafter(cost_per_round=per_round_cost),
        refiner=_CountingRefiner(cost_per_round=per_round_cost),
        budget_usd=budget,
        score_threshold=1.0,
        plateau_window=MAX_REFINEMENT_ROUNDS + 10,
    )
    runner_large = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        budget_usd=budget * 10.0,
        score_threshold=1.0,
        plateau_window=MAX_REFINEMENT_ROUNDS + 10,
    )
    task = _task(refinement_rounds=MAX_REFINEMENT_ROUNDS)
    report_small = runner_small.run(task)
    report_large = runner_large.run(task)
    assert report_large.rounds_run >= report_small.rounds_run


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
)
def test_property_deterministic_with_seed(seed: int, rounds: int) -> None:
    """Two runners with the same seed produce identical reports."""
    scores = [0.1 + 0.1 * i for i in range(rounds)]
    r1 = _runner(critic=_ScriptedCritic(scores=scores.copy()), seed=seed, plateau_window=rounds + 10)
    r2 = _runner(critic=_ScriptedCritic(scores=scores.copy()), seed=seed, plateau_window=rounds + 10)
    rep1 = r1.run(_task(refinement_rounds=rounds))
    rep2 = r2.run(_task(refinement_rounds=rounds))
    assert rep1.rounds_run == rep2.rounds_run
    assert rep1.early_stop_reason == rep2.early_stop_reason
    assert rep1.per_round_quality_score == rep2.per_round_quality_score


@settings(
    derandomize=True,
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(scores=st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=0, max_size=10))
def test_property_detect_plateau_never_fires_on_strict_growth(scores: list[float]) -> None:
    """When scores strictly increase, plateau is never triggered."""
    growing = sorted({round(s, 3) for s in scores})
    if len(growing) < PLATEAU_WINDOW + 1:
        return
    assert detect_plateau(growing, window=PLATEAU_WINDOW) is False


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(score=st.floats(min_value=-100.0, max_value=100.0).filter(lambda x: not math.isnan(x)))
def test_property_clamp_score_in_unit_interval(score: float) -> None:
    out = clamp_score(score)
    assert 0.0 <= out <= 1.0


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rounds=st.integers(min_value=-50, max_value=200))
def test_property_clamp_rounds_in_range(rounds: int) -> None:
    out = clamp_rounds(rounds)
    if rounds < MIN_REFINEMENT_ROUNDS:
        assert out == 1
    else:
        assert MIN_REFINEMENT_ROUNDS <= out <= MAX_REFINEMENT_ROUNDS


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
    cost=st.floats(min_value=0.0, max_value=5.0),
)
def test_property_cumulative_cost_is_nonnegative(rounds: int, cost: float) -> None:
    drafter = _CountingDrafter(cost_per_round=cost)
    refiner = _CountingRefiner(cost_per_round=cost)
    critic = _ScriptedCritic(scores=[0.0] * rounds)
    runner = _runner(
        critic=critic,
        drafter=drafter,
        refiner=refiner,
        score_threshold=1.0,
        plateau_window=rounds + 10,
    )
    report = runner.run(_task(refinement_rounds=rounds))
    assert report.cumulative_cost_usd >= 0.0


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS))
def test_property_per_round_lists_align(rounds: int) -> None:
    critic = _ScriptedCritic(scores=[0.1 * i for i in range(rounds + 2)])
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=rounds + 10)
    report = runner.run(_task(refinement_rounds=rounds))
    assert len(report.per_round_cost) == report.rounds_run
    assert len(report.per_round_critique) == report.rounds_run
    assert len(report.per_round_quality_score) == report.rounds_run


@settings(
    derandomize=True,
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(veto_round=st.integers(min_value=1, max_value=MAX_REFINEMENT_ROUNDS))
def test_property_adversary_veto_stops_no_later_than_veto_round(veto_round: int) -> None:
    rounds = MAX_REFINEMENT_ROUNDS
    critic = _ScriptedCritic(scores=[0.1] * rounds, veto_rounds={veto_round})
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=rounds + 10)
    report = runner.run(_task(refinement_rounds=rounds))
    if veto_round <= rounds:
        assert report.rounds_run <= veto_round
    if report.rounds_run == veto_round:
        assert report.early_stop_reason == "adversary_veto"


@settings(
    derandomize=True,
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS))
def test_property_gate_fail_round_set_iff_gate_stop(rounds: int) -> None:
    critic = _ScriptedCritic(scores=[0.1] * rounds)
    runner = _runner(critic=critic, score_threshold=1.0, plateau_window=rounds + 10)
    report = runner.run(_task(refinement_rounds=rounds))
    assert (report.gate_failed_round is not None) == (report.early_stop_reason == "gate")


@settings(
    derandomize=True,
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    threshold=st.floats(min_value=0.0, max_value=1.0),
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
)
def test_property_threshold_lowered_never_increases_rounds(threshold: float, rounds: int) -> None:
    """Lowering the threshold never *increases* rounds_run."""
    if threshold < 0.5:
        return
    critic_high = _ScriptedCritic(scores=[0.6] * rounds)
    runner_high = _runner(critic=critic_high, score_threshold=1.0, plateau_window=rounds + 10)
    rep_high = runner_high.run(_task(refinement_rounds=rounds))

    critic_low = _ScriptedCritic(scores=[0.6] * rounds)
    runner_low = _runner(critic=critic_low, score_threshold=threshold, plateau_window=rounds + 10)
    rep_low = runner_low.run(_task(refinement_rounds=rounds))
    if threshold <= 0.6:
        # Lower threshold trips faster.
        assert rep_low.rounds_run <= rep_high.rounds_run


@settings(
    derandomize=True,
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    score=st.floats(min_value=0.0, max_value=1.0),
    veto=st.booleans(),
    rationale=st.text(max_size=80),
)
def test_property_critique_round_trip(score: float, veto: bool, rationale: str) -> None:
    crit = Critique(score=clamp_score(score), veto=veto, rationale=rationale)
    restored = Critique.from_dict(crit.to_dict())
    assert restored.score == clamp_score(score)
    assert restored.veto is veto
    assert restored.rationale == rationale


@settings(
    derandomize=True,
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    rounds=st.integers(min_value=MIN_REFINEMENT_ROUNDS, max_value=MAX_REFINEMENT_ROUNDS),
    seed=st.integers(min_value=0, max_value=2**31),
)
def test_property_seeded_run_report_stable_across_repeat(rounds: int, seed: int) -> None:
    """A seeded runner produces identical reports on repeated invocation."""
    scores = [0.05 * (i + 1) for i in range(rounds)]
    r1 = _runner(critic=_ScriptedCritic(scores=scores.copy()), seed=seed, plateau_window=rounds + 10)
    r2 = _runner(critic=_ScriptedCritic(scores=scores.copy()), seed=seed, plateau_window=rounds + 10)
    rep1 = r1.run(_task(refinement_rounds=rounds))
    rep2 = r2.run(_task(refinement_rounds=rounds))
    assert rep1.per_round_critique == rep2.per_round_critique
    assert rep1.per_round_cost == rep2.per_round_cost
    assert rep1.early_stop_reason == rep2.early_stop_reason


@settings(
    derandomize=True,
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    cost=st.floats(min_value=0.0, max_value=1.0),
    budget=st.floats(min_value=0.0, max_value=10.0),
)
def test_property_budget_monotonicity_per_round_cost(cost: float, budget: float) -> None:
    """At fixed budget, larger per-round cost cannot extend rounds_run."""
    rounds = MAX_REFINEMENT_ROUNDS
    critic_a = _ScriptedCritic(scores=[0.1] * rounds)
    runner_a = _runner(
        critic=critic_a,
        drafter=_CountingDrafter(cost_per_round=cost),
        refiner=_CountingRefiner(cost_per_round=cost),
        budget_usd=budget,
        score_threshold=1.0,
        plateau_window=rounds + 10,
    )
    critic_b = _ScriptedCritic(scores=[0.1] * rounds)
    runner_b = _runner(
        critic=critic_b,
        drafter=_CountingDrafter(cost_per_round=cost * 2),
        refiner=_CountingRefiner(cost_per_round=cost * 2),
        budget_usd=budget,
        score_threshold=1.0,
        plateau_window=rounds + 10,
    )
    rep_a = runner_a.run(_task(refinement_rounds=rounds))
    rep_b = runner_b.run(_task(refinement_rounds=rounds))
    assert rep_b.rounds_run <= rep_a.rounds_run


# ---------------------------------------------------------------------------
# Integration tests against a mock adapter loop
# ---------------------------------------------------------------------------


class _MockAdapter:
    """Mock adapter that simulates a coding-agent stream.

    Each invocation produces an artefact whose content embeds the
    accumulated critique text and which monotonically improves a hidden
    quality signal.  The runner can drive this adapter through any
    number of rounds and the test asserts on the produced
    :class:`RefinementReport`.
    """

    def __init__(self, max_quality: float = 1.0, cost_per_round: float = 0.05) -> None:
        self.max_quality = max_quality
        self.cost_per_round = cost_per_round
        self.history: list[str] = []

    def draft(self, task: Task) -> RoundArtefact:
        self.history.append("draft")
        return RoundArtefact(
            content=f"initial-draft-for-{task.id}",
            cost_usd=self.cost_per_round,
            metadata={"phase": "draft"},
        )

    def refine(self, task: Task, prior: RoundArtefact, critique: Critique) -> RoundArtefact:
        self.history.append("refine")
        improved_content = f"{prior.content}\n# applied critique: {critique.rationale}"
        return RoundArtefact(
            content=improved_content,
            cost_usd=self.cost_per_round,
            metadata={"phase": "refine", "improvement": critique.score},
        )

    def critique(self, target_curve: list[float]) -> Callable[[Task, RoundArtefact, int], Critique]:
        def _critic(task: Task, artefact: RoundArtefact, round_index: int) -> Critique:
            idx = min(round_index - 1, len(target_curve) - 1)
            return Critique(
                score=target_curve[idx],
                issues=[CritiqueIssue(severity="medium", message=f"round-{round_index}")],
                rationale=f"adversary-round-{round_index}",
            )

        return _critic


def test_integration_full_loop_runs_full_budget() -> None:
    adapter = _MockAdapter(cost_per_round=0.1)
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.2, 0.4, 0.6]),
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert report.rounds_run == 3
    assert adapter.history == ["draft", "refine", "refine"]
    assert math.isclose(report.cumulative_cost_usd, 0.3, rel_tol=1e-6)
    assert "applied critique" in report.final_artefact.content


def test_integration_threshold_stop_with_climbing_curve() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.2, 0.6, 0.97]),
        score_threshold=0.9,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "threshold"
    assert report.rounds_run == 3


def test_integration_plateau_stops_loop() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.5, 0.5, 0.5, 0.5]),
        score_threshold=1.0,
        plateau_window=2,
    )
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "plateau"
    assert report.rounds_run == 3


def _integration_gate_fail_after_first(task: Task, artefact: RoundArtefact, round_index: int) -> bool:
    return round_index < 2


def test_integration_gate_failure_halts_loop() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.3, 0.5, 0.9]),
        gate_runner=_integration_gate_fail_after_first,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=4))
    assert report.early_stop_reason == "gate"
    assert report.gate_failed_round == 2


def test_integration_budget_breaker_caps_rounds() -> None:
    adapter = _MockAdapter(cost_per_round=0.5)
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.1, 0.2, 0.3, 0.4]),
        budget_usd=1.0,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=4))
    assert report.early_stop_reason == "budget"
    assert report.rounds_run == 2


def test_integration_adversary_veto_halts_after_first_round() -> None:
    adapter = _MockAdapter()

    def _vetoing_critic(t: Task, a: RoundArtefact, r: int) -> Critique:
        return Critique(score=0.9, veto=True, rationale=f"reject-{r}")

    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=_vetoing_critic,
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "adversary_veto"
    assert report.rounds_run == 1


def test_integration_critic_rationale_feeds_next_round() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.3, 0.5]),
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=2))
    assert "adversary-round-1" in report.final_artefact.content


def test_integration_report_records_metadata_through_pipeline() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.2, 0.5, 0.7]),
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    assert report.final_artefact.metadata["phase"] == "refine"
    assert "improvement" in report.final_artefact.metadata


def test_integration_loop_emits_round_indexed_critiques() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.1, 0.2, 0.3]),
        score_threshold=1.0,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=3))
    rationales = [c.rationale for c in report.per_round_critique]
    assert rationales == ["adversary-round-1", "adversary-round-2", "adversary-round-3"]


def test_integration_loop_no_refiner_call_after_early_stop() -> None:
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.99]),
        score_threshold=0.5,
        plateau_window=10,
    )
    runner.run(_task(refinement_rounds=5))
    # Threshold tripped on round 1  -  refiner must not have been called.
    assert adapter.history == ["draft"]


def test_integration_loop_disable_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop runs cleanly when decision-log writers are disabled."""
    monkeypatch.setenv("BERNSTEIN_DECISION_LOG", "0")
    adapter = _MockAdapter()
    runner = RefinementLoopRunner(
        drafter=adapter.draft,
        refiner=adapter.refine,
        critic=adapter.critique([0.2, 0.4, 0.99]),
        score_threshold=0.9,
        plateau_window=10,
    )
    report = runner.run(_task(refinement_rounds=5))
    assert report.early_stop_reason == "threshold"
