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
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from bernstein.core.security.audit_chain import (
    EVENT_SIGNAL_GATE_PROJECTION,
    EVENT_TASK_CLAIM_RECEIPT,
    record_signal_gate_projection,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
    from bernstein.core.security.audit import AuditEvent
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
            "deadline": int(deadline),
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
    seed = _canonical({"content_hash": content_hash, "journal_prefix_hash": journal_prefix_hash, "scope": scope})
    clearance_task_id = "clearance-" + _sha256_hex(seed)[:16]
    injected = tuple(sorted({str(t) for t in scope_task_ids}))
    deadline = int(blocker.timestamp) + int(ttl_seconds) if ttl_seconds and ttl_seconds > 0 else 0
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


@dataclass
class InMemoryClearanceInjector:
    """In-memory :class:`ClearanceGateInjector` for tests and dry runs.

    Records every mutation so a caller can assert on the projected graph
    without a live task store.
    """

    open_by_cell: dict[str, list[str]] = field(default_factory=dict)
    created: list[ClearanceGateSpec] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def open_dependent_task_ids(self, scope_cell_id: str) -> list[str]:
        return list(self.open_by_cell.get(scope_cell_id, []))

    def create_clearance_task(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> None:
        self.created.append(spec)

    def add_dependency_edge(self, dependent_task_id: str, clearance_task_id: str) -> None:
        self.edges.append((dependent_task_id, clearance_task_id))

    def release_clearance_task(self, clearance_task_id: str) -> None:
        self.released.append(clearance_task_id)


# ---------------------------------------------------------------------------
# Coordinator (Phase 2 + 3)
# ---------------------------------------------------------------------------


class ClearanceGateCoordinator:
    """Materializes blocker signals into audit-anchored clearance gates.

    The coordinator reads new blocker signals from a bulletin board, projects
    each deterministically, applies the projection through an injector, and
    seals a ``signal.gate_projection`` receipt on the HMAC chain. Resolving a
    clearance emits a signed release entry that references the materialization
    entry hash. Materialization is idempotent per clearance-task id, so
    reprocessing the same journal never double-injects.
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
        self._last_bulletin_ts: float = 0.0

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

    # -- materialization ----------------------------------------------------

    def materialize(self, blocker: BulletinMessage) -> ClearanceGateSpec | None:
        """Project *blocker* into a clearance gate and seal the receipt.

        Returns the :class:`ClearanceGateSpec`, or ``None`` when the signal type
        is observe-only. Idempotent: a blocker already materialized returns its
        existing spec without re-injecting or re-recording.
        """
        if action_for(blocker.type) != ACTION_MATERIALIZE_CLEARANCE_GATE:
            return None

        scope = blocker.cell_id or ""
        jph = journal_prefix_hash(self._prefix_for(blocker))
        # A prior gate's clearance task is itself an open task in the cell; never
        # gate a gate on another gate.
        open_deps = [
            dep for dep in self._injector.open_dependent_task_ids(scope) if not str(dep).startswith("clearance-")
        ]
        spec = project_clearance_gate(
            blocker=blocker,
            scope_task_ids=open_deps,
            journal_prefix_hash=jph,
            ttl_seconds=self._ttl_seconds,
        )
        if spec.clearance_task_id in self._materialized:
            return spec

        self._injector.create_clearance_task(spec, blocker)
        for dependent_id in spec.injected_edges:
            self._injector.add_dependency_edge(dependent_id, spec.clearance_task_id)

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
            actor=self._actor,
        )
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
            references the materialization entry.
        """
        if resolution not in ("cleared", "expired"):
            raise ValueError(f"resolution must be 'cleared' or 'expired', got {resolution!r}")
        spec = self._materialized.get(clearance_task_id)
        if spec is None:
            raise KeyError(clearance_task_id)

        blocker_entry_hash = self._entry_hmac.get(clearance_task_id, "")
        self._injector.release_clearance_task(clearance_task_id)

        journal_entry_hash = self._lineage_seal(spec, resolution) if self._lineage_seal is not None else ""
        return record_signal_gate_projection(
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
            actor=resolver or self._actor,
        )


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
    """
    states: dict[str, ClearanceGateState] = {}
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
    open_gates: dict[str, dict[str, object]] = {}
    seen: set[str] = set()

    for idx, event in enumerate(events):
        if event.event_type == EVENT_SIGNAL_GATE_PROJECTION:
            details = event.details
            clearance_task_id = str(details.get("clearance_task_id", ""))
            resolution = str(details.get("resolution", "pending"))
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
                seen.add(clearance_task_id)
                open_gates[clearance_task_id] = {
                    "edges": {str(e) for e in details.get("injected_edges", [])},
                    "index": idx,
                }
            else:
                if clearance_task_id not in seen:
                    errors.append(
                        f"gate {clearance_task_id}: resolution '{resolution}' at chain index {idx} "
                        "has no prior projection"
                    )
                open_gates.pop(clearance_task_id, None)
        elif event.event_type == EVENT_TASK_CLAIM_RECEIPT:
            claimed = str(event.details.get("task_id", "") or event.resource_id)
            for clearance_task_id, gate in open_gates.items():
                edges = gate["edges"]
                if isinstance(edges, set) and claimed in edges:
                    violations.append((claimed, idx))
                    errors.append(
                        f"dependent {claimed} claimed at chain index {idx} while clearance gate "
                        f"{clearance_task_id} (opened at index {gate['index']}) was still open"
                    )

    ok = not errors and not violations
    return GateVerifyResult(ok=ok, gate_count=len(seen), errors=errors, violations=violations)


__all__ = [
    "ACTION_MATERIALIZE_CLEARANCE_GATE",
    "ACTION_OBSERVE",
    "SIGNAL_ACTIONS",
    "ClearanceGateCoordinator",
    "ClearanceGateInjector",
    "ClearanceGateSpec",
    "ClearanceGateState",
    "ClearanceStatus",
    "GateVerifyResult",
    "InMemoryClearanceInjector",
    "action_for",
    "blocker_content_hash",
    "compute_graph_delta_hash",
    "journal_prefix_hash",
    "project_clearance_gate",
    "project_gate_states",
    "verify_clearance_gates",
]
