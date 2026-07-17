"""Deterministic adaptive decay for named fleet-wide rate limits (#2544).

The effective limit at any point is a *pure function* over the chain-recorded
429 observation timestamps -- never in-memory backoff state. Because the
function reads only the observation timestamps carried in hashed ledger rows
plus the ``now`` supplied by the caller, two operators replaying the same
ledger prefix derive the byte-identical effective limit in force at every
claim. The limit is derivable from the ledger, not reconstructed from a live
socket's backoff timer.

Decay model (deterministic)
---------------------------
Each 429 observed within the trailing :data:`RECOVERY_WINDOW_S` seconds halves
the limit, down to the spec floor::

    recent   = count of 429 observations with (now - ts) < RECOVERY_WINDOW_S
    effective = max(floor, base_limit >> recent)

Observations older than the recovery window no longer bite, so the limit
recovers on its own as 429s age out -- adaptive, but a deterministic function
of the recorded observation times, not of wall-clock backoff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["RECOVERY_WINDOW_S", "effective_rate_limit", "recent_observation_count"]

#: How long a recorded 429 keeps biting the effective limit. Fixed so the
#: decay is a deterministic function of the observation times.
RECOVERY_WINDOW_S: int = 300


def recent_observation_count(observation_ts: Iterable[int], now: int, *, window_s: int = RECOVERY_WINDOW_S) -> int:
    """Return how many 429 observations fall inside the trailing window.

    Args:
        observation_ts: Logical timestamps of recorded 429 observations.
        now: Logical time the effective limit is evaluated at.
        window_s: Recovery window; observations older than this do not bite.
    """
    return sum(1 for ts in observation_ts if 0 <= now - ts < window_s)


def effective_rate_limit(
    base_limit: int,
    observation_ts: Iterable[int],
    now: int,
    *,
    floor: int = 1,
    window_s: int = RECOVERY_WINDOW_S,
) -> int:
    """Return the effective limit given recorded 429 observations.

    Pure and deterministic: the return value depends only on the arguments,
    so two processes replaying the same ledger prefix compute the identical
    effective limit. Each recent 429 halves the limit down to ``floor``.

    Args:
        base_limit: The ceiling with zero recent observations.
        observation_ts: Logical timestamps of recorded 429 observations.
        now: Logical time to evaluate at.
        floor: The lowest the limit may decay to (clamped to >= 1).
        window_s: Recovery window in seconds.
    """
    safe_floor = max(1, floor)
    if base_limit <= safe_floor:
        return base_limit
    recent = recent_observation_count(observation_ts, now, window_s=window_s)
    # Halve per recent observation; a large ``recent`` saturates to the floor
    # without an unbounded shift (Python ints never overflow, but a big shift
    # is wasteful).
    decayed = base_limit >> recent if recent < base_limit.bit_length() else 0
    return max(safe_floor, decayed)
