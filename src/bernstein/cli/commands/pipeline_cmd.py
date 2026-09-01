"""``bernstein pipeline`` - drive the tracker handoff pipeline.

Subcommands:

* ``pipeline run`` - one sweep across configured trackers (used by
  cron/timers; not a long-running loop).
* ``pipeline status`` - print open handoffs for the configured
  trackers from the in-process ledger and (optionally) JSON.

The CLI is deliberately thin: every meaningful decision lives in
:class:`bernstein.core.orchestration.tracker_pipeline.TrackerPipeline`.
The CLI is responsible only for resolving ``bernstein.yaml``, wiring
adapters from the registered tracker module, driving the sweep, and
recording the audit chain record.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import yaml

from bernstein.cli.helpers import console
from bernstein.core.orchestration.tracker_pipeline import (
    DEFAULT_LEDGER_RELPATH,
    ClaimLedger,
    DispatchOutcome,
    PipelineConfig,
    StageHandoff,
    TrackerPipelineError,
    build_pipeline_from_yaml,
)
from bernstein.core.security.audit_chain import (
    AuditChainStore,
    record_tracker_pipeline_sweep,
)
from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    AttachResult,
    ClaimResult,
    CommentResult,
    Ticket,
    TransitionResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)


SDD_ROOT_RELPATH = Path(".sdd")


class _TrackingTrackerAdapter(AbstractTrackerAdapter):
    """Wraps a tracker adapter to capture runtime sweep exceptions."""

    def __init__(self, name: str, inner: AbstractTrackerAdapter, error_sink: list[str]) -> None:
        self.name = name
        self._inner = inner
        self._error_sink = error_sink

    def pull_open_tickets(self, filter: dict[str, Any] | None = None) -> Iterator[Ticket]:
        try:
            return self._inner.pull_open_tickets(filter)
        except Exception as exc:
            self._error_sink.append(f"tracker {self.name!r} pull_open_tickets failed: {exc}")
            raise

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommentResult:
        try:
            return self._inner.add_comment(ticket_id, body, idempotency_key=idempotency_key)
        except Exception as exc:
            self._error_sink.append(f"tracker {self.name!r} ticket {ticket_id!r} add_comment failed: {exc}")
            raise

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult:
        try:
            return self._inner.transition(ticket_id, status_id, idempotency_key=idempotency_key, etag=etag)
        except Exception as exc:
            self._error_sink.append(f"tracker {self.name!r} ticket {ticket_id!r} transition failed: {exc}")
            raise

    def claim_ticket(
        self,
        ticket_id: str,
        agent_id: str,
        *,
        etag: str | None = None,
    ) -> ClaimResult:
        return self._inner.claim_ticket(ticket_id, agent_id, etag=etag)

    def attach_blob(
        self,
        ticket_id: str,
        blob: bytes,
        mime: str,
        *,
        idempotency_key: str | None = None,
    ) -> AttachResult:
        return self._inner.attach_blob(ticket_id, blob, mime, idempotency_key=idempotency_key)


class _CliDispatcher:
    """Default role-execution surface for CLI sweeps."""

    def dispatch(
        self,
        *,
        tracker: str,
        ticket: Ticket,
        role: str,
        stage_attempt: int,
        idempotency_key: str,
    ) -> DispatchOutcome:
        return DispatchOutcome(success=True, summary=f"cli sweep: {role}")


@click.group("pipeline")
def pipeline_group() -> None:
    """Drive the tracker comments handoff pipeline."""


@pipeline_group.command("run")
@click.option(
    "--workflow",
    "workflow_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override path to the pipeline YAML (defaults to bernstein.yaml).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("bernstein.yaml"),
    show_default=True,
    help="Path to bernstein.yaml.",
)
@click.option(
    "--state-root",
    "state_root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=SDD_ROOT_RELPATH,
    show_default=True,
    help="Project state root containing the SQLite ledger and audit chain.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved pipeline config without contacting any tracker.",
)
def run_cmd(
    *,
    workflow_path: Path | None,
    config_path: Path,
    state_root: Path = SDD_ROOT_RELPATH,
    dry_run: bool,
) -> None:
    """Run a single sweep of the tracker handoff pipeline.

    The command is non-blocking: each invocation walks every
    configured tracker once. Operators schedule recurring invocations
    via systemd, cron, or the existing ``bernstein daemon`` runner.
    """
    target_path = workflow_path or config_path
    config = _resolve_config(target_path)
    if not config.pipeline_stages:
        console.print(
            "[yellow]No pipeline stages configured under orchestration.tracker_pipeline; nothing to do.[/yellow]"
        )
        return

    if dry_run:
        _print_config(config)
        return

    raw_block = _load_pipeline_block(target_path)
    sweep_errors: list[str] = []
    tracker_configs = _load_tracker_configurations(target_path, fallback_path=config_path)
    trackers, init_errors = _instantiate_trackers(tracker_configs, sweep_errors)

    dispatcher = _CliDispatcher()
    pipeline = build_pipeline_from_yaml(
        raw_block,
        trackers=trackers,
        dispatcher=dispatcher,
        state_root=state_root,
    )

    try:
        pipeline.tick()
    except Exception as exc:
        logger.exception("pipeline sweep encountered an unhandled exception")
        sweep_errors.append(f"pipeline.tick() failed: {exc}")

    config_digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
    all_errors = init_errors + sweep_errors

    stage_outcomes: dict[str, str] = {}
    for stage in config.pipeline_stages:
        stage_handoffs = [h for h in pipeline.handoffs if h.role == stage.role]
        if all_errors:
            if not stage_handoffs:
                stage_outcomes[stage.role] = "error"
            elif all(h.outcome == "success" for h in stage_handoffs):
                stage_outcomes[stage.role] = "partial_error"
            else:
                stage_outcomes[stage.role] = "failure"
        else:
            if not stage_handoffs:
                stage_outcomes[stage.role] = "idle"
            elif all(h.outcome == "success" for h in stage_handoffs):
                stage_outcomes[stage.role] = "success"
            elif all(h.outcome == "failure" for h in stage_handoffs):
                stage_outcomes[stage.role] = "failure"
            else:
                stage_outcomes[stage.role] = "partial"

    audit_dir = state_root / "audit"
    try:
        chain = AuditChainStore(audit_dir)
        record_tracker_pipeline_sweep(
            chain=chain,
            config_digest=config_digest,
            trackers_configured=list(tracker_configs.keys()),
            trackers_contacted=list(trackers.keys()),
            handoffs=pipeline.open_handoffs(),
            stage_outcomes=stage_outcomes,
            status="ok" if not all_errors else "failed",
            errors=all_errors if all_errors else None,
        )
    except Exception as exc:
        raise click.ClickException(f"Failed to record sweep audit chain entry: {exc}") from exc

    if pipeline.handoffs:
        from rich.table import Table

        table = Table(title=f"Tracker pipeline sweep ({len(pipeline.handoffs)} handoff(s))")
        table.add_column("Tracker")
        table.add_column("Ticket")
        table.add_column("Role")
        table.add_column("Outcome")
        table.add_column("Transition")
        for h in pipeline.handoffs:
            outcome_str = f"[green]{h.outcome}[/green]" if h.outcome == "success" else f"[red]{h.outcome}[/red]"
            table.add_row(
                h.tracker,
                h.ticket_id,
                h.role,
                outcome_str,
                f"{h.from_status} -> {h.to_status}",
            )
        console.print(table)
    else:
        tracker_count = len(trackers)
        if tracker_count == 0 and not all_errors:
            console.print("[dim]Sweep complete: 0 trackers configured; 0 handoffs.[/dim]")
        elif not all_errors:
            console.print(f"[dim]Sweep complete: {tracker_count} tracker(s) contacted; 0 handoffs.[/dim]")

    if all_errors:
        console.print(f"[red]Sweep completed with {len(all_errors)} error(s):[/red]")
        for err in all_errors:
            console.print(f"  [red]• {err}[/red]")
        raise click.ClickException(f"Tracker pipeline sweep failed with {len(all_errors)} error(s)")


@pipeline_group.command("status")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("bernstein.yaml"),
    show_default=True,
    help="Path to bernstein.yaml.",
)
@click.option(
    "--state-root",
    "state_root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=SDD_ROOT_RELPATH,
    show_default=True,
    help="Project state root containing the SQLite ledger.",
)
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of a Rich table.",
)
def status_cmd(*, config_path: Path, state_root: Path, as_json: bool) -> None:
    """Print open handoffs from the SQLite ledger.

    The output is generated from the SQLite ledger so it stays
    accurate across worker restarts. The pipeline config is loaded to
    resolve role names back to their declared statuses.
    """
    config = _resolve_config(config_path)
    ledger_path = state_root / DEFAULT_LEDGER_RELPATH
    open_handoffs = _read_open_handoffs(ledger_path, config)
    if as_json:
        click.echo(json.dumps(open_handoffs, indent=2, sort_keys=True))
        return
    if not open_handoffs:
        console.print("[dim]No open handoffs.[/dim]")
        return
    from rich.table import Table  # local import keeps CLI start-up fast

    table = Table(title="Open tracker handoffs")
    table.add_column("Tracker")
    table.add_column("Ticket")
    table.add_column("Role")
    table.add_column("Attempt", justify="right")
    table.add_column("Lease expires (s)")
    for row in open_handoffs:
        table.add_row(
            row["tracker"],
            row["ticket_id"],
            row["role"],
            str(row["stage_attempt"]),
            f"{row['lease_seconds_remaining']:.0f}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_config(path: Path) -> PipelineConfig:
    """Load and parse the pipeline block, reporting config errors as CLI errors.

    ``PipelineStage.from_dict`` already produces the sentence an operator needs -
    "pipeline stage missing required key: role" - and both commands discarded it by
    letting ``TrackerPipelineError`` escape. A YAML typo surfaced as a Python traceback
    with the useful line buried in the middle, which reads as a crash in Bernstein rather
    than a mistake in the file the operator just edited.
    """
    try:
        raw = _load_pipeline_block(path)
    except yaml.YAMLError as exc:
        # The commoner mistake of the two: a bad indent or an unclosed quote
        # reached the operator as a ParserError traceback, which reads as a
        # crash in bernstein rather than a typo in the file they just edited.
        raise click.ClickException(f"{path}: {exc}") from exc
    try:
        return PipelineConfig.from_dict(raw)
    except TrackerPipelineError as exc:
        raise click.ClickException(str(exc)) from exc


def _load_pipeline_block(path: Path) -> Mapping[str, Any]:
    """Return the ``orchestration.tracker_pipeline`` block from ``path``.

    ``path`` may point at ``bernstein.yaml`` (we walk the nested keys)
    or at a stand-alone workflow file (we accept the block at root).
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    if "pipeline_stages" in data:
        return data
    orchestration_block = data.get("orchestration", {}) or {}
    if not isinstance(orchestration_block, dict):
        return {}
    pipeline_block = orchestration_block.get("tracker_pipeline", {}) or {}
    if not isinstance(pipeline_block, dict):
        return {}
    return pipeline_block


