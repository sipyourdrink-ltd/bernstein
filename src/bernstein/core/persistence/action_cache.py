"""Action-level cache and deterministic replay for agent runs.

Built on top of :class:`bernstein.core.persistence.fingerprint.MemoStore`.
We do **not** re-implement on-disk storage or LRU eviction - those belong
to ``MemoStore`` and we would only diverge if forked.

What this module adds on top of MemoStore:

* :class:`ActionRecord` - a typed payload (model_id, prompt, output_text,
  tool_results, token_counts, timestamp, run_id, cost_usd) that captures
  one LLM-or-tool call deterministically.
* :func:`derive_key` - content-addressed cache key derived from
  ``hash(model_id, normalized_prompt, tool_name, tool_args)``.  Crucially
  this is **input-only** (unlike fingerprint memoization, which folds the
  function body into the key) - that's the whole point of action caching:
  identical inputs MUST produce a hit even after Bernstein's code changes.
* :class:`ActionCache` - orchestrates ``MemoStore`` get/put plus
  per-record metric increments.  Modes: ``record`` (always live, append),
  ``replay`` (cache-only, miss → ``CacheMiss``), ``hybrid`` (cache then
  fallthrough - the common case for CI replays).
* :func:`redact_secrets` - strips API keys, bearer tokens, and known
  header patterns from the prompt before hashing or persisting.

Pattern source: https://github.com/nibzard/awesome-agentic-patterns
(``patterns/action-caching-replay.md``).

Single-host only for v1.  Cross-machine sharing can layer on
``core/storage/sink.py`` later - same record schema, different store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from bernstein.core.persistence.fingerprint import MemoStore

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

CacheMode = Literal["record", "replay", "hybrid", "off"]

_DEFAULT_MAX_MB: Final[int] = 500
_RECORD_VERSION: Final[int] = 1

# Patterns we strip from prompts before hashing/persisting.  Conservative -
# if it looks remotely like a credential, redact.  Order matters: longer /
# stricter patterns first.
_REDACT_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"sk-[\w-]{20,}", re.ASCII), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"sk-ant-[\w-]{20,}", re.ASCII), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"ghp_[^\W_]{20,}", re.ASCII), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"github_pat_\w{20,}", re.ASCII), "[REDACTED_GH_PAT]"),
    (re.compile(r"AKIA[\dA-Z]{16}", re.ASCII), "[REDACTED_AWS_KEY]"),
    (re.compile(r"bearer\s+[\w.-]{16,}", re.IGNORECASE | re.ASCII), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)(authorization|x-api-key|api[_-]?key)\s*[:=]\s*[^\s,;]{8,}"),
        r"\1: [REDACTED]",
    ),
)


def redact_secrets(text: str) -> str:
    """Return *text* with known credential patterns replaced by placeholders.

    Used both before hashing (so two runs with different API keys still
    produce the same cache key) and before persisting (so we never write
    a secret to ``.sdd/runtime/memo``).
    """
    redacted = text
    for pattern, replacement in _REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _normalize_prompt(prompt: str) -> str:
    """Strip whitespace volatility and redact secrets before hashing.

    Two runs that differ only in trailing newlines or in their bearer
    token MUST hash identically - otherwise caches never hit in CI.
    """
    return redact_secrets(prompt.strip())


def _canonical_args(tool_args: Mapping[str, Any] | None) -> bytes:
    """Return deterministic bytes for arbitrary tool argument mappings."""
    if not tool_args:
        return b"{}"
    try:
        return json.dumps(dict(tool_args), sort_keys=True, default=repr).encode("utf-8")
    except (TypeError, ValueError):
        return repr(sorted(tool_args.items())).encode("utf-8", errors="replace")


def derive_key(
    *,
    model_id: str,
    prompt: str,
    tool_name: str | None = None,
    tool_args: Mapping[str, Any] | None = None,
) -> bytes:
    """Return a 32-byte SHA-256 digest of the action's input identity.

    Key inputs are deliberately limited to *what the model sees*:
    - ``model_id`` (e.g. ``claude-opus-4-7``)
    - ``prompt`` after redaction + normalization
    - optional ``tool_name`` + ``tool_args`` for tool-call records

    NOTE: this is **not** a fingerprint key.  We want hits across code
    changes - fingerprints want misses across code changes.
    """
    hasher = hashlib.sha256(model_id.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(_normalize_prompt(prompt).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update((tool_name or "").encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(_canonical_args(tool_args))
    return hasher.digest()


@dataclass(frozen=True)
class TokenCounts:
    """Token accounting for one action."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ActionRecord:
    """One recorded LLM (or tool) invocation, suitable for replay.

    Stored as the payload behind a ``derive_key`` digest in MemoStore.
    ``version`` lets future readers reject unknown schemas instead of
    crashing on attribute lookup.
    """

    model_id: str
    prompt: str  # already redacted at construction time
    output_text: str
    tool_name: str | None = None
    tool_args: Mapping[str, Any] | None = None
    tool_results: Mapping[str, Any] | None = None
    tokens: TokenCounts = field(default_factory=TokenCounts)
    cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)
    run_id: str | None = None
    version: int = _RECORD_VERSION

    def to_json(self) -> str:
        """Serialize for the optional sidecar JSON dump (debug / replay CLI)."""
        return json.dumps(asdict(self), sort_keys=True, default=repr)


