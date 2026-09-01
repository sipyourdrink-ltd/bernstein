"""Model-admission registry projected from the audit chain (issue #5038).

An installation needs to be able to say which models it is permitted to use,
and -- months later -- to prove what was permitted at the moment a particular
artefact was produced. A list in a configuration file answers only the first
question: editing it destroys the previous answer, so "was this model allowed
on the 12th?" has nowhere to be read from.

So admission is not a row that gets edited. Admitting a model appends a
``model.admitted`` event to the HMAC-chained audit log; withdrawing it appends
a ``model.withdrawn`` event. The registry is the *replay* of that log at a
named instant -- recomputed, never stored, exactly as
:func:`bernstein.core.lineage.activity.active_set` recomputes the active set
from the lineage ledger. Two readers holding the same log and the same instant
get byte-identical state.

The posture is fail-closed throughout, matching ``active_set``:

* A reference the log never admitted is not admitted -- silence is refusal.
* A reference presents both the model that was *asked for* and the model the
  provider *reported* (issue #5037). Both identities must be admitted, or the
  reference is refused: a provider substituting an unadmitted model is the
  case the registry exists to catch.
* An admission lapses at its expiry with no event written at expiry time, so a
  registry nobody maintains decays to permitting nothing.
* A chain that does not verify yields no registry at all, rather than a
  registry assembled from rows that could have been rewritten.

This module is slice 1 and 2 of the issue: the event shapes, the projection
and reconstruction at a past instant. It reads the chain; nothing in the
routing path consults it yet, and admission evidence is recorded but not
re-verified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.audit_chain import (
    EVENT_MODEL_ADMITTED,
    EVENT_MODEL_WITHDRAWN,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from bernstein.core.lineage.entry import ModelRef
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

#: Schema version of the projected state's canonical form. Bumped only when
#: the canonical bytes change shape, so a stored digest keeps its meaning.
MODEL_REGISTRY_PROJECTION_VERSION = 1

#: Task class standing for "every task class". An admission naming it covers
#: any class; it has to be written explicitly, so a blanket admission is a
#: deliberate operator act rather than the default.
ANY_TASK_CLASS = "*"

#: Version component of a model key when the admission (or the reference)
#: pins no snapshot.
_UNPINNED_VERSION = "*"

#: The audit log's timestamp format (``AuditLog.log`` writes exactly this).
#: Fixed width and UTC, so lexicographic order is chronological order and the
#: projection compares instants without parsing dates.
_TIMESTAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ModelRegistryError(Exception):
    """The registry could not be projected from the log it was given."""


def format_timestamp(when: datetime) -> str:
    """Render *when* in the audit log's timestamp format.

    Args:
        when: A timezone-aware instant. Naive input is refused rather than
            assumed to be UTC -- an admission expiring at the wrong hour
            because a local clock was read as UTC is a governance defect.

    Returns:
        The UTC timestamp in ``%Y-%m-%dT%H:%M:%S.%fZ`` form.

    Raises:
        ValueError: If *when* carries no timezone.
    """
    if when.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return when.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _require_timestamp(value: str, field: str) -> str:
    if not _TIMESTAMP_RE.match(value):
        raise ValueError(f"{field} must be a UTC timestamp of the form 2026-09-02T00:00:00.000000Z, got {value!r}")
    return value


def _require_text(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def model_key(provider: str, model: str, version: str | None = None) -> str:
    """Return the canonical key an admission and a reference are matched on.

    The key is ``provider/model@version``, with ``@*`` when no snapshot is
    pinned. ``provider`` may not contain ``/`` so the encoding stays
    unambiguous for model names that do (``meta-llama/Llama-3``).

    Args:
        provider: Provider identifier, as on :class:`ModelRef`.
        model: Model name.
        version: Provider snapshot/revision, or ``None`` for unpinned.

    Returns:
        The canonical model key.

    Raises:
        ValueError: On empty parts or a provider containing ``/``.
    """
    _require_text(provider, "provider")
    _require_text(model, "model")
    if "/" in provider:
        raise ValueError(f"provider must not contain '/', got {provider!r}")
    if version is not None:
        _require_text(version, "version")
    return f"{provider}/{model}@{version or _UNPINNED_VERSION}"


def _identity_keys(provider: str, model: str, version: str | None) -> tuple[str, ...]:
    """The admission keys that would cover one (provider, model, version).

    An unpinned admission (``@*``) is an operator saying "this model, any
    snapshot", so it covers a pinned reference too. A pinned admission covers
    only its own snapshot -- and covers no unpinned reference, because a
    reference that names no snapshot cannot be shown to be the admitted one.
    """
    exact = model_key(provider, model, version)
    unpinned = model_key(provider, model, None)
    return (exact,) if exact == unpinned else (exact, unpinned)


def _presented_identities(ref: ModelRef) -> tuple[tuple[str, ...], ...]:
    """The identities *ref* presents, each as its covering key set.

    A reference carries the model that was requested and, when the provider
    said so, the model it reported. They can differ; both are identities this
    installation ended up using, so both have to be admitted.
    """
    names = [ref.model_requested]
    if ref.model_reported and ref.model_reported != ref.model_requested:
        names.append(ref.model_reported)
    return tuple(_identity_keys(ref.provider, name, ref.version) for name in names)


@dataclass(frozen=True, slots=True)
class ModelAdmission:
    """One live admission as projected from the log.

    Attributes:
        model_key: Canonical key the admission was written for.
        provider: Provider identifier.
        model: Model name.
        version: Pinned snapshot, or ``None`` when the admission is unpinned.
        task_classes: Sorted, deduplicated task classes the admission covers.
            ``("*",)`` covers every class.
        admitted_by: Identity that admitted the model.
        admitted_at: Chain timestamp of the admitting event.
        expires_at: Instant the admission lapses at (exclusive).
        evidence_ref: Reference to the evidence the operator relied on, or
            ``""``. Recorded, not yet re-verified.
        event_hmac: HMAC of the chain event this row was projected from, so a
            row can be traced back to the record that produced it.
    """

    model_key: str
    provider: str
    model: str
    version: str | None
    task_classes: tuple[str, ...]
    admitted_by: str
    admitted_at: str
    expires_at: str
    evidence_ref: str
    event_hmac: str

    def covers(self, task_class: str) -> bool:
        """Whether this admission covers *task_class*."""
        return ANY_TASK_CLASS in self.task_classes or task_class in self.task_classes

    def to_dict(self) -> dict[str, Any]:
        """Return the row's canonical mapping."""
        return {
            "model_key": self.model_key,
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "task_classes": list(self.task_classes),
            "admitted_by": self.admitted_by,
            "admitted_at": self.admitted_at,
            "expires_at": self.expires_at,
            "evidence_ref": self.evidence_ref,
            "event_hmac": self.event_hmac,
        }