def _print_config(config: PipelineConfig) -> None:
    """Render the resolved config as a Rich table."""
    from rich.table import Table

    table = Table(title="Tracker pipeline (resolved)")
    table.add_column("Role")
    table.add_column("Claim status")
    table.add_column("Success status")
    table.add_column("Failure status")
    table.add_column("Requires prior")
    for stage in config.pipeline_stages:
        table.add_row(
            stage.role,
            stage.claim_status,
            stage.success_status,
            stage.failure_status,
            stage.requires_prior_role or "-",
        )
    console.print(table)
    console.print(
        f"[dim]claim_lock_ttl_seconds={config.claim_lock_ttl_seconds} "
        f"per_role_max_in_flight={config.per_role_max_in_flight}[/dim]"
    )


def _read_open_handoffs(ledger_path: Path, config: PipelineConfig) -> list[dict[str, Any]]:
    """Return live (non-expired) ledger rows ordered (tracker, role, ticket).

    The ledger has a single source of truth for the SQLite schema and
    PRAGMAs; we route this read through :meth:`ClaimLedger.live_claims`
    so the status view stays consistent with the runtime claim path,
    and expired rows are filtered server-side so they do not bleed into
    operator dashboards.
    """
    if not ledger_path.exists():
        return []
    ledger = ClaimLedger(ledger_path)
    try:
        rows = ledger.live_claims()
    finally:
        ledger.close()
    # ``config`` is accepted so we can flag unknown roles in the
    # status table; tag them with ``unknown_role`` in JSON output.
    known_roles = {stage.role for stage in config.pipeline_stages}
    for row in rows:
        if row["role"] not in known_roles:
            row["unknown_role"] = True
    return rows