@dataclass(frozen=True)
class ActionCacheStats:
    """Per-process counters for hits/misses/savings."""

    hits: int = 0
    misses: int = 0
    savings_usd: float = 0.0


class CacheMiss(LookupError):
    """Raised in ``replay`` mode when no record exists for the key."""


class ActionCache:
    """Action-level cache layered on a :class:`MemoStore`.

    The store handles bytes-on-disk and LRU eviction; this class adds the
    typed record schema, key derivation, mode handling, and metric hooks.
    """

    def __init__(
        self,
        store: MemoStore,
        *,
        mode: CacheMode = "hybrid",
        run_id: str | None = None,
    ) -> None:
        self._store = store
        self._mode: CacheMode = mode
        self._run_id = run_id
        self._hits = 0
        self._misses = 0
        self._savings_usd = 0.0

    @property
    def mode(self) -> CacheMode:
        return self._mode

    @property
    def store(self) -> MemoStore:
        return self._store

    def stats(self) -> ActionCacheStats:
        return ActionCacheStats(
            hits=self._hits,
            misses=self._misses,
            savings_usd=self._savings_usd,
        )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def lookup(
        self,
        *,
        model_id: str,
        prompt: str,
        tool_name: str | None = None,
        tool_args: Mapping[str, Any] | None = None,
    ) -> ActionRecord | None:
        """Fetch a cached action for the exact model + prompt + tool call.

        Returns ``None`` if the feature is disabled, the record is missing, or
        the stored payload is corrupt.
        """
        if self._mode == "off":
            return None
        digest = derive_key(model_id=model_id, prompt=prompt, tool_name=tool_name, tool_args=tool_args)
        raw = self._store.get(digest)

        if raw is None:
            self._misses += 1
            return None

        record = _coerce_record(raw)
        if record is None:
            self._misses += 1
            return None

        self._hits += 1
        self._savings_usd += record.cost_usd
        _emit_hit_metric(model_id, record.cost_usd)
        return record

    def resolve_by_content_hash(self, content_hash: str) -> ActionRecord | None:
        """Resolve a known content_hash to its ActionRecord.

        Used by orchestration boundaries (e.g. WorkerLoopDetector) to recover
        the canonical (argv, outcome) of a cached action.
        """
        if self._mode == "off" or not content_hash:
            return None
        try:
            digest = bytes.fromhex(content_hash)
        except (ValueError, TypeError):
            return None
        return _coerce_record(self._store.get(digest))

    def record(
        self,
        *,
        model_id: str,
        prompt: str,
        output_text: str,
        tool_name: str | None = None,
        tool_args: Mapping[str, Any] | None = None,
        tool_results: Mapping[str, Any] | None = None,
        tokens: TokenCounts | None = None,
        cost_usd: float = 0.0,
    ) -> ActionRecord:
        """Persist a fresh action record and return the typed object.

        No-op (returns the typed record without writing) when ``mode`` is
        ``replay`` or ``off`` - replay must never mutate the cache.
        """
        rec = ActionRecord(
            model_id=model_id,
            prompt=redact_secrets(prompt),
            output_text=output_text,
            tool_name=tool_name,
            tool_args=dict(tool_args) if tool_args else None,
            tool_results=dict(tool_results) if tool_results else None,
            tokens=tokens or TokenCounts(),
            cost_usd=cost_usd,
            run_id=self._run_id,
        )
        if self._mode in ("replay", "off"):
            return rec
        digest = derive_key(model_id=model_id, prompt=prompt, tool_name=tool_name, tool_args=tool_args)
        self._store.put(digest, rec)
        return rec

    def get_or_call(
        self,
        *,
        model_id: str,
        prompt: str,
        tool_name: str | None = None,
        tool_args: Mapping[str, Any] | None = None,
        live_call: Any,
    ) -> ActionRecord:
        """Cache-aware wrapper around a live LLM/tool invocation.

        ``live_call`` is a zero-arg callable returning a tuple
        ``(output_text, tool_results, tokens, cost_usd)``.  In ``replay``
        mode a miss raises :class:`CacheMiss` rather than calling out.
        """
        cached = self.lookup(model_id=model_id, prompt=prompt, tool_name=tool_name, tool_args=tool_args)
        if cached is not None:
            return cached
        if self._mode == "replay":
            raise CacheMiss(
                f"action_cache: replay-mode miss for model={model_id} tool={tool_name or 'llm'} (no recorded action)"
            )
        output_text, tool_results, tokens, cost_usd = live_call()
        return self.record(
            model_id=model_id,
            prompt=prompt,
            output_text=output_text,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_results=tool_results,
            tokens=tokens,
            cost_usd=cost_usd,
        )


