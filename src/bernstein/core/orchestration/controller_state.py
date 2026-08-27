"""Controller state sidecar: persists adaptive parallelism and claim-conflict state.

Sidecar file: ``.sdd/runtime/controllers.json``.

Purpose
-------
The orchestrator's ``AdaptiveParallelism`` controller and its claim-conflict
backoff dictionary are purely in-process state — a restart loses everything.
This sidecar saves a serialisable snapshot on every slow tick and on
shutdown, and restores it on startup so that:

- Adaptive parallelism resumes with the same ``configured_max``,
  ``_current_max``, and ``_slo_constrained_max`` that were in effect when the
  previous process exited.
- Claim-conflict cooldowns (per-task backoff windows) are preserved across
  restarts, preventing a stuck task from immediately re-entering a 409
  churn loop the instant the orchestrator comes back up.
- Expired entries (windows that have elapsed) are pruned on load so the
  saved state does not resurrect dead cooldowns.

Format
------
JSON::

    {
      "version": 1,
      "adaptive_parallelism": {
        "configured_max": 6,
        "current_max": 5,
        "slo_constrained_max": null,
        "last_adjustment_reason": "error_rate_high (25%)",
        "low_error_since_epoch": 1690000000.0
      },
      "claim_conflict_state": {
        "<task-id>": {
          "episode_count": 2,
          "backoff_until_epoch": 1690001200.5
        }
      },
      "saved_at_epoch": 1690000500.0
    }

Error handling
--------------
Every public function is best-effort. Missing sidecar, corrupt JSON, schema
mismatches, and I/O errors are all logged at ``WARNING`` and treated as an
empty state — the orchestrator never crashes over a stale or absent sidecar.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.persistence.atomic_write import write_atomic_json

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = "controllers.json"
STATE_VERSION = 1
# Maximum age of a saved claim-conflict entry before it is aged out on load.
# 300 s (5 min) exceeds the backoff ceiling (_CLAIM_CONFLICT_BACKOFF_MAX_S)
# so any entry still valid in the source process will never be purged here.
CLAIM_CONFLICT_MAX_AGE_S = 300.0


# ---------------------------------------------------------------------------
# Serializable state records
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveParallelismState:
    """Persisted snapshot of ``AdaptiveParallelism`` internal state."""

    configured_max: int
    current_max: int
    slo_constrained_max: int | None
    last_adjustment_reason: str
    low_error_since_epoch: float | None


@dataclass
class ClaimConflictEntry:
    """Persisted snapshot of one per-task claim-conflict cooldown entry."""

    episode_count: int
    backoff_until_epoch: float


@dataclass
class ControllerSidecarState:
    """Top-level serialisable state for the controllers sidecar."""

    version: int
    adaptive_parallelism: AdaptiveParallelismState
    claim_conflict_state: dict[str, ClaimConflictEntry] = field(default_factory=dict)
    saved_at_epoch: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _sidecar_path(workdir: Path) -> Path:
    """Return the absolute path to the controllers sidecar file."""
    return workdir / ".sdd" / "runtime" / SIDECAR_FILENAME


def load(
    workdir: Path,
) -> tuple[AdaptiveParallelismState, dict[str, ClaimConflictEntry]]:
    """Load persisted controller state from the sidecar.

    Returns:
        A ``(ap_state, conflict_state)`` tuple. On any error (missing file,
        corrupt JSON, version mismatch, I/O failure) the functions log a
        warning and return clean-naive state — the orchestrator starts as if
        no sidecar was ever written.
    """
    path = _sidecar_path(workdir)
    if not path.exists():
        logger.debug("Controller sidecar not found at %s — starting fresh", path)
        return _naive_state()

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Controller sidecar corrupt at %s — starting fresh: %s", path, exc)
        return _naive_state()

    return _unmarshal(raw, path)


def save(
    workdir: Path,
    ap_state: AdaptiveParallelismState,
    claim_conflict_state: dict[str, ClaimConflictEntry],
) -> None:
    """Persist controller state to the sidecar.

    Best-effort: any I/O error is logged at WARNING and the orchestrator
    continues — losing the sidecar is never fatal.
    """
    path = _sidecar_path(workdir)
    state = ControllerSidecarState(
        version=STATE_VERSION,
        adaptive_parallelism=ap_state,
        claim_conflict_state={tid: entry for tid, entry in claim_conflict_state.items()},
        saved_at_epoch=time.time(),
    )
    try:
        write_atomic_json(path, _to_payload(state))
    except OSError as exc:
        logger.warning("Failed to write controller sidecar at %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _naive_state() -> tuple[AdaptiveParallelismState, dict[str, ClaimConflictEntry]]:
    """Return clean-naive state for a fresh start."""
    return AdaptiveParallelismState(
        configured_max=0,
        current_max=0,
        slo_constrained_max=None,
        last_adjustment_reason="initial",
        low_error_since_epoch=None,
    ), {}


def _unmarshal(
    raw: dict[str, Any],
    path: Path,
) -> tuple[AdaptiveParallelismState, dict[str, ClaimConflictEntry]]:
    """Deserialize and age-out a loaded sidecar payload.

    Args:
        raw: Parsed JSON dict from the sidecar file.
        path: File path (for logging).

    Returns:
        ``(ap_state, claim_conflict_state)`` with expired entries pruned.
    """
    now = time.time()

    # Version gate — reject unknown schemas without crashing.
    version = raw.get("version")
    if version is None:
        logger.warning("Controller sidecar at %s missing 'version' — starting fresh", path)
        return _naive_state()
    if version != STATE_VERSION:
        logger.warning(
            "Controller sidecar version mismatch at %s (have=%s want=%s) — starting fresh",
            path,
            version,
            STATE_VERSION,
        )
        return _naive_state()

    # Adaptive parallelism
    ap_raw = raw.get("adaptive_parallelism")
    if not isinstance(ap_raw, dict):
        logger.warning("Controller sidecar: 'adaptive_parallelism' missing or malformed — starting fresh")
        return _naive_state()
    ap_keys = set(AdaptiveParallelismState.__dataclass_fields__.keys())
    if not ap_keys.issubset(set(ap_raw.keys())):
        logger.warning(
            "Controller sidecar: adaptive_parallelism schema mismatch — starting fresh",
        )
        return _naive_state()
    try:
        ap_state = AdaptiveParallelismState(
            configured_max=int(ap_raw["configured_max"]),
            current_max=int(ap_raw["current_max"]),
            slo_constrained_max=ap_raw.get("slo_constrained_max"),
            last_adjustment_reason=str(ap_raw.get("last_adjustment_reason", "initial")),
            low_error_since_epoch=_as_float(ap_raw.get("low_error_since_epoch")),
        )
    except (ValueError, TypeError) as exc:
        logger.warning("Controller sidecar: adaptive_parallelism parse error — starting fresh: %s", exc)
        return _naive_state()

    # Claim-conflict state — age out expired entries
    raw_conflicts = raw.get("claim_conflict_state") or {}
    claim_conflict_state: dict[str, ClaimConflictEntry] = {}
    for task_id, entry_raw in raw_conflicts.items():
        if not isinstance(entry_raw, dict):
            continue
        try:
            entry = ClaimConflictEntry(
                episode_count=int(entry_raw["episode_count"]),
                backoff_until_epoch=float(entry_raw["backoff_until_epoch"]),
            )
        except (ValueError, TypeError, KeyError):
            logger.debug("Controller sidecar: skipping malformed entry for task %s", task_id)
            continue
        # Age out expired entries — if the backoff window elapsed before this
        # process started, there is no reason to resurrect it.
        if entry.backoff_until_epoch <= now:
            continue
        claim_conflict_state[task_id] = entry

    logger.info(
        "Restored controller sidecar: ap configured_max=%d current_max=%d conflict entries %d (aged out %d)",
        ap_state.configured_max,
        ap_state.current_max,
        len(claim_conflict_state),
        len(raw_conflicts) - len(claim_conflict_state),
    )
    return ap_state, claim_conflict_state


def _to_payload(state: ControllerSidecarState) -> dict[str, Any]:
    """Convert a ControllerSidecarState to a plain dict for JSON serialisation."""
    return {
        "version": state.version,
        "adaptive_parallelism": {
            "configured_max": state.adaptive_parallelism.configured_max,
            "current_max": state.adaptive_parallelism.current_max,
            "slo_constrained_max": state.adaptive_parallelism.slo_constrained_max,
            "last_adjustment_reason": state.adaptive_parallelism.last_adjustment_reason,
            "low_error_since_epoch": state.adaptive_parallelism.low_error_since_epoch,
        },
        "claim_conflict_state": {
            tid: {"episode_count": e.episode_count, "backoff_until_epoch": e.backoff_until_epoch}
            for tid, e in state.claim_conflict_state.items()
        },
        "saved_at_epoch": state.saved_at_epoch,
    }


def _as_float(value: Any) -> float | None:
    """Coerce *value* to float or return None."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
