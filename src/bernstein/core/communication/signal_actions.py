"""Typed-signal action layer: BLOCKER -> audit-anchored clearance gate (#2556).

The bulletin board already ships a typed signal vocabulary
(``MessageType = alert | blocker | finding | status | dependency``), but a
posted ``blocker`` is inert: the multi-cell tick only logs it, so dependent
work keeps getting claimed against an unresolved blocker and there is no
attestable record of which dependent tasks ran while a blocker was open.

This module adds the missing auto-action layer. A per-``MessageType`` action
registry (default ``observe`` no-op) turns a ``blocker`` into a deterministic
projection into the task graph:

    blocker signal -> clearance task + injected ``depends_on`` edges -> resolution

The projection is a **pure function** of ``(ordered bulletin journal prefix,
blocker content hash, scope)`` onto a canonical ``(clearance_task_id,
injected_edge_set, graph_delta_hash)`` -- no wall-clock, no RNG -- so two
operators replaying the same journal produce byte-identical gates. The clearance
task participates as an ordinary ``depends_on`` edge, so the existing dependency
gate (``store.claim_next`` / ``store.list_tasks`` withhold tasks whose
dependencies are not terminal) already halts dependent work until the clearance
reaches a terminal cleared state.

Every projection and every resolution is sealed as a ``signal.gate_projection``
receipt on the HMAC-chained audit log
(:func:`bernstein.core.security.audit_chain.record_signal_gate_projection`).
The gate state is a projection of those chained rows -- strip the deterministic
scheduler and the audit chain and the feature collapses to today's logged
blocker. An offline verifier reconstructs, from the chain alone, that no
dependent task was claimed while a gate was open.

Non-blocker signal types (``alert`` / ``finding`` / ``status`` / ``dependency``)
stay observe-only through the registry default, leaving the action layer
extensible (``INFO`` / ``REQUEST`` can follow the same shape).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from bernstein.core.security.audit_chain import (
    EVENT_SIGNAL_GATE_PROJECTION,
    EVENT_TASK_CLAIM_RECEIPT,
    GATE_TERMINAL_RESOLUTIONS,
    record_signal_gate_projection,
    validate_gate_resolution,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
    from bernstein.core.security.audit import AuditEvent, ChainScanCursor
    from bernstein.core.security.audit_chain import AuditChainStore


# ---------------------------------------------------------------------------
# Per-MessageType action registry (Phase 1)
# ---------------------------------------------------------------------------

#: No-op action: the signal is observed but never mutates the scheduler.
ACTION_OBSERVE = "observe"
#: Blocker action: the signal materializes a deterministic clearance gate.
ACTION_MATERIALIZE_CLEARANCE_GATE = "materialize_clearance_gate"

#: Registry mapping each ``MessageType`` to its action. The default (used for
#: any unregistered type) is :data:`ACTION_OBSERVE`, so a new signal type never
#: mutates the scheduler until it is explicitly wired. ``INFO`` / ``REQUEST``
#: can follow ``blocker`` by adding an entry here plus a projection.
SIGNAL_ACTIONS: dict[str, str] = {
    "alert": ACTION_OBSERVE,
    "blocker": ACTION_MATERIALIZE_CLEARANCE_GATE,
    "finding": ACTION_OBSERVE,
    "status": ACTION_OBSERVE,
    "dependency": ACTION_OBSERVE,
}


def action_for(msg_type: str) -> str:
    """Return the registered action for *msg_type* (default ``observe``)."""
    return SIGNAL_ACTIONS.get(msg_type, ACTION_OBSERVE)


class ClearanceChainUnverified(RuntimeError):
    """Raised when the gate index cannot be built from an authenticated chain.

    The coordinator derives its idempotency decisions from
    ``signal.gate_projection`` rows. ``AuditChainStore.query`` parses the JSONL
    without checking any HMAC, so admitting its rows unverified would let
    anyone with write access to the audit directory forge a ``pending`` row and
    suppress gate materialization outright. When the chain does not verify the
    coordinator refuses to act rather than trust the rows (#2648).
    """


class ClearanceStatus(Enum):
    """Lifecycle states for a clearance gate, mirroring ``DelegationStatus``."""

    PENDING = "pending"  # Gate is open; dependent work is withheld
    CLEARED = "cleared"  # Blocker resolved; dependents released
    EXPIRED = "expired"  # Deadline passed (deterministic timeout terminal)


# ---------------------------------------------------------------------------
# Pure projection core (Phase 1)
# ---------------------------------------------------------------------------


def _canonical(obj: object) -> bytes:
    """Serialise *obj* to canonical (sorted-key, compact) JSON bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blocker_content_hash(*, agent_id: str, content: str, cell_id: str | None) -> str:
    """Return a ``sha256:`` digest over a blocker's stable content fields.

    The hash omits the wall-clock timestamp so it is a stable identity for the
    blocker's *content*; journal position is folded into the clearance-task id
    separately (see :func:`project_clearance_gate`).
    """
    payload = _canonical({"agent_id": agent_id, "cell_id": cell_id or "", "content": content, "type": "blocker"})
    return "sha256:" + _sha256_hex(payload)


def journal_prefix_hash(messages: Sequence[BulletinMessage]) -> str:
    """Return a ``sha256:`` digest over an ordered bulletin journal prefix.

    Two operators replaying the same ordered messages compute the same prefix
    hash; a different prefix (extra prior message, different order) yields a
    different hash, which disambiguates otherwise-identical blockers.
    """
    items = [
        {
            "agent_id": m.agent_id,
            "cell_id": m.cell_id or "",
            "content": m.content,
            "timestamp": m.timestamp,
            "type": m.type,
        }
        for m in messages
    ]
    return "sha256:" + _sha256_hex(_canonical(items))


def compute_graph_delta_hash(
    *,
    clearance_task_id: str,
    injected_edges: Sequence[str],
    blocker_content_hash: str,
    scope_cell_id: str,
    deadline: int,
) -> str:
    """Return the canonical task-graph delta hash (64 hex chars).

    Computed over exactly the fields recorded in the ``signal.gate_projection``
    audit entry, so an offline verifier holding only the chain entry recomputes
    it byte-identically. Flipping any recorded input (e.g. the blocker content
    hash) diverges the result.
    """
    payload = _canonical(
        {
            "clearance_task_id": clearance_task_id,
            "injected_edges": sorted(injected_edges),
            "blocker_content_hash": blocker_content_hash,
            "scope_cell_id": scope_cell_id,
            "deadline": deadline,
        }
    )
    return _sha256_hex(payload)


@dataclass(frozen=True)
class ClearanceGateSpec:
    """The canonical projection of a blocker signal onto the task graph.

    Attributes:
        blocker_content_hash: ``sha256:`` digest of the blocker's content.
        clearance_task_id: Deterministic clearance-task id (``clearance-<hex>``).
        injected_edges: Sorted, de-duplicated dependent task ids receiving a
            ``depends_on`` edge onto the clearance task.
        scope_cell_id: The blocker's cell scope.
        journal_prefix_hash: Digest of the ordered journal prefix the projection
            was computed against.
        graph_delta_hash: Canonical task-graph delta hash (64 hex chars).
        deadline: Deterministic expiry deadline (Unix seconds; 0 = no expiry).
    """

    blocker_content_hash: str
    clearance_task_id: str
    injected_edges: tuple[str, ...]
    scope_cell_id: str
    journal_prefix_hash: str
    graph_delta_hash: str
    deadline: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation of the spec."""
        return {
            "blocker_content_hash": self.blocker_content_hash,
            "clearance_task_id": self.clearance_task_id,
            "injected_edges": list(self.injected_edges),
            "scope_cell_id": self.scope_cell_id,
            "journal_prefix_hash": self.journal_prefix_hash,
            "graph_delta_hash": self.graph_delta_hash,
            "deadline": self.deadline,
        }


def clearance_task_id_for(*, blocker: BulletinMessage, journal_prefix_hash: str) -> str:
    """Return the deterministic clearance-task id for *blocker*.

    The id is a pure function of the blocker's content hash, the ordered
    journal prefix, and the blocker's scope. It deliberately excludes the
    injected edge set, so the identity of a gate is stable even as the open
    dependent set moves; that is what lets a replay recognise an already-sealed
    gate from the chain without re-reading the task graph (#2648).
    """
    scope = blocker.cell_id or ""
    content_hash = blocker_content_hash(agent_id=blocker.agent_id, content=blocker.content, cell_id=blocker.cell_id)
    seed = _canonical({"content_hash": content_hash, "journal_prefix_hash": journal_prefix_hash, "scope": scope})
    return "clearance-" + _sha256_hex(seed)[:16]


def spec_from_receipt(details: Mapping[str, object], *, journal_prefix_hash: str = "") -> ClearanceGateSpec:
    """Rebuild a :class:`ClearanceGateSpec` from a recorded gate receipt.

    The ``signal.gate_projection`` row is the source of truth for a
    materialized gate, so a replay (or a restart) reconstructs the spec from
    the chain rather than from process-local state. ``journal_prefix_hash`` is
    not part of the receipt; callers that recomputed it pass it back in.
    """
    return ClearanceGateSpec(
        blocker_content_hash=str(details.get("blocker_content_hash", "")),
        clearance_task_id=str(details.get("clearance_task_id", "")),
        injected_edges=tuple(str(edge) for edge in details.get("injected_edges", []) or []),
        scope_cell_id=str(details.get("scope_cell_id", "")),
        journal_prefix_hash=journal_prefix_hash or str(details.get("journal_prefix_hash", "")),
        graph_delta_hash=str(details.get("graph_delta_hash", "")),
        deadline=int(details.get("deadline", 0) or 0),
    )


@dataclass
class GateAnchor:
    """The recorded materialization state of one clearance gate.

    This is the **single source of truth** for the question "which row anchors
    this gate". The writer (:class:`ClearanceGateCoordinator`) and both readers
    (:func:`verify_clearance_gates`, :func:`project_gate_states`) build their
    anchors through :meth:`absorb_pending`, so they cannot drift apart.

    Two rules, deliberately asymmetric:

    * The **latest** ``pending`` row supplies the comparison fields. A
      re-materialization supersedes its predecessor, so the newest row is the
      gate's current shape.
    * **Every** ``pending`` row's HMAC is an acceptable ``blocker_entry_hash``.
      Chains written before durable idempotency landed carry one ``pending``
      row per restart for the same gate id; requiring a single blessed HMAC
      would make those gates impossible to close (#2648).
    """

    clearance_task_id: str
    blocker_content_hash: str = ""
    graph_delta_hash: str = ""
    scope_cell_id: str = ""
    deadline: int = 0
    journal_prefix_hash: str = ""
    edges: set[str] = field(default_factory=set)
    index: int = -1
    entry_hmac: str = ""
    entry_hmacs: set[str] = field(default_factory=set)

    def absorb_pending(self, details: Mapping[str, object], *, hmac: str, index: int) -> None:
        """Fold a ``pending`` row into this anchor."""
        self.blocker_content_hash = str(details.get("blocker_content_hash", ""))
        self.graph_delta_hash = str(details.get("graph_delta_hash", ""))
        self.scope_cell_id = str(details.get("scope_cell_id", ""))
        self.deadline = int(details.get("deadline", 0) or 0)
        self.journal_prefix_hash = str(details.get("journal_prefix_hash", ""))
        self.edges = {str(edge) for edge in details.get("injected_edges", []) or []}
        self.index = index
        self.entry_hmac = hmac
        if hmac:
            self.entry_hmacs.add(hmac)

    def to_spec(self, *, journal_prefix_hash: str = "") -> ClearanceGateSpec:
        """Rebuild the projected spec this anchor represents."""
        return ClearanceGateSpec(
            blocker_content_hash=self.blocker_content_hash,
            clearance_task_id=self.clearance_task_id,
            injected_edges=tuple(sorted(self.edges)),
            scope_cell_id=self.scope_cell_id,
            journal_prefix_hash=journal_prefix_hash or self.journal_prefix_hash,
            graph_delta_hash=self.graph_delta_hash,
            deadline=self.deadline,
        )


def build_gate_anchors(events: Sequence[AuditEvent]) -> dict[str, GateAnchor]:
    """Build the gate anchor index from an ordered chain slice.

    Every consumer of ``signal.gate_projection`` rows derives its anchors here,
    so the writer's ``blocker_entry_hash`` is by construction one the readers
    accept.
    """
    anchors: dict[str, GateAnchor] = {}
    for idx, event in enumerate(events):
        if event.event_type != EVENT_SIGNAL_GATE_PROJECTION:
            continue
        details = event.details
        clearance_task_id = str(details.get("clearance_task_id", ""))
        if not clearance_task_id or str(details.get("resolution", "pending")) != "pending":
            continue
        anchor = anchors.setdefault(clearance_task_id, GateAnchor(clearance_task_id=clearance_task_id))
        anchor.absorb_pending(details, hmac=event.hmac, index=idx)
    return anchors


def project_clearance_gate(
    *,
    blocker: BulletinMessage,
    scope_task_ids: Sequence[str],
    journal_prefix_hash: str,
    ttl_seconds: int = 0,
) -> ClearanceGateSpec:
    """Deterministically project a blocker signal onto a clearance gate.

    Pure function: no wall-clock read, no RNG. Given the same ``blocker``,
    ``scope_task_ids`` (order-independent), and ``journal_prefix_hash``, the
    returned spec is byte-identical across runs and across operators.

    Args:
        blocker: The posted blocker message.
        scope_task_ids: Open dependent task ids in the blocker's scope; the set
            is sorted and de-duplicated to form the injected edge set.
        journal_prefix_hash: Digest of the ordered journal prefix up to and
            including the blocker (see :func:`journal_prefix_hash`).
        ttl_seconds: Optional deterministic expiry horizon; the deadline is a
            pure function of the recorded blocker timestamp plus this value.

    Returns:
        The canonical :class:`ClearanceGateSpec`.
    """
    scope = blocker.cell_id or ""
    content_hash = blocker_content_hash(agent_id=blocker.agent_id, content=blocker.content, cell_id=blocker.cell_id)
    clearance_task_id = clearance_task_id_for(blocker=blocker, journal_prefix_hash=journal_prefix_hash)
    injected = tuple(sorted(set(scope_task_ids)))
    deadline = int(blocker.timestamp) + ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0
    graph_delta_hash = compute_graph_delta_hash(
        clearance_task_id=clearance_task_id,
        injected_edges=injected,
        blocker_content_hash=content_hash,
        scope_cell_id=scope,
        deadline=deadline,
    )
    return ClearanceGateSpec(
        blocker_content_hash=content_hash,
        clearance_task_id=clearance_task_id,
        injected_edges=injected,
        scope_cell_id=scope,
        journal_prefix_hash=journal_prefix_hash,
        graph_delta_hash=graph_delta_hash,
        deadline=deadline,
    )


# ---------------------------------------------------------------------------
# Task-graph injector (Phase 2)
# ---------------------------------------------------------------------------


class ClearanceGateInjector(Protocol):
    """Applies a projected clearance gate to a task graph.

    Implementations enqueue the clearance task and inject the dependency edges
    into whichever task store backs the deployment. The coordinator owns the
    projection, receipt, and idempotency; the injector owns the graph mutation.
    """

    def open_dependent_task_ids(self, scope_cell_id: str) -> list[str]:
        """Return the open dependent task ids in *scope_cell_id*."""
        ...

    def create_clearance_task(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> None:
        """Create the clearance task with ``spec.clearance_task_id``."""
        ...

    def add_dependency_edge(self, dependent_task_id: str, clearance_task_id: str) -> None:
        """Inject a ``depends_on`` edge from *dependent_task_id* -> clearance."""
        ...

    def release_clearance_task(self, clearance_task_id: str) -> None:
        """Mark the clearance task terminal so its dependents are released."""
        ...


class AtomicClearanceGateInjector(ClearanceGateInjector, Protocol):
    """A :class:`ClearanceGateInjector` that applies a gate in one atomic step.

    Creating the clearance task and injecting its ``depends_on`` edges as two
    separate store mutations leaves a claim race window in which the gate
    exists but its dependents are not yet gated. An injector implementing this
    protocol collapses both into a single atomic mutation; the coordinator
    prefers it whenever the wired injector provides it (#2648).
    """

    def apply_gate(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> Sequence[str]:
        """Create the clearance task and inject every edge atomically.

        Returns:
            The dependent task ids that actually received an edge. A store that
            re-selects its OPEN dependents under its own lock may inject a
            narrower set than ``spec.injected_edges``, which was computed from
            an unlocked read; the coordinator re-derives the spec from this
            return value so the signed receipt never attests an edge the store
            did not create (#2648).
        """
        ...


@dataclass
class InMemoryClearanceInjector:
    """In-memory :class:`ClearanceGateInjector` for tests and dry runs.

    Records every mutation so a caller can assert on the projected graph
    without a live task store. ``apply_gate`` holds an internal lock so the
    recorded mutations mirror the atomicity of the real store path.
    """

    open_by_cell: dict[str, list[str]] = field(default_factory=dict)
    created: list[ClearanceGateSpec] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def open_dependent_task_ids(self, scope_cell_id: str) -> list[str]:
        return list(self.open_by_cell.get(scope_cell_id, []))

    def create_clearance_task(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> None:
        self.created.append(spec)

    def add_dependency_edge(self, dependent_task_id: str, clearance_task_id: str) -> None:
        self.edges.append((dependent_task_id, clearance_task_id))

    def release_clearance_task(self, clearance_task_id: str) -> None:
        self.released.append(clearance_task_id)

    def apply_gate(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> Sequence[str]:
        """Record the gate task and all its edges under one lock acquisition."""
        with self._lock:
            self.create_clearance_task(spec, blocker)
            for dependent_id in spec.injected_edges:
                self.add_dependency_edge(dependent_id, spec.clearance_task_id)
        return spec.injected_edges


# ---------------------------------------------------------------------------
# Coordinator (Phase 2 + 3)
# ---------------------------------------------------------------------------


class ClearanceGateCoordinator:
    """Materializes blocker signals into audit-anchored clearance gates.

    The coordinator reads new blocker signals from a bulletin board, projects
    each deterministically, applies the projection through an injector, and
    seals a ``signal.gate_projection`` receipt on the HMAC chain. Resolving a
    clearance emits a signed release entry that references the materialization
    entry hash.

    Idempotency is keyed on the ``clearance_task_id`` **as recorded on the
    chain**, not on process-local state, so a restart or a second coordinator
    replaying the same journal recognises the already-sealed gate and never
    double-injects. Both ``materialize`` and ``resolve`` run under a coordinator
    lock, so the check-then-act is atomic and a concurrent caller cannot slip
    between the idempotency probe and the mutation (#2648).
    """

    def __init__(
        self,
        *,
        bulletin: BulletinBoard,
        injector: ClearanceGateInjector,
        chain: AuditChainStore,
        actor: str = "clearance_gate",
        ttl_seconds: int = 0,
        lineage_seal: Callable[[ClearanceGateSpec, str], str] | None = None,
    ) -> None:
        self._bulletin = bulletin
        self._injector = injector
        self._chain = chain
        self._actor = actor
        self._ttl_seconds = ttl_seconds
        self._lineage_seal = lineage_seal
        self._materialized: dict[str, ClearanceGateSpec] = {}
        self._entry_hmac: dict[str, str] = {}
        self._terminal: dict[str, AuditEvent] = {}
        self._released: set[str] = set()
        self._last_bulletin_ts: float = 0.0
        self._lock = threading.RLock()
        self._chain_loaded = False
        self._scan_cursor: ChainScanCursor | None = None
        self._gate_events: list[AuditEvent] = []

    # -- durable gate index -------------------------------------------------

    def _load_chain_state(self, *, force: bool = False) -> None:
        """Hydrate the gate index from the chain.

        The chain is the durable record of which gates were materialized and
        which reached a terminal resolution, so idempotency survives a restart
        and is shared by any coordinator reading the same chain. Loaded once
        per coordinator; pass ``force`` to re-read before a mutation, which is
        how a cache miss stays correct when another writer materialized or
        resolved the same gate since this coordinator started.

        The chain is authenticated before any row is admitted. ``query()``
        performs no HMAC checking, so an unverified read would let a forged
        ``pending`` row stand in for a real receipt and suppress the gate
        entirely; the coordinator refuses instead of trusting it.

        Entries already known to this process are never overwritten.

        Raises:
            ClearanceChainUnverified: If the HMAC chain does not verify.
        """
        if self._chain_loaded and not force:
            return
        # Authenticate and read the same segments, incrementally. The first call
        # walks the whole chain; later calls verify and parse only the bytes
        # appended since, so materializing a gate stays O(new rows) instead of
        # O(entire chain) on a path that runs per blocker inside POST /bulletin.
        result = self._chain.scan_verified(self._scan_cursor, event_type=EVENT_SIGNAL_GATE_PROJECTION)
        if not result.ok:
            raise ClearanceChainUnverified(
                "refusing to build the clearance-gate index from an unverified audit chain: "
                + "; ".join(result.errors[:3])
            )
        if result.rescanned:
            # History under the cursor changed, so the previously derived index
            # is not trustworthy; rebuild it from the full re-walk.
            self._gate_events.clear()
        self._scan_cursor = result.cursor
        self._gate_events.extend(event for event in result.events if event.event_type == EVENT_SIGNAL_GATE_PROJECTION)
        anchors = build_gate_anchors(self._gate_events)
        for clearance_task_id, anchor in anchors.items():
            self._materialized[clearance_task_id] = anchor.to_spec()
            self._entry_hmac[clearance_task_id] = anchor.entry_hmac
        for event in self._gate_events:
            details = event.details
            clearance_task_id = str(details.get("clearance_task_id", ""))
            if not clearance_task_id or str(details.get("resolution", "pending")) == "pending":
                continue
            if clearance_task_id not in self._terminal:
                self._terminal[clearance_task_id] = event
        self._chain_loaded = True

    # -- read side ----------------------------------------------------------

    def _prefix_for(self, blocker: BulletinMessage) -> list[BulletinMessage]:
        """Return the ordered journal prefix up to and including *blocker*."""
        msgs = self._bulletin.snapshot()
        for i, m in enumerate(msgs):
            if m is blocker:
                return msgs[: i + 1]
        if blocker in msgs:
            return msgs[: msgs.index(blocker) + 1]
        return [*msgs, blocker]

    def _apply(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> ClearanceGateSpec:
        """Apply *spec* to the task graph, atomically when the injector can.

        An injector exposing :class:`AtomicClearanceGateInjector` creates the
        gate task and injects every edge in one step, closing the claim race
        window. Injectors that predate that protocol fall back to the two-step
        path, which the coordinator lock still serialises.

        Returns:
            The spec as actually applied. An atomic injector reports the edge
            set it really injected, which can be narrower than the projected
            set if a dependent was claimed between the unlocked scope read and
            the locked mutation; the spec (and therefore ``graph_delta_hash``)
            is re-derived from that truth so the receipt cannot attest an edge
            that does not exist (#2648).
        """
        apply_gate = getattr(self._injector, "apply_gate", None)
        if callable(apply_gate):
            applied = apply_gate(spec, blocker)
            if applied is None:
                return spec
            injected = tuple(sorted({str(edge) for edge in applied}))
            if injected == spec.injected_edges:
                return spec
            return ClearanceGateSpec(
                blocker_content_hash=spec.blocker_content_hash,
                clearance_task_id=spec.clearance_task_id,
                injected_edges=injected,
                scope_cell_id=spec.scope_cell_id,
                journal_prefix_hash=spec.journal_prefix_hash,
                graph_delta_hash=compute_graph_delta_hash(
                    clearance_task_id=spec.clearance_task_id,
                    injected_edges=injected,
                    blocker_content_hash=spec.blocker_content_hash,
                    scope_cell_id=spec.scope_cell_id,
                    deadline=spec.deadline,
                ),
                deadline=spec.deadline,
            )
        self._injector.create_clearance_task(spec, blocker)
        for dependent_id in spec.injected_edges:
            self._injector.add_dependency_edge(dependent_id, spec.clearance_task_id)
        return spec

    def _release_once(self, clearance_task_id: str) -> None:
        """Release *clearance_task_id* in the graph at most once per coordinator.

        Called after the terminal receipt is sealed. Tracking the release
        separately from the receipt lets a retry converge the graph when a
        prior call sealed the receipt but failed before the release, without
        ever releasing twice for a single terminal receipt.
        """
        if clearance_task_id in self._released:
            return
        self._injector.release_clearance_task(clearance_task_id)
        self._released.add(clearance_task_id)

    # -- materialization ----------------------------------------------------

    def materialize(self, blocker: BulletinMessage) -> ClearanceGateSpec | None:
        """Project *blocker* into a clearance gate and seal the receipt.

        Returns the :class:`ClearanceGateSpec`, or ``None`` when the signal type
        is observe-only. Durably idempotent: a blocker whose gate is already on
        the chain returns the recorded spec without re-injecting or
        re-recording, even in a fresh process.

        The graph mutation and the receipt are applied as one saga under the
        coordinator lock. If sealing the receipt fails after the graph was
        mutated, the gate is compensated (released) and the failure propagates,
        so no un-attested gate is left withholding work.
        """
        if action_for(blocker.type) != ACTION_MATERIALIZE_CLEARANCE_GATE:
            return None

        scope = blocker.cell_id or ""
        jph = journal_prefix_hash(self._prefix_for(blocker))
        clearance_task_id = clearance_task_id_for(blocker=blocker, journal_prefix_hash=jph)

        with self._lock:
            self._load_chain_state()
            if clearance_task_id not in self._materialized and clearance_task_id not in self._terminal:
                # About to mutate: re-read the chain so a gate sealed by another
                # writer since this coordinator started is still recognised.
                self._load_chain_state(force=True)
            recorded = self._materialized.get(clearance_task_id)
            if recorded is not None:
                # The receipt on the chain is the source of truth for an
                # already-materialized gate; return it rather than a fresh
                # projection over a since-moved open-task set.
                return spec_from_receipt(recorded.to_dict(), journal_prefix_hash=jph)
            terminal = self._terminal.get(clearance_task_id)
            if terminal is not None:
                return spec_from_receipt(terminal.details, journal_prefix_hash=jph)

            # A prior gate's clearance task is itself an open task in the cell;
            # never gate a gate on another gate.
            open_deps = [
                dep for dep in self._injector.open_dependent_task_ids(scope) if not dep.startswith("clearance-")
            ]
            spec = project_clearance_gate(
                blocker=blocker,
                scope_task_ids=open_deps,
                journal_prefix_hash=jph,
                ttl_seconds=self._ttl_seconds,
            )

            spec = self._apply(spec, blocker)
            try:
                # The lineage seal is part of the sealing step, so it lives
                # inside the compensating region: a seal failure must release
                # the gate rather than leave it injected with no receipt.
                journal_entry_hash = self._lineage_seal(spec, "pending") if self._lineage_seal is not None else ""
                event = record_signal_gate_projection(
                    chain=self._chain,
                    blocker_content_hash=spec.blocker_content_hash,
                    clearance_task_id=spec.clearance_task_id,
                    injected_edges=list(spec.injected_edges),
                    graph_delta_hash=spec.graph_delta_hash,
                    scope_cell_id=spec.scope_cell_id,
                    deadline=spec.deadline,
                    resolution="pending",
                    resolver="",
                    last_state_hash="genesis",
                    journal_entry_hash=journal_entry_hash,
                    blocker_entry_hash="",
                    journal_prefix_hash=spec.journal_prefix_hash,
                    actor=self._actor,
                )
            except BaseException:
                # Compensate the graph mutation: an unsealed gate would withhold
                # dependent work with no attestation explaining why.
                self._injector.release_clearance_task(spec.clearance_task_id)
                raise
            self._materialized[spec.clearance_task_id] = spec
            self._entry_hmac[spec.clearance_task_id] = event.hmac
            return spec

    def process_new_blockers(self) -> list[ClearanceGateSpec]:
        """Materialize gates for every blocker posted since the last call."""
        new_messages = self._bulletin.read_since(self._last_bulletin_ts)
        if new_messages:
            self._last_bulletin_ts = max(m.timestamp for m in new_messages)
        specs: list[ClearanceGateSpec] = []
        seen: set[str] = set()
        for message in new_messages:
            if action_for(message.type) != ACTION_MATERIALIZE_CLEARANCE_GATE:
                continue
            spec = self.materialize(message)
            if spec is not None and spec.clearance_task_id not in seen:
                seen.add(spec.clearance_task_id)
                specs.append(spec)
        return specs

    # -- resolution ---------------------------------------------------------

    def resolve(
        self,
        clearance_task_id: str,
        resolver: str,
        resolution: str = "cleared",
    ) -> AuditEvent:
        """Resolve a clearance gate and emit a signed release entry.

        Args:
            clearance_task_id: The gate to resolve.
            resolver: Identity clearing the gate (recorded as the actor).
            resolution: ``cleared`` (blocker fixed) or ``expired`` (timeout).

        Returns:
            The recorded release :class:`AuditEvent`; its ``blocker_entry_hash``
            references the materialization entry. When the gate already reached
            a terminal receipt, that first receipt is returned unchanged and no
            new entry is written.

        Raises:
            ClearanceResolutionRefusal: If ``resolution`` is outside
                ``{cleared, expired}``.
            KeyError: If no materialization receipt exists for the gate.
        """
        validate_gate_resolution(resolution, allowed=GATE_TERMINAL_RESOLUTIONS)
        with self._lock:
            self._load_chain_state()
            # A gate resolves exactly once. A second call is a no-op that
            # replays the first terminal receipt, so a retry (or a concurrent
            # resolver) can never append a second terminal row or re-release
            # the clearance task.
            already = self._terminal.get(clearance_task_id)
            if already is None:
                # About to append a terminal receipt: re-read the chain so a
                # resolution sealed by another writer is not duplicated.
                self._load_chain_state(force=True)
                already = self._terminal.get(clearance_task_id)
            if already is not None:
                # The receipt already exists; make sure the graph mutation it
                # attests actually landed (a prior call may have failed between
                # the append and the release).
                self._release_once(clearance_task_id)
                return already

            spec = self._materialized.get(clearance_task_id)
            if spec is None:
                raise KeyError(clearance_task_id)

            blocker_entry_hash = self._entry_hmac.get(clearance_task_id, "")

            # Seal the terminal receipt *before* touching the graph. Releasing
            # first and failing to append would leave the gate open on the
            # chain while its dependents became claimable, which the offline
            # verifier would (correctly) report as a violation with nothing
            # attesting the release. Failing this way round leaves the gate
            # withheld, which is the safe direction, and the release is
            # re-attempted on the next call.
            journal_entry_hash = self._lineage_seal(spec, resolution) if self._lineage_seal is not None else ""
            event = record_signal_gate_projection(
                chain=self._chain,
                blocker_content_hash=spec.blocker_content_hash,
                clearance_task_id=spec.clearance_task_id,
                injected_edges=list(spec.injected_edges),
                graph_delta_hash=spec.graph_delta_hash,
                scope_cell_id=spec.scope_cell_id,
                deadline=spec.deadline,
                resolution=resolution,
                resolver=resolver,
                last_state_hash=blocker_entry_hash or "genesis",
                journal_entry_hash=journal_entry_hash,
                blocker_entry_hash=blocker_entry_hash,
                journal_prefix_hash=spec.journal_prefix_hash,
                actor=resolver or self._actor,
            )
            self._terminal[clearance_task_id] = event
            self._release_once(clearance_task_id)
            return event


# ---------------------------------------------------------------------------
# Gate-state projection + offline verification (Phase 3 + 4)
# ---------------------------------------------------------------------------


@dataclass
class ClearanceGateState:
    """A clearance gate's state, projected from chained ``signal.gate_projection`` rows."""

    clearance_task_id: str
    blocker_content_hash: str
    injected_edges: tuple[str, ...]
    graph_delta_hash: str
    scope_cell_id: str
    deadline: int
    status: ClearanceStatus
    resolver: str
    projection_index: int
    resolution_index: int | None


def project_gate_states(
    events: Sequence[AuditEvent],
    *,
    as_of: int | None = None,
) -> dict[str, ClearanceGateState]:
    """Project clearance-gate states from ``signal.gate_projection`` events.

    The gate state is a pure projection of the chained rows, not mutable
    side-table state. When *as_of* is supplied, a still-pending gate whose
    recorded deadline has passed is reported ``EXPIRED`` -- expiry is a
    deterministic function of the recorded inputs and the explicit *as_of*, not
    a wall-clock read at query time.

    A gate closes here on exactly the rows
    :func:`verify_clearance_gates` accepts: a ``cleared`` / ``expired``
    resolution that references its materialization entry and whose recorded
    fields still match. A row failing any of those checks leaves the gate
    ``PENDING``, so this read side and the verifier never disagree about
    whether a gate is open (#2648).
    """
    states: dict[str, ClearanceGateState] = {}
    # Same shared anchor selection the verifier and the coordinator use.
    anchors = build_gate_anchors(events)
    for idx, event in enumerate(events):
        if event.event_type != EVENT_SIGNAL_GATE_PROJECTION:
            continue
        details = event.details
        clearance_task_id = str(details.get("clearance_task_id", ""))
        resolution = str(details.get("resolution", "pending"))
        if resolution == "pending":
            states[clearance_task_id] = ClearanceGateState(
                clearance_task_id=clearance_task_id,
                blocker_content_hash=str(details.get("blocker_content_hash", "")),
                injected_edges=tuple(details.get("injected_edges", [])),
                graph_delta_hash=str(details.get("graph_delta_hash", "")),
                scope_cell_id=str(details.get("scope_cell_id", "")),
                deadline=int(details.get("deadline", 0) or 0),
                status=ClearanceStatus.PENDING,
                resolver="",
                projection_index=idx,
                resolution_index=None,
            )
            continue
        state = states.get(clearance_task_id)
        if state is None:
            continue
        if _validate_gate_resolution_row(
            clearance_task_id=clearance_task_id,
            resolution=resolution,
            details=details,
            anchor=anchors.get(clearance_task_id),
            index=idx,
        ):
            continue  # unvalidated resolution: the gate stays open
        state.status = ClearanceStatus.CLEARED if resolution == "cleared" else ClearanceStatus.EXPIRED
        state.resolver = str(details.get("resolver", ""))
        state.resolution_index = idx

    if as_of is not None:
        for state in states.values():
            if state.status is ClearanceStatus.PENDING and state.deadline > 0 and as_of >= state.deadline:
                state.status = ClearanceStatus.EXPIRED
    return states


@dataclass
class GateVerifyResult:
    """Outcome of an offline clearance-gate verification pass."""

    ok: bool
    gate_count: int
    errors: list[str] = field(default_factory=list)
    violations: list[tuple[str, int]] = field(default_factory=list)


def _validate_gate_resolution_row(
    *,
    clearance_task_id: str,
    resolution: str,
    details: Mapping[str, object],
    anchor: GateAnchor | None,
    index: int,
) -> list[str]:
    """Return the reasons a resolution row must not close its gate.

    A gate closes only on a resolution that is in the terminal vocabulary, that
    references a prior materialization by its entry HMAC, and whose recorded
    fields still match that materialization. Anything else (an unknown
    resolution string, an orphan resolution, a widened edge set, a forged
    back-reference) leaves the gate open (#2648).

    Returns:
        An empty list when the row is a valid closure, else one error per
        failed check.
    """
    errors: list[str] = []
    if resolution not in GATE_TERMINAL_RESOLUTIONS:
        errors.append(
            f"gate {clearance_task_id}: resolution {resolution!r} at chain index {index} is outside "
            f"the terminal vocabulary {sorted(GATE_TERMINAL_RESOLUTIONS)}"
        )
    if anchor is None:
        errors.append(
            f"gate {clearance_task_id}: resolution '{resolution}' at chain index {index} has no prior projection"
        )
        return errors

    recorded_back_ref = str(details.get("blocker_entry_hash", ""))
    if recorded_back_ref not in anchor.entry_hmacs:
        errors.append(
            f"gate {clearance_task_id}: resolution at chain index {index} does not reference its "
            f"materialization entry (blocker_entry_hash mismatch)"
        )

    field_checks: tuple[tuple[str, object, object], ...] = (
        ("blocker_content_hash", str(details.get("blocker_content_hash", "")), anchor.blocker_content_hash),
        ("graph_delta_hash", str(details.get("graph_delta_hash", "")), anchor.graph_delta_hash),
        ("scope_cell_id", str(details.get("scope_cell_id", "")), anchor.scope_cell_id),
        ("deadline", int(details.get("deadline", 0) or 0), anchor.deadline),
        ("injected_edges", {str(e) for e in details.get("injected_edges", []) or []}, anchor.edges),
    )
    for name, actual, expected in field_checks:
        if actual != expected:
            errors.append(
                f"gate {clearance_task_id}: resolution at chain index {index} diverges from the open gate on {name}"
            )
    return errors


def verify_clearance_gates(
    events: Sequence[AuditEvent],
    *,
    as_of: int | None = None,
) -> GateVerifyResult:
    """Reconstruct clearance-gate integrity from the audit chain alone.

    Walks the ordered chain and, for every ``signal.gate_projection`` entry,
    recomputes ``graph_delta_hash`` from the recorded fields (a mismatch is an
    error). It then confirms no ``task.claim_receipt`` granted a scoped
    dependent while its clearance gate was still open, reporting each violation
    as ``(task_id, claim chain index)``.

    Args:
        events: The full ordered audit chain (materializations, resolutions, and
            claim receipts interleaved).
        as_of: Unused today; reserved so callers can pin a deterministic
            evaluation instant without changing the signature.

    Returns:
        A :class:`GateVerifyResult`. ``ok`` is ``True`` only when there are no
        hash mismatches and no claims during an open gate.
    """
    del as_of  # reserved for a future deterministic-expiry policy hook
    errors: list[str] = []
    violations: list[tuple[str, int]] = []
    open_gates: dict[str, GateAnchor] = {}
    seen: set[str] = set()

    # Anchor selection is owned by build_gate_anchors, the same function the
    # coordinator writes its blocker_entry_hash from. Re-deriving it inline here
    # is what let the writer and the verifier drift apart (#2648).
    materialized = build_gate_anchors(events)

    for idx, event in enumerate(events):
        if event.event_type == EVENT_SIGNAL_GATE_PROJECTION:
            details = event.details
            clearance_task_id = str(details.get("clearance_task_id", ""))
            resolution = str(details.get("resolution", "pending"))
            # Every gate row counts, including an orphan resolution: a chain
            # made only of forged resolutions must never report "no gates" and
            # pass silently.
            seen.add(clearance_task_id)
            recomputed = compute_graph_delta_hash(
                clearance_task_id=clearance_task_id,
                injected_edges=[str(e) for e in details.get("injected_edges", [])],
                blocker_content_hash=str(details.get("blocker_content_hash", "")),
                scope_cell_id=str(details.get("scope_cell_id", "")),
                deadline=int(details.get("deadline", 0) or 0),
            )
            stored = str(details.get("graph_delta_hash", ""))
            if recomputed != stored:
                errors.append(
                    f"gate {clearance_task_id}: graph_delta_hash mismatch at chain index {idx} "
                    f"(recomputed {recomputed[:12]}.. != stored {stored[:12]}..)"
                )
            if resolution == "pending":
                anchor = materialized.get(clearance_task_id)
                if anchor is not None:
                    open_gates[clearance_task_id] = anchor
            else:
                closing_errors = _validate_gate_resolution_row(
                    clearance_task_id=clearance_task_id,
                    resolution=resolution,
                    details=details,
                    anchor=materialized.get(clearance_task_id),
                    index=idx,
                )
                errors.extend(closing_errors)
                # A gate closes only on a resolution whose vocabulary, recorded
                # fields, and back-reference all check out. An unvalidated row
                # leaves the gate open, so a later claim of a scoped dependent
                # is still reported as a violation.
                if not closing_errors:
                    open_gates.pop(clearance_task_id, None)
        elif event.event_type == EVENT_TASK_CLAIM_RECEIPT:
            claimed = str(event.details.get("task_id", "") or event.resource_id)
            for clearance_task_id, gate in open_gates.items():
                if claimed in gate.edges:
                    violations.append((claimed, idx))
                    errors.append(
                        f"dependent {claimed} claimed at chain index {idx} while clearance gate "
                        f"{clearance_task_id} (opened at index {gate.index}) was still open"
                    )

    ok = not errors and not violations
    return GateVerifyResult(ok=ok, gate_count=len(seen), errors=errors, violations=violations)


__all__ = [
    "ACTION_MATERIALIZE_CLEARANCE_GATE",
    "ACTION_OBSERVE",
    "SIGNAL_ACTIONS",
    "AtomicClearanceGateInjector",
    "ClearanceChainUnverified",
    "ClearanceGateCoordinator",
    "ClearanceGateInjector",
    "ClearanceGateSpec",
    "ClearanceGateState",
    "ClearanceStatus",
    "GateAnchor",
    "GateVerifyResult",
    "InMemoryClearanceInjector",
    "action_for",
    "blocker_content_hash",
    "build_gate_anchors",
    "clearance_task_id_for",
    "compute_graph_delta_hash",
    "journal_prefix_hash",
    "project_clearance_gate",
    "project_gate_states",
    "spec_from_receipt",
    "verify_clearance_gates",
]
