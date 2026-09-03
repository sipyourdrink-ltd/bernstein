"""Cluster-scoped governance by declaration (#4988).

``bernstein cluster`` and ``deploy/helm/bernstein/`` govern the orchestrator's
own workload. An operator whose agents run as ordinary workloads next to it had
to wire the ingest boundary by hand, once per workload -- so governance covered
the workloads someone remembered.

This module makes the coverage declarative. It is a read-only projection from a
listing of Kubernetes workload manifests (``kubectl get ... -o json``) onto:

* a :class:`ClusterWorkload` record per workload, carrying its governance
  state as declared by a label;
* an :class:`~bernstein.core.govern.inventory_models.Inventory` in the shape
  :func:`~bernstein.core.govern.compute_plan` already consumes, so the posture
  check needs no second inventory format;
* :class:`GovernanceTransition` records diffed between two snapshots, so
  leaving governance is an event rather than a row that stops appearing.

Three properties hold by construction:

* **No manifest is written.** Enrolling a workload is labelling it; nothing in
  this module edits, patches, or annotates the manifests it reads. A workload
  is inventoried exactly as the cluster reports it.
* **Nothing drops out silently.** An unlabelled workload is inventoried as
  ``UNGOVERNED`` rather than skipped, an opted-out workload as ``OPTED_OUT``,
  and a label value that cannot be read raises instead of resolving to a
  posture nobody declared.
* **The projection is deterministic.** Records sort by ``(namespace, kind,
  name)`` and carry no wall-clock or environment state, so two operators
  listing the same cluster derive byte-identical inventories and therefore the
  same :meth:`Inventory.content_hash`.

Telemetry routing stays out of the data path: :func:`route_workload_telemetry`
hands a governed workload's OTLP spans to the existing ingest boundary under a
source label derived from the workload's own identity, so the signed receipt
names which workload the spans came from. An admission webhook -- a blocking
path -- is deliberately not part of this surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.govern.inventory_models import Inventory, Surface

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.observability.otlp_ingest_receipt import IngestReceipt

__all__ = [
    "GOVERN_LABEL",
    "GOVERN_LABEL_DISABLED",
    "GOVERN_LABEL_ENABLED",
    "ClusterGovernanceError",
    "ClusterWorkload",
    "GovernanceState",
    "GovernanceTransition",
    "TransitionKind",
    "build_inventory",
    "diff_workloads",
    "inventory_workloads",
    "route_workload_telemetry",
    "telemetry_source_label",
]

#: Label key a workload sets to declare its governance posture. The key sits in
#: the same group as the CRDs in :mod:`bernstein.core.orchestration.operator`.
GOVERN_LABEL: Final = "bernstein.io/govern"

#: Label value enrolling a workload into governance.
GOVERN_LABEL_ENABLED: Final = "enabled"

#: Label value opting a workload out. The workload stays in the inventory.
GOVERN_LABEL_DISABLED: Final = "disabled"

#: Accepted spellings, normalised to lower case with surrounding space stripped.
#: ``"true"``/``"false"`` are accepted because Kubernetes tooling renders
#: boolean-looking values that way; anything else is a typo, not a posture.
_ENABLED_VALUES: Final = frozenset({GOVERN_LABEL_ENABLED, "true"})
_DISABLED_VALUES: Final = frozenset({GOVERN_LABEL_DISABLED, "false"})

#: Namespace assumed when a manifest omits one, matching ``kubectl`` behaviour.
_DEFAULT_NAMESPACE: Final = "default"


class ClusterGovernanceError(ValueError):
    """Raised when a workload listing cannot be projected onto a posture."""


class GovernanceState(Enum):
    """Governance posture a workload declares through its label.

    - ``GOVERNED``: the label enrols the workload; its telemetry is routed to
      the ingest boundary and its posture is diffed against the playbook.
    - ``OPTED_OUT``: the label explicitly declines governance. The workload is
      still inventoried, so opting out is visible rather than absent.
    - ``UNGOVERNED``: no label. Reported, never omitted -- an unlabelled
      workload is the case the inventory exists to surface.
    """

    GOVERNED = "governed"
    OPTED_OUT = "opted_out"
    UNGOVERNED = "ungoverned"


class TransitionKind(Enum):
    """How a workload's governance changed between two inventory snapshots.

    - ``ENROLLED``: the workload became governed (labelled, or arrived
      already labelled).
    - ``OPTED_OUT``: the workload stopped being governed while still running.
      Removing the label lands here, so it is an event and not a silent
      disappearance from the inventory.
    - ``APPEARED``: a new workload that is not governed.
    - ``WITHDRAWN``: the workload is no longer in the listing at all. Distinct
      from ``OPTED_OUT``: the workload is gone, not merely unenrolled.
    """

    ENROLLED = "enrolled"
    OPTED_OUT = "opted_out"
    APPEARED = "appeared"
    WITHDRAWN = "withdrawn"


def telemetry_source_label(namespace: str, kind: str, name: str) -> str:
    """Return the ingest source label identifying one cluster workload.

    The same string identifies the workload as an inventory surface, so a
    signed ingest receipt and a govern-plan entry name the workload the same
    way and can be joined without a lookup table.
    """
    return f"k8s:{namespace}/{kind}/{name}"


@dataclass(frozen=True, slots=True)
class ClusterWorkload:
    """One workload as the cluster reports it, plus its declared posture.

    Attributes:
        uid: The workload's ``metadata.uid``, or its reference when the
            listing came from files rather than the API and carries no uid.
        namespace: The workload's namespace.
        kind: The workload's kind (``Deployment``, ``StatefulSet``, ...).
        name: The workload's name.
        state: Posture declared by :data:`GOVERN_LABEL`.
        label_value: The label value as written, or ``None`` when unlabelled.
            Kept verbatim so the inventory reports what the cluster says.
    """

    uid: str
    namespace: str
    kind: str
    name: str
    state: GovernanceState
    label_value: str | None

    @property
    def ref(self) -> str:
        """Return ``namespace/kind/name``, the workload's stable identity."""
        return f"{self.namespace}/{self.kind}/{self.name}"

    @property
    def telemetry_source_label(self) -> str:
        """Return the ingest source label this workload's spans arrive under."""
        return telemetry_source_label(self.namespace, self.kind, self.name)

    def to_surface(self) -> Surface:
        """Return this workload as a govern-plan inventory surface."""
        return Surface(
            surface=self.telemetry_source_label,
            observed_value=self.state.value,
            evidence_ref=f"uid:{self.uid}",
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "uid": self.uid,
            "namespace": self.namespace,
            "kind": self.kind,
            "name": self.name,
            "state": self.state.value,
            "label_value": self.label_value,
            "telemetry_source_label": self.telemetry_source_label,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ClusterWorkload:
        """Rebuild a workload record from its serialized form."""
        try:
            state = GovernanceState(str(raw["state"]))
        except (KeyError, ValueError) as exc:
            raise ClusterGovernanceError(f"malformed workload record: {exc}") from exc
        label_value = raw.get("label_value")
        return cls(
            uid=str(raw["uid"]),
            namespace=str(raw["namespace"]),
            kind=str(raw["kind"]),
            name=str(raw["name"]),
            state=state,
            label_value=None if label_value is None else str(label_value),
        )


@dataclass(frozen=True, slots=True)
class GovernanceTransition:
    """One governance change between two inventory snapshots.

    Attributes:
        kind: What changed.
        workload_ref: ``namespace/kind/name`` of the workload that changed.
        uid: The workload's uid from whichever snapshot still has it.
        previous_state: Posture in the earlier snapshot, ``None`` when the
            workload was not in it.
        current_state: Posture in the later snapshot, ``None`` when the
            workload is no longer in it.
    """

    kind: TransitionKind
    workload_ref: str
    uid: str
    previous_state: GovernanceState | None
    current_state: GovernanceState | None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "kind": self.kind.value,
            "workload_ref": self.workload_ref,
            "uid": self.uid,
            "previous_state": None if self.previous_state is None else self.previous_state.value,
            "current_state": None if self.current_state is None else self.current_state.value,
        }


