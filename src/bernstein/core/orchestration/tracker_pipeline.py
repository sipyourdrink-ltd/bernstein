"""Tracker comments as a multi-agent handoff message bus.

The tracker becomes the durable, audit-trailed, human-observable message
bus for a crew of specialist agents (architect, backend, qa, security).
Each role reads and writes the same ticket; the tracker workflow itself
encodes the pipeline. There is no queue server, no DB, no service mesh:
just the tracker plus four primitives that the in-place
"filter + claim + comment + transition" pattern lacks.

Primitives shipped here
-----------------------
* :class:`PipelineStage` - typed view of a single stage in
  ``bernstein.yaml: orchestration.tracker_pipeline.pipeline_stages``.
* :class:`PipelineConfig` - typed view of the full block (stages, lock
  TTL, per-role concurrency).
* :class:`ClaimLedger` - SQLite-backed distributed claim ledger with
  ``INSERT OR FAIL`` semantics, lease TTL and ``claimer_id`` recovery.
* :func:`make_idempotency_key` - stable
  ``sha256(tracker || ticket_id || role || stage || stage_attempt)``
  key threaded through tracker writes.
* :class:`FailurePayload` /
  :func:`format_failure_comment` - structured failure taxonomy emitted
  as a fenced YAML block inside the comment body, preserving free-text
  prose around it.
* :class:`TrackerPipeline` - the stateless loop: for each tracker,
  apply per-role filters, attempt a distributed claim, dispatch to
  the role, write a structured success/failure comment, transition.
* :class:`PipelineDispatcher` protocol - the role-execution surface
  the pipeline calls. Real callers wire this to the orchestrator's
  spawn machinery; tests inject in-process fakes.

What this module deliberately omits
-----------------------------------
* The tracker adapters themselves (separate per-tracker tickets).
* Webhook ingestion (separate ticket).
* Auto-discovery of pipeline shape from a tracker's existing workflow.

Lifecycle hook
--------------
On every stage transition (success or failure) the pipeline emits the
``tracker_pipeline.handoff`` lifecycle event. Its
:class:`bernstein.core.lifecycle.hooks.LifecycleContext` carries
``tracker``, ``ticket_id``, ``role``, ``from_status``, ``to_status``,
``stage_attempt``, ``outcome`` (``"success"`` or ``"failure"``) and
``idempotency_key``. Operators wire automation (metrics dashboards,
escalation rules) without modifying the pipeline core.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar, Final, Protocol, cast, runtime_checkable

from bernstein.core.lineage.tracker_audit import (
    GENESIS_PREV_HASH,
    _canonical_bytes,
    _exclusive_lock,
)
from bernstein.core.security.audit_head_signature import (
    build_head_signature,
    verify_head_signature,
)

if TYPE_CHECKING:
    from bernstein.core.lifecycle.hooks import HookRegistry
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.lineage_kms import KMSAdapter
    from bernstein.core.trackers.contract import (
        AbstractTrackerAdapter,
        Ticket,
    )


log = logging.getLogger(__name__)


__all__ = [
    "ALLOWED_FAILURE_CATEGORIES",
    "ALLOWED_FAILURE_NEXT_ACTIONS",
    "CLAIM_JOURNAL_SCHEMA_VERSION",
    "CLAIM_RECEIPT_KINDS",
    "DEFAULT_CLAIM_JOURNAL_RELPATH",
    "DEFAULT_CLAIM_LOCK_TTL_SECONDS",
    "DEFAULT_LEDGER_RELPATH",
    "DEFAULT_PER_ROLE_MAX_IN_FLIGHT",
    "FAILURE_BLOCK_BEGIN",
    "FAILURE_BLOCK_END",
    "ClaimFork",
    "ClaimHold",
    "ClaimIngestResult",
    "ClaimJournal",
    "ClaimJournalVerifyResult",
    "ClaimLedger",
    "ClaimOutcome",
    "ClaimReceipt",
    "ClaimState",
    "DispatchOutcome",
    "FailurePayload",
    "PipelineConfig",
    "PipelineDispatcher",
    "PipelineStage",
    "StageHandoff",
    "TrackerPipeline",
    "TrackerPipelineError",
    "compute_claim_entry_hash",
    "default_claim_journal_path",
    "format_failure_comment",
    "format_success_comment",
    "make_idempotency_key",
    "parse_failure_block",
    "parse_success_blocks",
    "project_claims",
]


# ---------------------------------------------------------------------------
# Defaults & constants
# ---------------------------------------------------------------------------


DEFAULT_CLAIM_LOCK_TTL_SECONDS: Final[int] = 600
"""Default lease TTL for an unfinished stage claim.

A crashed worker's claim ages out after this many seconds and another
worker may pick the ticket up. Operators can shorten this for tests or
extend it for slow agents via ``claim_lock_ttl_seconds`` in YAML.
"""

DEFAULT_PER_ROLE_MAX_IN_FLIGHT: Final[int] = 1
"""Default per-role concurrency ceiling enforced by the ledger.

Tickets currently leased to one role count against this ceiling. The
loop skips dispatching new claims while the count is at or above the
ceiling for that role.
"""

DEFAULT_LEDGER_RELPATH: Final[Path] = Path("state") / "tracker_claims.db"
"""Path under ``.sdd/`` where the SQLite ledger lives by default."""

DEFAULT_CLAIM_JOURNAL_RELPATH: Final[Path] = Path("cluster") / "claim_journal.jsonl"
"""Path under ``.sdd/`` where the signed MESH claim journal lives by default.

