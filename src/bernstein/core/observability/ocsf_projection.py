"""Deterministic OCSF projection of a run's security evidence (#6037).

``bernstein trace export --format ocsf`` emits an `OCSF
<https://github.com/ocsf/ocsf-schema>`_ event stream so a run's security
evidence reaches the place operators already send security telemetry: a SIEM or
security data lake. It is a re-encoding of records that already exist, not a
second source of truth, and nothing reads OCSF back.

Two stores, because the evidence lives in two
-----------------------------------------------
The mapping doc (``docs/observability/ocsf-export.md``) settled this and the
issue's review confirmed it. The run **journal** carries orchestration and
lifecycle - ``run_started``, ``agent_spawned``, ``task_claimed`` - and no
security decisions at all. The **HMAC audit chain** is where a denied tool
call, an admission refusal or a capability refusal is recorded
(``core/approval/gate.py`` writes ``auto_approve_decision`` there, never into
the journal). Exporting only the journal would therefore produce a run
timeline and none of the security telemetry the issue is about, so both stores
are projected and every event says which one it came from.

Decision and outcome are two facts
----------------------------------
OCSF already separates them, so nothing is invented here:

* the **decision** is ``action_id`` from the Security Control profile -
  ``1`` Allowed, ``2`` Denied, ``0`` Unknown;
* the **outcome** is ``status_id`` on the base event - ``1`` Success,
  ``2`` Failure, ``0`` Unknown.

A decision with no recorded outcome is therefore an ``action_id`` with
``status_id = 0``, and never ``status_id = 1``. Applying the Security Control
profile is load-bearing rather than decorative: ``action_id`` is not on
``base_event`` and arrives only with that profile.

Absence is explicit
-------------------
``status_id = 0`` covers *never established*. For *determined to be absent*
OCSF has no enum, so ``status_detail`` carries one of two fixed strings
(:data:`ABSENT_DETERMINED`, :data:`ABSENT_UNESTABLISHED`) rather than
overloading ``status_id``. A consumer reading only the enum is never misled.

Correlation
-----------
Every event carries the run id in ``metadata.correlation_uid`` and, under
``unmapped.source``, the store it came from plus the hash of the record it was
projected from - the journal ``event_hash`` or the audit ``hmac``. An event can
therefore be walked back to its record and that record checked against the
chain head.

Request parameters are digests
------------------------------
Tool arguments routinely carry file contents, prompts and shell commands, so
nothing is forwarded verbatim - including the ``command`` string that
``auto_approve_decision`` already stores in the clear, because forwarding to a
SIEM is a wider disclosure than keeping it in ``.sdd/audit``. Each event
carries ``raw_data_hash`` (SHA-256) and names the canonicalization in
``unmapped.canonicalization``, so the digest is checkable rather than
decorative. ``raw_data`` is never populated.

Determinism
-----------
For a fixed journal and audit range the bytes are byte-identical across runs:
canonical JSON throughout, RFC 8785 for every digest, and no wall-clock value
of its own - the ``time`` on each event is the time *in the record*, not the
time of export.

No runtime dependency is added. Schema validation is a test-only concern.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ABSENT_DETERMINED",
    "ABSENT_UNESTABLISHED",
    "AUDIT_MAPPING",
    "CANONICALIZATION",
    "JOURNAL_MAPPING",
    "OCSF_SCHEMA_VERSION",
    "PRODUCER_NAME",
    "PRODUCER_VENDOR",
    "UNMAPPED_CLASS_UID",
    "OcsfExportResult",
    "build_ocsf_events",
    "export_ocsf",
    "render_ocsf_jsonl",
]

#: The one schema version this projection is pinned to. Every class_uid and
#: enum value below was read out of ocsf/ocsf-schema at this tag.
OCSF_SCHEMA_VERSION: str = "1.9.0"

PRODUCER_NAME: str = "bernstein"
PRODUCER_VENDOR: str = "bernstein"

#: ``status_detail`` values that distinguish the two kinds of absence.
ABSENT_DETERMINED: str = "bernstein:absent-determined"
ABSENT_UNESTABLISHED: str = "bernstein:absent-unestablished"

#: Named in every event so a reader can recompute ``raw_data_hash``.
CANONICALIZATION: str = "jcs-rfc8785"

#: OCSF category uids (categories.json at 1.9.0).
_CAT_FINDINGS = 2
_CAT_IAM = 3
_CAT_APPLICATION = 6

#: class_uid = category_uid * 1000 + the class's own uid.
CLASS_APPLICATION_LIFECYCLE = 6002
CLASS_API_ACTIVITY = 6003
CLASS_AUTHORIZE_SESSION = 3003
CLASS_ENTITY_MANAGEMENT = 3004
CLASS_DETECTION_FINDING = 2004

_CLASS_CATEGORY = {
    CLASS_APPLICATION_LIFECYCLE: _CAT_APPLICATION,
    CLASS_API_ACTIVITY: _CAT_APPLICATION,
    CLASS_AUTHORIZE_SESSION: _CAT_IAM,
    CLASS_ENTITY_MANAGEMENT: _CAT_IAM,
    CLASS_DETECTION_FINDING: _CAT_FINDINGS,
}
_CLASS_NAME = {
    CLASS_APPLICATION_LIFECYCLE: "Application Lifecycle",
    CLASS_API_ACTIVITY: "API Activity",
    CLASS_AUTHORIZE_SESSION: "Authorize Session",
    CLASS_ENTITY_MANAGEMENT: "Entity Management",
    CLASS_DETECTION_FINDING: "Detection Finding",
}
_CATEGORY_NAME = {
    _CAT_FINDINGS: "Findings",
    _CAT_IAM: "Identity & Access Management",
    _CAT_APPLICATION: "Application Activity",
}

#: Security Control profile, ``action_id`` (the decision).
ACTION_UNKNOWN = 0
ACTION_ALLOWED = 1
ACTION_DENIED = 2
ACTION_MODIFIED = 4
_ACTION_NAME = {
    ACTION_UNKNOWN: "Unknown",
    ACTION_ALLOWED: "Allowed",
    ACTION_DENIED: "Denied",
    ACTION_MODIFIED: "Modified",
}

#: Base event ``status_id`` (the outcome).
STATUS_UNKNOWN = 0
STATUS_SUCCESS = 1
STATUS_FAILURE = 2
_STATUS_NAME = {
    STATUS_UNKNOWN: "Unknown",
    STATUS_SUCCESS: "Success",
    STATUS_FAILURE: "Failure",
}

#: ``severity_id`` values used (dictionary.json at 1.9.0).
_SEV_INFORMATIONAL = 1
_SEV_MEDIUM = 3
_SEV_HIGH = 4
_SEV_NAME = {_SEV_INFORMATIONAL: "Informational", _SEV_MEDIUM: "Medium", _SEV_HIGH: "High"}

#: ``fingerprint.algorithm_id`` for SHA-256.
_ALGORITHM_SHA256 = 3

#: Where an unmapped journal kind lands. An unknown kind is a recorded
#: omission, never a crash and never a silent drop: the journal has no central
#: registry of event kinds, so any mapping table is a snapshot.
UNMAPPED_CLASS_UID = CLASS_API_ACTIVITY


@dataclass(frozen=True)
class _Mapped:
    """One row of a mapping table."""

    class_uid: int
    activity_id: int
    activity_name: str
    severity_id: int = _SEV_INFORMATIONAL
    #: Decision, for the audit table. ``None`` means "derive from the record".
    action_id: int | None = None
    #: Outcome, where the kind itself settles it.
    status_id: int = STATUS_UNKNOWN


#: Journal kinds. Lifecycle and orchestration; no security decisions.
JOURNAL_MAPPING: dict[str, _Mapped] = {
    "run_started": _Mapped(CLASS_APPLICATION_LIFECYCLE, 3, "Start"),
    "run_completed": _Mapped(CLASS_APPLICATION_LIFECYCLE, 4, "Stop", status_id=STATUS_SUCCESS),
    "run_stalled": _Mapped(CLASS_APPLICATION_LIFECYCLE, 4, "Stop", _SEV_MEDIUM, status_id=STATUS_FAILURE),
    "run_quiescence": _Mapped(CLASS_APPLICATION_LIFECYCLE, 0, "Unknown"),
    "agent_spawned": _Mapped(CLASS_APPLICATION_LIFECYCLE, 3, "Start"),
    "agent_reaped": _Mapped(CLASS_APPLICATION_LIFECYCLE, 4, "Stop"),
    "task_claimed": _Mapped(CLASS_API_ACTIVITY, 1, "Create"),
    "task_completed": _Mapped(CLASS_API_ACTIVITY, 3, "Update", status_id=STATUS_SUCCESS),
    "task_retried": _Mapped(CLASS_API_ACTIVITY, 3, "Update", _SEV_MEDIUM, status_id=STATUS_FAILURE),
    "task_verification_failed": _Mapped(CLASS_API_ACTIVITY, 3, "Update", _SEV_MEDIUM, status_id=STATUS_FAILURE),
    "workflow_approval_granted": _Mapped(CLASS_AUTHORIZE_SESSION, 1, "Assign Privileges", action_id=ACTION_ALLOWED),
    "workflow_phase_advanced": _Mapped(CLASS_ENTITY_MANAGEMENT, 3, "Update"),
    "skills.injected": _Mapped(CLASS_ENTITY_MANAGEMENT, 1, "Create"),
}

#: Journal kinds deliberately not exported. Listed rather than dropped: ticks
#: would dominate the stream with no security content, and a task DAG is not an
#: event OCSF has a class for.
JOURNAL_EXCLUDED: frozenset[str] = frozenset(
    {"tick_start", "plan.graph", "plan.graph.full", "provider_state_capability"}
)

#: Audit-chain kinds. These are the security decisions.
AUDIT_MAPPING: dict[str, _Mapped] = {
    "auto_approve_decision": _Mapped(CLASS_API_ACTIVITY, 0, "Unknown", _SEV_MEDIUM),
    "human_approval_decision": _Mapped(CLASS_AUTHORIZE_SESSION, 1, "Assign Privileges", _SEV_MEDIUM),
    "approval_pending": _Mapped(CLASS_API_ACTIVITY, 0, "Unknown", _SEV_MEDIUM, action_id=ACTION_UNKNOWN),
    "approval_resolved": _Mapped(CLASS_AUTHORIZE_SESSION, 1, "Assign Privileges", _SEV_MEDIUM),
    "admission_refusal": _Mapped(CLASS_API_ACTIVITY, 0, "Unknown", _SEV_HIGH, action_id=ACTION_DENIED),
    "capability_matrix_refusal": _Mapped(CLASS_API_ACTIVITY, 0, "Unknown", _SEV_HIGH, action_id=ACTION_DENIED),
    "always_allow_promotion": _Mapped(CLASS_ENTITY_MANAGEMENT, 3, "Update", _SEV_MEDIUM, action_id=ACTION_MODIFIED),
    "lineage_tamper_detected": _Mapped(CLASS_DETECTION_FINDING, 1, "Create", _SEV_HIGH, action_id=ACTION_UNKNOWN),
}

#: Verdict strings a gate records, mapped to the OCSF decision.
_VERDICT_ACTION = {
    "APPROVE": ACTION_ALLOWED,
    "ALLOW": ACTION_ALLOWED,
    "ALLOWED": ACTION_ALLOWED,
    "DENY": ACTION_DENIED,
    "REJECT": ACTION_DENIED,
    "DENIED": ACTION_DENIED,
    "ASK": ACTION_UNKNOWN,
    "PENDING": ACTION_UNKNOWN,
}

#: The proposed OCSF extension this projection can mark but not rely on.
PROPOSED_SCHEMA_PROPOSAL: str = "ocsf/ocsf-schema#1760"


@dataclass
class OcsfExportResult:
    """Bytes of the JSONL stream plus the parsed events (for tests)."""

    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    jsonl: bytes = b""
    #: Journal kinds seen that no mapping row covers.
    unmapped_kinds: tuple[str, ...] = ()
    #: Journal kinds skipped because the table excludes them.
    excluded_count: int = 0

    @property
    def event_count(self) -> int:
        return len(self.events)


def _digest(payload: Any) -> str:
    """SHA-256 over the RFC 8785 canonical form of *payload*.

    ``canonicalize_jcs`` already returns UTF-8 bytes, which is the preimage -
    re-encoding a decoded string would be the same bytes but invites drift.
    """
    return hashlib.sha256(canonicalize_jcs(payload)).hexdigest()


def _epoch_ms_from_journal(row: dict[str, Any]) -> int:
    """Journal rows carry a float epoch in ``ts``."""
    raw = row.get("ts")
    return int(float(raw) * 1000) if isinstance(raw, (int, float)) else 0


def _epoch_ms_from_audit(entry: dict[str, Any]) -> int:
    """Audit entries carry an ISO-8601 ``timestamp`` with a ``Z`` suffix."""
    raw = entry.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _base_event(
    *,
    mapped: _Mapped,
    time_ms: int,
    run_id: str,
    store: str,
    record_hash: str,
    index: int,
    message: str,
    params: Any,
    action_id: int | None,
    status_id: int,
    status_detail: str | None,
    include_proposed: bool,
    extra_unmapped: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one OCSF event with the fields every class requires."""
    category_uid = _CLASS_CATEGORY[mapped.class_uid]
    event: dict[str, Any] = {
        "activity_id": mapped.activity_id,
        "activity_name": mapped.activity_name,
        "category_name": _CATEGORY_NAME[category_uid],
        "category_uid": category_uid,
        "class_name": _CLASS_NAME[mapped.class_uid],
        "class_uid": mapped.class_uid,
        "message": message,
        "metadata": {
            "correlation_uid": run_id,
            "product": {"name": PRODUCER_NAME, "vendor_name": PRODUCER_VENDOR},
            "version": OCSF_SCHEMA_VERSION,
        },
        "severity": _SEV_NAME[mapped.severity_id],
        "severity_id": mapped.severity_id,
        "status": _STATUS_NAME[status_id],
        "status_id": status_id,
        "time": time_ms,
        "type_uid": mapped.class_uid * 100 + mapped.activity_id,
        "type_name": f"{_CLASS_NAME[mapped.class_uid]}: {mapped.activity_name}",
        "raw_data_hash": {"algorithm_id": _ALGORITHM_SHA256, "value": _digest(params)},
    }
    if status_detail is not None:
        event["status_detail"] = status_detail
    if action_id is not None:
        # Security Control profile. action_id is not on base_event, so the
        # profile has to be declared for a validator to accept these.
        event["action_id"] = action_id
        event["action"] = _ACTION_NAME[action_id]
        event["metadata"]["profiles"] = ["security_control"]
    unmapped: dict[str, Any] = {
        "canonicalization": CANONICALIZATION,
        "source": {"store": store, "record_hash": record_hash, "index": index},
    }
    if extra_unmapped:
        unmapped.update(extra_unmapped)
    if include_proposed:
        unmapped["proposed"] = {
            "schema_proposal": PROPOSED_SCHEMA_PROPOSAL,
            "status": "open",
            "fields": ["accounting_decision", "containment_reason"],
        }
    event["unmapped"] = unmapped
    return event