def _declared_state(label_value: str | None, *, ref: str, label_key: str) -> GovernanceState:
    """Resolve a label value onto a posture, refusing values nobody declared."""
    if label_value is None:
        return GovernanceState.UNGOVERNED
    normalised = label_value.strip().lower()
    if normalised in _ENABLED_VALUES:
        return GovernanceState.GOVERNED
    if normalised in _DISABLED_VALUES:
        return GovernanceState.OPTED_OUT
    raise ClusterGovernanceError(
        f"{ref}: label {label_key}={label_value!r} is not a governance declaration; "
        f"expected one of {sorted(_ENABLED_VALUES | _DISABLED_VALUES)}"
    )


def _read_workload(manifest: dict[str, Any], *, label_key: str) -> ClusterWorkload:
    """Project one manifest onto a record. Reads only; never writes back."""
    metadata_raw = manifest.get("metadata")
    if not isinstance(metadata_raw, dict):
        raise ClusterGovernanceError("workload manifest has no metadata object")
    metadata = cast("dict[str, Any]", metadata_raw)

    name = str(metadata.get("name") or "")
    if not name:
        raise ClusterGovernanceError("workload manifest has no metadata.name")
    kind = str(manifest.get("kind") or "")
    if not kind:
        raise ClusterGovernanceError(f"{name}: workload manifest has no kind")
    namespace = str(metadata.get("namespace") or _DEFAULT_NAMESPACE)
    ref = f"{namespace}/{kind}/{name}"

    labels_raw: Any = metadata.get("labels") or {}
    if not isinstance(labels_raw, dict):
        raise ClusterGovernanceError(f"{ref}: metadata.labels is not a mapping")
    labels = cast("dict[str, Any]", labels_raw)
    raw_label: Any = labels.get(label_key)
    label_value = None if raw_label is None else str(raw_label)

    # A manifest read from a file carries no uid. Falling back to the reference
    # keeps such a listing usable while still giving every record an evidence
    # anchor, rather than refusing the whole listing over a server-side field.
    uid = str(metadata.get("uid") or ref)

    return ClusterWorkload(
        uid=uid,
        namespace=namespace,
        kind=kind,
        name=name,
        state=_declared_state(label_value, ref=ref, label_key=label_key),
        label_value=label_value,
    )


