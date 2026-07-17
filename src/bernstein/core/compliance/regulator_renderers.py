"""Deterministic renderers for the regulator-mapped compliance packs.

Every renderer here is a pure projection of already-derived facts: it
takes no wall-clock reading and embeds none, so two builds over the same
chain window produce byte-identical members. PDFs are rendered with
reportlab's ``invariant`` mode so the ``/CreationDate`` / document-id
metadata that would otherwise vary run-to-run is fixed.

The rendered members carry no verification authority of their own: the
offline verifier binds a pack by member sha256 (recorded in the signed
manifest ``input_hashes``) plus recomputation of the chained substrate
(the lineage log, audit slice, and receipt bindings). These renderers only
make that substrate human-readable.
"""

from __future__ import annotations

import contextlib
import csv
import io
from typing import TYPE_CHECKING, Any

import reportlab.rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "render_incident_csv",
    "render_incident_pdf",
    "render_oversight_csv",
    "render_oversight_pdf",
    "render_retention_csv",
    "render_retention_pdf",
]


@contextlib.contextmanager
def _invariant_pdf() -> Iterator[None]:
    """Force reportlab into deterministic (invariant) output for one build."""
    prior = reportlab.rl_config.invariant
    reportlab.rl_config.invariant = 1
    try:
        yield
    finally:
        reportlab.rl_config.invariant = prior


def _pdf(title: str, story_rows: Sequence[Any]) -> bytes:
    with _invariant_pdf():
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
            title=title,
            invariant=1,
        )
        doc.build(list(story_rows))
        return buf.getvalue()


def _fact_table(rows: list[list[str]], col_widths: list[float]) -> Table:
    tbl = Table(rows, colWidths=col_widths)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ]
        )
    )
    return tbl


def _csv(fieldnames: Sequence[str], rows: Sequence[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def render_retention_pdf(*, org: str, period: tuple[str, str], evidence: dict[str, Any]) -> bytes:
    """Human-readable chain-continuity summary (Article 12(3) retention)."""
    styles = getSampleStyleSheet()
    boundary = evidence["boundary"]
    params = evidence["retention_params"]
    story: list[Any] = [
        Paragraph("<b>EU AI Act Article 12(3) - Retention Evidence</b>", styles["Title"]),
        Spacer(1, 0.4 * cm),
        Paragraph(f"<b>Organisation:</b> {org}", styles["Normal"]),
        Paragraph(f"<b>Period:</b> {period[0]} &#8594; {period[1]}", styles["Normal"]),
        Paragraph(f"<b>Entries in period:</b> {evidence['entry_count']}", styles["Normal"]),
        Spacer(1, 0.4 * cm),
    ]
    rows = [
        ["Fact", "Value"],
        ["First entry hash", str(boundary["first_entry_hash"])],
        ["Last entry hash", str(boundary["last_entry_hash"])],
        ["Entry count", str(evidence["entry_count"])],
        ["Coverage gaps", str(len(evidence["coverage_gaps"]))],
        ["Period days", str(params["period_days"])],
        ["Minimum required days", str(params["minimum_required_days"])],
        ["Meets minimum", str(params["meets_minimum"])],
    ]
    story.append(_fact_table(rows, [5 * cm, 11 * cm]))
    return _pdf(f"Article 12(3) Retention - {org}", story)


def render_retention_csv(evidence: dict[str, Any]) -> str:
    boundary = evidence["boundary"]
    params = evidence["retention_params"]
    row = {
        "period_since": evidence["period"]["since"],
        "period_until": evidence["period"]["until"],
        "entry_count": evidence["entry_count"],
        "first_entry_hash": boundary["first_entry_hash"],
        "last_entry_hash": boundary["last_entry_hash"],
        "coverage_gaps": len(evidence["coverage_gaps"]),
        "period_days": params["period_days"],
        "minimum_required_days": params["minimum_required_days"],
        "meets_minimum": params["meets_minimum"],
    }
    return _csv(list(row.keys()), [row])


# ---------------------------------------------------------------------------
# Oversight
# ---------------------------------------------------------------------------


def render_oversight_pdf(*, org: str, period: tuple[str, str], evidence: dict[str, Any]) -> bytes:
    """Human-readable human-oversight summary (Article 14)."""
    styles = getSampleStyleSheet()
    receipts = evidence["receipts"]
    story: list[Any] = [
        Paragraph("<b>EU AI Act Article 14 - Human Oversight Evidence</b>", styles["Title"]),
        Spacer(1, 0.4 * cm),
        Paragraph(f"<b>Organisation:</b> {org}", styles["Normal"]),
        Paragraph(f"<b>Period:</b> {period[0]} &#8594; {period[1]}", styles["Normal"]),
        Paragraph(f"<b>Approval receipts:</b> {len(receipts)}", styles["Normal"]),
        Spacer(1, 0.4 * cm),
    ]
    rows = [["Receipt", "Principal", "Decision", "Displayed=Executed"]]
    for r in receipts:
        rows.append([r["receipt_id"], r["principal"], r["decision"], "yes" if r["binding_ok"] else "NO"])
    story.append(_fact_table(rows, [3.5 * cm, 5.5 * cm, 3 * cm, 4 * cm]))
    return _pdf(f"Article 14 Oversight - {org}", story)


def render_oversight_csv(evidence: dict[str, Any]) -> str:
    fields = ("receipt_id", "principal", "decision", "ts_ns", "displayed_hash", "executed_hash", "binding_ok")
    return _csv(fields, evidence["receipts"])


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------


def render_incident_pdf(*, org: str, timeline: dict[str, Any], gaps: list[dict[str, Any]]) -> bytes:
    """Human-readable serious-incident report (Article 73 shape)."""
    styles = getSampleStyleSheet()
    events = timeline.get("events", [])
    agents = ", ".join(timeline.get("involved_agents", [])) or "n/a"
    artifacts = ", ".join(timeline.get("artifacts", [])) or "n/a"
    story: list[Any] = [
        Paragraph("<b>EU AI Act Article 73 - Serious Incident Report</b>", styles["Title"]),
        Spacer(1, 0.4 * cm),
        Paragraph(f"<b>Organisation:</b> {org}", styles["Normal"]),
        Paragraph(f"<b>Run:</b> {timeline.get('run_id', 'n/a')}", styles["Normal"]),
        Paragraph(f"<b>Involved agents:</b> {agents}", styles["Normal"]),
        Paragraph(f"<b>Affected artifacts:</b> {artifacts}", styles["Normal"]),
        Spacer(1, 0.4 * cm),
        Paragraph("<b>Timeline</b>", styles["Heading3"]),
    ]
    tl_rows = [["ts_ns", "kind", "detail"]]
    for e in events:
        tl_rows.append([str(e.get("ts_ns", "")), str(e.get("kind", "")), str(e.get("detail", ""))])
    story.extend(
        (
            _fact_table(tl_rows, [4 * cm, 3 * cm, 9 * cm]),
            Spacer(1, 0.4 * cm),
            Paragraph(f"<b>Evidence gaps: {len(gaps)}</b>", styles["Heading3"]),
        )
    )
    if gaps:
        gap_rows = [["kind", "ref", "reason"]]
        for g in gaps:
            gap_rows.append([str(g.get("kind", "")), str(g.get("ref", "")), str(g.get("reason", ""))])
        story.append(_fact_table(gap_rows, [4 * cm, 6 * cm, 6 * cm]))
    return _pdf(f"Article 73 Incident - {org}", story)


def render_incident_csv(timeline: dict[str, Any]) -> str:
    fields = ("ts_ns", "kind", "detail")
    return _csv(fields, timeline.get("events", []))
