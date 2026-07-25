"""Cross-task knowledge base: explicit publish/subscribe over tag-indexed memory.

The orchestrator already ships a tag-indexed SQLite store and a JSONL log under
``bernstein.core.memory``. What was missing is a small, explicit public surface
for one task to *publish* a fact under a tag and another task to *subscribe* on
that tag - without writing files to a shared worktree path and hoping the next
agent reads them.

This module is a thin facade. It does not introduce new storage. Every fact is
persisted as a row in the existing ``memory`` table with ``type='cross_task'``
and an extra ``cross_task_meta`` row carrying the lineage-style attribution
triple ``(producer_task_id, ts_ns, content_hash)``. That mirrors the pattern
used by :mod:`bernstein.core.lineage.signed_write` for artefact writes.

Two scopes are supported, both single-host:

* ``run`` - facts visible only within the current orchestration run. The
  ``run_id`` is supplied at facade-construction time.
* ``project`` - facts visible to every task in the same ``.sdd/`` project root.

Conflict resolution for two tasks publishing the same ``(scope, tag, key)`` is
last-write-wins, with a warning emitted into the trace log. Operator-policy
resolution is out of scope for this iteration; see the ticket for v2 plans.

Public surface::

    facade = CrossTaskKB(store, run_id="r-1", producer_task_id="t-7")
    facade.publish(tag="api-schema", key="users", value="...", scope="run")
    for fact in facade.subscribe(tag="api-schema", scope="run"):
        ...
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "CrossTaskKB",
    "Fact",
    "PublishCounter",
    "Scope",
    "redact_value",
]

Scope = Literal["run", "project"]
"""Visibility scope for a published fact.

``run``     - same orchestration run only (single ``run_id``).
``project`` - any task in the same ``.sdd/`` project root.
"""

_VALID_SCOPES: frozenset[str] = frozenset({"run", "project"})


@dataclass(frozen=True, slots=True)
class Fact:
    """A single published fact with attribution.

    The triple ``(producer_task_id, ts_ns, content_hash)`` mirrors the
    attribution shape used by :class:`bernstein.core.lineage.entry.LineageEntry`
    so a downstream auditor can correlate a fact with the lineage entry of the
    artefact that produced it.

    Attributes:
        tag: The subscription tag the value was published under.
        key: A short stable identifier scoped under ``tag``.
        value: The published payload. Returned as stored; subscribers that
            need redaction should call :func:`redact_value` themselves.
        scope: ``"run"`` or ``"project"``.
        producer_task_id: Task ID of the agent that published the fact.
        ts_ns: Wall-clock nanoseconds at publish time.
        content_hash: ``sha256:<hex>`` of the UTF-8 encoded ``value`` bytes.
    """

    tag: str
    key: str
    value: str
    scope: Scope
    producer_task_id: str
    ts_ns: int
    content_hash: str


class PublishCounter:
    """Thread-safe in-process counters for publish/subscribe operations.

    Exposed so the orchestrator's run summary can read totals without taking
    a dependency on the SQLite schema. Counters reset per process.
    """

    __slots__ = ("_lock", "publish", "subscribe")

    def __init__(self) -> None:
        self.publish: int = 0
        self.subscribe: int = 0
        self._lock = threading.Lock()

    def incr_publish(self) -> None:
        with self._lock:
            self.publish += 1

    def incr_subscribe(self, count: int = 1) -> None:
        with self._lock:
            self.subscribe += count

    def snapshot(self) -> tuple[int, int]:
        """Return ``(publish, subscribe)`` counts atomically."""
        with self._lock:
            return self.publish, self.subscribe


_GLOBAL_COUNTER = PublishCounter()


def get_global_counter() -> PublishCounter:
    """Return the process-global counter consumed by the run summary."""
    return _GLOBAL_COUNTER


def _content_hash(value: str) -> str:
    """Compute ``sha256:<hex>`` over the UTF-8 encoding of ``value``."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_tag(tag: str) -> None:
    if not tag or not tag.strip():
        raise ValueError("tag must be a non-empty string")
    if "," in tag:
        # The underlying ``memory.tags`` column is comma-joined; a comma in
        # the tag itself would break the tag-LIKE filter used by ``list()``.
        raise ValueError("tag must not contain ','")