def _coerce_record(raw: Any) -> ActionRecord | None:
    """Best-effort coercion of a stored payload into an :class:`ActionRecord`.

    MemoStore round-trips via ``pickle`` so we usually get the original
    dataclass back.  We still defensively check the version to reject
    payloads from older schemas.
    """
    if isinstance(raw, ActionRecord):
        if raw.version != _RECORD_VERSION:
            logger.debug("action_cache: skipping record with version=%s", raw.version)
            return None
        return raw
    return None


# ---------------------------------------------------------------------------
# Factory / metric helpers
# ---------------------------------------------------------------------------


def default_store(workdir: Path, max_mb: int | None = None) -> MemoStore:
    """Return an action-cache-rooted MemoStore at ``.sdd/runtime/action_cache``.

    Reuses ``MemoStore`` directly - same on-disk format and eviction as
    fingerprint memoization, just under a sibling directory so the two
    caches don't share a key namespace.
    """
    if max_mb is None:
        try:
            from bernstein.core import defaults as _defaults

            max_mb = int(getattr(_defaults.ACTION_CACHE, "size_mb", _DEFAULT_MAX_MB))
        except (ImportError, AttributeError):
            max_mb = _DEFAULT_MAX_MB
    return MemoStore(root=workdir / ".sdd" / "runtime" / "action_cache", max_mb=max_mb)


def open_cache(
    workdir: Path,
    *,
    mode: CacheMode | None = None,
    run_id: str | None = None,
    max_mb: int | None = None,
) -> ActionCache:
    """Construct an :class:`ActionCache` with config-driven defaults.

    Reads ``defaults.ACTION_CACHE`` for ``mode`` and ``size_mb`` when not
    explicitly provided.  Returns an ``off``-mode cache when the feature
    flag is disabled - callers can still use ``record()``/``lookup()``
    safely.
    """
    resolved_mode: CacheMode = mode or "hybrid"
    if mode is None:
        try:
            from bernstein.core import defaults as _defaults

            cfg = _defaults.ACTION_CACHE
            resolved_mode = "off" if not getattr(cfg, "enabled", True) else getattr(cfg, "mode", "hybrid")
        except (ImportError, AttributeError):
            resolved_mode = "hybrid"
    store = default_store(workdir, max_mb=max_mb)
    return ActionCache(store, mode=resolved_mode, run_id=run_id)


def _emit_hit_metric(model_id: str, cost_usd: float) -> None:
    """Best-effort Prometheus counter increment.  No-op when unavailable."""
    try:
        from bernstein.core.observability import prometheus as _p
    except ImportError:
        return
    hits = getattr(_p, "action_cache_hits_total", None)
    savings = getattr(_p, "action_cache_savings_usd_total", None)
    if hits is not None:
        with contextlib.suppress(Exception):
            hits.labels(model=model_id).inc()
    if savings is not None and cost_usd > 0:
        with contextlib.suppress(Exception):
            savings.labels(model=model_id).inc(cost_usd)


__all__ = [
    "ActionCache",
    "ActionCacheStats",
    "ActionRecord",
    "CacheMiss",
    "CacheMode",
    "TokenCounts",
    "default_store",
    "derive_key",
    "open_cache",
    "redact_secrets",
]