The journal is opt-in: STAR deployments never materialise it. Only the
leaderless MESH path (issue #2558) constructs a :class:`ClaimJournal` and
threads it into :class:`ClaimLedger`.
"""

CLAIM_JOURNAL_SCHEMA_VERSION: Final[int] = 3
"""On-disk schema version stamped into every :class:`ClaimReceipt`.

Bumping requires a parallel reader for the old version, mirroring the
tracker-audit stream's versioning contract. :func:`_claim_signing_bytes`
provides that parallel reader by *projecting away* fields introduced after a
receipt's own ``schema_version``, so an older receipt's ``entry_hash``
recomputes byte-identically under a newer binary and an append-only journal
written by a previous release keeps verifying.

v2 adds the ``superseded_node_id`` / ``superseded_claimer_id`` reference
fields so a ``supersede`` receipt records the loser's identity as data it
speaks *about* while its own ``node_id`` / ``claimer_id`` name the
reconciling node that signs it (issue #2558).

v3 adds the ``fork`` kind's reference fields (``fork_divergence_index`` /
``fork_entry_hash`` / ``fork_local_head``) and ``target_entry_hash``, the
claim ``entry_hash`` a ``release`` / ``expire`` receipt acts on. Both follow
the same referenced-data rule v2 established: the receipt's own identity
fields always name its signer, and what it speaks about is carried as data.
"""

_CLAIM_FIELDS_BY_SCHEMA: Final[dict[int, frozenset[str]]] = {
    3: frozenset(
        {
            "target_entry_hash",
            "fork_divergence_index",
            "fork_entry_hash",
            "fork_local_head",
        },
    ),
}
"""Receipt fields introduced *at* each schema version, keyed by that version.

Used by :func:`_claim_signing_bytes` to reconstruct the exact body an older
release hashed, so the chain of a journal written before an upgrade still
verifies byte-for-byte. Version 2 and below is the implicit base set.
"""

CLAIM_RECEIPT_KINDS: Final[frozenset[str]] = frozenset(
    {"claim", "release", "renew", "expire", "supersede", "fork"},
)
"""The closed set of :class:`ClaimReceipt` kinds.

Exposed as a module constant so downstream tools can introspect the taxonomy
without reaching into the dataclass internals.

``fork`` is an *observation*, not a claim transition: it records that a
gossiped receipt failed to extend the local head, so the fold ignores it for
holder selection while :meth:`ClaimJournal.verify` and the CLI surface it.
"""

FAILURE_BLOCK_BEGIN: Final[str] = "```yaml bernstein:failure"
"""Opening fence of the structured failure block embedded in comments."""

FAILURE_BLOCK_END: Final[str] = "```"
"""Closing fence of the structured failure block embedded in comments."""

_SUCCESS_BLOCK_BEGIN: Final[str] = "```yaml bernstein:success"

ALLOWED_FAILURE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"transient", "permanent", "policy", "unknown"},
)
"""Allowed values for :class:`FailurePayload.category`.

Exposed as a module constant so downstream tools (linters, schema
generators, integration tests) can introspect the taxonomy without
reaching into the dataclass internals.
"""

ALLOWED_FAILURE_NEXT_ACTIONS: Final[frozenset[str]] = frozenset(
    {"retry", "escalate", "abandon", "manual"},
)
"""Allowed values for :class:`FailurePayload.next_action`."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TrackerPipelineError(Exception):
    """Base class for tracker-pipeline errors."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """One stage in the pipeline.

    Attributes:
        role: Bernstein role name (e.g. ``"architect"``, ``"backend"``,
            ``"qa"``, ``"security"``). Maps to the role prompts under
            ``templates/roles/``.
        claim_status: Tracker status from which this role claims a
            ticket. A ticket in any other status is invisible to this
            stage.
        success_status: Status the ticket transitions to when the role
            completes successfully.
        failure_status: Status the ticket transitions to on a failure
            that the pipeline does not retry in-stage.
        requires_prior_role: Optional role whose successful comment
            must already exist on the ticket before this stage may
            claim. Enforces the ordering of a directed pipeline.
    """

    role: str
    claim_status: str
    success_status: str
    failure_status: str
    requires_prior_role: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> PipelineStage:
        """Build from a parsed YAML mapping; raise on missing required keys."""
        try:
            role = str(raw["role"])
            claim_status = str(raw["claim_status"])
            success_status = str(raw["success_status"])
            failure_status = str(raw["failure_status"])
        except KeyError as exc:
            msg = f"pipeline stage missing required key: {exc.args[0]}"
            raise TrackerPipelineError(msg) from exc
        prior_raw = raw.get("requires_prior_role")
        requires_prior = str(prior_raw) if isinstance(prior_raw, str) and prior_raw else None
        return cls(
            role=role,
            claim_status=claim_status,
            success_status=success_status,
            failure_status=failure_status,
            requires_prior_role=requires_prior,
        )


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Typed view over ``orchestration.tracker_pipeline`` in ``bernstein.yaml``.

    Attributes:
        pipeline_stages: Ordered tuple of :class:`PipelineStage` records.
        claim_lock_ttl_seconds: How long a stage claim survives without
            progress before another worker may steal it.
        per_role_max_in_flight: Maximum number of tickets a single role
            may have leased simultaneously, summed across trackers.
    """

    pipeline_stages: tuple[PipelineStage, ...] = ()
    claim_lock_ttl_seconds: int = DEFAULT_CLAIM_LOCK_TTL_SECONDS
    per_role_max_in_flight: int = DEFAULT_PER_ROLE_MAX_IN_FLIGHT

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> PipelineConfig:
        """Build from a parsed YAML mapping; tolerant of missing keys."""
        stages_raw = raw.get("pipeline_stages", ())
        stages: list[PipelineStage] = []
        if isinstance(stages_raw, Iterable) and not isinstance(stages_raw, (str, bytes)):
            for item in cast(Iterable[object], stages_raw):
                if isinstance(item, Mapping):
                    stages.append(PipelineStage.from_dict(cast(Mapping[str, object], item)))
        ttl_raw = raw.get("claim_lock_ttl_seconds", DEFAULT_CLAIM_LOCK_TTL_SECONDS)
        ttl = int(ttl_raw) if isinstance(ttl_raw, (int, float)) else DEFAULT_CLAIM_LOCK_TTL_SECONDS
        max_in_flight = DEFAULT_PER_ROLE_MAX_IN_FLIGHT
        concurrency_raw = raw.get("concurrency")
        if isinstance(concurrency_raw, Mapping):
            concurrency = cast(Mapping[str, object], concurrency_raw)
            value = concurrency.get("per_role_max_in_flight", DEFAULT_PER_ROLE_MAX_IN_FLIGHT)
            if isinstance(value, (int, float)):
                max_in_flight = max(1, int(value))
        return cls(
            pipeline_stages=tuple(stages),
            claim_lock_ttl_seconds=max(1, ttl),
            per_role_max_in_flight=max_in_flight,
        )

    def stage_for_role(self, role: str) -> PipelineStage | None:
        """Return the stage owning ``role`` or ``None`` if unknown."""
        for stage in self.pipeline_stages:
            if stage.role == role:
                return stage
        return None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def make_idempotency_key(
    *,
    tracker: str,
    ticket_id: str,
    role: str,
    stage: str,
    stage_attempt: int,
) -> str:
    """Return a stable ``sha256`` idempotency key for one stage write.

    The key is the hex digest of ``tracker || ticket_id || role || stage
    || stage_attempt`` joined with ``"\\x1f"`` (ASCII unit separator).
    The separator removes ambiguity when one component contains
    characters that appear in another.

    Args:
        tracker: Tracker adapter name (e.g. ``"github_projects"``).
        ticket_id: Tracker-side ticket id.
        role: Bernstein role processing the ticket.
        stage: Stage label (typically the role name; kept separate so
            multi-stage roles remain addressable).
        stage_attempt: Zero-based attempt count for this stage.

    Returns:
        Hex digest string suitable for ``Idempotency-Key`` headers or
        in-comment fingerprints.
    """
    parts = [tracker, ticket_id, role, stage, str(stage_attempt)]
    joined = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailurePayload:
    """Structured failure taxonomy emitted by a stage.

    Attributes:
        reason_code: Stable machine-readable code, dot-separated
            (e.g. ``"timeout"``, ``"tests.failed"``,
            ``"policy.denied"``).
        category: Coarse bucket (``"transient"``, ``"permanent"``,
            ``"policy"``, ``"unknown"``).
        transient: ``True`` when retrying the same stage is likely to
            succeed; the pipeline may flip the ticket back to the
            claim status for another attempt.
        next_action: One of ``"retry"``, ``"escalate"``,
            ``"abandon"``, ``"manual"``. Drives downstream automation.
        detail: Optional human-readable extra context. Free text but
            kept short.
    """

    reason_code: str
    category: str
    transient: bool
    next_action: str
    detail: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclasses run __post_init__ for validation only.
        if not self.reason_code or not self.reason_code.strip():
            msg = "reason_code must be non-empty"
            raise TrackerPipelineError(msg)
        if self.category not in ALLOWED_FAILURE_CATEGORIES:
            msg = f"category must be one of {sorted(ALLOWED_FAILURE_CATEGORIES)}; got {self.category!r}"
            raise TrackerPipelineError(msg)
        if self.next_action not in ALLOWED_FAILURE_NEXT_ACTIONS:
            msg = f"next_action must be one of {sorted(ALLOWED_FAILURE_NEXT_ACTIONS)}; got {self.next_action!r}"
            raise TrackerPipelineError(msg)


def format_failure_comment(
    *,
    role: str,
    stage_attempt: int,
    idempotency_key: str,
    payload: FailurePayload,
    prose: str = "",
) -> str:
    """Return the comment body that wraps ``payload`` in a fenced block.

    The free-text ``prose`` (if any) renders above the fenced YAML so
    humans see the narrative first. The fence is the contract for
    downstream automation; parsers should anchor on
    :data:`FAILURE_BLOCK_BEGIN` and :data:`FAILURE_BLOCK_END`.
    """
    detail_line = ""
    if payload.detail:
        # Single-line for YAML safety. Multiline detail belongs in prose.
        safe_detail = payload.detail.replace("\n", " ").strip()
        detail_line = f"\ndetail: {_yaml_quote(safe_detail)}"
    body_lines = [
        FAILURE_BLOCK_BEGIN,
        f"role: {_yaml_quote(role)}",
        f"stage_attempt: {stage_attempt}",
        f"idempotency_key: {_yaml_quote(idempotency_key)}",
        f"reason_code: {_yaml_quote(payload.reason_code)}",
        f"category: {_yaml_quote(payload.category)}",
        f"transient: {'true' if payload.transient else 'false'}",
        f"next_action: {_yaml_quote(payload.next_action)}{detail_line}",
        FAILURE_BLOCK_END,
    ]
    block = "\n".join(body_lines)
    if prose:
        return f"{prose.strip()}\n\n{block}"
    return block


def format_success_comment(
    *,
    role: str,
    stage_attempt: int,
    idempotency_key: str,
    summary: str,
    prose: str = "",
) -> str:
    """Return the success-side counterpart of :func:`format_failure_comment`.

    Symmetric structured block lets downstream automation recognise a
    successful handoff without re-parsing free text.
    """
    safe_summary = summary.replace("\n", " ").strip()
    body_lines = [
        _SUCCESS_BLOCK_BEGIN,
        f"role: {_yaml_quote(role)}",
        f"stage_attempt: {stage_attempt}",
        f"idempotency_key: {_yaml_quote(idempotency_key)}",
        f"summary: {_yaml_quote(safe_summary)}",
        FAILURE_BLOCK_END,
    ]
    block = "\n".join(body_lines)
    if prose:
        return f"{prose.strip()}\n\n{block}"
    return block


def parse_failure_block(comment_body: str) -> dict[str, Any] | None:
    """Return the parsed failure block found in ``comment_body``, if any.

    The function is permissive: it tokenises the block as
    ``key: value`` lines without pulling in a full YAML parser. Values
    are stripped of surrounding double quotes; ``true``/``false``
    become Python booleans; integer-shaped tokens become ``int``.

    Returns:
        Parsed mapping with keys like ``reason_code``, ``category``,
        ``transient``, ``next_action``, ``detail``, plus the meta keys
        ``role``, ``stage_attempt``, ``idempotency_key``. ``None`` when
        the block is missing.
    """
    blocks = _iter_fenced_blocks(comment_body, FAILURE_BLOCK_BEGIN)
    for parsed in blocks:
        return parsed
    return None


def parse_success_blocks(comment_body: str) -> list[dict[str, Any]]:
    """Return every parsed ``bernstein:success`` block in ``comment_body``.

    Used by :class:`TrackerPipeline._stage_is_eligible` to check the
    prior-role gate via structured fields rather than raw string match,
    so cosmetic formatting changes around the fence do not silently
    break the pipeline ordering contract.
    """
    return list(_iter_fenced_blocks(comment_body, _SUCCESS_BLOCK_BEGIN))


def _iter_fenced_blocks(comment_body: str, begin_marker: str) -> Iterable[dict[str, Any]]:
    """Yield every ``begin_marker`` ... ``FAILURE_BLOCK_END`` block as a dict.

    The closing fence is the same backtick triplet used by both success
    and failure blocks. Empty blocks and blocks missing a closing fence
    are skipped.
    """
    start = 0
    while True:
        index = comment_body.find(begin_marker, start)
        if index < 0:
            return
        after_start = index + len(begin_marker)
        end = comment_body.find(FAILURE_BLOCK_END, after_start)
        if end < 0:
            return
        inner = comment_body[after_start:end].strip()
        start = end + len(FAILURE_BLOCK_END)
        if not inner:
            continue
        parsed: dict[str, Any] = {}
        for raw_line in inner.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            parsed[key.strip()] = _decode_yaml_value(value.strip())
        if parsed:
            yield parsed


def _yaml_quote(value: str) -> str:
    """Return ``value`` rendered as a double-quoted YAML scalar."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _decode_yaml_value(token: str) -> Any:
    """Decode a single ``key: value`` right-hand side."""
    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
        body = token[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if token == "true":
        return True
    if token == "false":
        return False
    if token.lstrip("-").isdigit():
        try:
            return int(token)
        except ValueError:
            return token
    return token


# ---------------------------------------------------------------------------
# Claim ledger (SQLite-backed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """Result of a single claim attempt.

    Attributes:
        granted: ``True`` when this caller now owns the claim.
        reason: Short reason code when ``granted`` is ``False``. One of
            ``"held"``, ``"prior_role_missing"``,
            ``"concurrency_ceiling"``, ``"ledger_error"``.
        claimer_id: ``claimer_id`` of the winning caller. When the
            current call won, equals the caller's id; otherwise the id
            that already holds the lease.
        lease_expires_at: Unix timestamp the claim expires. Zero when
            no claim is held.
    """

    granted: bool
    reason: str
    claimer_id: str
    lease_expires_at: float


class ClaimLedger:
    """SQLite-backed distributed claim ledger.

    The ledger keys claims by ``(tracker, ticket_id, role)`` and uses
    ``INSERT OR FAIL`` semantics so two agents racing for the same
    ticket+role on the same tick produce exactly one INSERT success and
    one INSERT failure. The losing caller is told the holder's
    ``claimer_id`` so retries can short-circuit cleanly.

    Lease TTL handles the crashed-worker case: when a claim's
    ``lease_expires_at`` is in the past, the next caller's
    :meth:`try_claim` re-acquires it.

    The implementation pins ``check_same_thread=False`` and serialises
    writes via a per-database process-local lock; the underlying
    SQLite connection is opened lazily so test code may instantiate
    many ledgers without paying file-system cost up front.
    """

    _SCHEMA: Final[str] = """
        CREATE TABLE IF NOT EXISTS claims (
            tracker TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            role TEXT NOT NULL,
            claimer_id TEXT NOT NULL,
            lease_expires_at REAL NOT NULL,
            stage_attempt INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            PRIMARY KEY (tracker, ticket_id, role)
        )
    """
    _locks: ClassVar[dict[str, threading.RLock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, db_path: Path, *, journal: ClaimJournal | None = None) -> None:
        """Build a ledger.

        Args:
            db_path: SQLite file the projection is materialised into.
            journal: Optional signed :class:`ClaimJournal`. When supplied the
                ledger runs the leaderless MESH path: every granted claim is
                first appended as a signed receipt and the SQLite row is
                materialised from it, so the ledger becomes a projection of the
                journal rather than the source of truth. When ``None`` (the
                default) the ledger behaves exactly as the STAR path always
                has, touching no journal -- existing callers are unaffected.
        """
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = self._lock_for_path(db_path)
        self._journal = journal

    @property
    def db_path(self) -> Path:
        """Filesystem path the ledger persists at."""
        return self._db_path

    @property
    def journal(self) -> ClaimJournal | None:
        """The signed claim journal on the MESH path, or ``None`` for STAR."""
        return self._journal

    @classmethod
    def _lock_for_path(cls, db_path: Path) -> threading.RLock:
        key = str(db_path.expanduser().resolve(strict=False))
        with cls._locks_guard:
            lock = cls._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._locks[key] = lock
            return lock

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,  # autocommit; we use explicit BEGIN IMMEDIATE
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_role ON claims(role)",
            )
            self._conn = conn
            return conn

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------
    # Claim lifecycle
    # ------------------------------------------------------------------

    def try_claim(
        self,
        *,
        tracker: str,
        ticket_id: str,
        role: str,
        claimer_id: str,
        ttl_seconds: int,
        per_role_max_in_flight: int,
        now: float | None = None,
    ) -> ClaimOutcome:
        """Attempt to claim ``(tracker, ticket_id, role)`` for ``claimer_id``.

        The method is atomic relative to other callers using the same
        ledger file. Concurrency-ceiling enforcement happens inside the
        same transaction so two callers cannot simultaneously push a
        role over its ceiling.

        Args:
            tracker: Tracker adapter name.
            ticket_id: Tracker-side ticket id.
            role: Bernstein role name.
            claimer_id: Unique caller identifier (typically a worker
                process id + UUID). Used for ownership and recovery.
            ttl_seconds: Lease duration; the claim expires at
                ``now + ttl_seconds``.
            per_role_max_in_flight: Maximum simultaneous live claims
                this role may hold. Pass an integer >= 1.
            now: Optional clock override; defaults to ``time.time()``.

        Returns:
            :class:`ClaimOutcome` describing whether the claim was
            granted and, on failure, why.
        """
        current = float(time.time() if now is None else now)
        expires_at = current + max(1, ttl_seconds)
        # Populated on the MESH path; the SQLite row below is materialised from
        # this receipt so the ledger stays a projection of the journal.
        receipt: ClaimReceipt | None = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Concurrency-ceiling check: count non-expired claims for the role.
                row = conn.execute(
                    "SELECT COUNT(*) FROM claims WHERE role = ? AND lease_expires_at > ?",
                    (role, current),
                ).fetchone()
                in_flight = int(row[0]) if row else 0
                # Drop expired rows so the next INSERT can succeed.
                conn.execute(
                    "DELETE FROM claims WHERE tracker = ? AND ticket_id = ? AND role = ? AND lease_expires_at <= ?",
                    (tracker, ticket_id, role, current),
                )
                existing = conn.execute(
                    "SELECT claimer_id, lease_expires_at FROM claims WHERE tracker = ? AND ticket_id = ? AND role = ?",
                    (tracker, ticket_id, role),
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    return ClaimOutcome(
                        granted=False,
                        reason="held",
                        claimer_id=str(existing[0]),
                        lease_expires_at=float(existing[1]),
                    )
                if per_role_max_in_flight >= 1 and in_flight >= per_role_max_in_flight:
                    conn.execute("ROLLBACK")
                    return ClaimOutcome(
                        granted=False,
                        reason="concurrency_ceiling",
                        claimer_id="",
                        lease_expires_at=0.0,
                    )
                # MESH path: the journal is the source of truth. Mint the signed
                # claim receipt FIRST, still inside the open transaction, then
                # materialise the SQLite row from that receipt and commit both
                # together. If the append fails (disk / signing), roll the
                # transaction back so no receipt-less row is ever committed --
                # the projection derives claim state from receipts only, so a
                # held row with no receipt would be a phantom holder. STAR
                # deployments pass no journal and insert from the raw inputs.
                if self._journal is not None:
                    try:
                        receipt = self._journal.append(
                            kind="claim",
                            tracker=tracker,
                            ticket_id=ticket_id,
                            role=role,
                            claimer_id=claimer_id,
                            lease_expires_at=expires_at,
                            ts_ns=int(current * 1_000_000_000),
                        )
                    except BaseException:
                        with contextlib.suppress(sqlite3.Error):
                            conn.execute("ROLLBACK")
                        raise
                insert_values = (
                    (tracker, ticket_id, role, claimer_id, expires_at, current)
                    if receipt is None
                    else (
                        receipt.tracker,
                        receipt.ticket_id,
                        receipt.role,
                        receipt.claimer_id,
                        receipt.lease_expires_at,
                        current,
                    )
                )
                try:
                    conn.execute(
                        "INSERT OR FAIL INTO claims "
                        "(tracker, ticket_id, role, claimer_id, lease_expires_at, stage_attempt, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 0, ?)",
                        insert_values,
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    row2 = conn.execute(
                        "SELECT claimer_id, lease_expires_at FROM claims "
                        "WHERE tracker = ? AND ticket_id = ? AND role = ?",
                        (tracker, ticket_id, role),
                    ).fetchone()
                    if row2 is None:
                        return ClaimOutcome(
                            granted=False,
                            reason="ledger_error",
                            claimer_id="",
                            lease_expires_at=0.0,
                        )
                    return ClaimOutcome(
                        granted=False,
                        reason="held",
                        claimer_id=str(row2[0]),
                        lease_expires_at=float(row2[1]),
                    )
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                log.exception("tracker_pipeline: ledger transaction failed")
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                return ClaimOutcome(
                    granted=False,
                    reason="ledger_error",
                    claimer_id="",
                    lease_expires_at=0.0,
                )
        granted_lease = expires_at if receipt is None else receipt.lease_expires_at
        return ClaimOutcome(
            granted=True,
            reason="granted",
            claimer_id=claimer_id,
            lease_expires_at=granted_lease,
        )

    def live_claims(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Return live (non-expired) claims as ordered dicts.

        Used by ``bernstein pipeline status`` and tests to render the
        live in-flight view without re-implementing the schema or
        opening a separate sqlite connection. ``now`` is overridable so
        callers can render a deterministic snapshot.
        """
        current = float(time.time() if now is None else now)
        with self._lock:
            cursor = self._connect().execute(
                "SELECT tracker, ticket_id, role, claimer_id, lease_expires_at, "
                "stage_attempt FROM claims WHERE lease_expires_at > ? "
                "ORDER BY tracker, role, ticket_id",
                (current,),
            )
            rows: list[dict[str, Any]] = [
                {
                    "tracker": tracker,
                    "ticket_id": ticket_id,
                    "role": role,
                    "claimer_id": claimer_id,
                    "stage_attempt": int(attempt),
                    "lease_seconds_remaining": float(expires) - current,
                }
                for tracker, ticket_id, role, claimer_id, expires, attempt in cursor.fetchall()
            ]
            return rows

    def release(self, *, tracker: str, ticket_id: str, role: str, claimer_id: str) -> bool:
        """Drop the claim if ``claimer_id`` still owns it.

        Returns ``True`` when a row was removed.
        """
        with self._lock:
            cursor = self._connect().execute(
                "DELETE FROM claims WHERE tracker = ? AND ticket_id = ? AND role = ? AND claimer_id = ?",
                (tracker, ticket_id, role, claimer_id),
            )
            return bool(cursor.rowcount)

    def attempt_count(self, *, tracker: str, ticket_id: str, role: str) -> int:
        """Return ``stage_attempt`` for the live or last claim row, or ``0``."""
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT stage_attempt FROM claims WHERE tracker = ? AND ticket_id = ? AND role = ?",
                    (tracker, ticket_id, role),
                )
                .fetchone()
            )
            if row is None:
                return 0
            return int(row[0])

    def bump_attempt(self, *, tracker: str, ticket_id: str, role: str, claimer_id: str) -> int:
        """Increment and return ``stage_attempt`` for the held claim.

        Returns ``-1`` when no live claim exists for ``claimer_id``.
        """
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT stage_attempt FROM claims "
                    "WHERE tracker = ? AND ticket_id = ? AND role = ? AND claimer_id = ?",
                    (tracker, ticket_id, role, claimer_id),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return -1
                attempt = int(row[0]) + 1
                conn.execute(
                    "UPDATE claims SET stage_attempt = ? "
                    "WHERE tracker = ? AND ticket_id = ? AND role = ? AND claimer_id = ?",
                    (attempt, tracker, ticket_id, role, claimer_id),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            return attempt


# ---------------------------------------------------------------------------
# Claim journal (signed, Merkle-chained) -- leaderless MESH substrate (#2558)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    """One signed, hash-chained entry in a leaderless MESH claim journal.

    A receipt records a single self-claim, release, renewal, expiry, or
    supersession against ``(tracker, ticket_id, role)``. Receipts chain via
    ``prev_entry_hash`` / ``entry_hash`` (reusing the tracker-audit
    canonicalisation) and are Ed25519-signed with the node's install identity
    through the same head-signature path the A2A message receipts use.

    The ``entry_hash`` is computed with the ``signature`` and ``entry_hash``
    fields blanked, so it is *signature-independent*: two nodes signing the
    same body with different install keys agree on the entry hash and therefore
    on the journal head. That is what lets the pure fold produce byte-identical
    state across nodes.

    The core binding is ``{tracker, ticket_id, role, claimer_id, node_id,
    lease_expires_at, prev_entry_hash, entry_hash}``. The ``claimer_id`` /
    ``node_id`` fields are the receipt's own identity -- *who the receipt is
    from* -- and they always name the node whose Ed25519 install key signs it.
    A ``supersede`` receipt is a statement *by* the reconciling node *about* a
    losing claim, so it is attributed to the reconciler: its ``claimer_id`` /
    ``node_id`` name the reconciler, and the loser is carried as *referenced
    data* -- the losing claim's ``entry_hash`` (``supersedes``) plus the
    loser's identity (``superseded_node_id`` / ``superseded_claimer_id``) --
    alongside the winner (``winner_claimer_id`` / ``winner_entry_hash``). This
    keeps the signature and the declared identity in agreement: a verifier that
    pins each node's public key by ``node_id`` finds every receipt signed by the
    node it claims to be from.
    """

    schema_version: int
    kind: str
    ts_ns: int
    tracker: str
    ticket_id: str
    role: str
    claimer_id: str
    node_id: str
    lease_expires_at: float
    prev_entry_hash: str
    entry_hash: str
    signature: dict[str, Any] = field(default_factory=dict)
    supersedes: str | None = None
    winner_claimer_id: str | None = None
    winner_entry_hash: str | None = None
    superseded_node_id: str | None = None
    superseded_claimer_id: str | None = None
    target_entry_hash: str | None = None
    fork_divergence_index: int | None = None
    fork_entry_hash: str | None = None
    fork_local_head: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CLAIM_RECEIPT_KINDS:
            msg = f"unknown claim-receipt kind: {self.kind!r}"
            raise ValueError(msg)
        for hash_field, label in (
            (self.prev_entry_hash, "prev_entry_hash"),
            (self.entry_hash, "entry_hash"),
        ):
            if not hash_field.startswith("sha256:"):
                msg = f"{label} must start with 'sha256:', got {hash_field!r}"
                raise ValueError(msg)

    @property
    def key(self) -> tuple[str, str, str]:
        """The ``(tracker, ticket_id, role)`` the receipt claims against."""
        return (self.tracker, self.ticket_id, self.role)

    def to_dict(self) -> dict[str, Any]:
        """Return the plain-dict wire form (the on-disk JSONL body)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ClaimReceipt:
        """Rebuild a receipt from a parsed JSON object.

        Validates the receipt shape through the dataclass ``__post_init__``
        invariants on construction.
        """
        supersedes = data.get("supersedes")
        winner_claimer_id = data.get("winner_claimer_id")
        winner_entry_hash = data.get("winner_entry_hash")
        superseded_node_id = data.get("superseded_node_id")
        superseded_claimer_id = data.get("superseded_claimer_id")
        target_entry_hash = data.get("target_entry_hash")
        fork_divergence_index = data.get("fork_divergence_index")
        fork_entry_hash = data.get("fork_entry_hash")
        fork_local_head = data.get("fork_local_head")
        return cls(
            schema_version=int(data["schema_version"]),
            kind=str(data["kind"]),
            ts_ns=int(data["ts_ns"]),
            tracker=str(data["tracker"]),
            ticket_id=str(data["ticket_id"]),
            role=str(data["role"]),
            claimer_id=str(data["claimer_id"]),
            node_id=str(data["node_id"]),
            lease_expires_at=float(data["lease_expires_at"]),
            prev_entry_hash=str(data["prev_entry_hash"]),
            entry_hash=str(data["entry_hash"]),
            signature=dict(data.get("signature") or {}),
            supersedes=None if supersedes is None else str(supersedes),
            winner_claimer_id=None if winner_claimer_id is None else str(winner_claimer_id),
            winner_entry_hash=None if winner_entry_hash is None else str(winner_entry_hash),
            superseded_node_id=None if superseded_node_id is None else str(superseded_node_id),
            superseded_claimer_id=(None if superseded_claimer_id is None else str(superseded_claimer_id)),
            target_entry_hash=None if target_entry_hash is None else str(target_entry_hash),
            fork_divergence_index=(None if fork_divergence_index is None else int(fork_divergence_index)),
            fork_entry_hash=None if fork_entry_hash is None else str(fork_entry_hash),
            fork_local_head=None if fork_local_head is None else str(fork_local_head),
        )


def _claim_signing_bytes(receipt: ClaimReceipt) -> bytes:
    """Return the JCS bytes the entry hash and signature run over.

    Mirrors :func:`bernstein.core.lineage.tracker_audit._signing_payload`: the
    ``signature`` and ``entry_hash`` fields are blanked so the digest is
    reproducible from the same body during replay or verification, and so the
    chain hash never depends on which node signed the receipt.

    Fields introduced *after* the receipt's own ``schema_version`` are dropped
    before canonicalisation, so a receipt written by an older release hashes
    over exactly the body that release hashed. Without that projection an
    upgrade would silently invalidate every prior entry in an append-only
    journal -- indistinguishable, to :meth:`ClaimJournal.verify`, from tamper.
    """
    body = asdict(receipt)
    body["signature"] = {}
    body["entry_hash"] = ""
    version = int(receipt.schema_version)
    for introduced_at, names in _CLAIM_FIELDS_BY_SCHEMA.items():
        if version < introduced_at:
            for name in names:
                body.pop(name, None)
    return _canonical_bytes(body)


def compute_claim_entry_hash(receipt: ClaimReceipt) -> str:
    """Return the content-addressed ``entry_hash`` for ``receipt``."""
    return "sha256:" + hashlib.sha256(_claim_signing_bytes(receipt)).hexdigest()


def _tail_hash_from_handle(fp: IO[bytes]) -> str:
    """Return the last receipt's ``entry_hash`` from an open journal handle.

    Reads from the start of ``fp`` so a caller already holding the exclusive
    append lock can resolve the current chain tail without opening a second
    descriptor. Keeping the tail read on the *locked* handle is what makes the
    read-modify-write of ``prev_entry_hash`` a single atomic critical section.
    """
    fp.seek(0)
    last_hash = GENESIS_PREV_HASH
    for raw in fp:
        stripped = raw.strip()
        if not stripped:
            continue
        last_hash = json.loads(stripped.decode("utf-8"))["entry_hash"]
    return last_hash


def _entry_hashes_from_handle(fp: IO[bytes]) -> list[str]:
    """Return every ``entry_hash`` in order from an open journal handle.

    Like :func:`_tail_hash_from_handle`, reads from the start of ``fp`` so a
    caller already holding the exclusive append lock resolves the whole chain
    without opening a second descriptor.
    """
    fp.seek(0)
    hashes: list[str] = []
    for raw in fp:
        stripped = raw.strip()
        if not stripped:
            continue
        hashes.append(str(json.loads(stripped.decode("utf-8"))["entry_hash"]))
    return hashes


@dataclass(frozen=True, slots=True)
class ClaimHold:
    """The current holder of one ``(tracker, ticket_id, role)`` claim."""

    tracker: str
    ticket_id: str
    role: str
    claimer_id: str
    node_id: str
    lease_expires_at: float
    entry_hash: str

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical dict form used by :meth:`ClaimState.canonical_bytes`."""
        return {
            "tracker": self.tracker,
            "ticket_id": self.ticket_id,
            "role": self.role,
            "claimer_id": self.claimer_id,
            "node_id": self.node_id,
            "lease_expires_at": self.lease_expires_at,
            "entry_hash": self.entry_hash,
        }


@dataclass(frozen=True, slots=True)
class ClaimFork:
    """One recorded divergence between a gossiped chain and the local one.

    Projected from a ``fork`` receipt. ``divergence_index`` is the number of
    leading local entries the rejected receipt's chain provably shares: entries
    from that index onward are divergent. ``entry_hash`` is the rejected
    receipt, ``local_head`` the local head at the moment it was rejected, and
    ``observed_by`` the node that signed the observation.
    """

    divergence_index: int
    entry_hash: str
    local_head: str
    observed_by: str
    fork_receipt_hash: str

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical dict form used by :meth:`ClaimState.canonical_bytes`."""
        return {
            "divergence_index": self.divergence_index,
            "entry_hash": self.entry_hash,
            "local_head": self.local_head,
            "observed_by": self.observed_by,
            "fork_receipt_hash": self.fork_receipt_hash,
        }


@dataclass(frozen=True, slots=True)
class ClaimState:
    """The projected claim state -- the pure fold of an ordered receipt set.

    ``holds`` maps each claimed key to its single deterministic holder;
    ``superseded`` is the set of every claim ``entry_hash`` that lost (whether
    by an explicit ``supersede`` receipt or by the lowest-``entry_hash`` rule);
    ``forks`` is every divergence a ``fork`` receipt recorded; ``head`` is the
    journal head hash (the ``entry_hash`` of the last receipt).

    Two nodes folding the same ordered receipt set must produce a
    byte-identical :meth:`canonical_bytes` and an identical ``head``.
    """

    holds: Mapping[tuple[str, str, str], ClaimHold]
    superseded: frozenset[str]
    head: str
    forks: tuple[ClaimFork, ...] = ()

    def holder(self, tracker: str, ticket_id: str, role: str) -> ClaimHold | None:
        """Return the current holder of the key, or ``None`` if unheld."""
        return self.holds.get((tracker, ticket_id, role))

    def canonical_bytes(self) -> bytes:
        """Return the deterministic JCS serialisation for byte-comparison."""
        payload = {
            "forks": [fork.as_dict() for fork in self.forks],
            "head": self.head,
            "holds": [self.holds[k].as_dict() for k in sorted(self.holds)],
            "superseded": sorted(self.superseded),
        }
        return _canonical_bytes(payload)


def _active_buckets(
    receipts: Sequence[ClaimReceipt],
) -> tuple[dict[tuple[str, str, str], dict[str, ClaimHold]], set[str]]:
    """Fold ``receipts`` into per-key live-claim buckets.

    Returns ``(active, explicitly_superseded)`` where ``active[key]`` maps each
    still-live claim's ``entry_hash`` to its :class:`ClaimHold`, and
    ``explicitly_superseded`` is the set of entry hashes removed by an explicit
    ``supersede`` receipt. Ordering follows the receipt sequence; the
    lowest-``entry_hash`` winner selection is applied by the callers.
    """
    active: dict[tuple[str, str, str], dict[str, ClaimHold]] = {}
    explicit_superseded: set[str] = set()
    for receipt in receipts:
        bucket = active.setdefault(receipt.key, {})
        if receipt.kind == "claim":
            bucket[receipt.entry_hash] = ClaimHold(
                tracker=receipt.tracker,
                ticket_id=receipt.ticket_id,
                role=receipt.role,
                claimer_id=receipt.claimer_id,
                node_id=receipt.node_id,
                lease_expires_at=receipt.lease_expires_at,
                entry_hash=receipt.entry_hash,
            )
        elif receipt.kind == "renew":
            for entry_hash, hold in list(bucket.items()):
                if hold.claimer_id == receipt.claimer_id and hold.node_id == receipt.node_id:
                    bucket[entry_hash] = replace(hold, lease_expires_at=receipt.lease_expires_at)
        elif receipt.kind in ("release", "expire"):
            # A receipt naming ``target_entry_hash`` acts on exactly that claim
            # (schema v3). That is what lets a node retire a *peer's* expired
            # lease without impersonating it: the receipt's own identity stays
            # its signer's, and the hold it retires is referenced data. Older
            # receipts carry no target, so they fall back to the v2 rule --
            # retire every hold whose identity matches the receipt's own.
            if receipt.target_entry_hash is not None:
                bucket.pop(receipt.target_entry_hash, None)
            else:
                for entry_hash, hold in list(bucket.items()):
                    if hold.claimer_id == receipt.claimer_id and hold.node_id == receipt.node_id:
                        del bucket[entry_hash]
        elif receipt.kind == "supersede" and receipt.supersedes is not None:
            bucket.pop(receipt.supersedes, None)
            explicit_superseded.add(receipt.supersedes)
    return active, explicit_superseded


def project_claims(receipts: Sequence[ClaimReceipt]) -> ClaimState:
    """Fold an ordered receipt set into a deterministic :class:`ClaimState`.

    Pure and total: given the same ordered receipts, two independently
    instantiated nodes produce a byte-identical state and an identical head
    hash. When more than one live claim contends for a key, the claim with the
    lexicographically-lowest ``entry_hash`` wins and the rest are superseded --
    a total order that does not depend on wall-clock, node identity, or which
    node observed which claim first.
    """
    active, superseded = _active_buckets(receipts)
    superseded = set(superseded)
    holds: dict[tuple[str, str, str], ClaimHold] = {}
    for key, bucket in active.items():
        if not bucket:
            continue
        winner_entry_hash = min(bucket)
        holds[key] = bucket[winner_entry_hash]
        for entry_hash in bucket:
            if entry_hash != winner_entry_hash:
                superseded.add(entry_hash)
    head = receipts[-1].entry_hash if receipts else GENESIS_PREV_HASH
    return ClaimState(
        holds=holds,
        superseded=frozenset(superseded),
        head=head,
        forks=_project_forks(receipts),
    )


def _project_forks(receipts: Sequence[ClaimReceipt]) -> tuple[ClaimFork, ...]:
    """Return every divergence recorded by a ``fork`` receipt, in journal order.

    Kept ordered by position rather than sorted so the sequence reads as the
    order the node observed the divergences; two nodes folding the same ordered
    receipts still agree byte-for-byte because the input order is the same.
    """
    forks_seen: list[ClaimFork] = []
    for receipt in receipts:
        if receipt.kind != "fork" or receipt.fork_entry_hash is None:
            continue
        forks_seen.append(
            ClaimFork(
                divergence_index=int(receipt.fork_divergence_index or 0),
                entry_hash=receipt.fork_entry_hash,
                local_head=receipt.fork_local_head or GENESIS_PREV_HASH,
                observed_by=receipt.node_id,
                fork_receipt_hash=receipt.entry_hash,
            ),
        )
    return tuple(forks_seen)


def _divergence_index(local_hashes: Sequence[str], foreign_prev_hash: str) -> int:
    """Return how many leading local entries a rejected receipt provably shares.

    When ``foreign_prev_hash`` names local entry ``i``, the two chains agree
    through ``i`` and disagree at ``i + 1`` -- that is the divergence index.
    When it is genesis, or names an entry the local chain has never seen, the
    chains share no verifiable prefix and the index is ``0``. The two cases are
    told apart by the ``fork_local_head`` the fork receipt records alongside.
    """
    if foreign_prev_hash == GENESIS_PREV_HASH:
        return 0
    for index, entry_hash in enumerate(local_hashes):
        if entry_hash == foreign_prev_hash:
            return index + 1
    return 0


@dataclass(frozen=True, slots=True)
class ClaimJournalVerifyResult:
    """Outcome of :meth:`ClaimJournal.verify`.

    ``ok`` covers *integrity*: every chain link, every recomputed entry hash,
    every Ed25519 node signature, and -- when an audit chain is supplied --
    every audit-chain anchor. ``forks`` is reported separately because a fork
    is a correctly-signed, correctly-chained record of a divergence: the local
    journal is intact, but coordination history is not single-threaded. A
    caller that treats a journal as authoritative must check both.
    """

    ok: bool
    entry_count: int
    bad_index: int | None = None
    failures: list[str] = field(default_factory=list)
    head: str = GENESIS_PREV_HASH
    forks: tuple[ClaimFork, ...] = ()
    anchors_checked: bool = False

    @property
    def clean(self) -> bool:
        """``True`` when integrity holds *and* no fork was recorded."""
        return self.ok and not self.forks


@dataclass(frozen=True, slots=True)
class ClaimIngestResult:
    """Outcome of :meth:`ClaimJournal.ingest` for one gossiped receipt.

    ``status`` is one of ``"applied"`` (the receipt extended the local head and
    was folded), ``"duplicate"`` (already present -- gossip is idempotent),
    ``"forked"`` (it did not extend the local head; a signed ``fork`` receipt
    was appended and the foreign receipt was *not* merged), or ``"rejected"``
    (the signature or the recomputed entry hash failed, so nothing was written).
    """

    status: str
    head: str
    reason: str | None = None
    fork_receipt: ClaimReceipt | None = None
    divergence_index: int | None = None


class ClaimJournal:
    """Signed, append-only, Merkle-chained journal of claim receipts.

    The journal is the source of truth for leaderless MESH coordination: the
    SQLite :class:`ClaimLedger` is a projection of it. Each append computes the
    chain link, signs the entry hash with the node's Ed25519 install identity,
    writes the JSONL body under an exclusive ``flock`` (so multiple nodes on a
    shared filesystem never interleave bytes), and -- when an audit chain is
    supplied -- anchors the receipt's ``entry_hash`` into the HMAC audit chain
    as a ``cluster.claim_journal_receipt`` event.

    Args:
        path: JSONL file the receipts are appended to.
        kms_adapter: The node's Ed25519 signer (its install identity).
        node_id: The node install identity id recorded on every receipt.
        chain: Optional :class:`AuditChainStore`; when supplied every receipt
            is anchored into the HMAC chain.
    """

    def __init__(
        self,
        path: Path,
        *,
        kms_adapter: KMSAdapter,
        node_id: str,
        chain: AuditChainStore | None = None,
    ) -> None:
        self.path: Path = Path(path)
        self._kms = kms_adapter
        self._node_id = node_id
        self._chain = chain
        self._lock = threading.RLock()

    @property
    def node_id(self) -> str:
        """The node install identity id recorded on receipts this journal mints."""
        return self._node_id

    # -- append -------------------------------------------------------

    def append(
        self,
        *,
        kind: str,
        tracker: str,
        ticket_id: str,
        role: str,
        claimer_id: str,
        lease_expires_at: float,
        ts_ns: int,
        node_id: str | None = None,
        supersedes: str | None = None,
        winner_claimer_id: str | None = None,
        winner_entry_hash: str | None = None,
        superseded_node_id: str | None = None,
        superseded_claimer_id: str | None = None,
        target_entry_hash: str | None = None,
        fork_divergence_index: int | None = None,
        fork_entry_hash: str | None = None,
        fork_local_head: str | None = None,
    ) -> ClaimReceipt:
        """Append one signed receipt and return the materialised entry.

        ``ts_ns`` is an explicit argument rather than an ambient clock read, so
        a replay with the same inputs reproduces a byte-identical receipt
        (including its signature -- Ed25519 is deterministic per RFC 8032).
        """
        if kind not in CLAIM_RECEIPT_KINDS:
            msg = f"unknown claim-receipt kind: {kind!r}"
            raise ValueError(msg)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The tail read and the byte-append are one critical section under
            # a single exclusive advisory lock. Reading ``prev_entry_hash``
            # before taking the lock would let two nodes on a shared filesystem
            # both observe the same tail and mint receipts that both link to it,
            # forking the linear chain -- a fork offline ``verify()`` cannot
            # tell apart from tampering. Holding the lock across the read, the
            # (deterministic, in-memory) Ed25519 signing, and the write keeps
            # every cross-process append strictly linear. ``a+b`` opens for read
            # and append; on POSIX an append write always lands at EOF
            # regardless of the read cursor.
            with self.path.open("a+b") as fp, _exclusive_lock(fp):
                prev_hash = _tail_hash_from_handle(fp)
                unsigned = ClaimReceipt(
                    schema_version=CLAIM_JOURNAL_SCHEMA_VERSION,
                    kind=kind,
                    ts_ns=int(ts_ns),
                    tracker=tracker,
                    ticket_id=ticket_id,
                    role=role,
                    claimer_id=claimer_id,
                    node_id=self._node_id if node_id is None else node_id,
                    lease_expires_at=float(lease_expires_at),
                    prev_entry_hash=prev_hash,
                    entry_hash=GENESIS_PREV_HASH,  # placeholder; recomputed below
                    signature={},
                    supersedes=supersedes,
                    winner_claimer_id=winner_claimer_id,
                    winner_entry_hash=winner_entry_hash,
                    superseded_node_id=superseded_node_id,
                    superseded_claimer_id=superseded_claimer_id,
                    target_entry_hash=target_entry_hash,
                    fork_divergence_index=fork_divergence_index,
                    fork_entry_hash=fork_entry_hash,
                    fork_local_head=fork_local_head,
                )
                digest = compute_claim_entry_hash(unsigned)
                signed = replace(unsigned, entry_hash=digest)
                signature = build_head_signature(digest.split(":", 1)[1], kms_adapter=self._kms)
                final = replace(signed, signature=signature)

                line = _canonical_bytes(asdict(final)) + b"\n"
                fp.seek(0, os.SEEK_END)
                fp.write(line)
                fp.flush()
                os.fsync(fp.fileno())
            self._anchor(final)
            return final

    def _anchor(self, receipt: ClaimReceipt) -> None:
        """Mirror ``receipt`` into the HMAC audit chain when one is configured."""
        if self._chain is None:
            return
        # Imported lazily so the (large) audit_chain module is not pulled in at
        # tracker_pipeline import time on the STAR path that never anchors.
        from bernstein.core.security.audit_chain import record_claim_journal_receipt

        record_claim_journal_receipt(
            chain=self._chain,
            kind=receipt.kind,
            tracker=receipt.tracker,
            ticket_id=receipt.ticket_id,
            role=receipt.role,
            claimer_id=receipt.claimer_id,
            node_id=receipt.node_id,
            lease_expires_at=receipt.lease_expires_at,
            prev_entry_hash=receipt.prev_entry_hash,
            journal_entry_hash=receipt.entry_hash,
            supersedes=receipt.supersedes,
            winner_claimer_id=receipt.winner_claimer_id,
            winner_entry_hash=receipt.winner_entry_hash,
            superseded_node_id=receipt.superseded_node_id,
            superseded_claimer_id=receipt.superseded_claimer_id,
        )

    # -- read ---------------------------------------------------------

    def read(self) -> list[ClaimReceipt]:
        """Return every receipt on disk, in insertion order."""
        return list(self.iter_receipts())

    def iter_receipts(self) -> Iterator[ClaimReceipt]:
        """Yield each receipt without holding the whole file in memory."""
        if not self.path.exists():
            return
        with self.path.open("rb") as fp:
            for raw in fp:
                stripped = raw.strip()
                if not stripped:
                    continue
                yield ClaimReceipt.from_dict(json.loads(stripped.decode("utf-8")))

    def project(self) -> ClaimState:
        """Return the pure fold of the on-disk receipts."""
        return project_claims(self.read())

    def head(self) -> str:
        """Return the journal head hash (last ``entry_hash`` or genesis)."""
        return self._tail_hash()

    # -- conflict reconciliation --------------------------------------

    def reconcile(self, *, ts_ns: int) -> list[ClaimReceipt]:
        """Append a ``supersede`` receipt for every deterministic loser.

        Reads the journal, folds it, and for each key with more than one live
        claim appends a chain-anchored ``claim_superseded`` receipt naming the
        winner (lowest ``entry_hash``) for every loser that does not already
        hold one. Returns the receipts appended, in a deterministic order.
        Idempotent: re-running once every loser is superseded is a no-op.

        The receipt is attributed to *this* reconciling node -- its
        ``claimer_id`` / ``node_id`` name this node, the one whose install key
        signs the entry -- so the signature and the declared identity agree. The
        losing claim it speaks about is carried as referenced data: the loser's
        ``entry_hash`` (``supersedes``) and identity (``superseded_node_id`` /
        ``superseded_claimer_id``), never the receipt's own identity fields.
        """
        active, explicit_superseded = _active_buckets(self.read())
        emitted: list[ClaimReceipt] = []
        cursor_ts = int(ts_ns)
        for key in sorted(active):
            bucket = active[key]
            if len(bucket) <= 1:
                continue
            winner_entry_hash = min(bucket)
            winner = bucket[winner_entry_hash]
            for loser_entry_hash in sorted(bucket):
                if loser_entry_hash == winner_entry_hash or loser_entry_hash in explicit_superseded:
                    continue
                loser = bucket[loser_entry_hash]
                receipt = self.append(
                    kind="supersede",
                    tracker=key[0],
                    ticket_id=key[1],
                    role=key[2],
                    claimer_id=self._node_id,
                    node_id=self._node_id,
                    lease_expires_at=0.0,
                    ts_ns=cursor_ts,
                    supersedes=loser_entry_hash,
                    winner_claimer_id=winner.claimer_id,
                    winner_entry_hash=winner_entry_hash,
                    superseded_node_id=loser.node_id,
                    superseded_claimer_id=loser.claimer_id,
                )
                emitted.append(receipt)
                explicit_superseded.add(loser_entry_hash)
                cursor_ts += 1
        return emitted

    # -- gossip ingest -------------------------------------------------

    def ingest(
        self,
        receipt: ClaimReceipt,
        *,
        ts_ns: int,
        trusted_keys: Mapping[str, dict[str, Any]] | None = None,
    ) -> ClaimIngestResult:
        """Fold one gossiped receipt, or record a signed fork if it diverges.

        The order is deliberate and is the whole security property of the
        gossip path: the Ed25519 signature and the recomputed ``entry_hash``
        are checked *before* anything touches the journal, so an unverifiable
        receipt is never written and never folded. Only then is the chain link
        considered.

        Three outcomes:

        * The receipt's ``prev_entry_hash`` is the local head -- it extends the
          chain, so it is written verbatim (its bytes, and therefore its hash
          and signature, are preserved exactly) and folded.
        * Its ``entry_hash`` is already on disk -- gossip is idempotent, so
          this is a no-op ``duplicate``.
        * It does not extend the local head -- the two chains have diverged. A
          signed ``fork`` receipt carrying the divergence entry index, the
          rejected receipt's hash, and the local head is appended, and the
          foreign receipt is **not** merged. A partition surfaces as a
          recorded, signed observation rather than a silent overwrite.

        Args:
            receipt: The gossiped receipt.
            ts_ns: Explicit timestamp for any fork receipt minted here.
            trusted_keys: Optional map of ``node_id`` to public-key JWK. When
                supplied the signer's key must match the pinned one, so a
                receipt cannot be accepted under a key it shipped itself.

        Returns:
            A :class:`ClaimIngestResult` naming the outcome and the resulting
            local head.
        """
        if compute_claim_entry_hash(receipt) != receipt.entry_hash:
            return ClaimIngestResult(
                status="rejected",
                head=self.head(),
                reason="entry_hash mismatch (tampered payload)",
            )
        trusted = None if trusted_keys is None else trusted_keys.get(receipt.node_id)
        if trusted_keys is not None and trusted is None:
            return ClaimIngestResult(
                status="rejected",
                head=self.head(),
                reason=f"no trusted key pinned for node {receipt.node_id!r}",
            )
        sig_check = verify_head_signature(
            receipt.entry_hash.split(":", 1)[1],
            receipt.signature,
            trusted_public_key_jwk=trusted,
        )
        if not sig_check.ok:
            return ClaimIngestResult(
                status="rejected",
                head=self.head(),
                reason=f"signature failure ({'; '.join(sig_check.errors)})",
            )

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+b") as fp, _exclusive_lock(fp):
                hashes = _entry_hashes_from_handle(fp)
                if receipt.entry_hash in hashes:
                    return ClaimIngestResult(status="duplicate", head=hashes[-1])
                local_head = hashes[-1] if hashes else GENESIS_PREV_HASH
                if receipt.prev_entry_hash == local_head:
                    fp.seek(0, os.SEEK_END)
                    fp.write(_canonical_bytes(asdict(receipt)) + b"\n")
                    fp.flush()
                    os.fsync(fp.fileno())
                    applied = True
                else:
                    applied = False
                    divergence_index = _divergence_index(hashes, receipt.prev_entry_hash)
            if applied:
                self._anchor(receipt)
                return ClaimIngestResult(status="applied", head=receipt.entry_hash)

        # Fork: minted outside the locked section because ``append`` takes the
        # same advisory lock on its own descriptor. The divergence facts were
        # captured under the lock above, so the recorded observation describes
        # the state that actually rejected the receipt.
        fork_receipt = self.append(
            kind="fork",
            tracker=receipt.tracker,
            ticket_id=receipt.ticket_id,
            role=receipt.role,
            claimer_id=self._node_id,
            lease_expires_at=0.0,
            ts_ns=ts_ns,
            fork_divergence_index=divergence_index,
            fork_entry_hash=receipt.entry_hash,
            fork_local_head=local_head,
        )
        return ClaimIngestResult(
            status="forked",
            head=fork_receipt.entry_hash,
            reason=(f"prev_entry_hash {receipt.prev_entry_hash} does not extend local head {local_head}"),
            fork_receipt=fork_receipt,
            divergence_index=divergence_index,
        )

    # -- verify -------------------------------------------------------

    def verify(
        self,
        *,
        trusted_keys: Mapping[str, dict[str, Any]] | None = None,
        chain: AuditChainStore | None = None,
    ) -> ClaimJournalVerifyResult:
        """Replay the journal offline, checking chain links and signatures.

        Walks every receipt confirming the ``prev_entry_hash`` linkage, the
        recomputed ``entry_hash`` (tamper detection), and the Ed25519 node
        signature. A single flipped byte or an inserted / dropped receipt fails
        at the exact entry index. ``trusted_keys`` optionally pins each node's
        public-key JWK by ``node_id``; when omitted the embedded key is trusted
        on first use (the signature still authenticates the bytes against *a*
        key, catching an unsigned tamper).

        When ``chain`` is supplied every receipt's audit-chain anchor is
        re-checked too: the HMAC chain must carry a
        ``cluster.claim_journal_receipt`` event whose ``journal_entry_hash``
        equals the receipt's. That closes the last gap an offline verifier
        would otherwise have -- a journal internally consistent with itself but
        never anchored, i.e. a chain someone rebuilt wholesale.

        Forks are reported in ``forks`` rather than as an integrity failure: a
        ``fork`` receipt is a correctly-signed record that the chain diverged,
        so the file is intact but the coordination history is not. Callers
        wanting "intact *and* single-threaded" should read
        :attr:`ClaimJournalVerifyResult.clean`.

        Runs with no live nodes and no network: everything it needs is the
        journal file plus, optionally, the local audit chain.
        """
        if not self.path.exists():
            return ClaimJournalVerifyResult(ok=True, entry_count=0, head=GENESIS_PREV_HASH)

        anchored: set[str] | None = None
        if chain is not None:
            from bernstein.core.security.audit_chain import EVENT_CLAIM_JOURNAL_RECEIPT

            anchored = {
                str(event.details.get("journal_entry_hash"))
                for event in chain.query(event_type=EVENT_CLAIM_JOURNAL_RECEIPT, include_archived=True)
            }

        prev_hash = GENESIS_PREV_HASH
        ordinal = 0
        receipts: list[ClaimReceipt] = []
        with self.path.open("rb") as fp:
            for raw in fp:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped.decode("utf-8"))
                    receipt = ClaimReceipt.from_dict(payload)
                except (ValueError, KeyError, json.JSONDecodeError) as exc:
                    return ClaimJournalVerifyResult(
                        ok=False,
                        entry_count=ordinal,
                        bad_index=ordinal,
                        failures=[f"entry {ordinal}: schema invalid ({exc})"],
                    )
                if receipt.prev_entry_hash != prev_hash:
                    return ClaimJournalVerifyResult(
                        ok=False,
                        entry_count=ordinal,
                        bad_index=ordinal,
                        failures=[
                            f"entry {ordinal}: prev_entry_hash mismatch "
                            f"(expected {prev_hash}, got {receipt.prev_entry_hash})",
                        ],
                    )
                if compute_claim_entry_hash(receipt) != receipt.entry_hash:
                    return ClaimJournalVerifyResult(
                        ok=False,
                        entry_count=ordinal,
                        bad_index=ordinal,
                        failures=[f"entry {ordinal}: entry_hash mismatch (tampered payload)"],
                    )
                trusted = None if trusted_keys is None else trusted_keys.get(receipt.node_id)
                sig_check = verify_head_signature(
                    receipt.entry_hash.split(":", 1)[1],
                    receipt.signature,
                    trusted_public_key_jwk=trusted,
                )
                if not sig_check.ok:
                    return ClaimJournalVerifyResult(
                        ok=False,
                        entry_count=ordinal,
                        bad_index=ordinal,
                        failures=[f"entry {ordinal}: signature failure ({'; '.join(sig_check.errors)})"],
                    )
                if anchored is not None and receipt.entry_hash not in anchored:
                    return ClaimJournalVerifyResult(
                        ok=False,
                        entry_count=ordinal,
                        bad_index=ordinal,
                        failures=[
                            f"entry {ordinal}: no audit-chain anchor for {receipt.entry_hash}",
                        ],
                        anchors_checked=True,
                    )
                receipts.append(receipt)
                prev_hash = receipt.entry_hash
                ordinal += 1
        return ClaimJournalVerifyResult(
            ok=True,
            entry_count=ordinal,
            head=prev_hash,
            forks=_project_forks(receipts),
            anchors_checked=anchored is not None,
        )

    # -- internals ----------------------------------------------------

    def _tail_hash(self) -> str:
        """Return the ``entry_hash`` of the last receipt, or genesis.

        A lock-free snapshot read used by :meth:`head`; the append path resolves
        the tail on its own locked handle via :func:`_tail_hash_from_handle`.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS_PREV_HASH
        with self.path.open("rb") as fp:
            return _tail_hash_from_handle(fp)


# ---------------------------------------------------------------------------
# Dispatcher protocol & outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Result of role-execution for one ticket.

    Attributes:
        success: ``True`` when the role completed cleanly.
        summary: Free-text summary for the success comment body.
        failure: Structured failure payload; required when
            ``success`` is ``False``.
        prose: Optional human-readable prose to render above the
            structured block.
    """

    success: bool
    summary: str = ""
    failure: FailurePayload | None = None
    prose: str = ""


