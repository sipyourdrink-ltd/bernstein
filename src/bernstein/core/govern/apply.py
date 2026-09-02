"""``govern apply``: execute a reviewed plan and bind the outcome to that plan.

A plan (``govern plan``) that nothing executes leaves the operator to apply the
change set by hand, after which the applied state and the reviewed plan drift
apart with nothing able to say by how much. :func:`apply_plan` closes that gap:
it executes the plan's entries in order and emits a change receipt whose
identity is the executed range itself.

What the receipt binds
----------------------
``playbook_digest`` (the posture in force), ``plan_digest`` (what was
reviewed), ``plan_journal_entry_hash`` (the decision record the plan was
anchored as), ``approver`` (the identity that approved it), and one recorded
outcome per attempted change. The receipt is not "an apply plus a log": it is a
signed projection over the HMAC-chained apply events, so removing the events
does not cost the receipt its log, it costs the receipt its subject digest and
the whole document stops verifying.

What is refused, before anything is mutated
-------------------------------------------
:func:`validate_apply` runs the whole diff through five checks and raises
:class:`GovernApplyRefused` on the first failure, so a refused apply has
attempted no change at all:

1. the plan names a decision record that is present in the lineage journal, and
   the plan's own bytes are the bytes that record anchored;
2. the environment digest recomputed from the playbook in force and the
   environment as inventoried still matches the digest the plan was reviewed
   under -- the review was of a specific world, and if the world moved the
   review is void;
3. no surface is named by more than one entry;
4. no entry names a surface the inventory could not read;
5. every removal-class entry is covered by a satisfied approval. Removal is its
   own class, not a mutation with a minus sign, so it is gated separately by
   :mod:`bernstein.core.security.dual_approval` rather than by re-implementing
   approval semantics here.

Terminal status
---------------
Tri-state and derived, never asserted: ``success`` when nothing failed,
``partial`` when some change applied and a later one failed, ``fail`` when a
failure occurred with nothing applied. All-must-pass is the default -- the
first failure stops the sequence and every later entry is recorded as
``not_attempted`` rather than silently dropped.

Idempotence
-----------
An applier reports :attr:`ChangeStatus.ALREADY_SATISFIED` for a change the
environment already carries. Re-applying a fully applied plan therefore changes
nothing, still emits a receipt saying so, and -- because :func:`compute_apply_id`
is a pure function of the four bound digests -- carries the same apply id as the
run that produced the state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

from bernstein.core.govern.plan_models import (
    GovernPlan,
    PlanEntry,
    PlanEntryKind,
    canonical_digest,
    compute_inputs_hash,
)
from bernstein.core.lineage.spine import content_hash_of
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.audit_receipt import (
    ALL_FORMATS,
    AuditReceipt,
    AuditReceiptError,
    materialize_receipt,
    rebuild_receipt_range,
    receipt_events_head,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.dual_approval import ApprovalStatus
    from bernstein.core.security.lineage_kms import KMSAdapter

GOVERN_APPLY_RECEIPT_TYPE = "https://bernstein.run/attestations/govern-apply-receipt/v1"
GOVERN_APPLY_SCHEMA_VERSION = "1.0.0"

#: Audit-chain event types the receipt range is built from.
EVENT_GOVERN_APPLY_OPENED = "govern.apply.opened"
EVENT_GOVERN_APPLY_CHANGE = "govern.apply.change"
EVENT_GOVERN_APPLY_CLOSED = "govern.apply.closed"

#: Actor recorded on every apply event.
GOVERN_APPLY_ACTOR = "bernstein.govern.apply"

_RESOURCE_TYPE = "govern_apply"


class ChangeStatus(Enum):
    """Outcome of one entry in the change set."""

    #: The applier changed the environment to the declared state.
    APPLIED = "applied"
    #: The environment already carried the declared state; nothing changed.
    ALREADY_SATISFIED = "already_satisfied"
    #: The applier could not make the change. The sequence stops here.
    FAILED = "failed"
    #: An earlier change failed, so this one was never attempted.
    NOT_ATTEMPTED = "not_attempted"


class ApplyStatus(Enum):
    """Terminal status of a whole apply."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAIL = "fail"


#: Entry kinds whose application removes something from the environment. A
#: surface the playbook forbids is not narrowed, it is taken away, so it is
#: gated by its own approval rather than by the ordinary apply path.
REMOVAL_KINDS = frozenset({PlanEntryKind.FORBIDDEN})