def inventory_workloads(
    manifests: list[dict[str, Any]],
    *,
    label_key: str = GOVERN_LABEL,
) -> tuple[ClusterWorkload, ...]:
    """Inventory *manifests* by their declared governance posture.

    Every manifest produces a record: governed, opted out, or ungoverned. None
    is skipped, and none is modified -- the manifests are read and the records
    are new objects, so enrolling a workload never means editing it.

    Records are sorted by ``(namespace, kind, name)`` so the projection is a
    pure function of the listing's contents, not its order.

    Args:
        manifests: Workload manifests, as ``kubectl get ... -o json`` reports
            them under ``items``.
        label_key: Label declaring the posture. Overridable so an operator
            running more than one governance domain can key on their own label.

    Returns:
        The workload records, sorted.

    Raises:
        ClusterGovernanceError: When a manifest is not a workload, when the
            label carries a value that is not a governance declaration, or when
            two manifests describe the same workload.
    """
    records = [_read_workload(m, label_key=label_key) for m in manifests]

    seen: dict[str, ClusterWorkload] = {}
    for record in records:
        if record.ref in seen:
            raise ClusterGovernanceError(f"{record.ref}: listed twice; a workload has one posture, not two")
        seen[record.ref] = record

    return tuple(sorted(records, key=lambda w: (w.namespace, w.kind, w.name)))