@runtime_checkable
class PipelineDispatcher(Protocol):
    """Role-execution surface the pipeline calls per ticket.

    Real callers wire this to the orchestrator's spawn machinery.
    Tests inject in-process fakes. The dispatcher MUST be deterministic
    enough that ``DispatchOutcome.failure`` carries an actionable
    ``reason_code``; the pipeline does not re-classify failures.
    """

    def dispatch(
        self,
        *,
        tracker: str,
        ticket: Ticket,
        role: str,
        stage_attempt: int,
        idempotency_key: str,
    ) -> DispatchOutcome:
        """Run ``role`` against ``ticket`` and return an outcome."""
        ...


# ---------------------------------------------------------------------------
# Handoff record (emitted via lifecycle hook)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageHandoff:
    """One stage transition emitted to the ``tracker_pipeline.handoff`` hook."""

    tracker: str
    ticket_id: str
    role: str
    from_status: str
    to_status: str
    stage_attempt: int
    outcome: str  # "success" | "failure"
    idempotency_key: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "tracker": self.tracker,
            "ticket_id": self.ticket_id,
            "role": self.role,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "stage_attempt": self.stage_attempt,
            "outcome": self.outcome,
            "idempotency_key": self.idempotency_key,
        }


def _new_handoff_log() -> list[StageHandoff]:
    return []


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