class GovernApplyRefused(RuntimeError):
    """Raised before any change is attempted when the plan may not be applied."""


class GovernApplyReceiptError(AuditReceiptError):
    """Raised when the executed range cannot be projected into a receipt."""


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    """What an applier reports about one attempted change."""

    status: ChangeStatus
    detail: str = ""


class ChangeApplier(Protocol):
    """Executes one plan entry against the environment.

    An applier owns all environment knowledge: it decides what the entry means
    for its surface class, and it reports
    :attr:`ChangeStatus.ALREADY_SATISFIED` when the declared state is already
    in place, which is what makes a re-apply a no-op.
    """

    def __call__(self, entry: PlanEntry) -> ChangeOutcome:
        """Return the outcome of applying *entry*."""
        ...


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """One recorded change: what was attempted and what came of it."""

    sequence: int
    surface: str
    kind: str
    status: ChangeStatus
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "detail": self.detail,
            "kind": self.kind,
            "sequence": self.sequence,
            "status": self.status.value,
            "surface": self.surface,
        }


@dataclass(frozen=True, slots=True)
class GovernApplyRecord:
    """The apply record: the executed change set and the receipt binding it."""

    apply_id: str
    plan_digest: str
    playbook_digest: str
    environment_digest: str
    plan_journal_entry_hash: str
    approver: str
    status: ApplyStatus
    results: tuple[ChangeResult, ...]
    receipt: AuditReceipt

    @property
    def last_applied_surface(self) -> str | None:
        """Return the surface of the last change that actually applied."""
        return _last_applied_surface(self.results)


@dataclass(frozen=True, slots=True)
class GovernApplyProjectionVerification:
    """Result of recomputing an apply projection from its retained events."""

    ok: bool
    apply_id: str
    status: ApplyStatus
    errors: tuple[str, ...]


def compute_apply_id(
    *,
    plan_digest: str,
    playbook_digest: str,
    environment_digest: str,
    approver: str,
) -> str:
    """Return the identifier threading plan, apply record and receipt.

    A pure function of the four bound values, so the same plan approved by the
    same identity against the same world always carries the same apply id --
    which is what lets a re-apply be recognised as the same decision rather
    than a new one.
    """
    material = canonical_digest(
        {
            "approver": approver,
            "environment_digest": environment_digest,
            "playbook_digest": playbook_digest,
            "plan_digest": plan_digest,
        }
    ).encode("utf-8")
    return "apply:" + hashlib.sha256(material).hexdigest()[:32]


def derive_apply_status(results: Sequence[ChangeResult]) -> ApplyStatus:
    """Derive the terminal status from the recorded outcomes.

    All-must-pass is the default: any failure denies ``success``. ``partial``
    is reserved for the case an operator has to act on differently -- the
    environment moved part of the way to the declared state and stopped.
    """
    applied = sum(1 for r in results if r.status is ChangeStatus.APPLIED)
    failed = sum(1 for r in results if r.status is ChangeStatus.FAILED)
    if failed and applied:
        return ApplyStatus.PARTIAL
    if failed:
        return ApplyStatus.FAIL
    return ApplyStatus.SUCCESS


def _last_applied_surface(results: Sequence[ChangeResult]) -> str | None:
    applied = [r.surface for r in results if r.status is ChangeStatus.APPLIED]
    return applied[-1] if applied else None


def validate_apply(
    *,
    plan: GovernPlan,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
    spine: LineageSpine,
    removal_approval: ApprovalStatus | None,
) -> None:
    """Validate the whole diff. Raise :class:`GovernApplyRefused` if it may not run.

    Every check runs before the first change is attempted, so a refusal leaves
    the environment exactly as it was.
    """
    _refuse_unless_anchored(plan, spine)
    _refuse_unless_environment_matches(plan, playbook, inventory)
    _refuse_duplicate_surfaces(plan)
    _refuse_unread_surfaces(plan)
    _refuse_unapproved_removals(plan, removal_approval)