@dataclass(frozen=True, slots=True)
class ModelRegistryState:
    """What the log says was admitted at one named instant.

    Attributes:
        at: The instant the projection was taken at.
        admissions: Live admissions, ordered by ``(model_key, event_hmac)``.
    """

    at: str
    admissions: tuple[ModelAdmission, ...]

    def admission_for(self, key: str) -> ModelAdmission | None:
        """Return the live admission for *key*, or ``None``."""
        for admission in self.admissions:
            if admission.model_key == key:
                return admission
        return None

    def canonical_bytes(self) -> bytes:
        """Return the JCS-canonical bytes of this state.

        Two readers replaying the same log at the same instant produce these
        bytes identically, which is what makes the state hashable, comparable
        and quotable in a later dispute.
        """
        return json.dumps(
            {
                "v": MODEL_REGISTRY_PROJECTION_VERSION,
                "at": self.at,
                "admissions": [a.to_dict() for a in self.admissions],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def record_model_admission(
    *,
    chain: AuditChainStore,
    provider: str,
    model: str,
    version: str | None,
    task_classes: Sequence[str],
    admitted_by: str,
    expires_at: str,
    evidence_ref: str = "",
) -> AuditEvent:
    """Append a ``model.admitted`` event into *chain*.

    Args:
        chain: The audit chain store accepting the entry.
        provider: Provider identifier, as on :class:`ModelRef`.
        model: Model name being admitted.
        version: Provider snapshot the admission pins, or ``None`` to admit
            every snapshot of the model.
        task_classes: Task classes the admission covers; ``("*",)`` covers
            every class. Must name at least one.
        admitted_by: Identity of the operator admitting the model. Admission
            is a human act and the record says whose.
        expires_at: Instant the admission lapses at, in the audit log's
            timestamp format (see :func:`format_timestamp`). Required: an
            admission that never lapses cannot be distinguished from one
            nobody has reviewed since.
        evidence_ref: Optional reference to the evidence relied on.

    Returns:
        The recorded :class:`AuditEvent`, whose details carry every input plus
        ``prev_chain_digest``.

    Raises:
        ValueError: On an empty part, an empty task-class set or a malformed
            ``expires_at``.
    """
    key = model_key(provider, model, version)
    classes = _normalise_task_classes(task_classes)
    _require_text(admitted_by, "admitted_by")
    _require_timestamp(expires_at, "expires_at")
    return chain.log_with_prev_digest(
        event_type=EVENT_MODEL_ADMITTED,
        actor=admitted_by,
        resource_type="model_admission",
        resource_id=key,
        details={
            "model_key": key,
            "provider": provider,
            "model": model,
            "version": version,
            "task_classes": list(classes),
            "admitted_by": admitted_by,
            "expires_at": expires_at,
            "evidence_ref": evidence_ref,
        },
    )


def record_model_withdrawal(
    *,
    chain: AuditChainStore,
    provider: str,
    model: str,
    version: str | None,
    withdrawn_by: str,
    reason: str,
) -> AuditEvent:
    """Append a ``model.withdrawn`` event into *chain*.

    The withdrawal appends; the admission it supersedes stays in the log
    unchanged, so the state that held before the withdrawal is still
    reconstructible.

    Args:
        chain: The audit chain store accepting the entry.
        provider: Provider identifier.
        model: Model name being withdrawn.
        version: Snapshot the withdrawn admission pinned, or ``None``.
        withdrawn_by: Identity of the operator withdrawing the model.
        reason: Why it was withdrawn. Recorded so a later reader does not
            have to reconstruct the motive from surrounding events.

    Returns:
        The recorded :class:`AuditEvent`.

    Raises:
        ValueError: On an empty part.
    """
    key = model_key(provider, model, version)
    _require_text(withdrawn_by, "withdrawn_by")
    _require_text(reason, "reason")
    return chain.log_with_prev_digest(
        event_type=EVENT_MODEL_WITHDRAWN,
        actor=withdrawn_by,
        resource_type="model_admission",
        resource_id=key,
        details={
            "model_key": key,
            "provider": provider,
            "model": model,
            "version": version,
            "withdrawn_by": withdrawn_by,
            "reason": reason,
        },
    )


def _normalise_task_classes(task_classes: Sequence[str]) -> tuple[str, ...]:
    classes = tuple(sorted({_require_text(c, "task_class") for c in task_classes}))
    if not classes:
        raise ValueError("an admission must name at least one task class")
    return classes


# ---------------------------------------------------------------------------
# Reading and projecting
# ---------------------------------------------------------------------------


def load_registry_events(chain: AuditChainStore) -> list[AuditEvent]:
    """Return the registry events of *chain*, in chain order.

    The chain is verified and read under one snapshot, and an unverifiable
    chain raises rather than returning the rows it holds: a registry built
    from records that may have been rewritten answers the one question it
    exists to answer with an answer nobody can rely on.

    Args:
        chain: The audit chain store to read.

    Returns:
        Every ``model.admitted`` and ``model.withdrawn`` event, in the order
        the log holds them.

    Raises:
        ModelRegistryError: If the chain does not verify.
    """
    ok, errors, events = chain.verify_and_query()
    if not ok:
        raise ModelRegistryError(f"audit chain does not verify; refusing to project a model registry: {errors[:3]}")
    return [e for e in events if e.event_type in (EVENT_MODEL_ADMITTED, EVENT_MODEL_WITHDRAWN)]


def _order(event: AuditEvent) -> tuple[str, str, str, str]:
    """Total order over registry events, independent of the caller's order."""
    return (event.timestamp, event.hmac, event.event_type, event.resource_id)


def _details_str(event: AuditEvent, field: str) -> str:
    value = event.details.get(field)
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(f"{event.event_type} event carries no usable {field!r}: {event.details!r}")
    return value


def _task_classes_from(event: AuditEvent) -> tuple[str, ...]:
    raw = event.details.get("task_classes")
    if not isinstance(raw, list):
        raise ModelRegistryError(f"model.admitted event carries no usable task_classes: {event.details!r}")
    classes: set[str] = set()
    for item in cast("list[object]", raw):
        if not isinstance(item, str) or not item:
            raise ModelRegistryError(f"model.admitted event carries no usable task_classes: {event.details!r}")
        classes.add(item)
    if not classes:
        raise ModelRegistryError(f"model.admitted event carries no usable task_classes: {event.details!r}")
    return tuple(sorted(classes))


def _admission_from(event: AuditEvent) -> ModelAdmission:
    task_classes = _task_classes_from(event)
    version = event.details.get("version")
    if version is not None and (not isinstance(version, str) or not version):
        raise ModelRegistryError(f"model.admitted event carries an unusable version: {event.details!r}")
    evidence_ref = event.details.get("evidence_ref", "")
    if not isinstance(evidence_ref, str):
        raise ModelRegistryError(f"model.admitted event carries an unusable evidence_ref: {event.details!r}")
    return ModelAdmission(
        model_key=_details_str(event, "model_key"),
        provider=_details_str(event, "provider"),
        model=_details_str(event, "model"),
        version=version,
        task_classes=task_classes,
        admitted_by=_details_str(event, "admitted_by"),
        admitted_at=_require_timestamp(event.timestamp, "event timestamp"),
        expires_at=_require_timestamp(_details_str(event, "expires_at"), "expires_at"),
        evidence_ref=evidence_ref,
        event_hmac=event.hmac,
    )


def project_registry(events: Iterable[AuditEvent], *, at: str) -> ModelRegistryState:
    """Replay *events* into the state that held at *at*.

    Pure: the result is a function of ``(events, at)`` alone. No wall clock is
    read -- the caller names the instant, including when that instant is
    "now" -- and permuting the input does not change the result, because the
    projection orders the log itself.

    An event is in effect from its own chain timestamp: what the chain
    attests, not a time the writer supplied, so an admission cannot be
    backdated past an artefact it did not cover. A withdrawal takes effect at
    its timestamp; an admission lapses at its expiry with no event written.

    Args:
        events: Registry events, in any order. Events after *at* are ignored.
        at: The instant to reconstruct, in the audit log's timestamp format.

    Returns:
        The :class:`ModelRegistryState` that held at *at*.

    Raises:
        ValueError: If *at* is not a well-formed timestamp.
        ModelRegistryError: If a registry event cannot be read. A projection
            that skipped records it did not understand would report a state
            no log supports.
    """
    _require_timestamp(at, "at")
    live: dict[str, ModelAdmission] = {}
    for event in sorted(events, key=_order):
        if event.event_type not in (EVENT_MODEL_ADMITTED, EVENT_MODEL_WITHDRAWN):
            continue
        if event.timestamp > at:
            continue
        if event.event_type == EVENT_MODEL_ADMITTED:
            admission = _admission_from(event)
            live[admission.model_key] = admission
        else:
            live.pop(_details_str(event, "model_key"), None)
    admissions = tuple(
        sorted(
            (a for a in live.values() if at < a.expires_at),
            key=lambda a: (a.model_key, a.event_hmac),
        )
    )
    return ModelRegistryState(at=at, admissions=admissions)


def is_admitted(state: ModelRegistryState, ref: ModelRef, *, task_class: str) -> bool:
    """Whether *ref* was admitted for *task_class* in *state*.

    Every identity the reference presents -- the model requested and, when the
    provider reported a different one, that one too -- must have a live
    admission covering *task_class*. Anything the log does not say is a
    refusal.

    Args:
        state: A projected registry state.
        ref: The model reference to check (issue #5037).
        task_class: The task class the model would be used for.

    Returns:
        ``True`` only when every presented identity is admitted.

    Raises:
        ValueError: If *task_class* is empty.
    """
    _require_text(task_class, "task_class")
    by_key = {a.model_key: a for a in state.admissions}
    for keys in _presented_identities(ref):
        if not any(key in by_key and by_key[key].covers(task_class) for key in keys):
            return False
    return True


__all__ = [
    "ANY_TASK_CLASS",
    "EVENT_MODEL_ADMITTED",
    "EVENT_MODEL_WITHDRAWN",
    "MODEL_REGISTRY_PROJECTION_VERSION",
    "ModelAdmission",
    "ModelRegistryError",
    "ModelRegistryState",
    "format_timestamp",
    "is_admitted",
    "load_registry_events",
    "model_key",
    "project_registry",
    "record_model_admission",
    "record_model_withdrawal",
]
