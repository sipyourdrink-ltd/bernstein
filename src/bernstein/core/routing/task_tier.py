"""Deterministic task-tier classification at dispatch (#4854).

A pure function of artefacts the task already carries. No network, clock, or
environment reads. The same feature bytes always produce the same tier under
a fixed :data:`TIER_POLICY_VERSION`.

Feature order (v1)
------------------
1. ``size_rank`` - ordinal of an issue ``size/*`` label (``xs``=0 … ``xl``=4);
   absent label → ``2`` (``m``).
2. ``file_count`` - number of path strings on the task surface.
3. ``test_touched`` - ``1`` if any path looks like a test module/dir, else ``0``.
4. ``code_file_count`` - paths that are not documentation-only extensions.
5. ``symbol_nodes`` - AST symbol-graph node count when supplied; ``0`` when
   absent (documented fallback — never raises).

Score and bands
---------------
``score = size_rank + file_count + 2*test_touched + code_file_count + min(symbol_nodes, 50)//10``

Ascending lower bounds (inclusive); the highest matching band wins so a score
exactly on a boundary is assigned that band (documented total order)::

    light     score <  STANDARD_MIN
    standard  score >= STANDARD_MIN and < HEAVY_MIN
    heavy     score >= HEAVY_MIN and < CRITICAL_MIN
    critical  score >= CRITICAL_MIN

``error`` is a reserved marker outside the tier set — config validation
refuses it as a ``tier_models`` key. Call sites catch classifier bugs and
record ``error`` rather than inventing a cheap-tier verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: Bump when feature order, weights, or thresholds change. Recorded on every
#: decision so a policy edit is a named divergence under replay, not silence.
TIER_POLICY_VERSION: Final = 1

#: Closed tier set in ascending strength order (band order for ties).
TIERS: Final[tuple[str, ...]] = ("light", "standard", "heavy", "critical")

#: Reserved marker outside :data:`TIERS`. Not a valid ``tier_models`` key.
TIER_ERROR: Final = "error"

#: Documented feature names in canonical digest order.
FEATURE_ORDER: Final[tuple[str, ...]] = (
    "size_rank",
    "file_count",
    "test_touched",
    "code_file_count",
    "symbol_nodes",
)

# Inclusive lower bounds for standard/heavy/critical; light is everything below.
_STANDARD_MIN: Final = 4
_HEAVY_MIN: Final = 10
_CRITICAL_MIN: Final = 18

_SIZE_RANK: Final[dict[str, int]] = {
    "xs": 0,
    "s": 1,
    "m": 2,
    "l": 3,
    "xl": 4,
}
_DEFAULT_SIZE_RANK: Final = 2  # medium when no size/* label

_DOC_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".md", ".mdx", ".rst", ".txt", ".adoc", ".markdown"},
)
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]+\.py$|(^|/)\w+_test\.py$",
    re.IGNORECASE,
)
_SIZE_LABEL_RE = re.compile(r"^size/(xs|s|m|l|xl)$", re.IGNORECASE)


@dataclass(frozen=True)
class TierFeatures:
    """Feature vector in :data:`FEATURE_ORDER` (all ints, always populated)."""

    size_rank: int
    file_count: int
    test_touched: int
    code_file_count: int
    symbol_nodes: int

    def as_ordered_dict(self) -> dict[str, int]:
        """Return features in canonical digest order."""
        return {
            "size_rank": self.size_rank,
            "file_count": self.file_count,
            "test_touched": self.test_touched,
            "code_file_count": self.code_file_count,
            "symbol_nodes": self.symbol_nodes,
        }


@dataclass(frozen=True)
class TierDecision:
    """Outcome of :func:`classify_tier` (or the reserved error marker)."""

    tier: str
    policy_version: int
    feature_digest: str
    features: dict[str, int]
    score: int

    def to_record(self) -> dict[str, Any]:
        """JSON-friendly payload for the audit-chain selection seam."""
        return {
            "tier": self.tier,
            "tier_policy_version": self.policy_version,
            "feature_digest": self.feature_digest,
            "features": dict(self.features),
            "score": self.score,
        }


def feature_digest(features: Mapping[str, int], *, policy_version: int = TIER_POLICY_VERSION) -> str:
    """SHA-256 hex of version + ordered feature values (stable across processes)."""
    payload = {
        "tier_policy_version": policy_version,
        "features": {name: int(features[name]) for name in FEATURE_ORDER},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def extract_features(
    *,
    labels: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    symbol_nodes: int | None = None,
) -> TierFeatures:
    """Build a complete feature vector; never raises on missing artefacts."""
    size_rank = _DEFAULT_SIZE_RANK
    for raw in labels or ():
        if not isinstance(raw, str):
            continue
        match = _SIZE_LABEL_RE.match(raw.strip())
        if match is not None:
            size_rank = _SIZE_RANK[match.group(1).lower()]
            break

    clean_paths = [p.replace("\\", "/") for p in (paths or ()) if isinstance(p, str) and p.strip()]
    file_count = len(clean_paths)
    test_touched = 1 if any(_TEST_PATH_RE.search(p) for p in clean_paths) else 0
    code_file_count = sum(1 for p in clean_paths if not _is_doc_path(p))
    nodes = 0 if symbol_nodes is None else max(0, int(symbol_nodes))
    return TierFeatures(
        size_rank=size_rank,
        file_count=file_count,
        test_touched=test_touched,
        code_file_count=code_file_count,
        symbol_nodes=nodes,
    )


def score_features(features: TierFeatures) -> int:
    """Return the v1 scalar score for *features*."""
    return (
        features.size_rank
        + features.file_count
        + 2 * features.test_touched
        + features.code_file_count
        + min(features.symbol_nodes, 50) // 10
    )


def tier_for_score(score: int) -> str:
    """Map a score onto a tier; boundary ties take the higher band (inclusive)."""
    if score >= _CRITICAL_MIN:
        return "critical"
    if score >= _HEAVY_MIN:
        return "heavy"
    if score >= _STANDARD_MIN:
        return "standard"
    return "light"


def classify_tier(features: TierFeatures) -> TierDecision:
    """Classify *features* under the current :data:`TIER_POLICY_VERSION`."""
    ordered = features.as_ordered_dict()
    score = score_features(features)
    tier = tier_for_score(score)
    return TierDecision(
        tier=tier,
        policy_version=TIER_POLICY_VERSION,
        feature_digest=feature_digest(ordered),
        features=ordered,
        score=score,
    )


def classify_from_artefacts(
    *,
    labels: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    symbol_nodes: int | None = None,
) -> TierDecision:
    """Extract features then classify. Still never raises on missing inputs."""
    return classify_tier(extract_features(labels=labels, paths=paths, symbol_nodes=symbol_nodes))


def error_decision(*, reason: str = "classifier_error") -> TierDecision:
    """Reserved ``error`` marker for a broken classifier at the call-site boundary."""
    del reason  # retained for call-site clarity / future diagnostic payload
    features = {name: 0 for name in FEATURE_ORDER}
    return TierDecision(
        tier=TIER_ERROR,
        policy_version=TIER_POLICY_VERSION,
        feature_digest=feature_digest(features),
        features=features,
        score=-1,
    )


def verify_tier_decision(
    recorded: Mapping[str, Any],
    *,
    labels: Sequence[str] | None = None,
    paths: Sequence[str] | None = None,
    symbol_nodes: int | None = None,
) -> str | None:
    """Recompute the decision; return a divergence reason or ``None`` if matched.

    A changed :data:`TIER_POLICY_VERSION` is named explicitly so replay does
    not report a generic feature mismatch.
    """
    recorded_version = recorded.get("tier_policy_version")
    if recorded_version != TIER_POLICY_VERSION:
        return f"tier_policy_version diverged: recorded={recorded_version!r} current={TIER_POLICY_VERSION}"
    if recorded.get("tier") == TIER_ERROR:
        # Error markers are not recomputed; presence alone is the record.
        return None
    recomputed = classify_from_artefacts(labels=labels, paths=paths, symbol_nodes=symbol_nodes)
    if recomputed.feature_digest != recorded.get("feature_digest"):
        return (
            f"tier feature_digest diverged: recorded={recorded.get('feature_digest')!r} "
            f"recomputed={recomputed.feature_digest!r}"
        )
    if recomputed.tier != recorded.get("tier"):
        return f"tier diverged: recorded={recorded.get('tier')!r} recomputed={recomputed.tier!r}"
    return None


def features_from_task(task: Any) -> TierFeatures:
    """Pull labels/paths/symbol stats from a task-like object (duck-typed).

    Missing attributes degrade to empty/zero — never raises for absent data.
    """
    labels = _labels_from_task(task)
    paths = _paths_from_task(task)
    symbol_nodes = _symbol_nodes_from_task(task)
    return extract_features(labels=labels, paths=paths, symbol_nodes=symbol_nodes)


def classify_task(task: Any) -> TierDecision:
    """Classify a task-like object under the current policy."""
    return classify_tier(features_from_task(task))


def _is_doc_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _DOC_SUFFIXES)


def _labels_from_task(task: Any) -> list[str]:
    meta = getattr(task, "metadata", None)
    labels: list[str] = []
    if isinstance(meta, Mapping):
        for key in ("labels", "issue_labels", "pr_labels"):
            raw = meta.get(key)
            if isinstance(raw, (list, tuple)):
                labels.extend(str(x) for x in raw if x is not None)
            elif isinstance(raw, str) and raw:
                labels.append(raw)
    tags = getattr(task, "tags", None)
    if isinstance(tags, (list, tuple)):
        labels.extend(str(x) for x in tags if x is not None)
    return labels


def _paths_from_task(task: Any) -> list[str]:
    paths: list[str] = []
    owned = getattr(task, "owned_files", None)
    if isinstance(owned, (list, tuple)):
        paths.extend(str(p) for p in owned if p)
    meta = getattr(task, "metadata", None)
    if isinstance(meta, Mapping):
        for key in ("changed_files", "files", "paths"):
            raw = meta.get(key)
            if isinstance(raw, (list, tuple)):
                paths.extend(str(p) for p in raw if p)
    return paths


def _symbol_nodes_from_task(task: Any) -> int | None:
    meta = getattr(task, "metadata", None)
    if not isinstance(meta, Mapping):
        return None
    graph = meta.get("symbol_graph") or meta.get("ast_symbol_graph")
    if isinstance(graph, Mapping):
        nodes = graph.get("node_count", graph.get("nodes"))
        if isinstance(nodes, int):
            return nodes
        if isinstance(nodes, (list, tuple)):
            return len(nodes)
    nodes = meta.get("symbol_node_count")
    if isinstance(nodes, int):
        return nodes
    return None
