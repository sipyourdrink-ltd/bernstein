"""Per-turn task-budget countdown banner shared across adapters.

Adoption of Anthropic's Opus 4.7 ``task-budgets-2026-03-13`` beta header
relies on the model seeing a running countdown of remaining tokens,
dollars, and steps every turn. This module owns:

- :func:`format_countdown` - deterministic banner string used by every
  adapter so the model sees the same single line regardless of provider.
- :func:`should_finish_gracefully` - predicate the orchestrator consults
  to decide between ``graceful-finish-on-low`` and ``hard-stop-on-zero``
  semantics declared on the agent identity card.
- :func:`task_budgets_beta_headers` - header dict (and matching
  ``ANTHROPIC_BETA`` env var) emitted on Anthropic-flavoured calls when
  the agent identity card opts in via its ``extensions`` map and the
  process has set ``BERNSTEIN_ANTHROPIC_TASK_BUDGETS=true``.

The banner is intentionally tiny (one line) and free of ANSI escapes so
it survives every transport - JSONL stdout, MCP messages, system-prompt
prefixes - and stays cheap enough to inject every turn without growing
the cached prefix.

This module is pure: no IO, no global state. It is safe to call from
adapter hot paths.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.identity.agent_card import (
    TASK_BUDGETS_BETA_HEADER,
    AgentIdentityCard,
)

if TYPE_CHECKING:
    from bernstein.core.cost.cost_tracker import CostTracker

#: Banner threshold below which the agent is asked to finish gracefully.
#: Mirrors the documented "low" surface in
#: ``agentic_systems_v2.md`` line 83 and matches Anthropic's published
#: graceful-finish guidance.
DEFAULT_LOW_THRESHOLD_PCT: int = 20

#: Environment variable that opts the orchestrator process into emitting
#: the Anthropic beta header. Defaults off until GA per the ticket's
#: opt-in requirement.
TASK_BUDGETS_OPT_IN_ENV: str = "BERNSTEIN_ANTHROPIC_TASK_BUDGETS"


@dataclass(frozen=True)
class TurnState:
    """Per-turn counters surfaced to the agent through the countdown banner.

    Attributes:
        step: 1-indexed step counter for the current task.
        tokens_used: Tokens consumed so far in the current turn / task,
            depending on what the adapter tracks. The banner reports
            ``tokens_left = card.max_tokens - tokens_used`` so callers
            should pass whichever counter matches their budget surface.
    """

    step: int
    tokens_used: int

    @property
    def tokens_left(self) -> int:
        """Tokens remaining; never negative (clamped at 0)."""
        return max(self.tokens_used, 0)  # placeholder; real value via with_card

    def remaining(self, card: AgentIdentityCard) -> int:
        """Return the token headroom for this card, clamped at 0."""
        return max(card.max_tokens - self.tokens_used, 0)

    def percentage_left(self, card: AgentIdentityCard) -> int:
        """Return tokens-left as an integer percentage in ``[0, 100]``.

        Truncates rather than rounds so the banner matches the documented
        example (``18,420 / 64,000 = 28.78%`` → ``28%``).

        Args:
            card: Identity card supplying ``max_tokens``.

        Returns:
            ``100`` when the card is unbounded (``max_tokens <= 0``);
            otherwise ``int(remaining / max_tokens * 100)`` clamped to
            ``[0, 100]``.
        """
        if card.max_tokens <= 0:
            return 100
        pct = int(self.remaining(card) / card.max_tokens * 100)
        return max(0, min(100, pct))


def format_countdown(
    card: AgentIdentityCard,
    tracker: CostTracker,
    turn_state: TurnState,
) -> str:
    """Render the per-turn countdown banner for an agent.

    The single-line format is deterministic and copy-checked by tests so
    that prompt-cache prefixes that include this banner stay stable across
    turns where only the right-hand counters move.

    Args:
        card: Agent identity card (supplies budget caps and mode).
        tracker: Active :class:`CostTracker` (supplies USD spend so far).
        turn_state: Live per-turn counters.

    Returns:
        A single line such as::

            [budget] tokens left: 18,420 of 64,000 (28%) | $0.42 of $1.50 |
                    steps: 7 of 30 | mode: graceful-finish-on-low

        Newlines in the docstring example are for readability - the real
        return value is a single line.
    """
    tokens_left = turn_state.remaining(card)
    pct = turn_state.percentage_left(card)
    spent = tracker.spent_usd
    return (
        f"[budget] tokens left: {tokens_left:,} of {card.max_tokens:,} ({pct}%) | "
        f"${spent:.2f} of ${card.max_budget_usd:.2f} | "
        f"steps: {turn_state.step} of {card.max_steps} | "
        f"mode: {card.budget_mode}"
    )


def should_finish_gracefully(
    card: AgentIdentityCard,
    turn_state: TurnState,
    *,
    low_threshold_pct: int = DEFAULT_LOW_THRESHOLD_PCT,
) -> bool:
    """Return True when the agent should land softly on the next turn.

    The decision honours :attr:`AgentIdentityCard.budget_mode`:

    - ``graceful-finish-on-low`` returns True once the token headroom
      falls under ``low_threshold_pct`` *or* the step counter has reached
      ``card.max_steps``. The orchestrator hands the agent one final
      ``finish-gracefully`` turn to commit WIP and emit a summary.
    - ``hard-stop-on-zero`` returns True only when the headroom is at or
      below 0 tokens, mirroring the Cursor Glass arbitration pause.

    Args:
        card: Agent identity card.
        turn_state: Live per-turn counters.
        low_threshold_pct: Percentage cut-off for the soft-landing path
            on ``graceful-finish-on-low``. Ignored under ``hard-stop-on-zero``.

    Returns:
        True if the orchestrator should stop spawning further work for
        this agent's task and let it land softly.
    """
    if card.budget_mode == "hard-stop-on-zero":
        return turn_state.remaining(card) <= 0
    # graceful-finish-on-low (default)
    if card.max_steps > 0 and turn_state.step >= card.max_steps:
        return True
    if card.max_tokens <= 0:
        return False
    return turn_state.percentage_left(card) <= low_threshold_pct


def is_task_budgets_opt_in() -> bool:
    """Return True when the orchestrator process opts in to the Anthropic beta.

    Reads :data:`TASK_BUDGETS_OPT_IN_ENV`. The flag is intentionally
    process-scoped (not card-scoped) because the underlying Anthropic
    header has to be emitted on every API call from the spawned process -
    a card-only opt-in could not propagate without the orchestrator
    cooperating.
    """
    raw = os.environ.get(TASK_BUDGETS_OPT_IN_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def task_budgets_beta_headers(card: AgentIdentityCard) -> dict[str, str]:
    """Return the Anthropic beta headers for direct API calls.

    Returns an empty dict unless both the orchestrator process opts in
    via :data:`TASK_BUDGETS_OPT_IN_ENV` *and* the card's ``extensions``
    map declares ``task_budgets`` truthy. Callers merge this dict into
    their existing header set.

    Args:
        card: Agent identity card.

    Returns:
        ``{"anthropic-beta": "task-budgets-2026-03-13"}`` when both
        opt-ins are present, else an empty dict.
    """
    if not is_task_budgets_opt_in():
        return {}
    if not card.extensions.get("task_budgets"):
        return {}
    return {"anthropic-beta": TASK_BUDGETS_BETA_HEADER}


def record_graceful_finish(card: AgentIdentityCard, turn_state: TurnState) -> None:
    """Record Prometheus metrics for a graceful-finish landing.

    Increments ``bernstein_task_budget_graceful_finish_total`` and
    observes the headroom percentage on
    ``bernstein_task_budget_remaining_at_finish_pct``. Both metrics are
    labelled by the agent role, with cardinality bounded by the
    role-template enumeration in :mod:`bernstein.core.identity.agent_card`.

    Args:
        card: Agent identity card whose ``role`` labels the metric.
        turn_state: Per-turn counters at the moment of finish.
    """
    from bernstein.core.observability import prometheus as _prom

    role = card.role or "unknown"
    with suppress(Exception):
        # ``prometheus_client`` ships without type stubs, hence the
        # ``reportUnknownMemberType`` pragmas. The pattern mirrors
        # :mod:`bernstein.core.agents.spawner_prompt`.
        _prom.task_budget_graceful_finish_total.labels(  # pyright: ignore[reportUnknownMemberType]
            role=role,
        ).inc()  # pyright: ignore[reportUnknownMemberType]
        _prom.task_budget_remaining_at_finish_pct.labels(  # pyright: ignore[reportUnknownMemberType]
            role=role,
        ).observe(  # pyright: ignore[reportUnknownMemberType]
            float(turn_state.percentage_left(card)),
        )


def task_budgets_env_overlay(card: AgentIdentityCard) -> dict[str, str]:
    """Return env overlay propagating the beta header to CLI-mediated calls.

    The Claude Code CLI forwards ``ANTHROPIC_BETA`` to the underlying
    SDK calls. Bernstein cannot intercept the Anthropic HTTP request
    when the adapter shells out to the CLI, so the only safe channel is
    a documented Anthropic env var.

    Args:
        card: Agent identity card.

    Returns:
        ``{"ANTHROPIC_BETA": "task-budgets-2026-03-13"}`` when both the
        process opt-in and the card extension are set, else ``{}``.
    """
    if not is_task_budgets_opt_in():
        return {}
    if not card.extensions.get("task_budgets"):
        return {}
    return {"ANTHROPIC_BETA": TASK_BUDGETS_BETA_HEADER}


__all__ = [
    "DEFAULT_LOW_THRESHOLD_PCT",
    "TASK_BUDGETS_OPT_IN_ENV",
    "TurnState",
    "format_countdown",
    "is_task_budgets_opt_in",
    "record_graceful_finish",
    "should_finish_gracefully",
    "task_budgets_beta_headers",
    "task_budgets_env_overlay",
]
