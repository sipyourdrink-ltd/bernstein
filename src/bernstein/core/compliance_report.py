"""GH-321: SOC 2 compliance reporting for evidence collection.

Transforms raw audit events (HMAC-chained JSONL dicts) into structured
compliance packages with control mappings, evidence summaries, and a
SHA-256 Merkle root attestation.

This module works on *event dicts* (as produced by ``AuditLog.query()``
or raw JSONL deserialization), not on files/directories.  For
file-based SOC 2 reporting see ``soc2_report.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Control mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlMapping:
    """Maps a SOC 2 Type II control to the audit event types that satisfy it.

    Attributes:
        control_id: Trust-service criterion identifier (e.g. ``CC6.1``).
        title: Short human-readable title.
        description: Full description of the control requirement.
        evidence_types: Event-type patterns that constitute evidence for
            this control.  Matching is prefix-based: an event whose
            ``event_type`` starts with any pattern is mapped to the control.
    """

    control_id: str
    title: str
    description: str
    evidence_types: list[str] = field(default_factory=lambda: list[str]())


# Pre-defined SOC 2 Type II control mappings relevant to Bernstein.
SOC2_CONTROLS: list[ControlMapping] = [
    ControlMapping(
        control_id="CC6.1",
        title="Logical Access",
        description=(
            "The entity implements logical access security measures to "
            "protect against unauthorized access."
        ),
        evidence_types=["auth.", "login", "access.", "permission."],
    ),
    ControlMapping(
        control_id="CC6.2",
        title="Access Provisioning",
        description=(
            "Prior to issuing system credentials, the entity registers "
            "and authorizes new users."
        ),
        evidence_types=["user.", "credential.", "provision.", "register."],
    ),
    ControlMapping(
        control_id="CC7.2",
        title="System Operations Monitoring",
        description=(
            "The entity monitors system components and their operation "
            "for anomalies that are indicative of malicious acts."
        ),
        evidence_types=["monitor.", "alert.", "anomaly.", "health.", "heartbeat."],
    ),
    ControlMapping(
        control_id="CC8.1",
        title="Change Management",
        description=(
            "The entity authorizes, designs, develops, configures, "
            "documents, tests, approves, and implements changes."
        ),
        evidence_types=[
            "task.",
            "deploy.",
            "change.",
            "config.",
            "merge.",
            "review.",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Evidence summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSummary:
    """Summarised evidence for a single control.

    Attributes:
        control_id: The control this evidence supports.
        event_count: Total matching events.
        first_event: ISO 8601 timestamp of the earliest matching event.
        last_event: ISO 8601 timestamp of the latest matching event.
        sample_events: Up to 5 representative events (full dicts).
    """

    control_id: str
    event_count: int
    first_event: str
    last_event: str
    sample_events: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())


# ---------------------------------------------------------------------------
# Compliance package
# ---------------------------------------------------------------------------

_MAX_SAMPLE_EVENTS = 5


@dataclass(frozen=True)
class CompliancePackage:
    """A complete SOC 2 compliance evidence package.

    Attributes:
        period: Reporting period label (e.g. ``2026-Q1``).
        generated_at: ISO 8601 timestamp of package generation.
        merkle_root: Hex-encoded SHA-256 Merkle root over event HMACs.
        controls: Control mappings included in this package.
        evidence: Per-control evidence summaries.
        total_events: Total number of events in the input set.
    """

    period: str
    generated_at: str
    merkle_root: str
    controls: list[ControlMapping]
    evidence: list[EvidenceSummary]
    total_events: int


# ---------------------------------------------------------------------------
# Merkle root computation
# ---------------------------------------------------------------------------


def compute_merkle_root(events: list[dict[str, Any]]) -> str:
    """Compute a SHA-256 binary Merkle tree root over sorted event HMACs.

    Each event dict is expected to carry an ``hmac`` key.  The HMACs are
    sorted lexicographically before tree construction so that the root is
    deterministic regardless of input order.

    For zero events the root is the SHA-256 of the empty byte string.
    For a single event the root is SHA-256 of its HMAC.

    Args:
        events: List of audit event dicts, each containing an ``hmac`` key.

    Returns:
        Hex-encoded SHA-256 Merkle root.
    """
    if not events:
        return hashlib.sha256(b"").hexdigest()

    hmacs = sorted(str(e.get("hmac", "")) for e in events)
    leaves: list[str] = [hashlib.sha256(h.encode()).hexdigest() for h in hmacs]

    # Build binary Merkle tree bottom-up.
    layer = leaves
    while len(layer) > 1:
        next_layer: list[str] = []
        for i in range(0, len(layer), 2):
            left = layer[i]
            right = layer[i + 1] if i + 1 < len(layer) else left
            combined = hashlib.sha256((left + right).encode()).hexdigest()
            next_layer.append(combined)
        layer = next_layer

    return layer[0]


# ---------------------------------------------------------------------------
# Event-to-control mapping
# ---------------------------------------------------------------------------


def _event_matches_control(event_type: str, control: ControlMapping) -> bool:
    """Return True if *event_type* matches any prefix in *control.evidence_types*."""
    return any(event_type.startswith(pattern) for pattern in control.evidence_types)


def map_events_to_controls(
    events: list[dict[str, Any]],
    controls: list[ControlMapping],
) -> list[EvidenceSummary]:
    """Classify audit events by control using prefix matching on ``event_type``.

    Each event may map to multiple controls.  Events whose ``event_type``
    does not match any control are silently ignored.

    Args:
        events: List of audit event dicts with at least an ``event_type`` key.
        controls: Control mappings to match against.

    Returns:
        One ``EvidenceSummary`` per control that has at least one matching
        event, ordered by control_id.
    """
    buckets: dict[str, list[dict[str, Any]]] = {c.control_id: [] for c in controls}

    for event in events:
        et = str(event.get("event_type", ""))
        for control in controls:
            if _event_matches_control(et, control):
                buckets[control.control_id].append(event)

    summaries: list[EvidenceSummary] = []
    for control in sorted(controls, key=lambda c: c.control_id):
        matched = buckets[control.control_id]
        if not matched:
            continue

        timestamps = [str(e.get("timestamp", "")) for e in matched]
        timestamps_sorted = sorted(t for t in timestamps if t)

        summaries.append(
            EvidenceSummary(
                control_id=control.control_id,
                event_count=len(matched),
                first_event=timestamps_sorted[0] if timestamps_sorted else "",
                last_event=timestamps_sorted[-1] if timestamps_sorted else "",
                sample_events=matched[:_MAX_SAMPLE_EVENTS],
            ),
        )

    return summaries


# ---------------------------------------------------------------------------
# Package builder
# ---------------------------------------------------------------------------


def build_compliance_package(
    events: list[dict[str, Any]],
    period: str,
    controls: list[ControlMapping] | None = None,
) -> CompliancePackage:
    """Build a complete SOC 2 compliance evidence package.

    Args:
        events: Raw audit event dicts.
        period: Reporting period label (e.g. ``2026-Q1``).
        controls: Control mappings to use.  Defaults to ``SOC2_CONTROLS``.

    Returns:
        A frozen ``CompliancePackage`` ready for serialization or reporting.
    """
    effective_controls = controls if controls is not None else SOC2_CONTROLS
    evidence = map_events_to_controls(events, effective_controls)
    merkle_root = compute_merkle_root(events)
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    return CompliancePackage(
        period=period,
        generated_at=generated_at,
        merkle_root=merkle_root,
        controls=effective_controls,
        evidence=evidence,
        total_events=len(events),
    )


# ---------------------------------------------------------------------------
# Human-readable report formatter
# ---------------------------------------------------------------------------


def format_compliance_report(package: CompliancePackage) -> str:
    """Render a ``CompliancePackage`` as a human-readable text report.

    Args:
        package: The compliance package to render.

    Returns:
        Multi-line plain-text report suitable for auditor review.
    """
    lines: list[str] = [
        "=" * 72,
        "SOC 2 Type II Compliance Report",
        "=" * 72,
        f"Period:        {package.period}",
        f"Generated:     {package.generated_at}",
        f"Total Events:  {package.total_events}",
        f"Merkle Root:   {package.merkle_root}",
        "",
        "-" * 72,
        "Controls",
        "-" * 72,
    ]

    for ctrl in package.controls:
        lines.append(f"  [{ctrl.control_id}] {ctrl.title}")
        lines.append(f"    {ctrl.description}")

    lines.append("")
    lines.append("-" * 72)
    lines.append("Evidence")
    lines.append("-" * 72)

    if not package.evidence:
        lines.append("  No matching events found.")
    else:
        for ev in package.evidence:
            lines.append(f"  [{ev.control_id}]")
            lines.append(f"    Events:  {ev.event_count}")
            lines.append(f"    First:   {ev.first_event}")
            lines.append(f"    Last:    {ev.last_event}")
            lines.append(f"    Samples: {len(ev.sample_events)}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)
