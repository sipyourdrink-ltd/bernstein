"""Tournament task-spec schema (issue #2353).

A *tournament* runs ``attempts`` independent siblings of one task in parallel
and selects the winner by scripted evaluators -- no model call in the decision
path. This module owns the declarative shape an operator writes on a task:

* :class:`EvaluatorSpec` -- one scripted check (test pass rate, lint status,
  coverage delta, mutation score, an arbitrary command). Each carries a weight
  and a direction (``higher_is_better``).
* :class:`TournamentSpec` -- ``attempts``, the ordered evaluators, and the
  tie-break rule.

Determinism: :meth:`TournamentSpec.spec_hash` is a pure ``sha256`` of the
canonical JSON of the spec, so the same declared tournament always hashes to
the same value and can be pinned in a selection receipt for offline replay.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: Version stamped into the canonical spec preimage. Bump only on a
#: wire-format change.
TOURNAMENT_SCHEMA_VERSION = 1

#: The only tie-break supported today: stable ascending attempt-hash order.
#: A tie-break must be a pure function of the attempt identities so replay is
#: byte-identical (AC1).
TIE_BREAK_ATTEMPT_HASH = "attempt_hash"

_VALID_TIE_BREAKS = frozenset({TIE_BREAK_ATTEMPT_HASH})


class TournamentSpecError(ValueError):
    """Raised when a tournament spec is malformed."""


@dataclass(frozen=True, slots=True)
class EvaluatorSpec:
    """One scripted evaluator declared on a tournament task.

    Attributes:
        name: Stable identifier for the check (e.g. ``tests``, ``lint``,
            ``coverage``, ``mutation``, or an operator-chosen command label).
        weight: Non-negative blend weight. A weight of ``0`` records the
            evaluator's output for forensics without letting it move the score.
        higher_is_better: When ``True`` a larger value scores higher; when
            ``False`` (e.g. runtime, lint-error count) a smaller value scores
            higher.
    """

    name: str
    weight: float = 1.0
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise TournamentSpecError("evaluator name must not be empty")
        if self.weight < 0.0:
            raise TournamentSpecError(f"evaluator {self.name!r} has a negative weight: {self.weight}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "higher_is_better": self.higher_is_better,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EvaluatorSpec:
        return cls(
            name=str(row["name"]),
            weight=float(row.get("weight", 1.0)),
            higher_is_better=bool(row.get("higher_is_better", True)),
        )


@dataclass(frozen=True, slots=True)
class TournamentSpec:
    """A declarative tournament: how many attempts and how to pick the winner.

    Attributes:
        attempts: Number of sibling attempts to fan out (``>= 1``; ``1``
            collapses to a single-attempt run).
        evaluators: Ordered, non-empty tuple of :class:`EvaluatorSpec`.
        tie_break: Rule applied when two attempts blend to the same score.
            Only :data:`TIE_BREAK_ATTEMPT_HASH` is supported.
    """

    attempts: int
    evaluators: tuple[EvaluatorSpec, ...]
    tie_break: str = TIE_BREAK_ATTEMPT_HASH

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise TournamentSpecError(f"attempts must be >= 1, got {self.attempts}")
        if not self.evaluators:
            raise TournamentSpecError("a tournament needs at least one evaluator")
        names = [e.name for e in self.evaluators]
        if len(names) != len(set(names)):
            raise TournamentSpecError(f"evaluator names must be unique: {names}")
        if self.tie_break not in _VALID_TIE_BREAKS:
            raise TournamentSpecError(f"unsupported tie_break: {self.tie_break!r}")
        if sum(e.weight for e in self.evaluators) <= 0.0:
            raise TournamentSpecError("total evaluator weight must be positive")

    def canonical(self) -> dict[str, Any]:
        """Return the canonical mapping hashed into :meth:`spec_hash`."""
        return {
            "v": TOURNAMENT_SCHEMA_VERSION,
            "attempts": self.attempts,
            "evaluators": [e.to_dict() for e in self.evaluators],
            "tie_break": self.tie_break,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.canonical()

    def spec_hash(self) -> str:
        """Return the ``sha256:`` content hash of the canonical spec."""
        raw = json.dumps(self.canonical(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> TournamentSpec:
        evaluators = tuple(EvaluatorSpec.from_dict(e) for e in row.get("evaluators", []))
        return cls(
            attempts=int(row["attempts"]),
            evaluators=evaluators,
            tie_break=str(row.get("tie_break", TIE_BREAK_ATTEMPT_HASH)),
        )

    @classmethod
    def from_task_dict(cls, task: dict[str, Any]) -> TournamentSpec:
        """Parse a tournament spec from a task-spec mapping.

        Accepts either a nested ``{"tournament": {...}}`` block or the flat
        ``{"attempts": N, "evaluators": [...]}`` shape.

        Raises:
            TournamentSpecError: When required fields are missing or invalid.
        """
        block = task.get("tournament", task)
        if not isinstance(block, dict) or "attempts" not in block:
            raise TournamentSpecError("task spec has no tournament block (need attempts + evaluators)")
        try:
            return cls.from_dict(block)
        except (KeyError, TypeError, ValueError) as exc:
            raise TournamentSpecError(f"malformed tournament spec: {exc}") from exc


__all__ = [
    "TIE_BREAK_ATTEMPT_HASH",
    "TOURNAMENT_SCHEMA_VERSION",
    "EvaluatorSpec",
    "TournamentSpec",
    "TournamentSpecError",
]
