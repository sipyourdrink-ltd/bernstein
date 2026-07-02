"""Precise step-level divergence detector for hash-chained journals (#1799).

The replay surface promises that non-determinism between two runs is
surfaced as a *named field divergence*, not as a hash-soup ``runs do
not match`` signature. ``diff_steps`` is the per-step primitive;
``diff_journals`` walks two chains side-by-side and returns the first
divergence (or ``None`` if the chains match).

This module deliberately operates on the hashed canonical fields only
(``prev_hash``, ``input_hash``, ``model``, ``prompt``, ``tool_call``,
``tool_result``, ``effort``). Auxiliary fields like ``ts`` and ``seq``
are ignored for divergence-detection purposes because they are not part
of the ``step_hash`` contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.journal import JournalReader

if TYPE_CHECKING:
    from pathlib import Path


#: Fields the step hash is computed over - see ``journal.canonical_step_payload``.
#: ``effort`` folds into the hash when non-None, so two runs that differ only
#: in reasoning effort surface as a named ``effort`` field divergence rather
#: than as an unexplained hash mismatch.
_HASHED_FIELDS = (
    "prev_hash",
    "input_hash",
    "model",
    "prompt",
    "tool_call",
    "tool_result",
    "effort",
)


@dataclass(frozen=True)
class StepDivergence:
    """First-divergence summary between two chain positions.

    Attributes:
        seq: Zero-based step index on which the divergence was observed.
        fields_changed: Tuple of field names (a subset of the hashed
            canonical fields) whose values differ between left and right.
            Always non-empty.
        left_values: ``field -> value`` for the changed fields on the
            *left* chain.
        right_values: ``field -> value`` for the changed fields on the
            *right* chain.
        reason: Human-readable summary suitable for an operator log line.
    """

    seq: int
    fields_changed: tuple[str, ...]
    left_values: dict[str, Any] = field(default_factory=dict)
    right_values: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __hash__(self) -> int:  # pragma: no cover - delegating
        # ``left_values``/``right_values`` are dicts (unhashable). The
        # ``(seq, fields_changed, reason)`` triple is unique enough to use
        # as a deduplication key inside a set.
        return hash((self.seq, self.fields_changed, self.reason))


def _coerce_for_diff(row: dict[str, Any]) -> dict[str, Any]:
    """Project a journal row onto the hashed fields only."""
    return {key: row.get(key) for key in _HASHED_FIELDS}


def diff_steps(left: dict[str, Any], right: dict[str, Any]) -> StepDivergence | None:
    """Return the per-field divergence between two journal rows, or ``None``.

    Args:
        left: One step row (hashed canonical fields; extra fields ignored).
        right: The opposing row.

    Returns:
        A :class:`StepDivergence` listing the fields that differ, or
        ``None`` when every hashed field is equal.
    """
    a = _coerce_for_diff(left)
    b = _coerce_for_diff(right)

    changed = tuple(key for key in _HASHED_FIELDS if a[key] != b[key])
    if not changed:
        return None

    left_values = {key: a[key] for key in changed}
    right_values = {key: b[key] for key in changed}
    reason = "field(s) differ: " + ", ".join(changed)
    seq = int(left.get("seq", right.get("seq", 0)))
    return StepDivergence(
        seq=seq,
        fields_changed=changed,
        left_values=left_values,
        right_values=right_values,
        reason=reason,
    )


def diff_journals(
    left_dir: Path,
    right_dir: Path,
) -> StepDivergence | None:
    """Walk two journals in lockstep and return the first divergence.

    Args:
        left_dir: Per-agent journal directory for the first chain.
        right_dir: Per-agent journal directory for the second chain.

    Returns:
        ``None`` when both chains contain the same number of steps and
        every step matches on the hashed fields. Otherwise a
        :class:`StepDivergence` identifying the first offending step.

        Length mismatches surface as a divergence at the seq of the
        first missing step, with ``fields_changed`` set to ``("length",)``.
    """
    left = list(JournalReader(left_dir).entries())
    right = list(JournalReader(right_dir).entries())

    pair_len = min(len(left), len(right))
    for seq in range(pair_len):
        result = diff_steps(left[seq].to_dict(), right[seq].to_dict())
        if result is not None:
            return result

    if len(left) == len(right):
        return None

    longer = "left" if len(left) > len(right) else "right"
    missing_at = pair_len
    return StepDivergence(
        seq=missing_at,
        fields_changed=("length",),
        left_values={"steps": len(left)},
        right_values={"steps": len(right)},
        reason=(
            f"chain length mismatch: {longer} has {abs(len(left) - len(right))} "
            f"extra step(s) past seq {missing_at - 1 if missing_at else -1}; "
            "missing step would be at this seq"
        ),
    )


__all__ = ["StepDivergence", "diff_journals", "diff_steps"]
