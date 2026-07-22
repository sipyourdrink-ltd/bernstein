"""``bernstein telemetry`` subcommand.

Operator-facing surface:

    bernstein telemetry on              opt in (operator-controlled backend)
    bernstein telemetry off             opt out (operator-controlled backend)
    bernstein telemetry status          show current state + flag-source provenance
    bernstein telemetry export          dump last 30 days of locally queued events
    bernstein telemetry enable          opt in to share with maintainer (RFC #1719)
    bernstein telemetry disable         revert share-with-maintainer flag
    bernstein telemetry tail [-n N]     preview the next N events offline

The output is deliberately compact and deterministic to enable snapshot
tests against the ``status`` command.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import click

from bernstein.core.telemetry import (
    OptInSource,
    config_file_path,
    ensure_install_id,
    install_id_path,
    queue_path,
    read_install_id,
    read_recent_events,
    reset_default_client,
    reset_install_id,
    resolve,
    write_enabled,
)
from bernstein.core.telemetry.consent import (
    consent_file_path,
    explain_share_source,
    resolve_share,
    write_share_flag,
)
from bernstein.core.telemetry.events import (
    CommandInvokedPayload,
    DailyActivePayload,
    FirstRunCompletedPayload,
    FirstRunStartedPayload,
    InstallCompletedPayload,
    TelemetryEvent,
)

# Operator-visible event schema and redaction list shown before the
# ``enable --share-with-maintainer`` flag is flipped. Both are kept here so
# the consent surface and the schema-guard test reference the same source
# of truth.
SAFE_PRIMITIVE_FIELDS: frozenset[str] = frozenset(
    {
        # InstallCompletedPayload
        "os",
        "py_version",
        "install_method",
        "bernstein_version",
        # FirstRunStartedPayload
        "time_since_install_seconds",
        # FirstRunCompletedPayload
        "ok",
        "duration_ms",
        "error_category",
        # CommandInvokedPayload
        "name_only",
        # DailyActivePayload
        "day_iso",
    }
)


REDACTION_RULES: dict[str, str] = {
    "file_paths": "any path-shaped value is replaced with a stable hash",
    "agent_output": "never collected; rendered text is dropped at the boundary",
    "diff_bytes": "never collected; source patches are not in the schema",
    "tool_call_args": "never collected; only the command name (no args) is emitted",
    "prompts": "never collected; user-authored prompts never enter the pipeline",
    "secrets": "never collected; env vars and credentials are not in the schema",
}


@click.group("telemetry")
def telemetry_group() -> None:
    """Manage opt-in operator observability.

    Bernstein collects no telemetry by default.  These commands let an
    operator opt in to a strictly bounded event set, inspect the local
    queue, and opt back out at any time.
    """


@telemetry_group.command("on")
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_on(home: Path | None) -> None:
    """Opt in.  Writes ``enabled: true`` and generates an install id."""
    write_enabled(True, home=home)
    reset_default_client()
    install_id = ensure_install_id(home=home)
    click.echo(f"telemetry: enabled (install_id={install_id[:8]}...)")


@telemetry_group.command("off")
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_off(home: Path | None) -> None:
    """Opt out.  Writes ``enabled: false`` and deletes the install id."""
    write_enabled(False, home=home)
    reset_install_id(home=home)
    reset_default_client()
    click.echo("telemetry: disabled")


@telemetry_group.command("status")
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_status(home: Path | None) -> None:
    """Show current state and which precedence layer determined it."""
    from bernstein.core.observability import sidechannel

    state = resolve(home=home)
    share = resolve_share(home=home)
    install_id = read_install_id(home=home)
    dsn = os.environ.get(sidechannel.DSN_ENV) or "(unset)"
    from bernstein.core.telemetry.share import resolve_share_endpoint

    share_endpoint_configured = resolve_share_endpoint(os.environ.copy()) is not None
    lines: list[str] = [
        f"enabled: {str(state.enabled).lower()}",
        f"source: {state.source.value}",
        f"install_id: {install_id or 'none'}",
        f"config_file: {config_file_path(home)}",
        f"install_id_path: {install_id_path(home)}",
        f"queue: {queue_path(home)}",
        f"share_with_maintainer: {str(share.enabled).lower()}",
        f"share_source: {share.source.value} ({explain_share_source(share.source)})",
        f"share_config_file: {consent_file_path(home=home)}",
        f"share_endpoint_configured: {str(share_endpoint_configured).lower()}",
        f"dsn: {dsn}",
    ]
    click.echo("\n".join(lines))


@telemetry_group.command("export")
@click.option(
    "--days",
    type=int,
    default=30,
    show_default=True,
    help="Number of days of locally queued events to dump.",
)
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_export(days: int, home: Path | None) -> None:
    """Dump locally queued events as JSONL."""
    for line in read_recent_events(days=days, home=home):
        click.echo(line)


@telemetry_group.command("probe")
@click.option(
    "--message",
    default="bernstein telemetry probe",
    show_default=True,
    help="Message body of the synthetic event.",
)
def telemetry_probe(message: str) -> None:
    """Emit a synthetic side-channel event so operators can verify the backend.

    Reads the portable ``BERNSTEIN_TELEMETRY_DSN`` and ships one synthetic
    ``probe`` event over the Sentry-compatible side channel. Use this after
    pointing a host-embedded Bernstein at your telemetry DSN to confirm the
    backend received the stream. No-op (with a clear message) when no DSN is
    configured.
    """
    import os

    from bernstein.core.observability import sidechannel

    if not os.environ.get(sidechannel.DSN_ENV):
        click.echo(
            f"telemetry probe: {sidechannel.DSN_ENV} is not set; nothing emitted.\n"
            "Set it to a Sentry-compatible DSN and re-run to verify the backend."
        )
        return

    sink = sidechannel.build_sidechannel()
    if isinstance(sink, sidechannel.NullSideChannel):
        click.echo(
            f"telemetry probe: {sidechannel.DSN_ENV} is set but could not be parsed; "
            "nothing emitted. See the log line above for the reason."
        )
        return

    event = sidechannel.SideChannelEvent(
        category="probe",
        message=message,
        level=sidechannel.EventLevel.INFO,
        tags={"synthetic": "true"},
        extra={"probe": True},
    )
    accepted = sink.emit(event)
    sink.flush()
    sink.close()
    if accepted:
        click.echo(f"telemetry probe: event {event.event_id} queued for delivery.")
        click.echo("Check your GlitchTip project; the event carries logger=bernstein.probe.")
    else:
        click.echo("telemetry probe: event was dropped under backpressure.")


def _render_consent_disclosure() -> str:
    """Render the schema + redaction disclosure shown before the flag flip.

    The text is the operator-facing source of truth for the consent
    contract. Any change to the event schema must update this disclosure
    and the matching schema-guard test in lockstep.
    """
    schemas: dict[str, type[Any]] = {
        TelemetryEvent.INSTALL_COMPLETED.value: InstallCompletedPayload,
        TelemetryEvent.FIRST_RUN_STARTED.value: FirstRunStartedPayload,
        TelemetryEvent.FIRST_RUN_COMPLETED.value: FirstRunCompletedPayload,
        TelemetryEvent.COMMAND_INVOKED.value: CommandInvokedPayload,
        TelemetryEvent.DAILY_ACTIVE.value: DailyActivePayload,
    }
    lines = [
        "Event schema (RFC #1719 foundation):",
        "",
    ]
    for name, cls in schemas.items():
        fields = ", ".join(cls.__slots__) or "(no fields)"
        lines.append(f"  - {name}: {fields}")
    lines.extend(
        [
            "",
            "Redaction rules applied at the boundary:",
            "",
        ]
    )
    for key, rule in REDACTION_RULES.items():
        lines.append(f"  - {key}: {rule}")
    lines.extend(
        [
            "",
            "No file paths, agent output, diff bytes, tool-call args, prompts, or",
            "secrets are collected. See docs/observability/telemetry-share.md for",
            "the full contract, how to audit offline (bernstein telemetry tail),",
            "and how to revoke (bernstein telemetry disable).",
        ]
    )
    return "\n".join(lines)


@telemetry_group.command("enable")
@click.option(
    "--share-with-maintainer",
    "share_with_maintainer",
    is_flag=True,
    required=True,
    help="Opt in to share telemetry with the project maintainer (RFC #1719).",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_enable(
    share_with_maintainer: bool,
    assume_yes: bool,
    home: Path | None,
) -> None:
    """Opt in to share telemetry with the project maintainer (RFC #1719).

    Prints the full event schema and redaction list, then requires
    explicit confirmation before persisting the
    ``share_with_maintainer = true`` flag to
    ``$XDG_CONFIG_HOME/bernstein/telemetry.toml``.
    """
    if not share_with_maintainer:
        # ``required=True`` on the click option keeps the flag mandatory,
        # but this guard documents the invariant for readers.
        raise click.UsageError("--share-with-maintainer is required")

    click.echo(_render_consent_disclosure())
    click.echo("")

    if not assume_yes:
        confirmed = click.confirm(
            "Proceed and set share_with_maintainer = true?",
            default=False,
        )
        if not confirmed:
            click.echo("telemetry: no change (consent declined).")
            return

    path = write_share_flag(True, home=home)
    click.echo(f"telemetry: share_with_maintainer = true (written to {path}).")
    click.echo("Revert any time with `bernstein telemetry disable`.")


@telemetry_group.command("disable")
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the operator home directory (testing).",
)
def telemetry_disable(home: Path | None) -> None:
    """Revert the maintainer-share consent flag.

    Writes ``share_with_maintainer = false`` to the consent TOML file. The
    operator-controlled telemetry pipeline (``bernstein telemetry on/off``)
    is unaffected.
    """
    path = write_share_flag(False, home=home)
    click.echo(f"telemetry: share_with_maintainer = false (written to {path}).")


@telemetry_group.command("export-otel")
@click.option("--run", "run_id", required=True, help="Run id whose event journal to export.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--endpoint",
    default=None,
    help="OTLP/gRPC collector endpoint (defaults to BERNSTEIN_OTEL_ENDPOINT).",
)
@click.option(
    "--no-genai-stability",
    "no_stability",
    is_flag=True,
    default=False,
    help="Omit the (Development-stage) GenAI convention attributes; ids stay journal-anchored.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the OTLP/JSON spans to stdout instead of exporting; no network, no audit event.",
)
def telemetry_export_otel(
    run_id: str,
    workdir: str,
    endpoint: str | None,
    no_stability: bool,
    dry_run: bool,
) -> None:
    """Backfill a completed run's journal-anchored spans over OTLP (#2526).

    Projects ``--run``'s event journal into the deterministic span set
    (span ids derived from journal entry hashes, every span carrying
    ``bernstein.journal.entry_hash`` and the ``bernstein.audit.anchor``
    run-head hash), exports it to the collector, and records the
    ``otel.projection`` audit event binding the exported trace to the
    chain. Re-running over the same journal exports byte-identical spans.

    Exit codes: 0 = exported, 1 = no journal / no endpoint / bad input.
    """
    from bernstein.core.observability.otel_bridge import (
        JournalOTLPBridge,
        OTLPExportError,
        projection_to_otlp_json_spans,
        record_projection_audit_event,
    )
    from bernstein.core.observability.otel_projection import (
        ProjectionError,
        project_spans,
        sign_projection,
    )
    from bernstein.core.observability.otlp_exporter import OTLPExporterConfig
    from bernstein.core.replay.journal import JournalPathError, load_events, run_journal_path
    from bernstein.core.security.audit_dsse import keyid_from_public_key
    from bernstein.core.security.install_key import (
        InstallKeyError,
        load_or_create_install_key,
        signing_key_path,
    )
    from bernstein.core.security.sanitize import sanitize_log

    root = Path(workdir).resolve()
    try:
        journal_path = run_journal_path(root / ".sdd", run_id)
    except JournalPathError as exc:
        raise click.ClickException(sanitize_log(str(exc))) from exc
    events = load_events(journal_path)
    if not events:
        raise click.ClickException(
            f"no event journal for run {sanitize_log(run_id)} at {sanitize_log(str(journal_path))}",
        )

    try:
        key = load_or_create_install_key(signing_key_path(root))
    except InstallKeyError as exc:
        raise click.ClickException(sanitize_log(str(exc))) from exc

    try:
        projection = project_spans(
            events,
            run_id=run_id,
            genai_stability=not no_stability,
            keyid=keyid_from_public_key(key.public_key()),
        )
    except ProjectionError as exc:
        raise click.ClickException(sanitize_log(str(exc))) from exc
    signed = sign_projection(projection, signing_key=key)

    if dry_run:
        click.echo(json.dumps(projection_to_otlp_json_spans(signed, events), sort_keys=True, indent=2))
        return

    base = OTLPExporterConfig.from_env()
    # Preserve every env-derived field (headers/auth, insecure/TLS,
    # resource attributes); only the endpoint is overridden. Rebuilding a
    # fresh config would silently drop those.
    config = base if endpoint is None else dataclasses.replace(base, endpoint=endpoint)
    if config.endpoint is None:
        raise click.ClickException(
            "no OTLP endpoint configured; set BERNSTEIN_OTEL_ENDPOINT or pass --endpoint (or use --dry-run)",
        )
    bridge = JournalOTLPBridge(config, batch=False)
    if not bridge.enabled:
        raise click.ClickException(
            "OTLP exporter unavailable; install 'bernstein[otel]' for opentelemetry-exporter-otlp-proto-grpc",
        )
    try:
        count = bridge.export_projection(signed, events)
    except OTLPExportError as exc:
        # The collector returned FAILURE (unreachable / rejected): do not
        # report success and do not record an audit event for undelivered
        # spans. Exit nonzero so scripts can detect the failed export.
        raise click.ClickException(
            f"OTLP export failed; no spans delivered and no audit event recorded: {sanitize_log(str(exc))}",
        ) from exc
    finally:
        bridge.shutdown()

    try:
        record_projection_audit_event(
            workdir=root,
            journal_path=journal_path,
            run_id=run_id,
            genai_stability=not no_stability,
        )
    except Exception as exc:  # pragma: no cover - defensive; export is primary
        click.echo(f"warning: otel projection audit record failed: {sanitize_log(str(exc))}", err=True)

    click.echo(
        f"exported {count} journal-anchored spans for run {sanitize_log(run_id)} "
        f"(trace {signed.trace_id[:16]}...) to {sanitize_log(config.endpoint)}"
    )


@telemetry_group.command("tail")
@click.option(
    "-n",
    "count",
    type=int,
    default=10,
    show_default=True,
    help="Number of recent preview events to print.",
)
def telemetry_tail(count: int) -> None:
    """Print the next N events that would be sent, offline.

    Reads from the side-channel preview ring buffer. Events are rendered
    in the same shape they would be posted in, one JSON object per line,
    oldest-first. Use this to audit the stream before deciding whether to
    opt in via ``bernstein telemetry enable --share-with-maintainer``.
    """
    from bernstein.core.observability import sidechannel

    events = sidechannel.read_preview(count)
    if not events:
        click.echo(
            "telemetry tail: no events buffered yet. The preview buffer fills\n"
            "as the side-channel emit helper records events at the boundary."
        )
        return
    for event in events:
        click.echo(json.dumps(event, sort_keys=True, separators=(",", ":")))


def explain_source(source: OptInSource) -> str:
    """Return a one-line operator-facing description of ``source``."""
    if source is OptInSource.DO_NOT_TRACK:
        return "DO_NOT_TRACK env var (universal opt-out)"
    if source is OptInSource.ENV:
        return "BERNSTEIN_TELEMETRY env var"
    if source is OptInSource.FILE:
        return "config file (~/.bernstein/telemetry.yaml)"
    return "default (off)"


# Expose the group for registration in main.py.
def register(parent: Any) -> None:
    """Register this subgroup on a parent click group."""
    parent.add_command(telemetry_group)


__all__ = [
    "REDACTION_RULES",
    "SAFE_PRIMITIVE_FIELDS",
    "explain_source",
    "register",
    "telemetry_disable",
    "telemetry_enable",
    "telemetry_export",
    "telemetry_export_otel",
    "telemetry_group",
    "telemetry_off",
    "telemetry_on",
    "telemetry_probe",
    "telemetry_status",
    "telemetry_tail",
]
