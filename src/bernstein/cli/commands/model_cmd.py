"""``bernstein model``: model registry and impact analysis (issue #5038).

The model registry is a deterministic projection of admission/withdrawal events
from the audit chain. ``registry --at <timestamp>`` reconstructs the permitted
set at any past instant; ``impact <ref>`` lists artefacts produced by a model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click


AUDIT_DIR = Path(".sdd/audit")
LINEAGE_DIR = Path(".sdd/lineage")


@click.group("model")
def model_group() -> None:
    """Model registry and impact analysis."""


@model_group.command("registry")
@click.option(
    "--at",
    "at_timestamp",
    default=None,
    help="Reconstruct the registry at this timestamp (YYYY-MM-DDTHH:MM:SS.fZ format).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def registry_cmd(at_timestamp: str | None, as_json: bool) -> None:
    """Show the model admission registry.

    Without --at, shows the current state. With --at, reconstructs the state
    that held at the named instant by replaying the audit chain up to that point.

    Examples:
        bernstein model registry
        bernstein model registry --at 2026-09-14T10:00:00.000000Z
        bernstein model registry --json
    """
    from bernstein.core.routing.model_registry import (
        format_timestamp,
        load_registry_events,
        project_registry,
    )
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore

    if not AUDIT_DIR.is_dir():
        click.echo("✗ Audit directory not found: .sdd/audit", err=True)
        raise SystemExit(1)

    chain = AuditChainStore(AUDIT_DIR, key=load_or_create_audit_key())
    events = load_registry_events(chain)

    if at_timestamp is None:
        at_timestamp = format_timestamp(datetime.now(tz=UTC))

    try:
        state = project_registry(events, at=at_timestamp)
    except (ValueError, Exception) as exc:
        click.echo(f"✗ Failed to project registry: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        import json

        output = {
            "at": state.at,
            "admissions": [
                {
                    "model_key": a.model_key,
                    "provider": a.provider,
                    "model": a.model,
                    "version": a.version,
                    "task_classes": list(a.task_classes),
                    "admitted_by": a.admitted_by,
                    "admitted_at": a.admitted_at,
                    "expires_at": a.expires_at,
                    "evidence_ref": a.evidence_ref,
                }
                for a in state.admissions
            ],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if not state.admissions:
            click.echo(f"No models admitted at {state.at}")
        else:
            click.echo(f"Model registry at {state.at}:\n")
            for admission in state.admissions:
                click.echo(f"  {admission.model_key}")
                click.echo(f"    Task classes: {', '.join(admission.task_classes)}")
                click.echo(f"    Admitted by:  {admission.admitted_by}")
                click.echo(f"    Admitted at:  {admission.admitted_at}")
                click.echo(f"    Expires at:   {admission.expires_at}")
                if admission.evidence_ref:
                    click.echo(f"    Evidence:     {admission.evidence_ref}")
                click.echo()


@model_group.command("impact")
@click.argument("model_ref", required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def impact_cmd(model_ref: str, as_json: bool) -> None:
    """List artefacts produced by a model.

    MODEL_REF is provider/model format, e.g., anthropic/opus or openai/gpt-4.

    This command joins the model registry to the lineage ledger's model_ref
    field to show which artefacts were produced by a given model, enabling
    impact analysis when a model is withdrawn.

    Examples:
        bernstein model impact anthropic/opus
        bernstein model impact openai/gpt-4 --json
    """
    from bernstein.core.lineage.store import LineageStore

    if not LINEAGE_DIR.is_dir():
        click.echo("✗ Lineage directory not found: .sdd/lineage", err=True)
        raise SystemExit(1)

    # Parse model_ref: provider/model or provider/model/version
    parts = model_ref.split("/")
    if len(parts) < 2:
        click.echo("✗ MODEL_REF must be provider/model or provider/model/version", err=True)
        raise SystemExit(1)

    provider = parts[0]
    model = parts[1]
    version = parts[2] if len(parts) > 2 else None

    # Load lineage entries and filter by model_ref
    store = LineageStore(LINEAGE_DIR)
    matching = []

    for entry, _jws in store.read_log():
        if entry.model_ref is None:
            continue
        ref = entry.model_ref
        if ref.provider != provider:
            continue
        # Match either requested or reported model name
        if ref.model_requested != model and (ref.model_reported is None or ref.model_reported != model):
            continue
        if version is not None and ref.version != version:
            continue
        matching.append(entry)

    if as_json:
        import json

        output = {
            "model_ref": model_ref,
            "count": len(matching),
            "artefacts": [
                {
                    "entry_hmac": e.entry_hmac,
                    "task_id": e.task_id,
                    "artefact_path": e.artefact_path,
                    "provider": e.model_ref.provider if e.model_ref else None,
                    "model_requested": e.model_ref.model_requested if e.model_ref else None,
                    "model_reported": e.model_ref.model_reported if e.model_ref else None,
                    "version": e.model_ref.version if e.model_ref else None,
                }
                for e in matching
            ],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        if not matching:
            click.echo(f"No artefacts found for {model_ref}")
            raise SystemExit(1)
        else:
            click.echo(f"Found {len(matching)} artefact(s) produced by {model_ref}:\n")
            for entry in matching:
                click.echo(f"  {entry.entry_hmac[:12]} — {entry.artefact_path}")
                if entry.task_id:
                    click.echo(f"    Task: {entry.task_id}")
                if entry.model_ref:
                    if entry.model_ref.model_reported and entry.model_ref.model_reported != entry.model_ref.model_requested:
                        click.echo(f"    (requested {entry.model_ref.model_requested}, got {entry.model_ref.model_reported})")
