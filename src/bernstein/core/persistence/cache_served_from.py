"""Served-from spine entries: every cache hit is a lineage-spine record.

A cache hit that leaves no trace in the lineage spine makes replay unable to
prove which steps were served from cache, and turns "which runs consumed this
since-revoked entry" into an unanswerable question. This module records every
hit as a ``served_from`` entry on the always-on :class:`LineageSpine`, at the
same write boundary where every artifact write is already intercepted.

Because the spine is Merkle-chained and HMAC-tagged, a run that served steps
from cache replays to the identical spine head; suppressing or mutating any
single ``served_from`` entry causes :meth:`LineageSpine.verify` to fail, so a
hidden or forged cache hit is falsification-evident rather than merely unlogged
(issue #2551 AC2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.persistence.cache_policy import validate_cache_key

if TYPE_CHECKING:
    from bernstein.core.lineage.spine import LineageSpine

_SERVED_FROM_PREFIX = ".sdd/cache/served_from"


def served_from_artifact_path(cache_key: str) -> str:
    """Return the repo-relative spine artifact path for a served-from hit.

    The key becomes the final path component, so it is validated first: a key
    carrying separators or traversal segments would otherwise let a spine entry
    claim an artifact path outside the served-from namespace.

    Raises:
        UnsafeCacheKeyError: When ``cache_key`` is not a safe cache key.
    """
    return f"{_SERVED_FROM_PREFIX}/{validate_cache_key(cache_key)}"


def served_from_content(*, cache_key: str, output_hash: str, policy_hash: str, recipe_hash: str) -> bytes:
    """Return the canonical served-from marker bytes hashed into the spine entry.

    The marker binds the cache key, the served output hash, and the policy and
    recipe hashes so the spine ``content_hash`` is a content-addressed anchor of
    exactly what was served under which policy.
    """
    return json.dumps(
        {
            "cache_key": cache_key,
            "output_hash": output_hash,
            "policy_hash": policy_hash,
            "recipe_hash": recipe_hash,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def record_served_from(
    spine: LineageSpine,
    *,
    cache_key: str,
    output_hash: str,
    policy_hash: str,
    recipe_hash: str,
    actor: str,
    step_id: str,
    model: str,
    timestamp: int,
) -> str:
    """Append a ``served_from`` entry to ``spine`` for one cache hit.

    Returns the entry hash. The spine's own append path provides the Merkle
    chaining and HMAC tag, so no new verification code is needed - the hit
    participates in the same chain a live artifact write would.
    """
    content = served_from_content(
        cache_key=cache_key,
        output_hash=output_hash,
        policy_hash=policy_hash,
        recipe_hash=recipe_hash,
    )
    return spine.record(
        artifact_path=served_from_artifact_path(cache_key),
        content=content,
        actor=actor,
        step_id=step_id,
        model=model,
        timestamp=timestamp,
    )


__all__ = [
    "record_served_from",
    "served_from_artifact_path",
    "served_from_content",
]
