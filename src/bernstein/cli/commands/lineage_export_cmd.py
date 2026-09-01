"""``bernstein lineage export`` -- regulator-shaped audit artefact.

Walks the lineage chain for a run and writes one of:

* CSV: one row per record, all schema-v2 fields as columns. Ingestable
  by any GRC vendor that accepts CSV.
* JSON-LD: a schema.org ``Action``-shaped document with
  ``object`` (output_artifact) and ``instrument`` (inputs) fields, so
  a verifier with a JSON-LD library can graph-walk it.
* HTML: a single, self-contained file -- no JS, no external assets,
  no fonts, no images. Suitable for embedding verbatim in a DORA /
  NIS2 evidence package.
* OpenLineage: a deterministic JSONL RunEvent stream (issue #4914)
  projecting the same chain for stacks that already speak OpenLineage.
  File transport only in this slice; HTTP collector is a follow-up.
"""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console

_FORMATS = ("csv", "jsonld", "html", "openlineage")

_FIELDS: tuple[str, ...] = (
    "schema_version",
    "timestamp",
    "run_id",
    "agent_id",
    "tick_id",
    "output_path",
    "output_sha256",
    "output_line_start",
    "output_line_end",
    "input_paths",
    "input_shas",
    "prompt_sha",
    "model",
    "cost_usd",
    "tokens",
    "regulatory_class",
    "customer_signature",
)


@click.command(name="export")
@click.argument("run_id", required=True)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(_FORMATS, case_sensitive=False),
    required=True,
    help="Output format.",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    required=True,
    help="Destination file (overwritten if it exists).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def lineage_export_cmd(run_id: str, fmt: str, output_path: str, workdir: str) -> None:
    """Export the lineage chain for *run_id* as a regulator artefact.

    \b
    Examples:
      bernstein lineage export r-2026-05-05 --format csv --output /tmp/audit.csv
      bernstein lineage export r-2026-05-05 --format jsonld --output /tmp/audit.jsonld
      bernstein lineage export r-2026-05-05 --format html --output /tmp/audit.html
      bernstein lineage export r-2026-05-05 --format openlineage --output /tmp/ol.jsonl
    """
    from bernstein.core.persistence.lineage import LineageReader

    sdd_dir = Path(workdir).resolve() / ".sdd"
    if not sdd_dir.is_dir():
        console.print(f"[red]No .sdd directory at[/red] {sdd_dir}")
        raise SystemExit(1)

    fmt_lower = fmt.lower()
    if fmt_lower == "openlineage":
        from bernstein.core.persistence.openlineage_export import export_openlineage

        result = export_openlineage(sdd_dir, run_id)
        if not result.events:
            console.print(f"[yellow]No lineage records for run[/yellow] {run_id}")
            raise SystemExit(2)
        out = Path(output_path)
        out.write_bytes(result.payload)
        console.print(f"[green]Wrote[/green] {len(result.events)} OpenLineage event(s) to {out}")
        return

    reader = LineageReader(sdd_dir)
    records = list(reader.iter_records(run_id=run_id))
    if not records:
        console.print(f"[yellow]No lineage records for run[/yellow] {run_id}")
        raise SystemExit(2)

    rows = [_record_row(rec) for rec in records]
    if fmt_lower == "csv":
        text = render_csv(rows)
    elif fmt_lower == "jsonld":
        text = render_jsonld(rows, run_id=run_id)
    else:
        text = render_html(rows, run_id=run_id)

    out = Path(output_path)
    out.write_text(text, encoding="utf-8")
    console.print(f"[green]Wrote[/green] {len(rows)} record(s) to {out} [{fmt_lower}]")


def _record_row(record: Any) -> dict[str, Any]:
    """Flatten a :class:`LineageRecord` into the column shape used by exporters."""
    return {
        "schema_version": record.schema_version,
        "timestamp": record.timestamp,
        "run_id": record.producer.run_id,
        "agent_id": record.producer.agent_id,
        "tick_id": record.producer.tick_id or "",
        "output_path": record.output_artifact.path,
        "output_sha256": record.output_artifact.sha256,
        "output_line_start": record.output_artifact.line_start,
        "output_line_end": record.output_artifact.line_end,
        "input_paths": "|".join(a.path for a in record.inputs),
        "input_shas": "|".join(a.sha256 for a in record.inputs),
        "prompt_sha": record.prompt_sha,
        "model": record.model,
        "cost_usd": record.cost_usd,
        "tokens": record.tokens,
        "regulatory_class": record.regulatory_class or "",
        "customer_signature": record.customer_signature or "",
    }