def build_inventory(workloads: tuple[ClusterWorkload, ...]) -> Inventory:
    """Return *workloads* as a govern-plan inventory.

    The result feeds :func:`~bernstein.core.govern.compute_plan` directly: the
    cluster inventory and the posture inventory are one artifact, so a playbook
    clause names a workload by the same identifier its ingest receipts carry.
    """
    return Inventory(surfaces=tuple(w.to_surface() for w in workloads))


def diff_workloads(
    previous: tuple[ClusterWorkload, ...],
    current: tuple[ClusterWorkload, ...],
) -> tuple[GovernanceTransition, ...]:
    """Return the governance transitions between two inventory snapshots.

    Leaving governance always produces a record. A workload that removes the
    label yields ``OPTED_OUT``; a workload that is gone from the listing yields
    ``WITHDRAWN``. The two are distinct because "stopped being governed" and
    "stopped existing" call for different operator responses.

    Transitions are ordered by workload reference, so the same pair of
    snapshots yields the same list on any host.
    """
    before = {w.ref: w for w in previous}
    after = {w.ref: w for w in current}

    transitions: list[GovernanceTransition] = []
    for ref in sorted(before.keys() | after.keys()):
        prior = before.get(ref)
        now = after.get(ref)

        if now is None:
            gone = before[ref]
            transitions.append(
                GovernanceTransition(
                    kind=TransitionKind.WITHDRAWN,
                    workload_ref=ref,
                    uid=gone.uid,
                    previous_state=gone.state,
                    current_state=None,
                )
            )
            continue

        if prior is None:
            kind = TransitionKind.ENROLLED if now.state is GovernanceState.GOVERNED else TransitionKind.APPEARED
            transitions.append(
                GovernanceTransition(
                    kind=kind,
                    workload_ref=ref,
                    uid=now.uid,
                    previous_state=None,
                    current_state=now.state,
                )
            )
            continue

        if prior.state is now.state:
            continue
        kind = TransitionKind.ENROLLED if now.state is GovernanceState.GOVERNED else TransitionKind.OPTED_OUT
        transitions.append(
            GovernanceTransition(
                kind=kind,
                workload_ref=ref,
                uid=now.uid,
                previous_state=prior.state,
                current_state=now.state,
            )
        )

    return tuple(transitions)


def route_workload_telemetry(
    workload: ClusterWorkload,
    spans: list[dict[str, Any]],
    *,
    audit_dir: Path,
    hmac_key: bytes,
    profile_name: str = "generic",
) -> IngestReceipt:
    """Route one governed workload's OTLP spans to the ingest boundary.

    The spans are handed to
    :class:`~bernstein.core.observability.otlp_ingest_receipt.IngestOTLPReceipt`
    under the workload's own source label, so the signed, chain-anchored
    receipt names which workload the activity came from. Nothing here sits in
    the workload's data path: the operator points a collector at Bernstein and
    the route is chosen by the label the workload already carries.

    Args:
        workload: The workload the spans belong to.
        spans: OTLP/JSON spans in the order the source submitted them.
        audit_dir: Audit-chain directory the receipt anchors into.
        hmac_key: The audit-chain HMAC key.
        profile_name: Ingest profile driving attribute mapping.

    Returns:
        The signed :class:`IngestReceipt` for the batch.

    Raises:
        ClusterGovernanceError: When *workload* is not governed. Opting out
            stops ingest; it does not quietly keep ingesting.
    """
    if workload.state is not GovernanceState.GOVERNED:
        raise ClusterGovernanceError(f"{workload.ref}: telemetry not routed, workload state is {workload.state.value}")

    from bernstein.core.observability.otlp_ingest_receipt import IngestOTLPReceipt

    boundary = IngestOTLPReceipt(
        source_label=workload.telemetry_source_label,
        profile_name=profile_name,
        audit_dir=audit_dir,
        hmac_key=hmac_key,
    )
    receipt, _ = boundary.ingest_batch(spans)
    return receipt