def _validate_key(key: str) -> None:
    if not key or not key.strip():
        raise ValueError("key must be a non-empty string")


def _validate_scope(scope: str) -> None:
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# The patterns mirror :mod:`bernstein.core.observability.log_redact`. We
# duplicate a minimal subset here so this module does not depend on the
# observability stack. Keep the regex list in sync if the canonical patterns
# evolve.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
    re.compile(r"(?:\+\d{1,3}[\s\-])?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
)


def redact_value(value: str) -> str:
    """Return ``value`` with email/phone/SSN/credit-card matches masked.

    Used by the CLI ``query`` subcommand. Storage is never mutated; redaction
    happens on read so an operator can still audit raw values out of band.
    """
    out = value
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cross_task_meta (
    memory_id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    tag TEXT NOT NULL,
    key TEXT NOT NULL,
    producer_task_id TEXT NOT NULL,
    ts_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    run_id TEXT,
    FOREIGN KEY(memory_id) REFERENCES memory(id) ON DELETE CASCADE
)
"""

_META_INDEXES_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_cross_task_scope_tag ON cross_task_meta(scope, tag)",
    "CREATE INDEX IF NOT EXISTS idx_cross_task_run ON cross_task_meta(scope, run_id, tag)",
    "CREATE INDEX IF NOT EXISTS idx_cross_task_key ON cross_task_meta(scope, tag, key)",
)


class CrossTaskKB:
    """Publish/subscribe facade over the existing SQLite memory store.

    A facade instance is bound to a single producing task and a single run.
    Subscribers are free to read facts from any producer; the binding only
    affects what ``publish`` will write into the attribution fields.

    Args:
        store: The shared :class:`SQLiteMemoryStore`. The facade reuses the
            underlying database file - it does not open a parallel store.
        run_id: Identifier for the current orchestration run; required for
            ``scope='run'`` publish and subscribe.
        producer_task_id: Task ID of the publishing agent. Used as the
            ``producer_task_id`` attribution field on facts this facade
            writes. Subscribers that only read may pass an empty string.
        hook_dispatch: Optional callable invoked once per publish with
            ``(event_name, payload)``. Wired by the orchestrator to fan a
            ``kb.fact_published`` lifecycle event out to plugins. The
            facade fires this best-effort; failures are swallowed so a
            misbehaving hook cannot break the publish path.
        counter: Counter for run-summary aggregation. Defaults to the
            process-global instance.
        clock_ns: Indirection point for tests to pin ``ts_ns``.
    """

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        run_id: str = "",
        producer_task_id: str = "",
        hook_dispatch: Callable[[str, dict[str, object]], None] | None = None,
        counter: PublishCounter | None = None,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._producer_task_id = producer_task_id
        self._hook_dispatch = hook_dispatch
        self._counter = counter if counter is not None else _GLOBAL_COUNTER
        self._clock_ns = clock_ns or time.time_ns
        self._init_meta_table()

    @property
    def store(self) -> SQLiteMemoryStore:
        """The underlying SQLite store. Exposed for tests and inspection."""
        return self._store

    @property
    def counter(self) -> PublishCounter:
        """The publish/subscribe counter consumed by the run summary."""
        return self._counter

    def _init_meta_table(self) -> None:
        """Create the attribution sidecar table on first use."""
        with sqlite3.connect(self._store.db_path) as conn:
            conn.execute(_META_TABLE_SQL)
            for stmt in _META_INDEXES_SQL:
                conn.execute(stmt)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(
        self,
        tag: str,
        key: str,
        value: str,
        *,
        scope: Scope,
        source_adapter: str | None = None,
    ) -> Fact:
        """Publish a fact under ``tag``/``key`` with the given ``scope``.

        Last-write-wins on ``(scope, tag, key)``. The previous fact is left
        in storage so the audit log retains every version; only the latest
        is returned by :meth:`subscribe`. A trace warning is logged when a
        prior fact under the same identity already exists.

        Args:
            tag: Subscription tag. Non-empty, no commas.
            key: Stable identifier within the tag. Non-empty.
            value: The payload. Stored verbatim.
            scope: ``"run"`` or ``"project"``.
            source_adapter: Optional CLI-adapter identifier (claude-code,
                codex, gemini-cli, ...). Forwarded to the underlying
                :class:`SQLiteMemoryStore` so subscribers that opt into the
                adapter read filter (``read_only_from_adapters=``) can
                isolate payloads by producing adapter.

        Returns:
            The :class:`Fact` that was persisted, including its attribution
            triple.

        Raises:
            ValueError: ``tag``, ``key``, or ``scope`` failed validation.
        """
        _validate_tag(tag)
        _validate_key(key)
        _validate_scope(scope)

        ts_ns = self._clock_ns()
        chash = _content_hash(value)

        # The free-text content lives in ``memory.content`` so the existing
        # tag-LIKE search keeps working for operators who already query the
        # store directly. The structured metadata sits in the sidecar table.
        # We embed ``scope`` and ``key`` into the memory tags so a CLI
        # ``memory list --tag <tag>`` surface still works without joining.
        memory_tags = [tag, f"cross_task:{scope}", f"key:{key}"]
        memory_id = self._store.add(
            type="cross_task",
            content=value,
            tags=memory_tags,
            importance=1.0,
            task_id=self._producer_task_id or None,
            source_adapter=source_adapter,
        )

        existing_warning = ""
        with sqlite3.connect(self._store.db_path) as conn:
            row = conn.execute(
                """
                SELECT memory_id FROM cross_task_meta
                WHERE scope = ? AND tag = ? AND key = ?
                  AND (? = '' OR run_id IS NULL OR run_id = ?)
                ORDER BY ts_ns DESC LIMIT 1
                """,
                (scope, tag, key, self._run_id, self._run_id),
            ).fetchone()
            if row is not None:
                existing_warning = (
                    f"cross-task fact ({scope}, {tag}, {key}) overwritten; "
                    f"previous memory_id={row[0]} - last-write-wins"
                )
            conn.execute(
                """
                INSERT INTO cross_task_meta
                    (memory_id, scope, tag, key, producer_task_id, ts_ns, content_hash, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    scope,
                    tag,
                    key,
                    self._producer_task_id,
                    ts_ns,
                    chash,
                    self._run_id if scope == "run" else None,
                ),
            )

        if existing_warning:
            logger.warning(existing_warning)

        fact = Fact(
            tag=tag,
            key=key,
            value=value,
            scope=scope,
            producer_task_id=self._producer_task_id,
            ts_ns=ts_ns,
            content_hash=chash,
        )

        self._counter.incr_publish()
        self._emit_trace("kb.publish", fact)
        self._fire_hook("kb.fact_published", fact)
        return fact

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        tag: str,
        *,
        scope: Scope,
        read_only_from_adapters: list[str] | None = None,
    ) -> Iterator[Fact]:
        """Yield the most recent fact per ``key`` published under ``tag``.

        Args:
            tag: Subscription tag. Non-empty, no commas.
            scope: ``"run"`` or ``"project"``. ``run`` returns only facts
                published with the same ``run_id`` as this facade.
            read_only_from_adapters: Optional opt-in allow-list. When set,
                only facts whose underlying memory row has a matching
                ``source_adapter`` are yielded; rows with NULL provenance
                are excluded. An empty list yields nothing. Default
                (``None``) preserves the legacy "every fact" behaviour.

        Yields:
            One :class:`Fact` per ``key``, newest first by ``ts_ns``.

        Raises:
            ValueError: ``tag`` or ``scope`` failed validation.
        """
        _validate_tag(tag)
        _validate_scope(scope)

        # Pick the latest row per key for the requested (scope, tag),
        # filtered by run_id when scope='run'. Done in two steps to keep
        # the SQL portable across SQLite versions: collect the matching
        # meta rows, then keep the newest per key in Python.
        if scope == "run":
            where_run = "AND meta.run_id = ?"
            params: tuple[object, ...] = (scope, tag, self._run_id)
        else:
            where_run = ""
            params = (scope, tag)

        if read_only_from_adapters is not None:
            if not read_only_from_adapters:
                # Empty allow-list = nobody allowed.
                self._counter.incr_subscribe(0)
                return
            adapter_placeholders = ",".join("?" for _ in read_only_from_adapters)
            where_adapter = f"AND mem.source_adapter IN ({adapter_placeholders})"
            params = params + tuple(read_only_from_adapters)
        else:
            where_adapter = ""

        query = f"""
            SELECT meta.tag, meta.key, mem.content, meta.scope,
                   meta.producer_task_id, meta.ts_ns, meta.content_hash
            FROM cross_task_meta meta
            JOIN memory mem ON mem.id = meta.memory_id
            WHERE meta.scope = ? AND meta.tag = ? {where_run} {where_adapter}
            ORDER BY meta.ts_ns DESC
        """

        latest_by_key: dict[str, Fact] = {}
        with sqlite3.connect(self._store.db_path) as conn:
            for row in conn.execute(query, params):
                key = row[1]
                if key in latest_by_key:
                    continue
                latest_by_key[key] = Fact(
                    tag=row[0],
                    key=row[1],
                    value=row[2],
                    scope=row[3],
                    producer_task_id=row[4] or "",
                    ts_ns=int(row[5]),
                    content_hash=row[6],
                )
        results: list[Fact] = sorted(latest_by_key.values(), key=lambda f: f.ts_ns, reverse=True)

        self._counter.incr_subscribe(len(results))
        for fact in results:
            self._emit_trace("kb.subscribe", fact)
            yield fact

    # ------------------------------------------------------------------
    # Trace + hook emission
    # ------------------------------------------------------------------

    def _emit_trace(self, event: str, fact: Fact) -> None:
        """Emit a structured trace line for ``event``.

        We use the standard :mod:`logging` channel so existing trace
        collectors (JSONL trace writers configured at the logging layer)
        pick it up. Best-effort; failures must never break the caller.
        """
        try:
            logger.info(
                "%s tag=%s key=%s scope=%s producer=%s ts_ns=%d hash=%s",
                event,
                fact.tag,
                fact.key,
                fact.scope,
                fact.producer_task_id,
                fact.ts_ns,
                fact.content_hash,
            )
        except Exception as exc:  # pragma: no cover - logging must never break flow
            logger.debug("cross_task_kb trace emit failed: %s", exc)

    def _fire_hook(self, event: str, fact: Fact) -> None:
        """Dispatch ``event`` through the optional hook callback.

        The orchestrator wires this to fire a ``kb.fact_published``
        lifecycle event. Hook failures are logged at debug level and
        swallowed: a misbehaving plugin must not break the publish path.
        """
        if self._hook_dispatch is None:
            return
        payload: dict[str, object] = {
            "tag": fact.tag,
            "key": fact.key,
            "scope": fact.scope,
            "producer_task_id": fact.producer_task_id,
            "ts_ns": fact.ts_ns,
            "content_hash": fact.content_hash,
        }
        try:
            self._hook_dispatch(event, payload)
        except Exception as exc:  # pragma: no cover - hook must not break flow
            logger.debug("cross_task_kb hook dispatch failed: %s", exc)
