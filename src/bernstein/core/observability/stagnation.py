"""Repository flow stagnation detection.

Pure-function detector that examines a time-series of
:class:`RepositoryFlowSample` snapshots and returns a
:class:`StagnationFinding` when merge-ready work has sat
untouched for the entire observation window.

Key design decisions (this module is deterministic — no I/O):

* **Work-ready rule** — a sample is *work_ready* when
  ``open_prs > 0`` **and** ``mergeable is True``.  Both conditions
  must hold; ``open_prs == 0`` means nothing is waiting, and
  ``mergeable == False`` means the PR(s) are not in a mergeable
  state yet.

* **Window boundary** — the look-back window
  ``[latest_ts - window_s, latest_ts]`` is **inclusive** on the
  left.  A sample whose timestamp equals exactly
  ``latest_ts - window_s`` is considered part of the window.

* **Stagnation definition** — the function returns a
  :class:`StagnationFinding` when *every* sample inside the
  window is *work_ready* **and** no merge occurred during that
  span.  A merge is inferred whenever ``mergeable`` flips from
  ``True`` to ``False`` between consecutive samples (the PR
  went away because it was merged or closed).

The function is side-effect-free: callers are responsible for
scheduling, persistence, and alerting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise


@dataclass(frozen=True)
class RepositoryFlowSample:
    """A point-in-time snapshot of repository flow state.

    Attributes:
        timestamp: Unix timestamp when the sample was recorded.
        open_prs: Number of open pull requests at this instant.
        mergeable: Whether at least one open PR is in a mergeable
            state (passes all checks, has approvals, etc.).
    """

    timestamp: float
    open_prs: int
    mergeable: bool


@dataclass(frozen=True)
class StagnationFinding:
    """Immutable evidence carrier returned by :func:`detect_stagnation`.

    Attributes:
        window_start: Inclusive lower bound of the detection window
            (unix timestamp).
        window_end: Upper bound of the detection window (unix
            timestamp).  Equals the timestamp of the latest sample
            that was examined.
        sample_count: Number of samples that fell inside the window.
        evidence_rows: The :class:`RepositoryFlowSample` snapshots
            that the decision was made from.  Kept verbatim so that
            downstream consumers (dashboards, audits) can reconstruct
            exactly why stagnation was flagged.
    """

    window_start: float
    window_end: float
    sample_count: int
    evidence_rows: tuple[RepositoryFlowSample, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict."""
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "sample_count": self.sample_count,
            "evidence_rows": [
                {
                    "timestamp": row.timestamp,
                    "open_prs": row.open_prs,
                    "mergeable": row.mergeable,
                }
                for row in self.evidence_rows
            ],
        }


def detect_stagnation(
    samples: list[RepositoryFlowSample],
    *,
    window_s: float = 1800.0,
) -> StagnationFinding | None:
    """Detect whether merge-ready work has gone unmerged for ``window_s``.

    The function operates on a *pre-sorted* (by ``timestamp``,
    ascending) list of :class:`RepositoryFlowSample` snapshots.  If
    the list is empty or has fewer than two entries the function
    returns ``None`` immediately — a single data point cannot
    establish a sustained pattern.

    Algorithm:
        1. Identify the latest timestamp across all samples.
        2. Select every sample whose ``timestamp`` falls inside
           ``[latest_ts - window_s, latest_ts]`` (left-inclusive).
        3. Check the **work-ready rule**: ``open_prs > 0`` AND
           ``mergeable is True``.
        4. Verify no merge happened: ``mergeable`` must never flip
           from ``True`` to ``False`` between consecutive samples
           (a flip to ``False`` means the PR was merged or closed).
        5. If all samples in the window are work-ready and no merge
           occurred, return a :class:`StagnationFinding`; otherwise
           return ``None``.

    Args:
        samples: Time-ordered (ascending) repository flow snapshots.
        window_s: Detection window in seconds (default 1800 = 30 min).

    Returns:
        A :class:`StagnationFinding` with evidence if stagnation is
        detected, ``None`` otherwise.
    """
    if len(samples) < 2:
        return None

    latest_ts = max(s.timestamp for s in samples)
    cutoff = latest_ts - window_s

    # Left-inclusive: timestamp >= cutoff
    window_samples = sorted(
        [s for s in samples if s.timestamp >= cutoff],
        key=lambda s: s.timestamp,
    )

    if len(window_samples) < 2:
        return None

    # --- work-ready rule ---
    for s in window_samples:
        if not (s.open_prs > 0 and s.mergeable):
            return None

    # --- no-merge check ---
    for prev, curr in pairwise(window_samples):
        if prev.mergeable and not curr.mergeable:
            # A True → False flip means a merge (or close) happened.
            return None

    return StagnationFinding(
        window_start=window_samples[0].timestamp,
        window_end=window_samples[-1].timestamp,
        sample_count=len(window_samples),
        evidence_rows=tuple(window_samples),
    )
