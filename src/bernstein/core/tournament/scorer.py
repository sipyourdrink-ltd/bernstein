"""Deterministic tournament scoring and winner selection (issue #2353).

The winner is a *pure function* of the evaluator outputs and the spec -- no
model, no clock, no filesystem, no randomness. Given the same
:class:`AttemptOutcome` list and :class:`TournamentSpec`, :func:`select_winner`
returns the identical winner and :func:`selection_projection_bytes` returns
byte-identical bytes regardless of input order (AC1). Ties break on ascending
attempt-hash order, which is itself a pure function of the attempt identities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.tournament.evaluators import AttemptOutcome
    from bernstein.core.tournament.spec import TournamentSpec


def _clamp(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def score_attempt(outcome: AttemptOutcome, spec: TournamentSpec) -> float:
    """Return the blended ``[0, 1]`` score for ``outcome`` under ``spec``.

    Each evaluator contributes ``weight * signal`` where ``signal`` is the
    clamped value (or ``1 - value`` when the evaluator scores lower-is-better).
    A missing output is treated as the worst signal (``0``). The sum is
    normalised by the total weight, so the score is comparable across specs.
    """
    total_weight = 0.0
    weighted = 0.0
    for ev in spec.evaluators:
        total_weight += ev.weight
        out = outcome.output_for(ev.name)
        raw = _clamp(out.value) if out is not None else 0.0
        signal = raw if ev.higher_is_better else (1.0 - raw)
        weighted += ev.weight * signal
    if total_weight <= 0.0:  # pragma: no cover - spec validation forbids this
        return 0.0
    return weighted / total_weight


@dataclass(frozen=True, slots=True)
class RankedAttempt:
    """One attempt's ranking row: its hash and blended score."""

    attempt_hash: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"attempt_hash": self.attempt_hash, "score": self.score}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RankedAttempt:
        return cls(attempt_hash=str(row["attempt_hash"]), score=float(row["score"]))


@dataclass(frozen=True, slots=True)
class Selection:
    """Outcome of a deterministic selection.

    Attributes:
        winner_hash: The chosen attempt's content hash.
        ranked: Every attempt ranked highest-score first, ties broken by
            ascending attempt hash.
    """

    winner_hash: str
    ranked: tuple[RankedAttempt, ...]


def rank_attempts(outcomes: Sequence[AttemptOutcome], spec: TournamentSpec) -> tuple[RankedAttempt, ...]:
    """Return every attempt ranked deterministically (score desc, hash asc)."""
    rows = [RankedAttempt(attempt_hash=o.attempt_hash, score=score_attempt(o, spec)) for o in outcomes]
    # Descending score, then ascending attempt hash as the stable tie-break.
    rows.sort(key=lambda r: (-r.score, r.attempt_hash))
    return tuple(rows)


def select_winner(outcomes: Sequence[AttemptOutcome], spec: TournamentSpec) -> Selection:
    """Pick the winning attempt as a pure function of outputs and spec.

    Raises:
        ValueError: When ``outcomes`` is empty.
    """
    if not outcomes:
        raise ValueError("select_winner requires at least one attempt outcome")
    ranked = rank_attempts(outcomes, spec)
    return Selection(winner_hash=ranked[0].attempt_hash, ranked=ranked)


def selection_projection_bytes(outcomes: Sequence[AttemptOutcome], spec: TournamentSpec) -> bytes:
    """Return canonical bytes of the selection decision (AC1 replay artefact).

    The bytes are a pure projection of ``(outcomes, spec)`` and are independent
    of the input ordering: two runs over the same attempts (in any order) yield
    byte-identical output, so a replay can be hash-asserted.
    """
    selection = select_winner(outcomes, spec)
    payload = {
        "spec_hash": spec.spec_hash(),
        "winner_hash": selection.winner_hash,
        "ranked": [r.to_dict() for r in selection.ranked],
        "tie_break": spec.tie_break,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


__all__ = [
    "RankedAttempt",
    "Selection",
    "rank_attempts",
    "score_attempt",
    "select_winner",
    "selection_projection_bytes",
]
