"""Inverse plan construction: undo an apply from its receipt, not from the environment.

A dry-run diff says what an apply will do; the value that was there before the
change is what makes the apply undoable. :class:`~bernstein.core.security.change_receipt.ChangeAttempt`
records that value at apply time, so the plan that undoes an apply is a
projection of the receipt an auditor already holds.

:func:`build_restore_plan` reads every restore value out of the receipt. The
observation map it takes is used for exactly one thing: deciding whether the
target still holds the value the apply wrote. It can never contribute a value
to the plan, which is what keeps the plan replayable -- two operators building
a restore from the same receipt get byte-identical plans regardless of what the
environment looks like when they run it.

Refusals are per entry and fail closed:

* the target holds something other than what the apply wrote -- it drifted, and
  restoring the prior value would silently discard whatever changed it;
* the target is absent from the observation map -- it could not be read, so the
  absence of drift is unproven.

Either refusal is lifted for a named entry by listing its change id in
``forced_entry_ids`` (the ``--force-entry`` operator escape). Forcing is
recorded on the entry, so a forced restore is distinguishable from an
unforced one in the resulting record.

Entries the receipt does not record as applied are not inverted at all: a
change that was skipped or failed left nothing to undo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.security.change_receipt import ChangeReceipt

#: Refusal reason: the target no longer holds the value the apply wrote.
RESTORE_REASON_DRIFTED: str = "target-drifted-since-apply"

#: Refusal reason: the target could not be read, so drift cannot be ruled out.
RESTORE_REASON_UNOBSERVABLE: str = "target-not-observable"

#: The change type that undoes each applied change type.
_INVERSE_CHANGE_TYPE: dict[str, str] = {
    "create": "delete",
    "delete": "create",
    "update": "update",
}


@dataclass(frozen=True, slots=True)
class RestoreEntry:
    """One change to write back, taken verbatim from the apply record.

    Attributes:
        change_id: The change id of the applied change this entry inverts.
        change_type: The change type that undoes the applied one: a 'create'
            is undone by a 'delete', a 'delete' by a 'create', an 'update' by
            an 'update'. An unrecognised applied type is inverted as an
            'update', which writes the prior value back.
        target: Resource identifier, copied from the applied change.
        restore_value: The value to write, copied from the applied change's
            ``prior_value``. Never derived from the environment.
        forced: True when this entry was refused on drift or on an unreadable
            target and an operator overrode the refusal.
    """

    change_id: str
    change_type: str
    target: str
    restore_value: str
    forced: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "target": self.target,
            "restore_value": self.restore_value,
            "forced": self.forced,
        }


@dataclass(frozen=True, slots=True)
class RestoreRefusal:
    """One applied change that was not inverted, and why.

    Attributes:
        change_id: The change id of the applied change that was refused.
        target: Resource identifier, copied from the applied change.
        reason: :data:`RESTORE_REASON_DRIFTED` or
            :data:`RESTORE_REASON_UNOBSERVABLE`.
        expected_value: The value the apply wrote, i.e. what the target would
            hold had nothing touched it since.
        observed_value: What the target holds now; empty when it could not be
            read at all.
    """

    change_id: str
    target: str
    reason: str
    expected_value: str
    observed_value: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "change_id": self.change_id,
            "target": self.target,
            "reason": self.reason,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
        }


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """The inverse of one apply record: what to write back, and what was refused.

    Attributes:
        original_receipt_digest: The digest of the apply record this plan
            inverts. The digest is the apply record's identity, so a plan can
            be tied back to its receipt with no index in between.
        plan_id: Identifier for this restore, derived from the original plan
            id so the same receipt always yields the same plan id.
        entries: Changes to write back, in the reverse of the order they were
            applied.
        refusals: Applied changes that were not inverted, with the reason.
    """

    original_receipt_digest: str
    plan_id: str
    entries: tuple[RestoreEntry, ...] = ()
    refusals: tuple[RestoreRefusal, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "original_receipt_digest": self.original_receipt_digest,
            "plan_id": self.plan_id,
            "entries": [e.to_dict() for e in self.entries],
            "refusals": [r.to_dict() for r in self.refusals],
        }


def build_restore_plan(
    *,
    receipt: ChangeReceipt,
    observed: Mapping[str, str],
    forced_entry_ids: Iterable[str] = (),
) -> RestorePlan:
    """Build the plan that undoes *receipt*, reading every value off the receipt.

    Args:
        receipt: The apply record to invert. Only entries whose outcome is
            ``'success'`` are inverted; a skipped or failed change left
            nothing to undo.
        observed: What each target holds now, keyed by target. Used only to
            detect drift -- no value from this mapping ever reaches the plan.
            A target missing from the mapping could not be read.
        forced_entry_ids: Change ids whose refusal an operator has overridden.

    Returns:
        A :class:`RestorePlan` whose entries are in the reverse of the applied
        order, plus one refusal per entry that was held back.
    """
    forced = frozenset(forced_entry_ids)
    entries: list[RestoreEntry] = []
    refusals: list[RestoreRefusal] = []

    for change in reversed(receipt.changes):
        if change.outcome != "success":
            continue

        is_forced = change.change_id in forced
        if not is_forced:
            if change.target not in observed:
                refusals.append(
                    RestoreRefusal(
                        change_id=change.change_id,
                        target=change.target,
                        reason=RESTORE_REASON_UNOBSERVABLE,
                        expected_value=change.written_value,
                        observed_value="",
                    ),
                )
                continue
            current = observed[change.target]
            if current != change.written_value:
                refusals.append(
                    RestoreRefusal(
                        change_id=change.change_id,
                        target=change.target,
                        reason=RESTORE_REASON_DRIFTED,
                        expected_value=change.written_value,
                        observed_value=current,
                    ),
                )
                continue

        entries.append(
            RestoreEntry(
                change_id=change.change_id,
                change_type=_INVERSE_CHANGE_TYPE.get(change.change_type, "update"),
                target=change.target,
                restore_value=change.prior_value,
                forced=is_forced,
            ),
        )

    return RestorePlan(
        original_receipt_digest=receipt.digest,
        plan_id=f"restore-{receipt.plan_id}",
        entries=tuple(entries),
        refusals=tuple(refusals),
    )


__all__ = [
    "RESTORE_REASON_DRIFTED",
    "RESTORE_REASON_UNOBSERVABLE",
    "RestoreEntry",
    "RestorePlan",
    "RestoreRefusal",
    "build_restore_plan",
]
