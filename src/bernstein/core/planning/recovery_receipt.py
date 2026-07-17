"""RecoveryReceipt - lineage-attested failure receipts for on_fail recovery.

Issue #2557. The workflow DSL (:mod:`bernstein.core.planning.workflow_dsl`)
routes control back to a recovery node when an upstream node fails under a
guard, but ``DAGExecutor.create_task`` historically built the recovery Task
from the node's static description alone. The failing upstream Task never
reached the recovery agent, so it started blind and re-derived the failure
from scratch.

This module makes the recovery task's primary artifact a content-addressed,
spine-anchored failure receipt. The receipt captures the failure the run
already observed - the failing node's terminal status, its condition context,
the tail of the run's Merkle event journal filtered to that task, and its
quality gate report - in a canonical, sorted-key serialization.

Substrate coupling (the artifact IS the proof):

* **Content addressing.** :meth:`RecoveryReceipt.content_hash` is a pure
  function of the receipt payload. Two runs over identical fixtures produce a
  byte-identical receipt and therefore a byte-identical content hash.
* **Lineage anchoring.** :func:`record_receipt_on_spine` records the canonical
  bytes on the run's :class:`~bernstein.core.lineage.spine.LineageSpine`,
  which returns a Merkle-chained, HMAC-tagged entry hash. That entry hash is
  the recovery task's provenance identity.
* **Tamper evidence.** :func:`resolve_receipt_on_spine` recomputes the content
  hash from the receipt bytes and checks it against the anchored entry, then
  re-verifies the whole spine chain. Mutating any receipt field breaks the
  content-hash match; forging the spine's stored content hash to cover it up
  breaks the HMAC chain, which has no key an editor can reproduce.

Strip the spine and the feature collapses to a bare re-spawn: the recovery
task can no longer prove which failure it recovers, and the content-addressed
handoff disappears. The receipt is not a log line bolted onto a re-spawn; it
is the re-spawn's reason, in verifiable form.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.quality.quality_gates import QualityGatesResult

#: Version stamped into every receipt payload. Bump only on a wire-format
#: change so a verifier can reject unknown receipt shapes.
RECOVERY_RECEIPT_VERSION = 1

#: Repo-relative directory the content-addressed receipt artifacts live under.
#: Kept repo-relative and POSIX so ``LineageSpine._reject_unsafe_artifact_path``
#: accepts it.
RECEIPT_ARTIFACT_DIR = ".sdd/lineage/receipts"

#: Journal envelope fields that vary across runs even when execution is
#: identical (wall-clock plus derived chain fields). Excluded from the receipt
#: so timing never leaks into the content hash, mirroring the journal's own
#: ``_NON_DETERMINISTIC_FIELDS`` policy (issue #2293).
_NON_DETERMINISTIC_JOURNAL_FIELDS = frozenset({"ts", "elapsed_s", "index", "prev_hash", "payload_hash", "event_hash"})

#: Default number of trailing journal events retained in a receipt.
DEFAULT_JOURNAL_TAIL = 20


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _project_journal_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop non-deterministic envelope fields from a journal event."""
    return {k: v for k, v in event.items() if k not in _NON_DETERMINISTIC_JOURNAL_FIELDS}


def journal_tail_for_task(
    events: Iterable[dict[str, Any]],
    *,
    task_id: str,
    limit: int = DEFAULT_JOURNAL_TAIL,
) -> tuple[dict[str, Any], ...]:
    """Return the bounded, timing-stripped journal tail for one task.

    Filters ``events`` to those whose ``task_id`` matches, keeps only the last
    ``limit`` in append order, and strips the wall-clock envelope so the slice
    is a deterministic projection of the failing task's decision trail.

    Args:
        events: Journal events in append order (e.g. from ``load_events``).
        task_id: The failing task id to filter on.
        limit: Maximum number of trailing events to retain (non-positive
            means keep none).

    Returns:
        A tuple of projected events, oldest first.
    """
    if limit <= 0:
        return ()
    matched = [_project_journal_event(e) for e in events if e.get("task_id") == task_id]
    return tuple(matched[-limit:])


