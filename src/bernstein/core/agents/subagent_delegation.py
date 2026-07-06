"""Deterministic subagent-delegation execution layer (issue #2308).

Bernstein keeps the *coordination plan* in its deterministic scheduler and
delegates the *mechanical execution* of each leaf to a native subagent
(Claude Code, Codex, Copilot, Gemini). This module is the boundary that lets
that composition stay replayable.

The outer plan is a :class:`OuterPlan` of :class:`DispatchNode` leaves. Each
node names a native ``target`` and the per-agent knobs the native primitive
consumes -- ``model``, ``effort``, ``tools``, ``background``, ``batch`` -- plus
the ``result_schema`` the native structured output must satisfy. Crucially the
node's identity (:meth:`DispatchNode.node_hash`) is a pure function of those
*plan* fields and never of the native result, so the identity is byte-identical
across replays even though inner execution is stochastic.

:func:`dispatch_node` crosses one delegation boundary:

1. It validates the native result against the node's ``result_schema`` at the
   worker boundary -- rejecting hallucinated keys and missing required fields
   (AC1) via the shared :mod:`bernstein.adapters.strict_schema` primitives.
2. It anchors the delegation into the run :class:`~bernstein.core.replay.journal.EventJournal`
   as a ``subagent.delegation`` event carrying the replay-invariant
   ``node_hash`` and the (stochastic) ``result_content_hash``. The outer DAG
   -- the sequence of ``node_hash`` values -- therefore replays byte-identically
   even when the anchored result content differs run to run (AC2).
3. For non-interactive fan-out (``batch=True``) it applies the batch-tier
   discount and records it in the spend ledger tagged ``tier=batch`` (AC3);
   prompt-cache reads are attributed as ``cache_read_tokens`` (AC4).
4. When an :class:`~bernstein.core.security.audit_chain.AuditChainStore` is
   supplied it also mirrors the boundary into the HMAC-chained audit log.

The module owns the *coordination boundary*, not the native call itself: the
caller performs the native subagent invocation (via the adapter surface -- e.g.
``claude`` with ``--agents``, or ``codex``) and passes the parsed structured
result in. That keeps the deterministic outer plan free of any live LLM in the
coordination loop, which is what makes the replay guarantee hold.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.adapters.strict_schema import SchemaViolation, assert_schema_sealed, seal_schema
from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.cost.spend_ledger import LedgerStatus
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "BATCH_TIER_DISCOUNT",
    "DELEGATION_EVENT",
    "DispatchNode",
    "DispatchResult",
    "NativeResultRejected",
    "OuterPlan",
    "delegate_plan",
    "dispatch_node",
]

#: Journal event type stamped for each crossed delegation boundary. Kept in
#: lockstep with :data:`bernstein.core.security.audit_chain.EVENT_SUBAGENT_DELEGATION`.
DELEGATION_EVENT = "subagent.delegation"

#: Fractional discount applied to the undiscounted cost when a node is
#: dispatched on the batch tier (non-interactive fan-out). The standard batch
#: tier bills at half the interactive rate, so the discount is ``0.5``.
BATCH_TIER_DISCOUNT: float = 0.5


class NativeResultRejected(SchemaViolation):
    """A native subagent's structured output failed its node's result schema.

    Raised at the worker boundary before the result is anchored, so a
    hallucinated-key or missing-field payload never reaches the journal or the
    outer plan. Inherits :class:`SchemaViolation` so callers already handling
    strict-schema faults treat it as a bounded, non-transient error.
    """


def _canonical_bytes(value: Any) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class DispatchNode:
    """One leaf of a deterministic outer plan delegated to a native subagent.

    Every field here is part of the *plan*, so :meth:`node_hash` is a pure
    function of the plan and is byte-identical across replays. The native
    result is never an input to the identity.

    Attributes:
        name: Stable per-plan node name (the delegation leaf identity).
        target: Native subagent target -- an adapter name such as ``claude``
            or ``codex``.
        model: Per-agent model string passed to the native primitive.
        effort: Per-agent effort tier (``low`` / ``medium`` / ``high`` / ``max``).
        prompt: The instruction handed to the native subagent.
        result_schema: JSON Schema the native structured output must satisfy at
            the boundary. Sealed against additional properties before use.
        tools: Allowed native tool names for the subagent.
        background: Whether the native subagent runs detached (background flag).
        batch: Whether this leaf is dispatched on the non-interactive batch tier.
    """

    name: str
    target: str
    model: str
    effort: str
    prompt: str
    result_schema: Mapping[str, Any]
    tools: tuple[str, ...] = ()
    background: bool = False
    batch: bool = False

    def sealed_schema(self) -> dict[str, Any]:
        """Return the node's result schema sealed against additional keys."""
        sealed = seal_schema(dict(self.result_schema))
        assert_schema_sealed(sealed)
        return sealed

    def node_hash(self) -> str:
        """Return the replay-invariant identity hash of this plan node.

        The pre-image is the canonical JSON of every *plan* field, so two
        byte-identical plans produce identical node hashes regardless of what
        the native subagent eventually returns.
        """
        return _sha256(
            {
                "name": self.name,
                "target": self.target,
                "model": self.model,
                "effort": self.effort,
                "prompt": self.prompt,
                "result_schema": self.result_schema,
                "tools": list(self.tools),
                "background": self.background,
                "batch": self.batch,
            }
        )


