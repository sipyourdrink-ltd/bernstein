"""Replay-log readers for orchestration runs.

The canonical per-run recorder is now the Merkle-chained
:class:`bernstein.core.replay.journal.EventJournal` (issue #2293). This
module retains the format-agnostic JSONL readers that the ``bernstein
replay`` CLI uses to load and fingerprint a run's event log:

  - :func:`load_replay_events`: parse a JSONL event log, skipping
    malformed lines.
  - :func:`compute_replay_fingerprint`: deterministic, timing-excluded
    SHA-256 over a log file (issue #1851).

Both read whichever per-run log exists (the canonical ``journal.jsonl``
or a legacy log) since the projection ignores the wall-clock envelope.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Event fields that vary across runs even when the execution is identical.
#: They stay in ``replay.jsonl`` (operators want the timeline) but are excluded
#: from the determinism fingerprint, which must be byte-stable across runs.
#: Keep this set limited to provably non-deterministic envelope fields:
#: over-excluding a real decision field would let two genuinely different runs
#: collide on the same fingerprint (issue #1851).
_NON_DETERMINISTIC_FIELDS = frozenset({"ts", "elapsed_s"})


def _canonical_event_bytes(event: dict[str, Any]) -> bytes:
    """Return canonical bytes for one event, excluding the timing envelope.

    The wall-clock fields in :data:`_NON_DETERMINISTIC_FIELDS` are dropped and
    the remaining keys are JSON-encoded with sorted keys and fixed separators,
    so two recordings of the same decision stream hash identically regardless
    of timing or incidental key order. Mirrors the canonical-bytes discipline
    used by the audit log and lineage entries.

    Args:
        event: One decoded ``replay.jsonl`` row.

    Returns:
        UTF-8 canonical JSON bytes of the deterministic projection.
    """
    projected = {k: v for k, v in event.items() if k not in _NON_DETERMINISTIC_FIELDS}
    return json.dumps(projected, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _fingerprint_lines(lines: Iterable[str]) -> str:
    """Hash the deterministic projection of each non-blank JSONL line.

    Lines that fail to parse as JSON are skipped (mirroring
    :func:`load_replay_events`) so a partial trailing write cannot wedge the
    fingerprint. The hash covers ``event`` plus the decision-relevant payload
    and excludes the wall-clock envelope (issue #1851).
    """
    sha = hashlib.sha256()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            # Defensive: a bare scalar/array line is not a recordable event.
            continue
        sha.update(_canonical_event_bytes(event))
        sha.update(b"\n")
    return sha.hexdigest()


def load_replay_events(replay_path: Path) -> list[dict[str, Any]]:
    """Load all events from a replay JSONL file.

    Args:
        replay_path: Path to the ``replay.jsonl`` file.

    Returns:
        List of event dicts, ordered by timestamp.
    """
    events: list[dict[str, Any]] = []
    if not replay_path.exists():
        return events
    with replay_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def compute_replay_fingerprint(replay_path: Path) -> str:
    """Compute the deterministic execution fingerprint of a replay log file.

    Hashes a canonical, timing-excluded projection of each event, so a
    recording and a faithful replay - which differ only in their ``ts`` /
    ``elapsed_s`` envelope - share one fingerprint, while any divergence in
    the decision stream changes it (issue #1851). This is a whole-file
    rehash used by the ``bernstein replay`` CLI; the canonical per-run
    identity is the :class:`~bernstein.core.replay.journal.EventJournal`
    Merkle head.

    Args:
        replay_path: Path to a per-run event log (``journal.jsonl``).

    Returns:
        Hex-encoded SHA-256 hash, or empty string if the file doesn't exist.
    """
    if not replay_path.exists():
        return ""
    try:
        with replay_path.open(encoding="utf-8") as f:
            return _fingerprint_lines(f)
    except OSError as exc:
        logger.warning("compute_replay_fingerprint: failed to read %s: %s", replay_path, exc)
        return ""