def _journal_event(row: dict[str, Any], index: int, run_id: str, *, include_proposed: bool) -> dict[str, Any] | None:
    """Project one journal row, or ``None`` when the table excludes it."""
    kind = str(row.get("event") or "")
    if kind in JOURNAL_EXCLUDED:
        return None
    mapped = JOURNAL_MAPPING.get(kind)
    extra: dict[str, Any] = {}
    if mapped is None:
        mapped = _Mapped(UNMAPPED_CLASS_UID, 0, "Unknown")
        extra["unmapped_kind"] = kind
    status_id = mapped.status_id
    status_detail = None
    if status_id == STATUS_UNKNOWN:
        # The journal records that the step happened, not how it ended.
        status_detail = ABSENT_UNESTABLISHED
    return _base_event(
        mapped=mapped,
        time_ms=_epoch_ms_from_journal(row),
        run_id=run_id,
        store="journal",
        record_hash=str(row.get("event_hash") or ""),
        index=int(row.get("index") or index),
        message=f"journal:{kind}",
        params={k: v for k, v in sorted(row.items()) if k not in ("ts", "elapsed_s")},
        action_id=mapped.action_id,
        status_id=status_id,
        status_detail=status_detail,
        include_proposed=include_proposed,
        extra_unmapped=extra,
    )


