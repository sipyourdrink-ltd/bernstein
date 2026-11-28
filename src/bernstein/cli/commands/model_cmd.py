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
        raise SystemExit(1) from exc

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


def _parse_model_ref(raw: str) -> tuple[str, str, str | None]:
    """Split ``provider/model[@version]`` into ``(provider, model, version)``.

    This is the key ``model registry`` prints. The provider is everything up to
    the first ``/`` (a provider never contains one) and the version is
    everything after the last ``@``; the model name in between may itself
    contain ``/``. ``@*`` and no ``@`` both mean "any snapshot" and return a
    version of ``None``.

    Raises:
        ValueError: When the reference has no provider or no model.
    """
    body, sep, version = raw.rpartition("@")
    if not sep:
        body, version = raw, ""
    provider, slash, model = body.partition("/")
    if not provider or not slash or not model:
        raise ValueError("MODEL_REF must be provider/model or provider/model@version")
    return provider, model, (None if version in ("", "*") else version)


@model_group.command("impact")
@click.argument("model_ref", required=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def impact_cmd(model_ref: str, as_json: bool) -> None:
    """List artefacts produced by a model.

    MODEL_REF is provider/model or provider/model@version, the key
    ``bernstein model registry`` prints, e.g. anthropic/opus or
    openrouter/meta-llama/Llama-3@2026-01. Without a version, every snapshot
    matches.

    This command joins the model registry to the lineage ledger's model_ref
    field to show which artefacts were produced by a given model, enabling
    impact analysis when a model is withdrawn. Only lineage entries written
    with a model reference can match; the command says so when the ledger holds
    none.

    Examples:
        bernstein model impact anthropic/opus
        bernstein model impact openai/gpt-4@2026-01 --json
    """
    from bernstein.core.lineage.entry import entry_hash
    from bernstein.core.lineage.store import LineageStore

    if not LINEAGE_DIR.is_dir():
        click.echo("✗ Lineage directory not found: .sdd/lineage", err=True)
        raise SystemExit(1)

    try:
        provider, model, version = _parse_model_ref(model_ref)
    except ValueError as exc:
        click.echo(f"✗ {exc}", err=True)
        raise SystemExit(1) from exc

    # Load lineage entries and filter by model_ref
    store = LineageStore(LINEAGE_DIR)
    matching = []
    with_model_ref = 0

    for entry, _jws in store.read_log():
        if entry.model_ref is None:
            continue
        with_model_ref += 1
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
            "entries_with_model_ref": with_model_ref,
            "artefacts": [
                {
                    "entry_hash": entry_hash(e),
                    "artefact_path": e.artefact_path,
                    "agent_id": e.agent_id,
                    "tool_call_id": e.tool_call_id,
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
            if with_model_ref == 0:
                click.echo(
                    "No lineage entry records a model reference, so this ledger cannot say "
                    f"which artefacts {model_ref} produced."
                )
            else:
                click.echo(f"No artefacts found for {model_ref}")
            raise SystemExit(1)
        click.echo(f"Found {len(matching)} artefact(s) produced by {model_ref}:\n")
        for entry in matching:
            click.echo(f"  {entry_hash(entry)[7:19]} — {entry.artefact_path}")
            click.echo(f"    Agent: {entry.agent_id}")
            if (
                entry.model_ref
                and entry.model_ref.model_reported
                and entry.model_ref.model_reported != entry.model_ref.model_requested
            ):
                click.echo(f"    (requested {entry.model_ref.model_requested}, got {entry.model_ref.model_reported})")


@model_group.command("admit")
@click.argument("model_ref")
@click.option("--bundle", "bundle_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--suite", required=True, help="Operator-qualified suite name or JSON file (not read from bundle).")
@click.option("--task-class", "task_classes", multiple=True, required=True, help="Requested admission scope.")
@click.option(
    "--qualified-task-class",
    "qualified_classes",
    multiple=True,
    required=True,
    help="Task classes the operator qualifies this suite for, independent of bundle claims.",
)
@click.option("--trusted-key", "trusted_keys", multiple=True, required=True, help="FINGERPRINT=PUBLIC_PEM_PATH.")
@click.option("--admitted-by", required=True, help="Identity of the human operator recording admission.")
@click.option("--expires-at", required=True, help="Required UTC expiry: YYYY-MM-DDTHH:MM:SS.ffffffZ.")
def admit_cmd(
    model_ref: str,
    bundle_path: Path,
    suite: str,
    task_classes: tuple[str, ...],
    qualified_classes: tuple[str, ...],
    trusted_keys: tuple[str, ...],
    admitted_by: str,
    expires_at: str,
) -> None:
    """Record an operator admission only after offline evidence qualification.

    Exits 0 after appending the event, 1 on invalid or unqualified evidence.
    This is an explicit human/operator action, never an automatic admission.
    Legacy bundles remain verifiable with bench verify but cannot qualify here.
    """
    from bernstein.core.routing.model_registry import record_model_admission
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.eval.bench.bench_cli import _get_suite, _load_trusted_keys, _resolve_adapter
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.model_admission import ModelAdmissionEvidence

    try:
        provider, model, version = _parse_model_ref(model_ref)
        suite_obj = _get_suite(suite)
        evidence = ModelAdmissionEvidence(
            bundle=SubmissionBundle.load(bundle_path),
            suite=suite_obj,
            adapter=_resolve_adapter(suite_obj),
            trusted_keys=_load_trusted_keys(trusted_keys),
            qualified_task_classes=qualified_classes,
        )
        # Qualify before creating any audit/key state. The event writer repeats
        # this check immediately before the append; no stored MATCH is trusted.
        evidence.verify(provider=provider, model=model, version=version, task_classes=task_classes)
        chain = AuditChainStore(AUDIT_DIR, key=load_or_create_audit_key())
        event = record_model_admission(
            chain=chain,
            provider=provider,
            model=model,
            version=version,
            task_classes=task_classes,
            admitted_by=admitted_by,
            expires_at=expires_at,
            evidence=evidence,
            require_verified_evidence=True,
        )
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Admitted {event.resource_id}; evidence {event.details['evidence_ref']}")