HANDOFF_EVENT_NAME: Final[str] = "tracker_pipeline.handoff"
"""String key the pipeline emits to ``HookRegistry`` for handoff events.

We deliberately use the string form rather than declaring a new
:class:`bernstein.core.lifecycle.hooks.LifecycleEvent` member: callers
that register a callable hook accept the string event, and we avoid a
core enum churn from this leaf module. Operators who prefer a typed
event can subscribe via the script-hook layer.
"""


@dataclass
class TrackerPipeline:
    """Stateless loop turning tracker comments into a handoff bus.

    The loop is deliberately stateless: each :meth:`tick` walks the
    configured trackers in declared order, applies role-specific
    filters, claims via the ledger, dispatches, and transitions. State
    that must survive a crash lives in the SQLite ledger and in the
    tracker itself.

    Args:
        config: Typed config view.
        trackers: Mapping of adapter name -> adapter instance. The
            pipeline pulls open tickets from each adapter in turn.
        ledger: Shared :class:`ClaimLedger`.
        dispatcher: Role-execution surface.
        claimer_id: Unique identifier for this worker process. When
            ``None`` the pipeline generates one from PID + UUID.
        hook_registry: Optional :class:`HookRegistry` to receive
            ``tracker_pipeline.handoff`` callbacks. The pipeline keeps
            a single :class:`StageHandoff` payload per emitted event.

    The pipeline never raises out of :meth:`tick`. Per-ticket errors
    are logged and recorded as failure transitions where possible so
    one broken ticket cannot wedge the loop for a healthy tenant.
    """

    config: PipelineConfig
    trackers: Mapping[str, AbstractTrackerAdapter]
    ledger: ClaimLedger
    dispatcher: PipelineDispatcher
    claimer_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:12]}")
    hook_registry: HookRegistry | None = None
    handoffs: list[StageHandoff] = field(default_factory=_new_handoff_log)
    """In-process log of handoffs emitted by the most recent ticks.

    Operators who do not wire a :class:`HookRegistry` can still inspect
    ``handoffs`` to drive dashboards or tests. The list is cumulative;
    callers may clear it between sweeps.
    """

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def tick(self) -> int:
        """Run one sweep across configured trackers, returning handoff count.

        Returns:
            Number of stage transitions emitted by this sweep
            (successes + failures). Useful for adaptive polling loops.
        """
        emitted = 0
        for tracker_name, adapter in self.trackers.items():
            for stage in self.config.pipeline_stages:
                emitted += self._sweep_stage(tracker_name, adapter, stage)
        return emitted

    def open_handoffs(self) -> list[dict[str, Any]]:
        """Return the in-process handoff log as serialisable dicts.

        Used by ``bernstein pipeline status`` to render open handoffs
        across configured trackers without re-pulling tickets.
        """
        return [h.to_payload() for h in self.handoffs]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sweep_stage(
        self,
        tracker_name: str,
        adapter: AbstractTrackerAdapter,
        stage: PipelineStage,
    ) -> int:
        """Process every ticket eligible for ``stage`` on ``adapter``."""
        emitted = 0
        try:
            iterator = adapter.pull_open_tickets({"status": stage.claim_status})
        except Exception:
            log.exception(
                "tracker_pipeline: pull failed tracker=%s role=%s",
                tracker_name,
                stage.role,
            )
            return 0
        for ticket in iterator:
            try:
                if not self._stage_is_eligible(adapter, ticket, stage):
                    continue
                outcome = self.ledger.try_claim(
                    tracker=tracker_name,
                    ticket_id=ticket.id,
                    role=stage.role,
                    claimer_id=self.claimer_id,
                    ttl_seconds=self.config.claim_lock_ttl_seconds,
                    per_role_max_in_flight=self.config.per_role_max_in_flight,
                )
                if not outcome.granted:
                    if outcome.reason == "concurrency_ceiling":
                        # No point looking at remaining tickets for this
                        # role until somebody releases.
                        break
                    continue
                attempt = self.ledger.bump_attempt(
                    tracker=tracker_name,
                    ticket_id=ticket.id,
                    role=stage.role,
                    claimer_id=self.claimer_id,
                )
                if attempt < 0:
                    # Race: someone released between try_claim and bump.
                    continue
                self._process_ticket(tracker_name, adapter, ticket, stage, attempt)
                emitted += 1
            except Exception:
                log.exception(
                    "tracker_pipeline: ticket %s/%s failed",
                    tracker_name,
                    ticket.id,
                )
                self.ledger.release(
                    tracker=tracker_name,
                    ticket_id=ticket.id,
                    role=stage.role,
                    claimer_id=self.claimer_id,
                )
        return emitted

    def _stage_is_eligible(
        self,
        adapter: AbstractTrackerAdapter,
        ticket: Ticket,
        stage: PipelineStage,
    ) -> bool:
        """Check the optional prior-role gate via structured parsing.

        Earlier revisions did a raw substring match on the rendered
        ``role: "<name>"`` line; small formatting changes (quoting
        style, extra fields, fence spacing) would silently break the
        gate. We now lift every ``bernstein:success`` block and look up
        its ``role`` key, so the gate remains stable across cosmetic
        changes in the comment renderer.
        """
        required_role = stage.requires_prior_role
        if not required_role:
            return True
        # Inspect ticket body plus recent free-text comments when the
        # adapter exposes a ``list_comments`` hook. The adapter contract
        # does not yet mandate one; we degrade to body-only matching
        # when the adapter does not provide it.
        haystacks: list[str] = [ticket.body or ""]
        list_comments_raw = getattr(adapter, "list_comments", None)
        if callable(list_comments_raw):
            list_comments = cast(Callable[[str], Iterable[object]], list_comments_raw)
            try:
                for comment in list_comments(ticket.id):
                    body = getattr(comment, "body", "")
                    if body:
                        haystacks.append(body)
            except Exception:
                log.debug(
                    "tracker_pipeline: list_comments failed for %s; using body-only",
                    ticket.id,
                    exc_info=True,
                )
        for text in haystacks:
            for block in parse_success_blocks(text):
                if block.get("role") == required_role:
                    return True
        return False

    def _process_ticket(
        self,
        tracker_name: str,
        adapter: AbstractTrackerAdapter,
        ticket: Ticket,
        stage: PipelineStage,
        attempt: int,
    ) -> None:
        idempotency_key = make_idempotency_key(
            tracker=tracker_name,
            ticket_id=ticket.id,
            role=stage.role,
            stage=stage.role,
            stage_attempt=attempt,
        )
        try:
            outcome = self.dispatcher.dispatch(
                tracker=tracker_name,
                ticket=ticket,
                role=stage.role,
                stage_attempt=attempt,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            log.exception(
                "tracker_pipeline: dispatcher raised tracker=%s role=%s",
                tracker_name,
                stage.role,
            )
            outcome = DispatchOutcome(
                success=False,
                failure=FailurePayload(
                    reason_code="dispatch.exception",
                    category="unknown",
                    transient=False,
                    next_action="manual",
                    detail=str(exc)[:200],
                ),
            )
        try:
            self._write_outcome_to_tracker(
                tracker_name=tracker_name,
                adapter=adapter,
                ticket=ticket,
                stage=stage,
                attempt=attempt,
                idempotency_key=idempotency_key,
                outcome=outcome,
            )
        finally:
            if outcome.success or (outcome.failure and not outcome.failure.transient):
                self.ledger.release(
                    tracker=tracker_name,
                    ticket_id=ticket.id,
                    role=stage.role,
                    claimer_id=self.claimer_id,
                )

    def _write_outcome_to_tracker(
        self,
        *,
        tracker_name: str,
        adapter: AbstractTrackerAdapter,
        ticket: Ticket,
        stage: PipelineStage,
        attempt: int,
        idempotency_key: str,
        outcome: DispatchOutcome,
    ) -> None:
        target_status: str
        if outcome.success:
            comment_body = format_success_comment(
                role=stage.role,
                stage_attempt=attempt,
                idempotency_key=idempotency_key,
                summary=outcome.summary or "ok",
                prose=outcome.prose,
            )
            target_status = stage.success_status
        else:
            payload = outcome.failure or FailurePayload(
                reason_code="unknown.failure",
                category="unknown",
                transient=False,
                next_action="manual",
            )
            comment_body = format_failure_comment(
                role=stage.role,
                stage_attempt=attempt,
                idempotency_key=idempotency_key,
                payload=payload,
                prose=outcome.prose,
            )
            target_status = stage.failure_status if not payload.transient else stage.claim_status
        comment_key = f"{idempotency_key}:comment"
        transition_key = f"{idempotency_key}:transition"
        try:
            adapter.add_comment(
                ticket.id,
                comment_body,
                idempotency_key=comment_key,
            )
        except Exception:
            log.exception(
                "tracker_pipeline: add_comment failed tracker=%s ticket=%s",
                tracker_name,
                ticket.id,
            )
            return
        try:
            adapter.transition(
                ticket.id,
                target_status,
                idempotency_key=transition_key,
                etag=ticket.etag,
            )
        except Exception:
            log.exception(
                "tracker_pipeline: transition failed tracker=%s ticket=%s -> %s",
                tracker_name,
                ticket.id,
                target_status,
            )
            return
        handoff = StageHandoff(
            tracker=tracker_name,
            ticket_id=ticket.id,
            role=stage.role,
            from_status=stage.claim_status,
            to_status=target_status,
            stage_attempt=attempt,
            outcome="success" if outcome.success else "failure",
            idempotency_key=idempotency_key,
        )
        self.handoffs.append(handoff)
        self._emit_handoff(handoff)

    def _emit_handoff(self, handoff: StageHandoff) -> None:
        if self.hook_registry is None:
            return
        # Imported lazily so a missing ``bernstein.core.lifecycle`` does
        # not break tests that exercise the loop in isolation.
        try:
            from bernstein.core.lifecycle.hooks import (
                LifecycleContext,
                LifecycleEvent,
            )
        except Exception:
            log.debug("tracker_pipeline: lifecycle module unavailable", exc_info=True)
            return
        # Use the closest cross-CLI event - the registry tolerates
        # callables registered against any event we ask it about. We
        # add a ``handoff_event_name`` key so subscribers can filter.
        ctx = LifecycleContext(
            event=LifecycleEvent.POST_TASK,
            task=handoff.ticket_id,
            data={"handoff_event_name": HANDOFF_EVENT_NAME} | handoff.to_payload(),
        )
        try:
            self.hook_registry.run(LifecycleEvent.POST_TASK, ctx)
        except Exception:
            log.exception("tracker_pipeline: handoff hook raised")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def default_ledger_path(state_root: Path) -> Path:
    """Return the conventional ledger path under ``state_root``.

    Typically ``state_root`` is the project's ``.sdd/`` directory.
    """
    return state_root / DEFAULT_LEDGER_RELPATH


def default_claim_journal_path(state_root: Path) -> Path:
    """Return the conventional MESH claim-journal path under ``state_root``.

    Typically ``state_root`` is the project's ``.sdd/`` directory. Only the
    leaderless MESH path materialises this file; STAR deployments never do.
    """
    return state_root / DEFAULT_CLAIM_JOURNAL_RELPATH


def build_pipeline_from_yaml(
    raw: Mapping[str, object],
    *,
    trackers: Mapping[str, AbstractTrackerAdapter],
    dispatcher: PipelineDispatcher,
    state_root: Path,
    hook_registry: HookRegistry | None = None,
) -> TrackerPipeline:
    """Assemble a :class:`TrackerPipeline` from the YAML ``raw`` view.

    ``raw`` is the contents of ``orchestration.tracker_pipeline`` from
    ``bernstein.yaml``. The ledger lives under
    ``state_root / DEFAULT_LEDGER_RELPATH``.
    """
    config = PipelineConfig.from_dict(raw)
    ledger = ClaimLedger(default_ledger_path(state_root))
    return TrackerPipeline(
        config=config,
        trackers=trackers,
        ledger=ledger,
        dispatcher=dispatcher,
        hook_registry=hook_registry,
    )


# Re-exported helpers used by callers wiring the pipeline up.
def stage_attempt_for(
    ledger: ClaimLedger,
    *,
    tracker: str,
    ticket_id: str,
    role: str,
) -> int:
    """Convenience: return the current stage_attempt or ``0`` if absent."""
    return ledger.attempt_count(tracker=tracker, ticket_id=ticket_id, role=role)


def role_names_in_flight(handoffs: Sequence[StageHandoff]) -> dict[str, int]:
    """Return per-role counts from a sequence of :class:`StageHandoff`.

    Used by ``bernstein pipeline status`` and tests to confirm the
    concurrency ceiling was respected over a window.
    """
    counts: dict[str, int] = {}
    for handoff in handoffs:
        if handoff.outcome == "failure":
            continue
        counts[handoff.role] = counts.get(handoff.role, 0) + 1
    return counts
