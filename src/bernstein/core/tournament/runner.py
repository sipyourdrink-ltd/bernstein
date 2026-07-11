"""Tournament fan-out with a budget-gated selection barrier (issue #2353).

The runner ties the pieces together: it gates fan-out on the task's budget
ceiling (AC4), fans out ``attempts`` sibling attempts, collects their scripted
evaluator outputs, and emits the signed selection receipt. No new cost
machinery is introduced -- the cap is resolved through the existing per-ticket
cap primitive (:func:`bernstein.core.cost.ticket_cap.resolve_ticket_cap_usd`),
so a tournament honours the same ceiling operators already configure.

The runner takes pluggable callbacks (spawn, evaluate, reclaim) so it is
agent-agnostic and testable without a live scheduler, mirroring the existing
best-of-N runner's shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.cost.ticket_cap import resolve_ticket_cap_usd
from bernstein.core.tournament.evaluators import AttemptOutcome
from bernstein.core.tournament.receipt import TournamentReceipt, emit_tournament_receipt

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tournament.spec import TournamentSpec

logger = logging.getLogger(__name__)


class TournamentBudgetExceeded(RuntimeError):
    """Raised when projected fan-out spend exceeds the task budget ceiling.

    Attributes:
        attempts: The requested attempt count.
        per_attempt_cost_usd: The projected per-attempt spend.
        projected_usd: ``attempts * per_attempt_cost_usd``.
        cap_usd: The resolved per-ticket cap.
    """

    def __init__(self, *, attempts: int, per_attempt_cost_usd: float, projected_usd: float, cap_usd: float) -> None:
        super().__init__(
            f"tournament fan-out aborted: {attempts} attempts x ${per_attempt_cost_usd:.4f} "
            f"= projected ${projected_usd:.4f} exceeds the task budget ceiling of ${cap_usd:.4f}"
        )
        self.attempts = attempts
        self.per_attempt_cost_usd = per_attempt_cost_usd
        self.projected_usd = projected_usd
        self.cap_usd = cap_usd


def resolve_fanout_cap(
    *,
    ticket_cap: float | None,
    default_cap: float | None = None,
    overrides: dict[str, float] | None = None,
    override_key: str | None = None,
) -> float | None:
    """Resolve the effective per-ticket cap for a tournament fan-out.

    Thin pass-through to the existing per-ticket cap resolution so a tournament
    reuses the operator's configured ceiling rather than inventing its own.
    """
    return resolve_ticket_cap_usd(
        ticket_cap=ticket_cap,
        default_cap=default_cap,
        overrides=overrides,
        override_key=override_key,
    )


def check_fanout_budget(*, attempts: int, per_attempt_cost_usd: float, cap_usd: float | None) -> float:
    """Return projected spend, aborting when it exceeds ``cap_usd`` (AC4).

    Args:
        attempts: Number of sibling attempts to fan out.
        per_attempt_cost_usd: Projected spend per attempt.
        cap_usd: The resolved per-ticket cap; ``None`` means no cap configured
            and the fan-out is permitted regardless of projected spend.

    Returns:
        The projected total spend (``attempts * per_attempt_cost_usd``).

    Raises:
        TournamentBudgetExceeded: When a cap is configured and the projected
            spend would breach it. The error names the attempt count, the
            per-attempt cost, the projection, and the ceiling.
    """
    projected = max(0, attempts) * max(0.0, per_attempt_cost_usd)
    if cap_usd is not None and projected > cap_usd:
        raise TournamentBudgetExceeded(
            attempts=attempts,
            per_attempt_cost_usd=per_attempt_cost_usd,
            projected_usd=projected,
            cap_usd=cap_usd,
        )
    return projected


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


AttemptSpawner = Callable[[str, int], list[str]]
"""Spawn ``n`` isolated sibling attempts for a task; return their attempt ids."""

AttemptEvaluator = Callable[[list[str]], list[AttemptOutcome]]
"""Block until the listed attempts finish, then collect their outcomes."""

AttemptReclaimer = Callable[[AttemptOutcome], None]
"""Tear down a losing attempt's worktree."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TournamentOutcome:
    """Aggregated result of one tournament round."""

    receipt: TournamentReceipt
    winner_hash: str
    projected_usd: float


@dataclass
class TournamentRunner:
    """Drive a task through ``attempts`` siblings and pick the winner.

    Attributes:
        spawner: Spawn ``n`` sibling attempts; returns their ids.
        evaluator: Block until attempts finish; returns their outcomes.
        reclaimer: Optional teardown callback for losing attempts.
    """

    spawner: AttemptSpawner
    evaluator: AttemptEvaluator
    reclaimer: AttemptReclaimer | None = field(default=None)

    def run(
        self,
        *,
        task_id: str,
        spec: TournamentSpec,
        per_attempt_cost_usd: float,
        cap_usd: float | None,
        workdir: Path,
        lineage_root: Path,
        hmac_key: bytes,
        private_key_pem: str,
        public_key_pem: str,
        timestamp: int,
    ) -> TournamentOutcome:
        """Gate on budget, fan out, select the winner, and emit the receipt.

        Raises:
            TournamentBudgetExceeded: When projected spend breaches the cap
                (AC4) -- fan-out never starts.
            RuntimeError: When zero attempts return an outcome.
        """
        projected = check_fanout_budget(
            attempts=spec.attempts,
            per_attempt_cost_usd=per_attempt_cost_usd,
            cap_usd=cap_usd,
        )
        ids = list(self.spawner(task_id, spec.attempts))
        outcomes = list(self.evaluator(ids))
        if not outcomes:
            raise RuntimeError(f"tournament for task {task_id!r} produced zero attempt outcomes")

        receipt = emit_tournament_receipt(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            private_key_pem=private_key_pem,
            public_key_pem=public_key_pem,
            task_id=task_id,
            spec=spec,
            outcomes=outcomes,
            timestamp=timestamp,
        )
        if self.reclaimer is not None:
            for outcome in outcomes:
                if outcome.attempt_hash == receipt.winner_hash:
                    continue
                try:
                    self.reclaimer(outcome)
                except Exception:  # pragma: no cover - reclaim must never fail the round
                    logger.exception("tournament reclaim failed for attempt %s", outcome.attempt_hash)
        logger.info(
            "tournament for task %s: winner=%s attempts=%d",
            task_id,
            receipt.winner_hash,
            len(outcomes),
        )
        return TournamentOutcome(receipt=receipt, winner_hash=receipt.winner_hash, projected_usd=projected)


__all__ = [
    "AttemptEvaluator",
    "AttemptReclaimer",
    "AttemptSpawner",
    "TournamentBudgetExceeded",
    "TournamentOutcome",
    "TournamentRunner",
    "check_fanout_budget",
    "resolve_fanout_cap",
]