@dataclass(frozen=True, slots=True)
class OuterPlan:
    """A deterministic ordered plan of native-subagent delegation leaves.

    The plan is the coordination artifact the scheduler owns; its
    :meth:`plan_hash` chains the per-node hashes in order, so the whole
    cross-worker DAG has one replay-invariant identity that is independent of
    the stochastic inner execution.
    """

    nodes: tuple[DispatchNode, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for node in self.nodes:
            if node.name in seen:
                raise ValueError(f"duplicate dispatch node name in plan: {node.name!r}")
            seen.add(node.name)

    def plan_hash(self) -> str:
        """Return the deterministic Merkle-style hash over the ordered nodes."""
        return _sha256([node.node_hash() for node in self.nodes])


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of crossing one delegation boundary.

    Attributes:
        node_name: The plan node that was dispatched.
        node_hash: The replay-invariant plan-node hash.
        result_content_hash: SHA-256 of the canonical validated native payload.
        payload: The validated native structured output.
        validated: Always ``True`` on return (a failed validation raises).
        journal_index: 0-based index of the anchoring journal entry, or
            ``None`` when no journal was supplied.
        tier: ``batch`` for non-interactive fan-out, else ``interactive``.
        ledger_status: The post-record ledger status, or ``None`` when no
            ledger was supplied.
    """

    node_name: str
    node_hash: str
    result_content_hash: str
    payload: dict[str, Any]
    validated: bool
    tier: str
    journal_index: int | None = None
    ledger_status: LedgerStatus | None = None


def _validate_against_schema(node: DispatchNode, native_result: Mapping[str, Any]) -> dict[str, Any]:
    """Validate *native_result* against the node's sealed result schema.

    Enforces the two failure modes the strict-schema doctrine cares about at a
    structured-output boundary: no hallucinated (additional) keys, and every
    ``required`` field present. Nested objects that declare ``properties`` are
    checked recursively. Raises :class:`NativeResultRejected` on the first
    violation so the payload never reaches the journal.
    """
    schema = node.sealed_schema()

    def _check(node_schema: Mapping[str, Any], value: Any, path: str) -> None:
        if node_schema.get("type") == "object" or "properties" in node_schema:
            if not isinstance(value, dict):
                raise NativeResultRejected(f"native result at {path or '<root>'} is not an object")
            props: dict[str, Any] = dict(node_schema.get("properties", {}))
            allow_extra = node_schema.get("additionalProperties", False) is not False
            if not allow_extra:
                extra = sorted(set(value) - set(props))
                if extra:
                    raise NativeResultRejected(
                        f"native result has additional properties not permitted at {path or '<root>'}: {extra}",
                        fields=tuple(extra),
                    )
            missing = sorted(f for f in node_schema.get("required", []) if f not in value)
            if missing:
                raise NativeResultRejected(
                    f"native result missing required fields at {path or '<root>'}: {missing}",
                    fields=tuple(missing),
                )
            for key, sub_schema in props.items():
                if key in value and isinstance(sub_schema, dict):
                    _check(sub_schema, value[key], f"{path}/{key}" if path else key)

    _check(schema, native_result, "")
    return dict(native_result)


def dispatch_node(
    node: DispatchNode,
    *,
    native_result: Mapping[str, Any],
    journal: EventJournal | None = None,
    chain: AuditChainStore | None = None,
    ledger: Any | None = None,
    undiscounted_cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    task_id: str = "",
    agent_id: str = "",
    role: str = "",
) -> DispatchResult:
    """Cross one delegation boundary: validate, anchor, and attribute.

    The native subagent has already run; the caller passes its parsed
    structured output as *native_result*. This function validates it against
    the node's result schema at the boundary (AC1), anchors the delegation into
    the run journal so the outer DAG stays replayable (AC2), and -- when a
    ledger is supplied -- records the batch-tier discount (AC3) and prompt-cache
    reads (AC4).

    Args:
        node: The outer-plan node being dispatched.
        native_result: The native subagent's parsed structured output.
        journal: Run event journal to anchor the delegation into.
        chain: Optional HMAC audit chain to mirror the boundary into.
        ledger: Optional :class:`~bernstein.core.cost.spend_ledger.SpendLedger`.
        undiscounted_cost_usd: Interactive-rate cost of the native call; the
            batch discount is applied to this before it hits the ledger.
        input_tokens: Native call input token count.
        output_tokens: Native call output token count.
        cache_read_tokens: Prompt-cache reads served on the stable prefix.
        cache_write_tokens: Prompt-cache writes for the stable prefix.
        task_id: Attribution tag for the ledger.
        agent_id: Attribution tag for the ledger.
        role: Attribution tag for the ledger.

    Returns:
        A :class:`DispatchResult` with the validated payload and anchoring
        metadata.

    Raises:
        NativeResultRejected: When the native result violates the node schema.
    """
    payload = _validate_against_schema(node, native_result)
    node_hash = node.node_hash()
    result_content_hash = _sha256(payload)
    tier = "batch" if node.batch else "interactive"

    journal_index: int | None = None
    journal_event_hash = ""
    if journal is not None:
        journal_index = journal.event_count()
        journal.record(
            DELEGATION_EVENT,
            node_name=node.name,
            target=node.target,
            model=node.model,
            effort=node.effort,
            tier=tier,
            node_hash=node_hash,
            result_content_hash=result_content_hash,
        )
        journal_event_hash = journal.head()

    ledger_status = None
    if ledger is not None:
        ledger_status = _record_spend(
            ledger,
            node=node,
            tier=tier,
            undiscounted_cost_usd=undiscounted_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            task_id=task_id,
            agent_id=agent_id,
            role=role,
        )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_subagent_delegation

        record_subagent_delegation(
            chain=chain,
            run_id=journal.run_id if journal is not None else "",
            node_name=node.name,
            target=node.target,
            node_hash=node_hash,
            result_content_hash=result_content_hash,
            journal_index=journal_index if journal_index is not None else -1,
            journal_event_hash=journal_event_hash,
            tier=tier,
        )

    logger.debug(
        "subagent delegation: node=%s target=%s tier=%s node_hash=%s",
        sanitize_log(node.name),
        sanitize_log(node.target),
        tier,
        node_hash,
    )
    return DispatchResult(
        node_name=node.name,
        node_hash=node_hash,
        result_content_hash=result_content_hash,
        payload=payload,
        validated=True,
        tier=tier,
        journal_index=journal_index,
        ledger_status=ledger_status,
    )


def _record_spend(
    ledger: Any,
    *,
    node: DispatchNode,
    tier: str,
    undiscounted_cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    task_id: str,
    agent_id: str,
    role: str,
) -> LedgerStatus:
    """Record the native call in the spend ledger with tier + cache attribution.

    Applies the batch-tier discount to *undiscounted_cost_usd* when the node is
    a batch leaf, tags the row with the execution ``tier``, and carries the
    prompt-cache read/write token counts so ``bernstein cost`` can attribute the
    savings.
    """
    from bernstein.core.cost.spend_ledger import CallTags

    discounted = undiscounted_cost_usd * (1.0 - BATCH_TIER_DISCOUNT) if tier == "batch" else undiscounted_cost_usd
    tags = CallTags(
        task_id=task_id,
        agent_id=agent_id,
        role=role,
        extra={"tier": tier, "delegation_node": node.name},
    )
    return ledger.record(
        tags=tags,
        model=node.model,
        cost_usd=discounted,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def delegate_plan(
    plan: OuterPlan,
    *,
    journal: EventJournal | None = None,
    native_results: Mapping[str, Mapping[str, Any]],
    chain: AuditChainStore | None = None,
    ledger: Any | None = None,
) -> list[DispatchResult]:
    """Dispatch every node of *plan* in deterministic outer order.

    The plan order is authoritative: leaves are dispatched in the exact order
    they appear in :attr:`OuterPlan.nodes`, so the sequence of anchored
    delegation events -- and therefore the outer DAG identity -- is a pure
    function of the plan, independent of which stochastic native payload each
    leaf produced.

    Args:
        plan: The deterministic outer plan.
        journal: Run event journal to anchor each delegation into.
        native_results: Map from node name to that node's parsed native result.
        chain: Optional HMAC audit chain to mirror each boundary into.
        ledger: Optional spend ledger.

    Returns:
        The per-node :class:`DispatchResult` list in plan order.

    Raises:
        KeyError: When a plan node has no entry in *native_results*.
        NativeResultRejected: When any native result violates its node schema.
    """
    out: list[DispatchResult] = []
    for node in plan.nodes:
        if node.name not in native_results:
            raise KeyError(f"no native result supplied for plan node {node.name!r}")
        out.append(
            dispatch_node(
                node,
                native_result=native_results[node.name],
                journal=journal,
                chain=chain,
                ledger=ledger,
            )
        )
    return out
