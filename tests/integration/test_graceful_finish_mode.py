"""Integration: graceful-finish-on-low vs hard-stop-on-zero behaviour.

Verifies that the agent identity card's ``budget_mode`` is honoured by
:func:`bernstein.core.cost.budget_countdown.should_finish_gracefully` so
that the orchestrator can route the agent to a soft-landing turn rather
than a SIGKILL when the budget runs low.

The orchestrator wiring itself is exercised in unit tests for the
formatter; this test is the end-to-end check that the right mode produces
the right outcome on a realistic 5-step run.
"""

from __future__ import annotations

import pytest

from bernstein.core.cost.budget_countdown import (
    TurnState,
    format_countdown,
    record_graceful_finish,
    should_finish_gracefully,
)
from bernstein.core.cost.cost_tracker import CostTracker
from bernstein.core.identity.agent_card import (
    AgentIdentityCard,
    issue_identity_card,
)


def _card_with(mode: str, *, max_tokens: int = 10_000, max_steps: int = 5) -> AgentIdentityCard:
    card = issue_identity_card("agent-int-1", "backend", "claude", "opus", max_budget_usd=0.10)
    card.max_tokens = max_tokens
    card.max_steps = max_steps
    card.budget_mode = mode  # type: ignore[assignment]
    return card


def _simulate_run(card: AgentIdentityCard, *, tokens_per_turn: int) -> tuple[int, TurnState | None]:
    """Loop until graceful-finish triggers or max_steps is exhausted.

    Returns:
        ``(final_step, finishing_state_or_None)``. ``None`` means the
        agent ran out of steps without triggering a graceful finish.
    """
    tracker = CostTracker(run_id="int-run", budget_usd=card.max_budget_usd)
    tokens_used = 0
    for step in range(1, card.max_steps + 1):
        tokens_used += tokens_per_turn
        state = TurnState(step=step, tokens_used=tokens_used)
        # Each turn the agent emits the banner - assert it stays a single
        # line so the prompt-cache prefix remains stable across turns.
        line = format_countdown(card, tracker, state)
        assert "\n" not in line
        if should_finish_gracefully(card, state):
            return step, state
    return card.max_steps, None


def test_graceful_mode_lands_softly_on_low_tokens() -> None:
    """A 5-step run with steady token usage finishes before max_steps."""
    card = _card_with("graceful-finish-on-low", max_tokens=10_000, max_steps=5)
    # 2,200 tokens/turn -> step 4 leaves 1,200 (12%) remaining → trigger.
    step, state = _simulate_run(card, tokens_per_turn=2_200)

    assert state is not None, "agent should have triggered graceful-finish"
    assert step < card.max_steps  # finished before the hard step cap
    assert state.percentage_left(card) <= 20


def test_hard_stop_mode_does_not_finish_until_zero() -> None:
    """Hard-stop ignores the low-headroom signal until tokens hit zero."""
    card = _card_with("hard-stop-on-zero", max_tokens=10_000, max_steps=5)
    # 1,500 tokens/turn -> 7,500 used at step 5 -> 25% headroom: NOT zero.
    step, state = _simulate_run(card, tokens_per_turn=1_500)

    assert state is None, "hard-stop should not finish on low headroom"
    assert step == card.max_steps


def test_hard_stop_finishes_when_budget_zeros_out() -> None:
    """Hard-stop trips the moment headroom <= 0."""
    card = _card_with("hard-stop-on-zero", max_tokens=10_000, max_steps=5)
    # 2,500 tokens/turn -> step 4 hits 10,000 used -> 0 left -> trigger.
    step, state = _simulate_run(card, tokens_per_turn=2_500)

    assert state is not None
    assert state.tokens_used >= card.max_tokens
    assert step <= card.max_steps


def test_max_steps_alone_triggers_graceful_finish() -> None:
    """Even with plentiful tokens, exhausting max_steps triggers a soft finish."""
    card = _card_with("graceful-finish-on-low", max_tokens=1_000_000, max_steps=3)
    step, state = _simulate_run(card, tokens_per_turn=10)

    assert state is not None
    assert step == card.max_steps


def test_record_graceful_finish_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telemetry hook is safe to call from the orchestrator hot path."""
    card = _card_with("graceful-finish-on-low")
    state = TurnState(step=4, tokens_used=8_500)

    # Even if the metric registry is broken, the call must swallow.
    from bernstein.core.observability import prometheus as prom

    monkeypatch.setattr(prom, "task_budget_graceful_finish_total", _RaisingMetric())
    record_graceful_finish(card, state)


class _RaisingMetric:
    """Helper: a metric stand-in whose ``labels()`` raises."""

    def labels(self, **_kwargs: str) -> object:
        raise RuntimeError("boom")
