"""First-divergence locator for replay event logs.

Walks two ``events.jsonl`` files line-by-line and reports the first index
at which the recorded responses diverge. Used by
``bernstein replay diff <run_a> <run_b>`` to pinpoint *where* two runs
behaved differently.

Comparison is intentionally simple - equality on the ``(kind, key,
response)`` triple for gateway logs, extended with the ``(event,
payload_hash)`` pair so canonical journal rows compare on their hashed
payload. Timestamps and metadata are ignored because they vary by
wall-clock even on identical runs. Callers who want stricter matching
can compose the dataclass with their own comparator.

A divergence whose first mismatching event is a provider-side context
mutation entry (issue #2507) is attributed with the named
:data:`REASON_CODE_PROVIDER_STATE_MUTATION` reason code, the mutation
kind, and the exact step index instead of a generic response mismatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.provider_state import PROVIDER_STATE_MUTATION_EVENT

if TYPE_CHECKING:
    from pathlib import Path

#: No divergence found.
REASON_CODE_NONE = ""

#: The compared events differ in their recorded payload.
REASON_CODE_RESPONSE_MISMATCH = "response_mismatch"

#: One log has extra events after the common prefix.
REASON_CODE_LENGTH_MISMATCH = "length_mismatch"

#: The first mismatching event is a provider-side context mutation entry.
REASON_CODE_PROVIDER_STATE_MUTATION = "provider_state_mutation"


@dataclass(frozen=True)
class DivergenceResult:
    """Outcome of comparing two event logs.

    Attributes:
        diverged: ``True`` if any divergence was found (including length
            mismatch).
        index: 0-based position of the first divergent event, or ``None``
            if the logs are identical *and* of equal length.
        reason: Short human-readable explanation of the divergence.
        a_event: The event from ``run_a`` at :attr:`index` (or ``None``
            if ``run_a`` ran out first).
        b_event: The event from ``run_b`` at :attr:`index` (or ``None``
            if ``run_b`` ran out first).
        reason_code: Machine-readable divergence class (one of the
            ``REASON_CODE_*`` constants).
        mutation_kind: Mutation kind when :attr:`reason_code` is
            :data:`REASON_CODE_PROVIDER_STATE_MUTATION`, else empty.
    """

    diverged: bool
    index: int | None
    reason: str
    a_event: dict[str, Any] | None = None
    b_event: dict[str, Any] | None = None
    reason_code: str = REASON_CODE_NONE
    mutation_kind: str = ""


def load_events(path: Path) -> list[dict[str, Any]]:
    """Load and parse an ``events.jsonl`` file.

    Args:
        path: Path to the file.

    Returns:
        Parsed event dicts in file order. Malformed lines are skipped.
    """
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open() as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return events


def _comparable(event: dict[str, Any]) -> tuple[Any, ...]:
    """Project an event onto the fields used for divergence comparison.

    Gateway rows compare on ``(kind, key, response)``; canonical journal
    rows carry ``(event, payload_hash)`` instead, so those are folded in
    additively (both are ``None`` for gateway rows on both sides).
    """
    return (
        event.get("kind"),
        event.get("key"),
        event.get("response"),
        event.get("event"),
        event.get("payload_hash"),
    )


def _mutation_kind_of(event: dict[str, Any] | None) -> str | None:
    """Return the mutation kind when *event* is a mutation entry, else ``None``.

    Recognises both canonical journal rows (``event`` field) and
    gateway-style rows (``kind`` field).
    """
    if event is None:
        return None
    if PROVIDER_STATE_MUTATION_EVENT not in (event.get("event"), event.get("kind")):
        return None
    return str(event.get("mutation_kind", "")) or "unknown"


def _attribute(
    index: int,
    a_event: dict[str, Any] | None,
    b_event: dict[str, Any] | None,
    *,
    fallback_reason: str,
    fallback_code: str,
) -> DivergenceResult:
    """Build the result for a divergence at *index*, naming mutations.

    When either side's event at the divergence point is a provider-side
    context mutation entry, the divergence is attributed with the named
    reason code, the mutation kind, and the step index (issue #2507)
    rather than the generic mismatch reason.
    """
    a_kind = _mutation_kind_of(a_event)
    b_kind = _mutation_kind_of(b_event)
    mutation_kind = a_kind or b_kind
    if mutation_kind is not None:
        present_in = "run_a" if a_kind is not None else "run_b"
        return DivergenceResult(
            diverged=True,
            index=index,
            reason=(
                f"event #{index}: provider-side context mutation ({mutation_kind}) recorded in "
                f"{present_in} has no counterpart at this step"
            ),
            a_event=a_event,
            b_event=b_event,
            reason_code=REASON_CODE_PROVIDER_STATE_MUTATION,
            mutation_kind=mutation_kind,
        )
    return DivergenceResult(
        diverged=True,
        index=index,
        reason=fallback_reason,
        a_event=a_event,
        b_event=b_event,
        reason_code=fallback_code,
    )


def diff_event_logs(path_a: Path, path_b: Path) -> DivergenceResult:
    """Return the first index at which two event logs diverge.

    Args:
        path_a: Path to the first ``events.jsonl``.
        path_b: Path to the second ``events.jsonl``.

    Returns:
        A :class:`DivergenceResult` describing the outcome.
    """
    a = load_events(path_a)
    b = load_events(path_b)

    if not a and not b:
        return DivergenceResult(
            diverged=False,
            index=None,
            reason="both event logs are empty",
        )

    limit = min(len(a), len(b))
    for i in range(limit):
        if _comparable(a[i]) != _comparable(b[i]):
            return _attribute(
                i,
                a[i],
                b[i],
                fallback_reason=f"event #{i} differs: kind/key/response triple does not match",
                fallback_code=REASON_CODE_RESPONSE_MISMATCH,
            )

    if len(a) == len(b):
        return DivergenceResult(
            diverged=False,
            index=None,
            reason=f"identical: {len(a)} events match",
        )

    longer = "a" if len(a) > len(b) else "b"
    return _attribute(
        limit,
        a[limit] if limit < len(a) else None,
        b[limit] if limit < len(b) else None,
        fallback_reason=(
            f"run_{longer} has {abs(len(a) - len(b))} extra event(s) after index {limit - 1 if limit else 0}"
        ),
        fallback_code=REASON_CODE_LENGTH_MISMATCH,
    )