def _refuse_unless_anchored(plan: GovernPlan, spine: LineageSpine) -> None:
    anchor = plan.journal_entry_hash.strip()
    if not anchor:
        raise GovernApplyRefused("plan names no decision record; a diff absent from the journal cannot be applied")
    for entry in spine.iter_entries():
        if entry.entry_hash != anchor:
            continue
        expected = content_hash_of(replace(plan, journal_entry_hash="").to_canonical_bytes())
        if entry.content_hash != expected:
            raise GovernApplyRefused(
                f"plan bytes do not match journal entry {anchor}: recorded {entry.content_hash}, presented {expected}"
            )
        return
    raise GovernApplyRefused(f"decision record {anchor} is absent from the journal")


def _refuse_unless_environment_matches(
    plan: GovernPlan,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    observed = compute_inputs_hash(playbook=playbook, inventory=inventory)
    if observed != plan.inputs_hash:
        raise GovernApplyRefused(
            "environment digest moved since the plan was reviewed: "
            f"plan was reviewed against {plan.inputs_hash}, environment is now {observed}"
        )


def _refuse_duplicate_surfaces(plan: GovernPlan) -> None:
    seen: set[str] = set()
    for entry in plan.entries:
        if entry.surface in seen:
            raise GovernApplyRefused(f"plan names surface {entry.surface!r} more than once")
        seen.add(entry.surface)


def _refuse_unread_surfaces(plan: GovernPlan) -> None:
    unread = sorted(e.surface for e in plan.entries if e.kind is PlanEntryKind.UNKNOWN)
    if unread:
        raise GovernApplyRefused(
            f"plan changes surfaces the inventory could not read: {unread}; "
            "an unread surface can be neither judged compliant nor mutated"
        )


def _refuse_unapproved_removals(plan: GovernPlan, removal_approval: ApprovalStatus | None) -> None:
    removals = sorted(e.surface for e in plan.entries if e.kind in REMOVAL_KINDS)
    if not removals:
        return
    if removal_approval is None or not removal_approval.is_approved:
        raise GovernApplyRefused(
            f"plan removes {removals}; removal is a separate approval class and its gate is not satisfied"
        )


def _run_change_set(plan: GovernPlan, applier: ChangeApplier) -> list[tuple[PlanEntry, ChangeResult]]:
    """Execute the entries in plan order, stopping at the first failure."""
    recorded: list[tuple[PlanEntry, ChangeResult]] = []
    stopped = False
    for index, entry in enumerate(plan.entries):
        if stopped:
            outcome = ChangeOutcome(ChangeStatus.NOT_ATTEMPTED, "an earlier change failed; sequence stopped")
        else:
            try:
                outcome = applier(entry)
            except Exception as exc:
                outcome = ChangeOutcome(ChangeStatus.FAILED, f"{type(exc).__name__}: {exc}")
            if outcome.status is ChangeStatus.FAILED:
                stopped = True
        recorded.append(
            (
                entry,
                ChangeResult(
                    sequence=index,
                    surface=entry.surface,
                    kind=entry.kind.value,
                    status=outcome.status,
                    detail=outcome.detail,
                ),
            )
        )
    return recorded


def _index_of_hmac(events: Sequence[Mapping[str, Any]], hmac: str) -> int:
    matches = [index for index, event in enumerate(events) if event.get("hmac") == hmac]
    if len(matches) != 1:
        raise GovernApplyReceiptError(f"apply boundary {hmac!r} was not found exactly once in the audit chain")
    return matches[0]


def _safe_apply_name(apply_id: str) -> str:
    return apply_id.replace(":", "-")


def apply_plan(
    *,
    plan: GovernPlan,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
    approver: str,
    applier: ChangeApplier,
    audit_dir: Path,
    key: bytes,
    kms_adapter: KMSAdapter,
    spine: LineageSpine,
    removal_approval: ApprovalStatus | None = None,
    formats: tuple[str, ...] | list[str] = ALL_FORMATS,
    output_dir: Path | None = None,
    write: bool = True,
) -> GovernApplyRecord:
    """Execute *plan* and return the record whose receipt binds it to its review.

    Args:
        plan: The reviewed plan, carrying the journal entry hash it was
            anchored as.
        playbook: The declared posture in force at apply time.
        inventory: The environment as inventoried at apply time.
        approver: Identity that approved this apply.
        applier: Executes one entry; see :class:`ChangeApplier`.
        audit_dir: Audit-chain directory the apply events are appended to.
        key: Audit-chain HMAC key.
        kms_adapter: Signer for the emitted receipt.
        spine: Lineage journal the plan's decision record must be present in.
        removal_approval: Approval status covering removal-class entries.
        formats: Receipt formats to emit.
        output_dir: Directory the receipt is written to.
        write: Whether to materialise the receipt on disk.

    Raises:
        GovernApplyRefused: The plan may not be applied. Nothing was attempted.
        GovernApplyReceiptError: The executed range could not be projected.
    """
    resolved_approver = approver.strip()
    if not resolved_approver:
        raise ValueError("approver must not be empty")

    validate_apply(
        plan=plan,
        playbook=playbook,
        inventory=inventory,
        spine=spine,
        removal_approval=removal_approval,
    )

    plan_digest = canonical_digest(plan.to_dict())
    playbook_digest = canonical_digest(playbook)
    environment_digest = canonical_digest(inventory)
    apply_id = compute_apply_id(
        plan_digest=plan_digest,
        playbook_digest=playbook_digest,
        environment_digest=environment_digest,
        approver=resolved_approver,
    )
    removal_approval_id = (
        removal_approval.request.request_id if removal_approval is not None and removal_approval.is_approved else None
    )

    chain = AuditChainStore(audit_dir, key=key)
    opened = chain.log(
        event_type=EVENT_GOVERN_APPLY_OPENED,
        actor=GOVERN_APPLY_ACTOR,
        resource_type=_RESOURCE_TYPE,
        resource_id=apply_id,
        details={
            "apply_id": apply_id,
            "approver": resolved_approver,
            "change_count": len(plan.entries),
            "environment_digest": environment_digest,
            "plan_digest": plan_digest,
            "plan_journal_entry_hash": plan.journal_entry_hash,
            "playbook_digest": playbook_digest,
            "removal_approval_id": removal_approval_id,
        },
    )

    recorded = _run_change_set(plan, applier)
    for _entry, result in recorded:
        chain.log(
            event_type=EVENT_GOVERN_APPLY_CHANGE,
            actor=GOVERN_APPLY_ACTOR,
            resource_type=_RESOURCE_TYPE,
            resource_id=apply_id,
            details={"apply_id": apply_id, **result.to_dict()},
        )

    results = tuple(result for _entry, result in recorded)
    status = derive_apply_status(results)
    closed = chain.log(
        event_type=EVENT_GOVERN_APPLY_CLOSED,
        actor=GOVERN_APPLY_ACTOR,
        resource_type=_RESOURCE_TYPE,
        resource_id=apply_id,
        details={
            "apply_id": apply_id,
            "applied_count": sum(1 for r in results if r.status is ChangeStatus.APPLIED),
            "failed_count": sum(1 for r in results if r.status is ChangeStatus.FAILED),
            "last_applied_surface": _last_applied_surface(results),
            "not_attempted_count": sum(1 for r in results if r.status is ChangeStatus.NOT_ATTEMPTED),
            "status": status.value,
        },
    )

    receipt = _materialize_apply_receipt(
        audit_dir,
        key=key,
        kms_adapter=kms_adapter,
        opened_hmac=str(opened.hmac),
        closed_hmac=str(closed.hmac),
        projection=_projection(
            apply_id=apply_id,
            plan=plan,
            plan_digest=plan_digest,
            playbook_digest=playbook_digest,
            environment_digest=environment_digest,
            approver=resolved_approver,
            removal_approval_id=removal_approval_id,
            status=status,
            results=results,
        ),
        apply_id=apply_id,
        plan_digest=plan_digest,
        formats=formats,
        output_dir=output_dir,
        write=write,
    )

    return GovernApplyRecord(
        apply_id=apply_id,
        plan_digest=plan_digest,
        playbook_digest=playbook_digest,
        environment_digest=environment_digest,
        plan_journal_entry_hash=plan.journal_entry_hash,
        approver=resolved_approver,
        status=status,
        results=results,
        receipt=receipt,
    )


def _projection(
    *,
    apply_id: str,
    plan: GovernPlan,
    plan_digest: str,
    playbook_digest: str,
    environment_digest: str,
    approver: str,
    removal_approval_id: str | None,
    status: ApplyStatus,
    results: Sequence[ChangeResult],
) -> dict[str, Any]:
    return {
        "schema_version": GOVERN_APPLY_SCHEMA_VERSION,
        "apply_id": apply_id,
        "approver": approver,
        "applied_count": sum(1 for r in results if r.status is ChangeStatus.APPLIED),
        "changes": [r.to_dict() for r in results],
        "environment_digest": environment_digest,
        "failed_count": sum(1 for r in results if r.status is ChangeStatus.FAILED),
        "last_applied_surface": _last_applied_surface(results),
        "not_attempted_count": sum(1 for r in results if r.status is ChangeStatus.NOT_ATTEMPTED),
        "plan_digest": plan_digest,
        "plan_journal_entry_hash": plan.journal_entry_hash,
        "playbook_digest": playbook_digest,
        "removal_approval_id": removal_approval_id,
        "status": status.value,
    }


def _materialize_apply_receipt(
    audit_dir: Path,
    *,
    key: bytes,
    kms_adapter: KMSAdapter,
    opened_hmac: str,
    closed_hmac: str,
    projection: dict[str, Any],
    apply_id: str,
    plan_digest: str,
    formats: tuple[str, ...] | list[str],
    output_dir: Path | None,
    write: bool,
) -> AuditReceipt:
    chain = AuditChainStore(audit_dir, key=key)
    with chain.chain_transaction():
        ok, errors, rows = chain.verify_and_query(include_archived=True)
    if not ok:
        raise GovernApplyReceiptError(f"source audit chain verification failed: {'; '.join(errors[:3])}")

    events = [asdict(row) for row in rows]
    start = _index_of_hmac(events, opened_hmac)
    end = _index_of_hmac(events, closed_hmac)
    if end < start:
        raise GovernApplyReceiptError("apply closure precedes the apply anchor in the audit chain")
    retained = events[start : end + 1]
    rebuilt, head_hmac, head_sha256 = rebuild_receipt_range(retained, key)

    return materialize_receipt(
        audit_dir,
        since=str(retained[0].get("timestamp", "")),
        until=str(retained[-1].get("timestamp", "")),
        rebuilt=rebuilt,
        head_hmac=head_hmac,
        head_sha256=head_sha256,
        kms_adapter=kms_adapter,
        requested=tuple(dict.fromkeys(formats)),
        subject_name=f"govern-apply:{apply_id}:{plan_digest}",
        online_rekor=False,
        output_dir=output_dir,
        write=write,
        receipt_type=GOVERN_APPLY_RECEIPT_TYPE,
        predicate_kind="govern-apply-receipt",
        predicate_extra={"govern_apply": projection},
        range_extra={
            "selection": "authenticated-chain-position",
            "source_start_hmac": opened_hmac,
            "source_end_hmac": closed_hmac,
        },
        receipt_extra={"govern_apply": projection},
        filename_prefix=f"govern-apply-{_safe_apply_name(apply_id)}",
    )


def _details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    details = event.get("details")
    return cast("Mapping[str, Any]", details) if isinstance(details, Mapping) else {}


def _source_hmac(event: Mapping[str, Any]) -> str:
    witnessed = _details(event).get("_original_hmac")
    return str(witnessed if witnessed is not None else event.get("hmac", "")).strip()


def _receipt_events(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = receipt.get("events")
    if not isinstance(raw, list):
        return []
    return [cast("dict[str, Any]", e) for e in cast("list[object]", raw) if isinstance(e, dict)]


def _recomputed_changes(events: Sequence[Mapping[str, Any]]) -> list[ChangeResult]:
    changes: list[ChangeResult] = []
    for event in events:
        if event.get("event_type") != EVENT_GOVERN_APPLY_CHANGE:
            continue
        details = _details(event)
        try:
            status = ChangeStatus(str(details.get("status", "")))
        except ValueError:
            continue
        changes.append(
            ChangeResult(
                sequence=int(details.get("sequence", -1)),
                surface=str(details.get("surface", "")),
                kind=str(details.get("kind", "")),
                status=status,
                detail=str(details.get("detail", "")),
            )
        )
    return changes


def _mapping(source: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    """Return ``source[field_name]`` when it is a mapping, else an empty mapping."""
    value = source.get(field_name)
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


def _check_range_binding(
    receipt: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    errors: list[str],
) -> None:
    recomputed_head = receipt_events_head([dict(e) for e in events])
    subject_head = str(_mapping(_mapping(receipt, "subject"), "digest").get("sha256", ""))
    range_value = receipt.get("range")
    range_block = cast("Mapping[str, Any]", range_value) if isinstance(range_value, Mapping) else None
    range_head = str(range_block.get("head_sha256", "")) if range_block is not None else ""
    if recomputed_head != subject_head or recomputed_head != range_head:
        errors.append("retained range does not match the signed subject head")
    if range_block is None or range_block.get("selection") != "authenticated-chain-position":
        errors.append("receipt range is not selected by authenticated chain position")
    elif range_block.get("source_start_hmac") != _source_hmac(events[0]) or range_block.get(
        "source_end_hmac"
    ) != _source_hmac(events[-1]):
        errors.append("receipt range boundary does not match the retained source witnesses")


def verify_govern_apply_projection(
    receipt: Mapping[str, Any],
) -> GovernApplyProjectionVerification:
    """Recompute the apply projection from the receipt's own retained events.

    Signature and signer trust remain the standalone audit-receipt verifier's
    job. This check answers the question that verifier cannot: does the stated
    outcome follow from the evidence the receipt carries? An edited status,
    change list, count, or bound digest fails here even when the signature is
    intact for the edited document.
    """
    errors: list[str] = []
    events = _receipt_events(receipt)
    if not events:
        return GovernApplyProjectionVerification(
            False, "", ApplyStatus.FAIL, ("receipt carries no retained event range",)
        )

    _check_range_binding(receipt, events, errors)

    opened, closed = events[0], events[-1]
    if opened.get("event_type") != EVENT_GOVERN_APPLY_OPENED:
        errors.append("retained range does not begin at a govern apply anchor")
    if closed.get("event_type") != EVENT_GOVERN_APPLY_CLOSED:
        errors.append("retained range does not end at a govern apply closure")

    opened_details = _details(opened)
    apply_id = str(opened_details.get("apply_id", "")).strip()
    if not apply_id:
        errors.append("apply anchor does not name an apply")
    if any(str(e.get("resource_id", "")) != apply_id for e in events):
        errors.append("retained range contains events from another apply")

    changes = _recomputed_changes(events)
    status = derive_apply_status(changes)

    projection_value = receipt.get("govern_apply")
    if not isinstance(projection_value, Mapping):
        errors.append("govern_apply projection is missing")
        return GovernApplyProjectionVerification(False, apply_id, status, tuple(errors))
    projection = cast("Mapping[str, Any]", projection_value)

    if projection.get("status") != status.value:
        errors.append("serialized status was not derived from the retained change evidence")
    if [c.to_dict() for c in changes] != projection.get("changes"):
        errors.append("serialized change list does not match the retained change evidence")
    if projection.get("last_applied_surface") != _last_applied_surface(changes):
        errors.append("serialized last applied surface does not match the retained change evidence")
    for field_name, expected in (
        ("applied_count", sum(1 for c in changes if c.status is ChangeStatus.APPLIED)),
        ("failed_count", sum(1 for c in changes if c.status is ChangeStatus.FAILED)),
        ("not_attempted_count", sum(1 for c in changes if c.status is ChangeStatus.NOT_ATTEMPTED)),
    ):
        if projection.get(field_name) != expected:
            errors.append(f"serialized {field_name} does not match the retained change evidence")
    for field_name in (
        "apply_id",
        "approver",
        "environment_digest",
        "plan_digest",
        "plan_journal_entry_hash",
        "playbook_digest",
        "removal_approval_id",
    ):
        if projection.get(field_name) != opened_details.get(field_name):
            errors.append(f"serialized {field_name} does not match the apply anchor")

    return GovernApplyProjectionVerification(
        ok=not errors,
        apply_id=apply_id,
        status=status,
        errors=tuple(errors),
    )


__all__ = [
    "EVENT_GOVERN_APPLY_CHANGE",
    "EVENT_GOVERN_APPLY_CLOSED",
    "EVENT_GOVERN_APPLY_OPENED",
    "GOVERN_APPLY_ACTOR",
    "GOVERN_APPLY_RECEIPT_TYPE",
    "GOVERN_APPLY_SCHEMA_VERSION",
    "REMOVAL_KINDS",
    "ApplyStatus",
    "ChangeApplier",
    "ChangeOutcome",
    "ChangeResult",
    "ChangeStatus",
    "GovernApplyProjectionVerification",
    "GovernApplyReceiptError",
    "GovernApplyRecord",
    "GovernApplyRefused",
    "apply_plan",
    "compute_apply_id",
    "derive_apply_status",
    "validate_apply",
    "verify_govern_apply_projection",
]