def gate_report_findings(report: QualityGatesResult | None) -> tuple[dict[str, Any], ...]:
    """Return a deterministic, ordered projection of a gate report.

    Each finding carries the gate name, its pass/block flags, its status, and a
    bounded detail string. The order matches the report's run order so the
    projection is stable across runs.

    Args:
        report: The failing task's quality gate result, or ``None``.

    Returns:
        A tuple of per-gate finding dicts (empty when ``report`` is ``None``).
    """
    if report is None:
        return ()
    findings: list[dict[str, Any]] = []
    for check in report.gate_results:
        findings.append(
            {
                "gate": check.gate,
                "passed": bool(check.passed),
                "blocked": bool(check.blocked),
                "status": check.status,
                "detail": (check.detail or "")[:2000],
            }
        )
    return tuple(findings)


@dataclass(frozen=True, slots=True)
class RecoveryReceipt:
    """A content-addressed, lineage-attestable failure receipt.

    The receipt is the recovery task's primary artifact. Every field is a pure
    function of the failure the run already captured, so the canonical
    serialization is byte-identical across two runs over identical fixtures.

    ``spine_entry_hash`` is set after the receipt is anchored; it is excluded
    from :meth:`canonical_payload` (it is derived from that payload, so
    including it would be circular) and from equality/content hashing.

    Attributes:
        failing_node_id: DAG node id of the failing upstream node.
        recovery_node_id: DAG node id of the recovery node being instantiated.
        source_status: Terminal status value of the failing task.
        condition_context: The failing task's condition context
            (``status`` / ``result`` / ``output``) as built by
            ``build_condition_context``.
        gate_report: Ordered per-gate findings from the failing task's quality
            gate report.
        journal_tail: Bounded, timing-stripped tail of the run journal filtered
            to the failing task.
        spine_entry_hash: Merkle-chained spine entry hash once anchored, else
            ``None``. Not part of the content-addressed payload.
    """

    failing_node_id: str
    recovery_node_id: str
    source_status: str
    condition_context: dict[str, Any]
    gate_report: tuple[dict[str, Any], ...] = ()
    journal_tail: tuple[dict[str, Any], ...] = ()
    v: int = RECOVERY_RECEIPT_VERSION
    spine_entry_hash: str | None = field(default=None, compare=False)

    def canonical_payload(self) -> dict[str, Any]:
        """Return the content-addressed payload (excludes the spine hash)."""
        return {
            "v": self.v,
            "failing_node_id": self.failing_node_id,
            "recovery_node_id": self.recovery_node_id,
            "source_status": self.source_status,
            "condition_context": self.condition_context,
            "gate_report": [dict(g) for g in self.gate_report],
            "journal_tail": [dict(e) for e in self.journal_tail],
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical JSON bytes hashed for content addressing."""
        return _canonical_bytes(self.canonical_payload())

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed digest of the canonical bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    def artifact_path(self) -> str:
        """Return the content-addressed, repo-relative receipt artifact path."""
        digest = self.content_hash().split(":", 1)[1]
        return f"{RECEIPT_ARTIFACT_DIR}/{digest}.json"

    def with_entry_hash(self, entry_hash: str) -> RecoveryReceipt:
        """Return a copy carrying the anchored spine entry hash."""
        return RecoveryReceipt(
            failing_node_id=self.failing_node_id,
            recovery_node_id=self.recovery_node_id,
            source_status=self.source_status,
            condition_context=self.condition_context,
            gate_report=self.gate_report,
            journal_tail=self.journal_tail,
            v=self.v,
            spine_entry_hash=entry_hash,
        )

    def render_preamble(self) -> str:
        """Render the markdown recovery-context preamble for the agent prompt.

        Mirrors the recovery-context block ``evict_degraded_sessions`` stashes
        on ``orchestrator._context_recovery``: a markdown section the
        replacement agent's prompt prepends. It leads with the lineage-attested
        failure summary and its spine entry hash so the recovery agent
        continues from the captured failure instead of rediscovering it.
        """
        lines: list[str] = [
            "## Recovery context (lineage-attested failure receipt)",
            "",
            f"You are the recovery task for failing node `{self.failing_node_id}` "
            f"(recovery node `{self.recovery_node_id}`).",
            f"The upstream task terminated with status **{self.source_status}**. "
            "Continue from this captured failure; do not re-derive it.",
            "",
        ]
        if self.spine_entry_hash:
            lines.append(f"- Lineage spine entry: `{self.spine_entry_hash}`")
        lines.append(f"- Receipt content hash: `{self.content_hash()}`")
        lines.append("")

        result = str(self.condition_context.get("result", "") or "")
        if result:
            lines.append("### Failing task result")
            lines.append(result[:2000])
            lines.append("")

        failed = [g for g in self.gate_report if not g.get("passed")]
        if failed:
            lines.append("### Gate findings")
            for g in failed:
                marker = "blocking" if g.get("blocked") else "non-blocking"
                detail = str(g.get("detail", "") or "").strip()
                suffix = f": {detail}" if detail else ""
                lines.append(f"- `{g.get('gate')}` ({marker}){suffix}")
            lines.append("")

        if self.journal_tail:
            lines.append("### Journal tail")
            for event in self.journal_tail:
                lines.append(f"- `{event.get('event', '')}`")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def build_receipt(
    *,
    failing_node_id: str,
    recovery_node_id: str,
    source_status: str,
    condition_context: dict[str, Any],
    source_task_id: str,
    journal_events: Iterable[dict[str, Any]] | None = None,
    gate_report: QualityGatesResult | None = None,
    journal_tail_limit: int = DEFAULT_JOURNAL_TAIL,
) -> RecoveryReceipt:
    """Assemble a :class:`RecoveryReceipt` from the captured failure inputs.

    Args:
        failing_node_id: DAG node id of the failing upstream node.
        recovery_node_id: DAG node id of the recovery node.
        source_status: Terminal status value of the failing task.
        condition_context: The failing task's condition context.
        source_task_id: The failing task's instance id (used to filter the
            journal tail).
        journal_events: Optional run journal events in append order.
        gate_report: Optional quality gate report for the failing task.
        journal_tail_limit: Maximum number of trailing journal events to keep.

    Returns:
        An unanchored :class:`RecoveryReceipt` (``spine_entry_hash`` is
        ``None``).
    """
    tail = (
        journal_tail_for_task(journal_events, task_id=source_task_id, limit=journal_tail_limit)
        if journal_events is not None
        else ()
    )
    return RecoveryReceipt(
        failing_node_id=failing_node_id,
        recovery_node_id=recovery_node_id,
        source_status=source_status,
        condition_context=dict(condition_context),
        gate_report=gate_report_findings(gate_report),
        journal_tail=tail,
    )


def record_receipt_on_spine(
    receipt: RecoveryReceipt,
    *,
    spine: LineageSpine,
    actor: str = "dag-executor",
    model: str = "",
    timestamp: int = 0,
) -> str:
    """Anchor a receipt on the run's lineage spine and return its entry hash.

    The canonical receipt bytes are the recorded content, so the spine entry's
    ``content_hash`` binds the anchored entry to the exact receipt payload. The
    ``step_id`` and ``artifact_path`` are pure functions of node identity and
    content, so two runs over identical fixtures against a fresh spine produce
    an identical entry hash (issue #2557 AC3).

    Args:
        receipt: The receipt to anchor.
        spine: The run's lineage spine.
        actor: Producing actor recorded on the spine entry.
        model: Optional model string recorded for provenance.
        timestamp: Stable integer timestamp; defaults to ``0`` so identical
            fixtures replay byte-identically.

    Returns:
        The Merkle-chained spine entry hash.
    """
    return spine.record(
        artifact_path=receipt.artifact_path(),
        content=receipt.canonical_bytes(),
        actor=actor,
        step_id=recovery_step_id(receipt),
        model=model,
        timestamp=timestamp,
    )


def recovery_step_id(receipt: RecoveryReceipt) -> str:
    """Return the deterministic spine ``step_id`` for a receipt.

    Derived from node identity rather than the recovery task's instance id
    (which carries a per-run uuid), so the spine entry hash is a stable
    function of the failure being recovered.
    """
    return f"recovery-receipt:{receipt.failing_node_id}->{receipt.recovery_node_id}"


@dataclass(frozen=True, slots=True)
class ReceiptResolution:
    """Outcome of resolving a recovery receipt hash against a spine.

    Attributes:
        resolved: The entry hash was found on the spine.
        chain_ok: The whole spine chain re-verified cleanly.
        content_match: ``True`` / ``False`` when receipt content was supplied
            and compared against the anchored entry, else ``None``.
        errors: Human-readable failure explanations.
    """

    resolved: bool
    chain_ok: bool
    content_match: bool | None = None
    errors: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        """True only when the entry resolves, the chain is intact, and (when
        checked) the receipt content matches the anchored entry."""
        return self.resolved and self.chain_ok and self.content_match is not False


def resolve_receipt_on_spine(
    spine: LineageSpine,
    *,
    entry_hash: str,
    receipt_content: bytes | None = None,
) -> ReceiptResolution:
    """Confirm a recovery receipt hash resolves to a valid spine entry.

    Walks the spine for ``entry_hash``, re-verifies the whole Merkle+HMAC
    chain, and - when ``receipt_content`` is supplied - checks the content
    address binds the anchored entry to the exact receipt bytes.

    Args:
        spine: The run's lineage spine.
        entry_hash: The spine entry hash embedded in the recovery task.
        receipt_content: Optional receipt bytes to content-address against the
            anchored entry's ``content_hash``.

    Returns:
        A :class:`ReceiptResolution`.
    """
    from bernstein.core.lineage.spine import content_hash_of

    errors: list[str] = []

    verify = spine.verify()
    chain_ok = verify.ok
    if not chain_ok:
        errors.extend(verify.errors or ["spine chain verification failed"])

    matched = next((e for e in spine.iter_entries() if e.entry_hash == entry_hash), None)
    resolved = matched is not None
    if not resolved:
        errors.append(f"receipt entry hash not found on spine: {entry_hash}")

    content_match: bool | None = None
    if receipt_content is not None and matched is not None:
        recomputed = content_hash_of(receipt_content)
        content_match = recomputed == matched.content_hash
        if not content_match:
            errors.append(f"receipt content hash mismatch: computed {recomputed} != anchored {matched.content_hash}")

    return ReceiptResolution(
        resolved=resolved,
        chain_ok=chain_ok,
        content_match=content_match,
        errors=errors,
    )


def verify_receipt(spine: LineageSpine, receipt: RecoveryReceipt) -> ReceiptResolution:
    """Resolve an anchored receipt against its spine using its own bytes.

    Convenience wrapper: uses ``receipt.spine_entry_hash`` and
    ``receipt.canonical_bytes()`` so a holder of the receipt object can verify
    it end to end in one call.

    Raises:
        ValueError: The receipt has no anchored spine entry hash.
    """
    if not receipt.spine_entry_hash:
        raise ValueError("receipt has no spine_entry_hash; anchor it first")
    return resolve_receipt_on_spine(
        spine,
        entry_hash=receipt.spine_entry_hash,
        receipt_content=receipt.canonical_bytes(),
    )


__all__ = [
    "DEFAULT_JOURNAL_TAIL",
    "RECEIPT_ARTIFACT_DIR",
    "RECOVERY_RECEIPT_VERSION",
    "ReceiptResolution",
    "RecoveryReceipt",
    "build_receipt",
    "gate_report_findings",
    "journal_tail_for_task",
    "record_receipt_on_spine",
    "recovery_step_id",
    "resolve_receipt_on_spine",
    "verify_receipt",
]

# Metadata keys the DAG executor stamps on a recovery Task so downstream
# consumers (prompt injection, the lineage verify CLI) can recover the anchor
# without re-reading the spine.
RECEIPT_HASH_METADATA_KEY = "recovery_receipt_hash"
RECEIPT_CONTENT_HASH_METADATA_KEY = "recovery_receipt_content_hash"
RECEIPT_FAILING_NODE_METADATA_KEY = "recovery_failing_node"
