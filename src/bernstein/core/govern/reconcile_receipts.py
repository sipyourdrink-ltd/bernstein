"""Apply receipts on the target, and the check that fails on absence *or* age.

Issue #5087. The receipt-write-and-anchor pattern already worked, but only for
one entity kind - a skill install (:mod:`bernstein.core.skills.provenance`) -
and nothing anywhere evaluated a receipt's *age*: a read returned the receipt or
``None``, never "present but no longer current".

This module generalises the pattern past skills:

* **Receipt on the target.** :func:`write_target_receipt` records a
  :class:`TargetReceipt` ``{target_id, entity_kind, policy_set_hash,
  applied_at}`` under the target's own root and anchors its canonical bytes in
  the ``govern`` lineage run. The receipt bytes *are* the artifact the spine
  hashes, so the returned anchor is the receipt's chain-verifiable identity.
  :func:`discover_receipt_attribute` reads it back as an ordinary
  :class:`~bernstein.core.govern.inventory_models.Surface`, so a later snapshot
  pass sees convergence state as one more attribute of the target rather than
  by consulting a side store.

* **One finding for missing-or-stale.** :func:`check_receipt_current` emits
  :data:`RECEIPT_NOT_CURRENT` both when no receipt exists and when the receipt
  is older than the configured window. From the chair asking "who is out of
  date", "never converged" and "stopped converging" are the same fact: this
  target is not known-current. The two cases differ only in the finding's
  ``reason`` field, which is descriptive - never a second finding name to
  filter, alert or triage on separately.

* **Configured window.** :class:`StalenessPolicy` is one global default with a
  per-entity-kind override. Kinds converge on very different cadences (a lane
  is re-applied every run; a scheduled task once a day), while a per-target
  window would make "who is out of date" unanswerable without reading every
  target's own config first.

* **Drift record.** :class:`DriftRecord` names the attribute, the decided
  value, the observed value and the probe that produced it, and
  :func:`record_drift` anchors it in the run spine next to the decision records
  :mod:`bernstein.core.security.governance` writes.

* **Deltas until staleness.** :func:`build_state_report` keeps the target's
  last reported state locally and reports only what moved since it, until the
  window elapses and forces a full resend. The stored-vs-observed diff is what
  the drift records are computed from, so a steady-state target reports an
  empty delta rather than re-scanning itself in full every pass.

Determinism: every persisted row is canonical JSON (sorted keys, minimal
separators, UTF-8), so two byte-identical inputs produce byte-identical files
and anchors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.govern.inventory_models import Surface
from bernstein.core.lineage.spine import LineageSpine

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Run id under which every target receipt is anchored. Convergence lineage is
#: kept in one dedicated run so it never interleaves with per-task journals.
RECONCILE_RUN_ID = "govern"

#: Attribute name under which a receipt is discovered on the target. A snapshot
#: pass enumerating the target's surfaces sees convergence state as one more
#: attribute, with no separate receipt store to consult.
RECEIPT_ATTRIBUTE = "bernstein.apply_receipt"

#: The single finding name for a target that is not known-current: no receipt
#: at all, or a receipt older than the configured window.
RECEIPT_NOT_CURRENT = "receipt_not_current"

#: Reason values carried by a :class:`ReceiptFinding`. Descriptive only - both
#: reasons travel under :data:`RECEIPT_NOT_CURRENT`.
REASON_MISSING = "missing"
REASON_STALE = "stale"

#: Fallback window when a policy declares none, in seconds (one day).
DEFAULT_STALENESS_WINDOW_S = 86_400

_RECEIPT_SUBPATH = (".sdd", "govern", "receipts")
_REPORTED_SUBPATH = (".sdd", "govern", "reported")
_DRIFT_SUBPATH = ("drift_records",)

_RECONCILE_ACTOR = "bernstein.govern.reconcile"
_DRIFT_ACTOR = "bernstein.govern.drift"

#: No model runs at apply time; the field is part of the spine schema.
_RECONCILE_MODEL = "none"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialise *payload* to canonical JSON bytes (the spine-hashed artifact)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _safe_name(value: str) -> str:
    """Return a filesystem-safe path component for *value*.

    Every character outside ``[A-Za-z0-9-_]`` is replaced, so a target id can
    never introduce a path separator or escape the receipt directory.
    """
    if not value:
        raise ValueError("empty identifier")
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


# ---------------------------------------------------------------------------
# TargetReceipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetReceipt:
    """The attestable record produced by one apply against one target.

    Attributes:
        target_id: The target the apply ran against (a lane name, an adapter
            id, a scheduled-task id, a capability entry).
        entity_kind: The kind of target, which selects the staleness window.
        policy_set_hash: Content address of the policy set that was applied
            (``sha256:<hex>``).
        applied_at: Integer timestamp of the apply; caller-chosen but stable so
            identical fixtures anchor byte-identically.
        journal_entry_hash: The lineage-spine entry hash anchoring the receipt.
            Empty until :func:`write_target_receipt` records it.
    """

    target_id: str
    entity_kind: str
    policy_set_hash: str
    applied_at: int
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "target_id": self.target_id,
            "entity_kind": self.entity_kind,
            "policy_set_hash": self.policy_set_hash,
            "applied_at": self.applied_at,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, anchor included."""
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> TargetReceipt:
        """Rebuild a receipt from a serialized dict."""
        return cls(
            target_id=str(row["target_id"]),
            entity_kind=str(row["entity_kind"]),
            policy_set_hash=str(row["policy_set_hash"]),
            applied_at=int(row["applied_at"]),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def receipt_path(target_root: Path, entity_kind: str, target_id: str) -> Path:
    """Return the on-target receipt path for ``(entity_kind, target_id)``."""
    return target_root.joinpath(*_RECEIPT_SUBPATH, _safe_name(entity_kind), f"{_safe_name(target_id)}.json")


def write_target_receipt(
    *,
    target_root: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt: TargetReceipt,
) -> TargetReceipt:
    """Write *receipt* onto the target and anchor it in the govern spine.

    The receipt's canonical bytes are what the spine hashes, so the returned
    copy's ``journal_entry_hash`` is the spine entry hash over exactly those
    bytes. Strip the spine and the receipt is just a file; anchored, it is a
    chain-verifiable attestation that this target converged to this policy set.

    Args:
        target_root: The target's own root; the receipt lands under
            ``.sdd/govern/receipts/<entity_kind>/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        receipt: The receipt to record; its ``journal_entry_hash`` is ignored.

    Returns:
        The anchored copy of *receipt*, as persisted.
    """
    artifact_path = "/".join(
        (*_RECEIPT_SUBPATH, _safe_name(receipt.entity_kind), f"{_safe_name(receipt.target_id)}.json")
    )
    anchor = LineageSpine(lineage_root, run_id=RECONCILE_RUN_ID, hmac_key=hmac_key).record(
        artifact_path=artifact_path,
        content=receipt.to_canonical_bytes(),
        actor=_RECONCILE_ACTOR,
        step_id=receipt.policy_set_hash,
        model=_RECONCILE_MODEL,
        timestamp=receipt.applied_at,
    )
    anchored = TargetReceipt(
        target_id=receipt.target_id,
        entity_kind=receipt.entity_kind,
        policy_set_hash=receipt.policy_set_hash,
        applied_at=receipt.applied_at,
        journal_entry_hash=anchor,
    )
    path = receipt_path(target_root, receipt.entity_kind, receipt.target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_bytes(anchored.to_dict()).decode("utf-8"), encoding="utf-8")
    return anchored


def read_target_receipt(target_root: Path, entity_kind: str, target_id: str) -> TargetReceipt | None:
    """Return the receipt on *target_root* for ``(entity_kind, target_id)``, or ``None``."""
    path = receipt_path(target_root, entity_kind, target_id)
    if not path.is_file():
        return None
    try:
        return TargetReceipt.from_dict(json.loads(path.read_bytes()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("govern reconcile: malformed target receipt at %s", path)
        return None


def discover_receipt_attribute(target_root: Path, entity_kind: str, target_id: str) -> Surface | None:
    """Return the receipt on the target as a discoverable surface attribute.

    A snapshot pass enumerating the target's surfaces picks this up like any
    other attribute, so convergence state is discovered where the target is
    read rather than looked up in a separate receipt store. The surface's
    evidence reference is the receipt's spine anchor, so a reader can verify
    the attribute offline against the chain.

    Returns ``None`` when the target carries no receipt.
    """
    receipt = read_target_receipt(target_root, entity_kind, target_id)
    if receipt is None:
        return None
    return Surface(
        surface=f"{entity_kind}/{target_id}#{RECEIPT_ATTRIBUTE}",
        observed_value=_canonical_bytes(
            {"policy_set_hash": receipt.policy_set_hash, "applied_at": receipt.applied_at}
        ).decode("utf-8"),
        evidence_ref=receipt.journal_entry_hash,
    )


# ---------------------------------------------------------------------------
# Staleness policy + the one finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StalenessPolicy:
    """How long a receipt stays evidence that its target is current.

    One global default with a per-entity-kind override. Kinds converge on very
    different cadences, while a per-target window would make "who is out of
    date" unanswerable without first reading every target's own configuration.

    Attributes:
        default_window_s: Window applied to any kind without an override.
        overrides: Per-entity-kind window, in seconds.
    """

    default_window_s: int = DEFAULT_STALENESS_WINDOW_S
    overrides: Mapping[str, int] = field(default_factory=dict[str, int])

    def window_for(self, entity_kind: str) -> int:
        """Return the staleness window in seconds for *entity_kind*."""
        return int(self.overrides.get(entity_kind, self.default_window_s))

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "default_window_s": self.default_window_s,
            "windows": dict(sorted(self.overrides.items())),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> StalenessPolicy:
        """Rebuild a policy from the desired-state document's staleness block."""
        windows: Mapping[str, Any] = raw.get("windows") or {}
        return cls(
            default_window_s=int(raw.get("default_window_s", DEFAULT_STALENESS_WINDOW_S)),
            overrides={str(key): int(value) for key, value in windows.items()},
        )


@dataclass(frozen=True, slots=True)
class ReceiptFinding:
    """A target that is not known-current.

    Attributes:
        name: Always :data:`RECEIPT_NOT_CURRENT`. Absence and age are one
            finding: from the chair asking the question they are the same fact.
        target_id: The target the finding is about.
        entity_kind: The target's kind, which selected ``window_s``.
        reason: :data:`REASON_MISSING` or :data:`REASON_STALE`. Descriptive
            detail for the operator, not a second finding name.
        observed_age_s: Seconds since the receipt was written, or ``None`` when
            there is no receipt to age.
        window_s: The window that judged this target.
        evidence_ref: The receipt's spine anchor, or empty when absent.
    """

    name: str
    target_id: str
    entity_kind: str
    reason: str
    observed_age_s: int | None
    window_s: int
    evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "name": self.name,
            "target_id": self.target_id,
            "entity_kind": self.entity_kind,
            "reason": self.reason,
            "observed_age_s": self.observed_age_s,
            "window_s": self.window_s,
            "evidence_ref": self.evidence_ref,
        }


def check_receipt_current(
    *,
    target_root: Path,
    entity_kind: str,
    target_id: str,
    policy: StalenessPolicy,
    now: int,
) -> ReceiptFinding | None:
    """Return a :data:`RECEIPT_NOT_CURRENT` finding, or ``None`` when current.

    A missing receipt and a receipt older than the window both fail under the
    one finding name; they differ only in the finding's ``reason``.

    Args:
        target_root: The target's own root.
        entity_kind: The target's kind; selects the window.
        target_id: The target to judge.
        policy: The configured staleness policy.
        now: Integer timestamp to age the receipt against.
    """
    window_s = policy.window_for(entity_kind)
    receipt = read_target_receipt(target_root, entity_kind, target_id)
    if receipt is None:
        return ReceiptFinding(
            name=RECEIPT_NOT_CURRENT,
            target_id=target_id,
            entity_kind=entity_kind,
            reason=REASON_MISSING,
            observed_age_s=None,
            window_s=window_s,
            evidence_ref="",
        )
    age_s = now - receipt.applied_at
    if age_s <= window_s:
        return None
    return ReceiptFinding(
        name=RECEIPT_NOT_CURRENT,
        target_id=target_id,
        entity_kind=entity_kind,
        reason=REASON_STALE,
        observed_age_s=age_s,
        window_s=window_s,
        evidence_ref=receipt.journal_entry_hash,
    )


# ---------------------------------------------------------------------------
# Drift record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftRecord:
    """One attribute whose observed value left its decided value.

    Attributes:
        target_id: The target the attribute belongs to.
        entity_kind: The target's kind.
        attribute: The attribute that drifted.
        decided_value: The value the policy set decided.
        observed_value: The value the probe read back.
        probe: The probe that produced ``observed_value``, so a reader can
            reproduce the observation rather than trust the record.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        journal_entry_hash: The lineage-spine entry hash anchoring the record.
            Empty until :func:`record_drift` records it.
    """

    target_id: str
    entity_kind: str
    attribute: str
    decided_value: str
    observed_value: str
    probe: str
    timestamp: int
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "target_id": self.target_id,
            "entity_kind": self.entity_kind,
            "attribute": self.attribute,
            "decided_value": self.decided_value,
            "observed_value": self.observed_value,
            "probe": self.probe,
            "timestamp": self.timestamp,
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, anchor included."""
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DriftRecord:
        """Rebuild a drift record from a serialized dict."""
        return cls(
            target_id=str(row["target_id"]),
            entity_kind=str(row["entity_kind"]),
            attribute=str(row["attribute"]),
            decided_value=str(row["decided_value"]),
            observed_value=str(row["observed_value"]),
            probe=str(row["probe"]),
            timestamp=int(row["timestamp"]),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


def drift_records_dir(lineage_root: Path, run_id: str) -> Path:
    """Return the directory holding persisted drift records for *run_id*.

    Colocated with the run's spine dir - alongside the decision records - so a
    record and its anchor share one root.
    """
    return lineage_root / run_id / _DRIFT_SUBPATH[0]


def _drift_filename(drift: DriftRecord, seq: int) -> str:
    """Return a stable, ordered artefact filename for *drift*."""
    return f"{seq:06d}-{_safe_name(drift.target_id)}-{_safe_name(drift.attribute)}.json"


def _next_seq(out_dir: Path) -> int:
    """Return the next zero-based emit index for *out_dir* (append order)."""
    if not out_dir.is_dir():
        return 0
    return sum(1 for _ in out_dir.glob("*.json"))


def record_drift(
    *,
    lineage_root: Path,
    hmac_key: bytes,
    run_id: str,
    drift: DriftRecord,
) -> DriftRecord:
    """Anchor *drift* in the run spine and persist it. Returns the anchored copy.

    The record's canonical bytes are what the spine hashes, so the returned
    record's ``journal_entry_hash`` is the spine entry hash over exactly those
    bytes - the same binding the decision records use.
    """
    out_dir = drift_records_dir(lineage_root, run_id)
    filename = _drift_filename(drift, _next_seq(out_dir))
    anchor = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key).record(
        artifact_path="/".join((*_DRIFT_SUBPATH, filename)),
        content=drift.to_canonical_bytes(),
        actor=_DRIFT_ACTOR,
        step_id=drift.attribute,
        model=_RECONCILE_MODEL,
        timestamp=drift.timestamp,
    )
    anchored = DriftRecord(
        target_id=drift.target_id,
        entity_kind=drift.entity_kind,
        attribute=drift.attribute,
        decided_value=drift.decided_value,
        observed_value=drift.observed_value,
        probe=drift.probe,
        timestamp=drift.timestamp,
        journal_entry_hash=anchor,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(
        _canonical_bytes(anchored.to_dict()).decode("utf-8"),
        encoding="utf-8",
    )
    return anchored


def read_drift_records(lineage_root: Path, run_id: str) -> list[DriftRecord]:
    """Load every persisted drift record for *run_id* (append order)."""
    out_dir = drift_records_dir(lineage_root, run_id)
    if not out_dir.is_dir():
        return []
    records: list[DriftRecord] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            records.append(DriftRecord.from_dict(json.loads(path.read_bytes())))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("govern reconcile: malformed drift record at %s", path)
            continue
    return records


# ---------------------------------------------------------------------------
# Reported state: deltas until staleness forces a full resend
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportedState:
    """The last state a target reported, kept on the target itself.

    Attributes:
        target_id: The target this state belongs to.
        entity_kind: The target's kind.
        attributes: The complete attribute map as of ``reported_at``. The
            local copy is always complete; only what goes out is a delta.
        reported_at: Integer timestamp of the last report.
    """

    target_id: str
    entity_kind: str
    attributes: Mapping[str, str]
    reported_at: int

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "target_id": self.target_id,
            "entity_kind": self.entity_kind,
            "attributes": dict(sorted(self.attributes.items())),
            "reported_at": self.reported_at,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ReportedState:
        """Rebuild a reported state from a serialized dict."""
        attributes: Mapping[str, Any] = row.get("attributes") or {}
        return cls(
            target_id=str(row["target_id"]),
            entity_kind=str(row["entity_kind"]),
            attributes={str(key): str(value) for key, value in attributes.items()},
            reported_at=int(row["reported_at"]),
        )


def reported_state_path(target_root: Path, entity_kind: str, target_id: str) -> Path:
    """Return the on-target reported-state path for ``(entity_kind, target_id)``."""
    return target_root.joinpath(*_REPORTED_SUBPATH, _safe_name(entity_kind), f"{_safe_name(target_id)}.json")


def read_reported_state(target_root: Path, entity_kind: str, target_id: str) -> ReportedState | None:
    """Return the target's last reported state, or ``None`` when it has none."""
    path = reported_state_path(target_root, entity_kind, target_id)
    if not path.is_file():
        return None
    try:
        return ReportedState.from_dict(json.loads(path.read_bytes()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("govern reconcile: malformed reported state at %s", path)
        return None


def write_reported_state(target_root: Path, state: ReportedState) -> None:
    """Persist *state* on the target, replacing any previous report."""
    path = reported_state_path(target_root, state.entity_kind, state.target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_bytes(state.to_dict()).decode("utf-8"), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class StateReport:
    """What a target sends upstream for one pass.

    Attributes:
        target_id: The target reporting.
        entity_kind: The target's kind.
        full: ``True`` when this is a full resend (no prior report, or the
            staleness window elapsed since the last one), ``False`` for a delta.
        attributes: The reported attributes - everything observed on a full
            resend, only what moved since the last report otherwise.
        drift: One record per reported attribute whose observed value differs
            from its decided value, unanchored until :func:`record_drift`.
        reported_at: Integer timestamp of this report.
    """

    target_id: str
    entity_kind: str
    full: bool
    attributes: Mapping[str, str]
    drift: tuple[DriftRecord, ...]
    reported_at: int


def build_state_report(
    *,
    target_root: Path,
    entity_kind: str,
    target_id: str,
    observed: Mapping[str, str],
    decided: Mapping[str, str],
    probe: str,
    now: int,
    policy: StalenessPolicy,
) -> StateReport:
    """Diff *observed* against the target's last report and persist the new one.

    The target keeps its complete state locally, so a steady-state pass reports
    an empty delta instead of re-sending everything. Once the staleness window
    has elapsed since the last report the next pass is a full resend, which is
    what makes a receipt's age a usable signal: a target that has gone quiet
    cannot stay quiet past the window.

    Drift records are computed over the *reported* attributes - the
    stored-vs-observed diff - not over a fresh full scan.

    Args:
        target_root: The target's own root; the reported state lands under
            ``.sdd/govern/reported/<entity_kind>/``.
        entity_kind: The target's kind; selects the staleness window.
        target_id: The reporting target.
        observed: What the probe read back, attribute to value.
        decided: What the policy set decided, attribute to value.
        probe: The probe that produced *observed*.
        now: Integer timestamp of this report.
        policy: The configured staleness policy.
    """
    previous = read_reported_state(target_root, entity_kind, target_id)
    full = previous is None or (now - previous.reported_at) > policy.window_for(entity_kind)

    if full or previous is None:
        reported = dict(observed)
    else:
        reported = {k: v for k, v in observed.items() if previous.attributes.get(k) != v}

    drift = tuple(
        DriftRecord(
            target_id=target_id,
            entity_kind=entity_kind,
            attribute=attribute,
            decided_value=decided[attribute],
            observed_value=reported[attribute],
            probe=probe,
            timestamp=now,
        )
        for attribute in sorted(reported)
        if attribute in decided and decided[attribute] != reported[attribute]
    )

    write_reported_state(
        target_root,
        ReportedState(
            target_id=target_id,
            entity_kind=entity_kind,
            attributes=dict(observed),
            reported_at=now,
        ),
    )
    return StateReport(
        target_id=target_id,
        entity_kind=entity_kind,
        full=full,
        attributes=reported,
        drift=drift,
        reported_at=now,
    )


__all__ = [
    "DEFAULT_STALENESS_WINDOW_S",
    "REASON_MISSING",
    "REASON_STALE",
    "RECEIPT_ATTRIBUTE",
    "RECEIPT_NOT_CURRENT",
    "RECONCILE_RUN_ID",
    "DriftRecord",
    "ReceiptFinding",
    "ReportedState",
    "StalenessPolicy",
    "StateReport",
    "TargetReceipt",
    "build_state_report",
    "check_receipt_current",
    "discover_receipt_attribute",
    "drift_records_dir",
    "read_drift_records",
    "read_reported_state",
    "read_target_receipt",
    "receipt_path",
    "record_drift",
    "reported_state_path",
    "write_reported_state",
    "write_target_receipt",
]
