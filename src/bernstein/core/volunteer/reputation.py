"""Deterministic per-worker reputation counts engine (issue #3879).

Pure, I/O-free domain logic for computing per-worker-key
``{submitted, verified, merged, reverted}`` counts from public PR data and
receipt bundles. Isolated from I/O so it can be unit-tested and reused by the
CLI and the static ``generate-earn-only-acceptance-rate`` script.

Recompute-from-fixture is deterministic: same input PR list yields
byte-identical dict ordering across runs (sorted keys, no unsorted set
iteration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

WorkerKeyId = str

_KEYID_RE = re.compile(r'(?:worker[_-]?keyid|keyid)["\s:=]+([a-fA-F0-9]{64})', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ReputationCounts:
    """Per-worker counts derived from merged PRs.

    Attributes:
        submitted: Number of merged PRs attributed to the worker.
        verified: Number of those whose receipt bundle verified (MVP: equals
            submitted when no verifier is supplied).
        merged: Number of merged PRs (same as submitted in MVP; kept separate
            for future semantics where submission != merge).
        reverted: Number of the worker's PRs that were later reverted.
    """

    submitted: int = 0
    verified: int = 0
    merged: int = 0
    reverted: int = 0


def is_reverted(pr: dict[str, Any], all_prs: list[dict[str, Any]]) -> bool:
    """Return True if *pr* was reverted by a later merged PR.

    MVP machine-readable decision: a PR is considered reverted if there exists
    a *different* merged PR whose title contains ``revert`` (case-insensitive)
    and whose title or body references the original PR number as ``#N``.

    Nothing in the codebase currently marks reverted PRs otherwise (no label,
    no GitHub revert flag); this heuristic is the MVP signal and is
    intentionally narrow to avoid false positives.

    Args:
        pr: PR dict with at least ``number`` and ``title``.
        all_prs: Full PR list to search for revert candidates.

    Returns:
        True if a revert PR referencing ``pr`` is found.
    """
    pr_number = pr.get("number")
    if pr_number is None:
        return False
    needle = f"#{pr_number}"
    for other in all_prs:
        if other.get("number") == pr_number:
            continue
        title = other.get("title") or ""
        if "revert" not in title.lower():
            continue
        body = other.get("body") or ""
        if needle in title or needle in body:
            return True
    return False


def extract_worker_keyids(pr_body: str | None) -> set[str]:
    """Extract worker keyids from a PR body.

    Scans for ``(?:worker[_-]?keyid|keyid)["\\s:=]+([a-fA-F0-9]{64})``
    case-insensitive, lowercases matches, and returns the set.

    Bundle-URL fetching is intentionally out of scope for this pure module;
    callers that need to resolve ``bundle_url`` references do so in I/O
    layers (e.g. the static generator).

    Args:
        pr_body: PR body text or None.

    Returns:
        Set of lowercased 64-hex keyids found.
    """
    if not pr_body:
        return set()
    return {m.group(1).lower() for m in _KEYID_RE.finditer(pr_body)}


def compute_reputation(
    prs: list[dict[str, Any]],
    verify_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, ReputationCounts]:
    """Compute per-worker reputation counts from PR data.

    For each PR where ``merged_at`` is not None, extract worker keyids via
    :func:`extract_worker_keyids`; skip PRs with no keyid (unknown-worker
    isolation). For each attributed worker, increment ``submitted`` and
    ``merged``; if :func:`is_reverted` is True increment ``reverted``; for
    ``verified``, if *verify_fn* is None then ``verified == submitted`` (MVP
    assumption documented — every merged PR with a keyid is treated as
    verified when no offline verifier is supplied), else ``verified`` increments
    only when ``verify_fn(pr)`` returns True.

    Reverted counting is independent: a revert PR itself is counted for its
    own worker, and separately marks the target PR's worker as reverted. So a
    worker that authored both the original and the revert will see
    ``submitted==2`` and ``reverted==1`` (only the original is flagged).

    Output dict is sorted by keyid for determinism; callers must not rely on
    insertion order of the input list.

    Args:
        prs: List of PR dicts (as returned by GitHub API / fixture).
        verify_fn: Optional callable receiving the PR dict and returning
            True if its receipt bundle verifies (mirrors
            ``verify_result_bundle(...).ok``). When None, MVP assumes
            verified == submitted.

    Returns:
        Mapping from lowercased worker keyid to :class:`ReputationCounts`,
        sorted by keyid.
    """
    merged_prs = [pr for pr in prs if pr.get("merged_at") is not None]
    acc: dict[str, ReputationCounts] = {}

    for pr in merged_prs:
        keyids = extract_worker_keyids(pr.get("body"))
        if not keyids:
            continue
        reverted = is_reverted(pr, merged_prs)
        for keyid in sorted(keyids):
            cur = acc.get(keyid, ReputationCounts())
            submitted = cur.submitted + 1
            merged = cur.merged + 1
            rev = cur.reverted + (1 if reverted else 0)
            if verify_fn is None:
                verified = cur.verified + 1
            else:
                try:
                    ok = bool(verify_fn(pr))
                except Exception:
                    ok = False
                verified = cur.verified + (1 if ok else 0)
            acc[keyid] = ReputationCounts(
                submitted=submitted,
                verified=verified,
                merged=merged,
                reverted=rev,
            )

    return dict(sorted(acc.items()))


def to_serializable(counts: dict[str, ReputationCounts]) -> dict[str, dict[str, int]]:
    """Convert counts to a JSON-serializable dict with stable ordering.

    Keys are sorted; values are plain dicts with keys in declaration order
    (submitted, verified, merged, reverted) for byte-stable emission.

    Args:
        counts: Mapping from worker keyid to :class:`ReputationCounts`.

    Returns:
        Sorted plain-dict representation suitable for ``json.dump(sort_keys=True)``.
    """
    return {
        k: {
            "submitted": v.submitted,
            "verified": v.verified,
            "merged": v.merged,
            "reverted": v.reverted,
        }
        for k, v in sorted(counts.items())
    }


def counts_from_fixture(prs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Helper: compute reputation and return serializable form.

    Convenience for fixture-based recomputation and CLI/script reuse.

    Args:
        prs: PR list (e.g. loaded from ``tests/fixtures/volunteer/test_prs.json``).

    Returns:
        Serializable counts dict, sorted by keyid.
    """
    return to_serializable(compute_reputation(prs))
