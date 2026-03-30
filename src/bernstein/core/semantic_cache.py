"""Semantic caching layer for LLM requests.

Compresses repeated LLM calls by caching responses keyed on the semantic
content of the request text. Two requests that describe the same task
(even with different wording) hit the same cache entry.

Strategy:
- Exact match: SHA-256 of normalized text (zero-cost lookup)
- Fuzzy match: TF-style word-frequency cosine similarity (no external deps)
- TTL: configurable, default 24h (planning outputs stay valid for a day)
- Storage: .sdd/caching/semantic_cache.jsonl (append-safe, single JSON line)

Target: 30-50% reduction in planning LLM calls via goal-level deduplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum cosine similarity to treat two requests as "the same".
# 1.0 = exact word match only, 0.0 = always cache-hit.
DEFAULT_SIMILARITY_THRESHOLD: float = 0.85

# Cache entries older than this are ignored (seconds). 0 = never expire.
DEFAULT_TTL_SECONDS: float = 86_400.0  # 24 hours

# Evict least-recently-used entries once we exceed this limit.
MAX_CACHE_ENTRIES: int = 500

# ---------------------------------------------------------------------------
# Response cache constants (higher bar — agent spawns are expensive)
# ---------------------------------------------------------------------------

# Cosine threshold for agent-output response cache.  0.95 requires very close
# semantic overlap before we skip spawning an agent.
RESPONSE_CACHE_SIMILARITY_THRESHOLD: float = 0.95

# 7-day TTL for agent results — implementations are more stable than plans.
RESPONSE_CACHE_TTL_SECONDS: float = 604_800.0

# Maximum cached agent results (larger pool — results are worth keeping longer).
RESPONSE_CACHE_MAX_ENTRIES: int = 1_000

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SemanticCacheEntry:
    """A single cached (key_text → response) pair.

    Attributes:
        cache_key: SHA-256 of the normalized key_text.
        key_text: The canonical text used as the cache key (e.g., task goal).
        response: The LLM response that was returned for this key.
        word_vector: Sparse TF word-frequency vector for fuzzy matching.
        model: LLM model name that produced the response.
        hit_count: Times this entry was served from cache (not counting initial store).
        created_at: Unix timestamp when the entry was first stored.
        last_used_at: Unix timestamp of the most recent cache hit.
        verified: Whether the entry came from a real completed task execution.
        git_diff_lines: Number of tracked diff lines in the producing worktree.
        source_task_id: Optional originating task ID for inspection/debugging.
    """

    cache_key: str
    key_text: str
    response: str
    word_vector: dict[str, float]
    model: str
    hit_count: int
    created_at: float
    last_used_at: float | None = None
    verified: bool = False
    git_diff_lines: int = 0
    source_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticCacheEntry:
        """Deserialize from JSON dict."""
        return cls(
            cache_key=data["cache_key"],
            key_text=data["key_text"],
            response=data["response"],
            word_vector=data["word_vector"],
            model=data["model"],
            hit_count=data.get("hit_count", 0),
            created_at=data["created_at"],
            last_used_at=data.get("last_used_at"),
            verified=bool(data.get("verified", False)),
            git_diff_lines=int(data.get("git_diff_lines", 0) or 0),
            source_task_id=(
                str(data["source_task_id"])
                if data.get("source_task_id") is not None
                else None
            ),
        )


@dataclass
class SemanticCacheManifest:
    """Manifest holding all cached entries and aggregate stats.

    Attributes:
        entries: Mapping of cache_key → SemanticCacheEntry.
        total_hits: Total number of cache hits across all entries.
        total_saved_calls: Alias for total_hits (used in dashboards).
    """

    entries: dict[str, SemanticCacheEntry] = field(default_factory=lambda: dict[str, SemanticCacheEntry]())
    total_hits: int = 0
    total_saved_calls: int = 0

    def to_json_line(self) -> str:
        """Serialize to a single compact JSON line."""
        data: dict[str, Any] = {
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
            "total_hits": self.total_hits,
            "total_saved_calls": self.total_saved_calls,
        }
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> SemanticCacheManifest:
        """Deserialize from a single JSON line."""
        data = json.loads(line)
        manifest = cls(
            total_hits=data.get("total_hits", 0),
            total_saved_calls=data.get("total_saved_calls", 0),
        )
        for cache_key, entry_data in data.get("entries", {}).items():
            manifest.entries[cache_key] = SemanticCacheEntry.from_dict(entry_data)
        return manifest


# ---------------------------------------------------------------------------
# Core manager
# ---------------------------------------------------------------------------


class SemanticCacheManager:
    """Goal-level semantic cache for LLM planning calls.

    Caches LLM responses keyed on the *semantic content* of a short key text
    (typically the planning goal).  Reduces redundant API calls when Bernstein
    re-plans similar goals.

    Args:
        workdir: Project root (cache is stored under workdir/.sdd/caching/).
        similarity_threshold: Cosine similarity required for a fuzzy cache hit.
        ttl_seconds: Entries older than this (in seconds) are ignored.
            Set to 0.0 to disable expiry.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._workdir = workdir
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._cache_path = workdir / ".sdd" / "caching" / "semantic_cache.jsonl"
        self._manifest = SemanticCacheManifest()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, key_text: str, model: str) -> tuple[str | None, float]:
        """Look up a cached response for the given key text and model.

        Performs an exact-hash check first, then falls back to cosine-similarity
        search over all non-expired entries for the same model.

        Args:
            key_text: Short text describing the request (e.g., goal string).
            model: LLM model name; entries for other models are ignored.

        Returns:
            ``(response, similarity)`` where *response* is the cached LLM
            output or ``None`` on a cache miss, and *similarity* is in [0, 1].
        """
        # --- exact match (O(1)) ---
        exact_key = _hash(_normalize(key_text))
        entry = self._manifest.entries.get(exact_key)
        if entry is not None and entry.model == model and not self._expired(entry):
            self._record_hit(entry)
            logger.debug("Semantic cache exact-hit for key=%s", exact_key[:12])
            return entry.response, 1.0

        # --- fuzzy match (O(n)) ---
        query_vec = _embed(key_text)
        best_score = 0.0
        best_entry: SemanticCacheEntry | None = None

        for e in self._manifest.entries.values():
            if e.model != model or self._expired(e):
                continue
            score = _cosine(query_vec, e.word_vector)
            if score > best_score:
                best_score = score
                best_entry = e

        if best_entry is not None and best_score >= self._threshold:
            self._record_hit(best_entry)
            logger.info(
                "Semantic cache fuzzy-hit (similarity=%.3f) for model=%s",
                best_score,
                model,
            )
            return best_entry.response, best_score

        return None, 0.0

    def store(self, key_text: str, response: str, model: str) -> None:
        """Store a new LLM response in the cache.

        If the cache exceeds ``MAX_CACHE_ENTRIES``, the least-recently-used
        entries are evicted first.

        Args:
            key_text: Short text that describes the request.
            response: The LLM response to cache.
            model: LLM model name that produced the response.
        """
        norm = _normalize(key_text)
        cache_key = _hash(norm)

        if cache_key in self._manifest.entries:
            # Refresh the entry in case the response improved.
            entry = self._manifest.entries[cache_key]
            entry.response = response
            entry.last_used_at = time.time()
            return

        self._evict_if_needed()

        entry = SemanticCacheEntry(
            cache_key=cache_key,
            key_text=key_text,
            response=response,
            word_vector=_embed(key_text),
            model=model,
            hit_count=0,
            created_at=time.time(),
        )
        self._manifest.entries[cache_key] = entry
        logger.debug("Semantic cache stored entry key=%s model=%s", cache_key[:12], model)

    def save(self) -> None:
        """Persist the current manifest to disk."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_path, "w") as fh:
            fh.write(self._manifest.to_json_line())
        logger.debug("Semantic cache saved to %s", self._cache_path)

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics for monitoring/dashboards.

        Returns:
            Dict with ``entries``, ``total_hits``, ``total_saved_calls``,
            ``threshold``, and ``cache_path``.
        """
        return {
            "entries": len(self._manifest.entries),
            "total_hits": self._manifest.total_hits,
            "total_saved_calls": self._manifest.total_saved_calls,
            "threshold": self._threshold,
            "cache_path": str(self._cache_path),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            text = self._cache_path.read_text().strip()
            if text:
                self._manifest = SemanticCacheManifest.from_json_line(text)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load semantic cache: %s", exc)

    def _expired(self, entry: SemanticCacheEntry) -> bool:
        if self._ttl <= 0:
            return False
        return (time.time() - entry.created_at) > self._ttl

    def _record_hit(self, entry: SemanticCacheEntry) -> None:
        entry.hit_count += 1
        entry.last_used_at = time.time()
        self._manifest.total_hits += 1
        self._manifest.total_saved_calls += 1

    def _evict_if_needed(self) -> None:
        """Remove least-recently-used entries if at capacity."""
        if len(self._manifest.entries) < MAX_CACHE_ENTRIES:
            return
        # Sort by last_used_at (None → created_at fallback), evict oldest 10%
        sorted_keys = sorted(
            self._manifest.entries.keys(),
            key=lambda k: self._manifest.entries[k].last_used_at or self._manifest.entries[k].created_at,
        )
        evict_count = max(1, len(sorted_keys) // 10)
        for key in sorted_keys[:evict_count]:
            del self._manifest.entries[key]
        logger.debug("Semantic cache evicted %d LRU entries", evict_count)


# ---------------------------------------------------------------------------
# Response cache: agent-output reuse at task level
# ---------------------------------------------------------------------------


class ResponseCacheManager:
    """Agent-output response cache for task-level result reuse.

    Caches completed task ``result_summary`` values keyed on the semantic
    content of the task (role + title + description).  When a new task is
    functionally identical (cosine >= 0.95) to a previously completed task,
    the orchestrator returns the cached result instead of spawning an agent.

    Target: 20-40% reduction in agent spawns via result reuse.

    The cache is model-agnostic — it stores *what was accomplished*, not an
    AI response, so the producing model is irrelevant for lookup.

    Storage: ``.sdd/caching/response_cache.jsonl`` (one JSON line).

    Args:
        workdir: Project root (cache stored under workdir/.sdd/caching/).
        similarity_threshold: Cosine similarity required for a cache hit.
            Default 0.95 — deliberately high to prevent incorrect reuse.
        ttl_seconds: Entries older than this (in seconds) are ignored.
            Default 7 days.  Set to 0.0 to disable expiry.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        similarity_threshold: float = RESPONSE_CACHE_SIMILARITY_THRESHOLD,
        ttl_seconds: float = RESPONSE_CACHE_TTL_SECONDS,
    ) -> None:
        self._workdir = workdir
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._cache_path = workdir / ".sdd" / "caching" / "response_cache.jsonl"
        self._manifest = SemanticCacheManifest()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def task_key(role: str, title: str, description: str) -> str:
        """Build a canonical lookup key for a task.

        Args:
            role: Task role (e.g. ``"backend"``, ``"qa"``).
            title: Task title.
            description: Task description.

        Returns:
            A single string suitable for ``lookup`` / ``store``.
        """
        return f"{role}:{title}\n{description}"

    def lookup(self, key_text: str) -> tuple[str | None, float]:
        """Look up a cached result for the given task key.

        Performs an exact-hash check first, then falls back to cosine-
        similarity search over all non-expired entries.

        Args:
            key_text: Task key from :meth:`task_key`.

        Returns:
            ``(result_summary, similarity)`` where *result_summary* is the
            cached agent output or ``None`` on a miss.
        """
        entry, similarity = self.lookup_entry(key_text)
        if entry is None:
            return None, similarity
        return entry.response, similarity

    def lookup_entry(self, key_text: str) -> tuple[SemanticCacheEntry | None, float]:
        """Look up a cached entry for the given task key.

        Args:
            key_text: Task key from :meth:`task_key`.

        Returns:
            ``(entry, similarity)`` where *entry* is the matched cache entry
            or ``None`` on a miss.
        """
        # --- exact match (O(1)) ---
        exact_key = _hash(_normalize(key_text))
        entry = self._manifest.entries.get(exact_key)
        if entry is not None and not self._expired(entry):
            self._record_hit(entry)
            logger.debug("Response cache exact-hit for key=%s", exact_key[:12])
            return entry, 1.0

        # --- fuzzy match (O(n)) ---
        query_vec = _embed(key_text)
        best_score = 0.0
        best_entry: SemanticCacheEntry | None = None

        for e in self._manifest.entries.values():
            if self._expired(e):
                continue
            score = _cosine(query_vec, e.word_vector)
            if score > best_score:
                best_score = score
                best_entry = e

        if best_entry is not None and best_score >= self._threshold:
            self._record_hit(best_entry)
            logger.info(
                "Response cache fuzzy-hit (similarity=%.3f) for task",
                best_score,
            )
            return best_entry, best_score

        return None, 0.0

    def store(
        self,
        key_text: str,
        result_summary: str,
        *,
        verified: bool = False,
        git_diff_lines: int = 0,
        source_task_id: str | None = None,
    ) -> None:
        """Cache a completed task's result_summary.

        Args:
            key_text: Task key from :meth:`task_key`.
            result_summary: The agent's result to cache.  Empty strings are
                not stored (they carry no reusable information).
            verified: Whether the entry came from a verified real execution.
            git_diff_lines: Number of tracked diff lines in the producing run.
            source_task_id: Originating task ID when known.
        """
        if not result_summary:
            return

        norm = _normalize(key_text)
        cache_key = _hash(norm)

        if cache_key in self._manifest.entries:
            # Refresh the stored result.
            entry = self._manifest.entries[cache_key]
            entry.response = result_summary
            entry.last_used_at = time.time()
            entry.verified = verified
            entry.git_diff_lines = git_diff_lines
            entry.source_task_id = source_task_id
            return

        self._evict_if_needed()

        entry = SemanticCacheEntry(
            cache_key=cache_key,
            key_text=key_text,
            response=result_summary,
            word_vector=_embed(key_text),
            model="agent",  # model-agnostic — result caching only
            hit_count=0,
            created_at=time.time(),
            verified=verified,
            git_diff_lines=git_diff_lines,
            source_task_id=source_task_id,
        )
        self._manifest.entries[cache_key] = entry
        logger.debug("Response cache stored entry key=%s", cache_key[:12])

    def list_entries(self) -> list[SemanticCacheEntry]:
        """Return response-cache entries sorted by recency."""
        return sorted(
            self._manifest.entries.values(),
            key=lambda entry: entry.last_used_at or entry.created_at,
            reverse=True,
        )

    def inspect_task(self, task_id: str) -> SemanticCacheEntry | None:
        """Return the response-cache entry that originated from *task_id*."""
        for entry in self._manifest.entries.values():
            if entry.source_task_id == task_id:
                return entry
        return None

    def clear(self, *, unverified_only: bool = False) -> int:
        """Remove response-cache entries and persist the updated manifest.

        Args:
            unverified_only: When True, keep verified entries and remove only
                unverified ones.

        Returns:
            Number of entries removed.
        """
        if unverified_only:
            removed = sum(1 for entry in self._manifest.entries.values() if not entry.verified)
            self._manifest.entries = {
                key: entry
                for key, entry in self._manifest.entries.items()
                if entry.verified
            }
        else:
            removed = len(self._manifest.entries)
            self._manifest.entries.clear()

        if removed > 0 or self._cache_path.exists():
            self.save()
        return removed

    def save(self) -> None:
        """Persist the current manifest to disk."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_path, "w") as fh:
            fh.write(self._manifest.to_json_line())
        logger.debug("Response cache saved to %s", self._cache_path)

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics for monitoring/dashboards.

        Returns:
            Dict with ``entries``, ``total_hits``, ``total_saved_calls``,
            ``threshold``, and ``cache_path``.
        """
        return {
            "entries": len(self._manifest.entries),
            "total_hits": self._manifest.total_hits,
            "total_saved_calls": self._manifest.total_saved_calls,
            "threshold": self._threshold,
            "cache_path": str(self._cache_path),
            "verified_entries": sum(1 for entry in self._manifest.entries.values() if entry.verified),
            "unverified_entries": sum(1 for entry in self._manifest.entries.values() if not entry.verified),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            text = self._cache_path.read_text().strip()
            if text:
                self._manifest = SemanticCacheManifest.from_json_line(text)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load response cache: %s", exc)

    def _expired(self, entry: SemanticCacheEntry) -> bool:
        if self._ttl <= 0:
            return False
        return (time.time() - entry.created_at) > self._ttl

    def _record_hit(self, entry: SemanticCacheEntry) -> None:
        entry.hit_count += 1
        entry.last_used_at = time.time()
        self._manifest.total_hits += 1
        self._manifest.total_saved_calls += 1

    def _evict_if_needed(self) -> None:
        """Remove least-recently-used entries if at capacity."""
        if len(self._manifest.entries) < RESPONSE_CACHE_MAX_ENTRIES:
            return
        sorted_keys = sorted(
            self._manifest.entries.keys(),
            key=lambda k: self._manifest.entries[k].last_used_at or self._manifest.entries[k].created_at,
        )
        evict_count = max(1, len(sorted_keys) // 10)
        for key in sorted_keys[:evict_count]:
            del self._manifest.entries[key]
        logger.debug("Response cache evicted %d LRU entries", evict_count)


# ---------------------------------------------------------------------------
# Pure functions (testable without instantiating the manager)
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    """SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode()).hexdigest()


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _embed(text: str) -> dict[str, float]:
    """Return a sparse TF (term-frequency) word vector."""
    words = _normalize(text).split()
    if not words:
        return {}
    counts = Counter(words)
    total = float(len(words))
    return {word: count / total for word, count in counts.items()}


def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
    """Sparse cosine similarity between two word-frequency vectors.

    Args:
        v1: First TF vector (word → frequency).
        v2: Second TF vector.

    Returns:
        Float in [0.0, 1.0]; 0.0 when either vector is empty.
    """
    if not v1 or not v2:
        return 0.0
    shared = set(v1.keys()) & set(v2.keys())
    if not shared:
        return 0.0
    dot = sum(v1[k] * v2[k] for k in shared)
    mag1 = math.sqrt(sum(v * v for v in v1.values()))
    mag2 = math.sqrt(sum(v * v for v in v2.values()))
    if mag1 == 0.0 or mag2 == 0.0:
        return 0.0
    return dot / (mag1 * mag2)