def render_handoff(handoff: StageHandoff) -> str:
    """Return a one-line display string for ``handoff`` (used by callers)."""
    return (
        f"{handoff.tracker}:{handoff.ticket_id} {handoff.role} "
        f"{handoff.from_status} -> {handoff.to_status} ({handoff.outcome})"
    )


def _load_tracker_configurations(
    path: Path,
    fallback_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return configured trackers mapping from ``path`` (or fallback)."""
    configs: dict[str, dict[str, Any]] = {}
    paths_to_try = [path]
    if fallback_path is not None and fallback_path != path:
        paths_to_try.append(fallback_path)
    for p in paths_to_try:
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise click.ClickException(f"{p}: {exc}") from exc
        if not isinstance(data, dict):
            continue
        raw_trackers = data.get("trackers")
        if raw_trackers is None:
            orch = data.get("orchestration")
            if isinstance(orch, dict):
                tp = orch.get("tracker_pipeline")
                if isinstance(tp, dict):
                    raw_trackers = tp.get("trackers")
        if isinstance(raw_trackers, dict):
            for name, cfg in raw_trackers.items():
                if isinstance(name, str):
                    configs[name] = cfg if isinstance(cfg, dict) else {}
            if configs:
                break
        elif isinstance(raw_trackers, (list, tuple)):
            for item in raw_trackers:
                if isinstance(item, str):
                    configs[item] = {}
                elif isinstance(item, dict):
                    for name, cfg in item.items():
                        if isinstance(name, str):
                            configs[name] = cfg if isinstance(cfg, dict) else {}
            if configs:
                break
    return configs


def _instantiate_trackers(
    tracker_configs: dict[str, dict[str, Any]],
    sweep_errors: list[str],
) -> tuple[dict[str, AbstractTrackerAdapter], list[str]]:
    """Resolve and construct tracker adapters from the registry."""
    from bernstein.core.trackers.registry import (
        discover_plugin_trackers,
        get_registry,
    )

    registry = get_registry()
    discovery_error: Exception | None = None
    try:
        discover_plugin_trackers()
    except Exception as exc:
        logger.debug("plugin tracker discovery failed: %s", exc)
        discovery_error = exc

    adapters: dict[str, AbstractTrackerAdapter] = {}
    init_errors: list[str] = []
    for name, cfg in tracker_configs.items():
        if name not in registry:
            if discovery_error is not None:
                err = (
                    f"Configured tracker {name!r} not found in registry "
                    f"(plugin discovery failed earlier: {discovery_error})"
                )
            else:
                err = f"Configured tracker {name!r} not found in registry"
            logger.warning(err)
            init_errors.append(err)
            continue
        try:
            raw_adapter = registry.create(name, **cfg)
            adapters[name] = _TrackingTrackerAdapter(name, raw_adapter, sweep_errors)
        except Exception as exc:
            err = f"Failed to instantiate tracker adapter {name!r}: {exc}"
            logger.warning(err)
            init_errors.append(err)
    return adapters, init_errors