def _audit_action_and_status(entry: dict[str, Any], mapped: _Mapped) -> tuple[int, int, str | None]:
    """Resolve (decision, outcome, absence note) for one audit entry.

    The decision comes from the recorded verdict where the entry carries one;
    otherwise from the mapping row. The outcome is deliberately *not* inferred
    from the decision: a permitted call whose effect was never recorded is
    ``Unknown``, never ``Success``.
    """
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    action_id = mapped.action_id
    raw_verdict = details.get("decision") or details.get("verdict")
    if isinstance(raw_verdict, str):
        action_id = _VERDICT_ACTION.get(raw_verdict.strip().upper(), ACTION_UNKNOWN)
    if action_id is None:
        action_id = ACTION_UNKNOWN
    # No audit entry records the effect, so the outcome is never established
    # here. #6270 tracks closing the decision -> dispatch -> effect triple.
    return action_id, STATUS_UNKNOWN, ABSENT_UNESTABLISHED


def _audit_event(entry: dict[str, Any], index: int, run_id: str, *, include_proposed: bool) -> dict[str, Any] | None:
    """Project one audit-chain entry, or ``None`` when its kind is unmapped."""
    kind = str(entry.get("event_type") or "")
    mapped = AUDIT_MAPPING.get(kind)
    if mapped is None:
        return None
    action_id, status_id, status_detail = _audit_action_and_status(entry, mapped)
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    extra: dict[str, Any] = {
        "actor": str(entry.get("actor") or ""),
        "resource_type": str(entry.get("resource_type") or ""),
        "resource_id": str(entry.get("resource_id") or ""),
    }
    reason = details.get("reason")
    matched = details.get("matched_pattern")
    if isinstance(reason, str) and reason:
        extra["policy_reason"] = reason
    if isinstance(matched, str) and matched:
        extra["policy"] = {"name": matched}
    return _base_event(
        mapped=mapped,
        time_ms=_epoch_ms_from_audit(entry),
        run_id=run_id,
        store="audit",
        record_hash=str(entry.get("hmac") or ""),
        index=index,
        message=f"audit:{kind}",
        params=details,
        action_id=action_id,
        status_id=status_id,
        status_detail=status_detail,
        include_proposed=include_proposed,
    )