def render_csv(rows: list[dict[str, Any]]) -> str:
    """Return CSV text with one header row and one record per row."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()


def render_jsonld(rows: list[dict[str, Any]], *, run_id: str) -> str:
    """Return a schema.org-shaped JSON-LD document.

    Each record becomes an ``Action`` whose ``object`` is the produced
    artefact and whose ``instrument`` collection is the input
    artefacts. The producing agent maps to ``agent``; cost / tokens /
    model are kept as additionalProperty so a verifier sees the raw
    values without inventing schema.org terms.
    """
    actions: list[dict[str, Any]] = [
        {
            "@type": "Action",
            "actionStatus": "CompletedActionStatus",
            "startTime": row["timestamp"],
            "agent": {
                "@type": "SoftwareApplication",
                "identifier": row["agent_id"],
                "applicationSuite": row["model"],
            },
            "object": {
                "@type": "DigitalDocument",
                "identifier": row["output_path"],
                "sha256": row["output_sha256"],
                "lineStart": row["output_line_start"],
                "lineEnd": row["output_line_end"],
            },
            "instrument": [
                {"@type": "DigitalDocument", "identifier": p, "sha256": s}
                for p, s in zip(
                    row["input_paths"].split("|") if row["input_paths"] else [],
                    row["input_shas"].split("|") if row["input_shas"] else [],
                    strict=False,
                )
            ],
            "additionalProperty": [
                {"@type": "PropertyValue", "name": "schema_version", "value": row["schema_version"]},
                {"@type": "PropertyValue", "name": "run_id", "value": row["run_id"]},
                {"@type": "PropertyValue", "name": "tick_id", "value": row["tick_id"]},
                {"@type": "PropertyValue", "name": "prompt_sha", "value": row["prompt_sha"]},
                {"@type": "PropertyValue", "name": "cost_usd", "value": row["cost_usd"]},
                {"@type": "PropertyValue", "name": "tokens", "value": row["tokens"]},
                {"@type": "PropertyValue", "name": "regulatory_class", "value": row["regulatory_class"]},
                {"@type": "PropertyValue", "name": "customer_signature", "value": row["customer_signature"]},
            ],
        }
        for row in rows
    ]
    doc: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Bernstein lineage chain for run {run_id}",
        "numberOfItems": len(actions),
        "itemListElement": actions,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


_HTML_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; margin-bottom: 0.2rem; }
.meta { color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
th { background: #f5f5f5; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; }
.sig { color: #2a7d2a; }
.nosig { color: #999; }
.footer { margin-top: 1.5rem; color: #777; font-size: 0.8rem; }
"""


def render_html(rows: list[dict[str, Any]], *, run_id: str) -> str:
    """Return a single self-contained HTML document.

    No JavaScript, no external CSS, no images. The full stylesheet is
    inlined inside ``<style>``. Suitable for committing into a
    customer's compliance package or attaching to an audit ticket.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head>',
        '<meta charset="utf-8">',
        f"<title>Bernstein lineage chain - {html.escape(run_id)}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head><body>",
        f"<h1>Lineage chain for run <code>{html.escape(run_id)}</code></h1>",
        (
            f'<div class="meta">{len(rows)} record(s) - schema v2 - '
            "generated by <code>bernstein lineage export</code></div>"
        ),
        "<table><thead><tr>",
    ]
    headers = (
        "Time",
        "Run",
        "Agent",
        "Tick",
        "Output",
        "SHA-256",
        "Lines",
        "Inputs",
        "Prompt SHA",
        "Model",
        "Tokens",
        "Cost USD",
        "Regulatory class",
        "Customer signature",
    )
    parts.extend(f"<th>{html.escape(h)}</th>" for h in headers)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        sig = row["customer_signature"]
        sig_cell = f'<td class="sig"><code>{html.escape(sig[:24])}…</code></td>' if sig else '<td class="nosig">-</td>'
        line_range = ""
        if row["output_line_start"] is not None and row["output_line_end"] is not None:
            line_range = f"{row['output_line_start']}-{row['output_line_end']}"
        parts.append(
            "<tr>"
            f"<td>{html.escape(str(row['timestamp']))}</td>"
            f"<td><code>{html.escape(row['run_id'])}</code></td>"
            f"<td>{html.escape(row['agent_id'])}</td>"
            f"<td>{html.escape(row['tick_id'])}</td>"
            f"<td><code>{html.escape(row['output_path'])}</code></td>"
            f"<td><code>{html.escape(row['output_sha256'][:16])}…</code></td>"
            f"<td>{html.escape(line_range)}</td>"
            f"<td><code>{html.escape(row['input_paths'])}</code></td>"
            f"<td><code>{html.escape(row['prompt_sha'][:16])}…</code></td>"
            f"<td>{html.escape(row['model'])}</td>"
            f"<td>{row['tokens']}</td>"
            f"<td>{row['cost_usd']:.4f}</td>"
            f"<td>{html.escape(row['regulatory_class']) or '-'}</td>"
            f"{sig_cell}"
            "</tr>"
        )
    parts.extend(
        (
            "</tbody></table>",
            '<div class="footer">'
            "This artefact is suitable for inclusion in a DORA / NIS2 evidence package. "
            "Each record is independently signed by the customer key (when configured); "
            "the customer auditor can verify the signatures using the corresponding public key."
            "</div>",
            "</body></html>",
        )
    )
    return "\n".join(parts) + "\n"