def build_ocsf_events(
    *,
    run_id: str,
    journal_rows: list[dict[str, Any]] | None = None,
    audit_entries: list[dict[str, Any]] | None = None,
    include_proposed: bool = False,
) -> OcsfExportResult:
    """Project a run's journal rows and audit entries into OCSF events.

    A pure function of its inputs and the pinned schema version. Journal
    events come first in journal order, then audit events in chain order, so
    the stream is stable for a fixed input.
    """
    events: list[dict[str, Any]] = []
    unmapped: list[str] = []
    excluded = 0

    for index, row in enumerate(journal_rows or []):
        kind = str(row.get("event") or "")
        if kind in JOURNAL_EXCLUDED:
            excluded += 1
            continue
        if kind not in JOURNAL_MAPPING and kind not in unmapped:
            unmapped.append(kind)
        event = _journal_event(row, index, run_id, include_proposed=include_proposed)
        if event is not None:
            events.append(event)

    for index, entry in enumerate(audit_entries or []):
        event = _audit_event(entry, index, run_id, include_proposed=include_proposed)
        if event is not None:
            events.append(event)

    return OcsfExportResult(
        run_id=run_id,
        events=events,
        jsonl=render_ocsf_jsonl(events),
        unmapped_kinds=tuple(unmapped),
        excluded_count=excluded,
    )


def render_ocsf_jsonl(events: list[dict[str, Any]]) -> bytes:
    """One canonical JSON object per line, newline terminated."""
    if not events:
        return b""
    lines = [json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events]
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_ocsf(
    sdd_dir: Path,
    run_id: str,
    *,
    include_proposed: bool = False,
    include_audit: bool = True,
) -> OcsfExportResult:
    """Read a run's journal (and the audit chain) and project them to OCSF.

    The audit chain is read without its HMAC key: this is a projection of the
    recorded entries, and verifying the chain itself remains the job of
    ``verify-audit-receipt``.
    """
    from bernstein.core.replay.journal import load_events, run_journal_path
    from bernstein.core.security.audit_multitenant import _read_audit_events

    journal_rows: list[dict[str, Any]] = []
    journal_path = run_journal_path(sdd_dir, run_id)
    if journal_path.is_file():
        journal_rows = load_events(journal_path).events

    audit_entries: list[dict[str, Any]] = []
    if include_audit:
        audit_entries = _read_audit_events(sdd_dir / "audit")

    return build_ocsf_events(
        run_id=run_id,
        journal_rows=journal_rows,
        audit_entries=audit_entries,
        include_proposed=include_proposed,
    )
