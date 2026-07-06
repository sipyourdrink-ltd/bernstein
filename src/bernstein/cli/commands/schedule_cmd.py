"""CLI commands for operator-registered recurring schedules.

Commands (issue #1798):

- ``bernstein schedule add --cron <expr> --goal <text> [--scenario <id>]``
- ``bernstein schedule list [--json]``
- ``bernstein schedule remove <id>``
- ``bernstein schedule show <id> [--json]``
- ``bernstein schedule run``
- ``bernstein schedule audit [--json]``

The CLI is a thin shell around
:class:`bernstein.core.planning.schedule_store.ScheduleStore` and
:class:`bernstein.core.orchestration.schedule_supervisor.ScheduleSupervisor`.
All business logic lives in the core modules so the same surface drives
the CLI, the daemon hook, and the tests.
"""

from __future__ import annotations

import json as _json
import time
from pathlib import Path
from typing import Any

import click

from bernstein.core.orchestration.schedule_supervisor import (
    DEFAULT_TICK_INTERVAL_S,
    ScheduleSupervisor,
    verify_receipts,
)
from bernstein.core.planning.schedule_store import (
    CronParseError,
    Schedule,
    ScheduleStore,
    validate_cron,
)


def _sdd_dir() -> Path:
    """Return the project ``.sdd`` directory, exiting if absent.

    Mirrors the convention used by ``triggers_cmd``: failing fast with a
    friendly message beats a deeper traceback when the operator forgot to
    run ``bernstein init`` first.
    """
    sdd = Path.cwd() / ".sdd"
    if not sdd.exists():
        click.echo("error: no .sdd/ directory found. Run 'bernstein init' first.", err=True)
        raise SystemExit(1)
    return sdd


def _schedule_to_public_dict(schedule: Schedule) -> dict[str, Any]:
    """Return a stable, JSON-safe view of a schedule.

    Keeping the JSON shape stable across releases is part of the AC's
    "human and --json output" requirement; downstream operator tooling
    diffs this output to compare two hosts.
    """
    return {
        "id": schedule.id,
        "cron": schedule.cron,
        "goal": schedule.goal,
        "scenario_id": schedule.scenario_id,
        "misfire_policy": schedule.misfire_policy,
        "created_at": schedule.created_at,
        "last_fire_at": schedule.last_fire_at,
    }


@click.group("schedule")
def schedule_group() -> None:
    """Manage operator-registered recurring schedules (#1798)."""


@schedule_group.command("add")
@click.option("--cron", required=True, help="Cron expression in 5-field standard form.")
@click.option(
    "--goal",
    default="",
    help="Goal text to dispatch when the schedule fires.",
)
@click.option(
    "--scenario",
    "scenario_id",
    default="",
    help="Named scenario id (optional, complements --goal).",
)
@click.option(
    "--misfire-policy",
    type=click.Choice(["skip", "catch_up"], case_sensitive=False),
    default="skip",
    show_default=True,
    help="Misfire policy (default 'skip'; opt into 'catch_up' explicitly).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def schedule_add(
    cron: str,
    goal: str,
    scenario_id: str,
    misfire_policy: str,
    as_json: bool,
) -> None:
    """Register a recurring schedule.

    The cron expression is validated up-front; the schedule is then
    persisted under ``.sdd/runtime/schedules/<id>.json``.
    """
    sdd = _sdd_dir()
    try:
        validate_cron(cron)
    except CronParseError as exc:
        click.echo(f"error: invalid cron expression: {exc}", err=True)
        raise SystemExit(2) from exc

    store = ScheduleStore(sdd)
    try:
        schedule = store.add(
            cron=cron,
            goal=goal,
            scenario_id=scenario_id,
            misfire_policy=misfire_policy.lower(),  # type: ignore[arg-type]
        )
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc

    payload = _schedule_to_public_dict(schedule)
    if as_json:
        click.echo(_json.dumps(payload, sort_keys=True, indent=2))
    else:
        click.echo(f"Registered schedule {schedule.id}")
        click.echo(f"  cron:     {schedule.cron}")
        if schedule.goal:
            click.echo(f"  goal:     {schedule.goal}")
        if schedule.scenario_id:
            click.echo(f"  scenario: {schedule.scenario_id}")
        click.echo(f"  misfire:  {schedule.misfire_policy}")


@schedule_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def schedule_list(as_json: bool) -> None:
    """List all registered schedules."""
    sdd = _sdd_dir()
    schedules = ScheduleStore(sdd).list()

    if as_json:
        click.echo(
            _json.dumps(
                {"schedules": [_schedule_to_public_dict(s) for s in schedules]},
                sort_keys=True,
                indent=2,
            ),
        )
        return

    if not schedules:
        click.echo("(no schedules registered)")
        return

    click.echo(f"{'ID':<24} {'CRON':<24} {'POLICY':<10} GOAL/SCENARIO")
    for s in schedules:
        body = s.goal or (f"scenario:{s.scenario_id}" if s.scenario_id else "(empty)")
        click.echo(f"{s.id:<24} {s.cron:<24} {s.misfire_policy:<10} {body[:60]}")


def _parse_at(value: str) -> int:
    """Parse an ``--at`` value into an integer Unix epoch (UTC).

    Accepts a bare integer epoch or an ISO-8601 timestamp. A naive
    timestamp is read as UTC so ``schedule show --at`` is host-timezone
    independent - the projection contract is UTC-only.
    """
    text = value.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    from datetime import UTC, datetime

    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise click.BadParameter(f"--at must be an epoch int or ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


@schedule_group.command("show")
@click.argument("schedule_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
@click.option(
    "--at",
    "at_value",
    default=None,
    help="Print the graph hash this schedule would dispatch at the given time (epoch or ISO-8601) without firing.",
)
def schedule_show(schedule_id: str, as_json: bool, at_value: str | None) -> None:
    """Show one schedule's full record, or the graph hash it would fire.

    With ``--at <time>`` this prints the deterministic ``graph_hash`` the
    schedule would dispatch at that instant WITHOUT firing: no receipt, no
    journal row, no audit-chain entry, no ``last_fire_at`` mutation. The
    fire is a hash, not a trigger, so the operator can preview and compare
    it against another host's projection before anything runs.
    """
    sdd = _sdd_dir()
    schedule = ScheduleStore(sdd).get(schedule_id)
    if schedule is None:
        click.echo(f"error: schedule {schedule_id!r} not found", err=True)
        raise SystemExit(1)

    if at_value is not None:
        from bernstein.core.orchestration.schedule_projection import project

        fire_time = _parse_at(at_value)
        recurrence = f"cron:{schedule.cron}" if schedule.cron else ""
        projection = project(
            schedule.id,
            fire_time,
            None,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
            recurrence=recurrence,
        )
        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "schedule_id": schedule.id,
                        "fire_time": fire_time,
                        "graph_hash": projection.graph_hash,
                        "rev": projection.rev,
                        "recurrence": projection.recurrence,
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
        else:
            click.echo(projection.graph_hash)
        return

    payload = _schedule_to_public_dict(schedule)
    if as_json:
        click.echo(_json.dumps(payload, sort_keys=True, indent=2))
        return

    click.echo(f"id:             {schedule.id}")
    click.echo(f"cron:           {schedule.cron}")
    click.echo(f"goal:           {schedule.goal or '(none)'}")
    click.echo(f"scenario_id:    {schedule.scenario_id or '(none)'}")
    click.echo(f"misfire_policy: {schedule.misfire_policy}")
    click.echo(f"created_at:     {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(schedule.created_at))} UTC")
    last_fire = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(schedule.last_fire_at)) if schedule.last_fire_at else "(never)"
    )
    click.echo(f"last_fire_at:   {last_fire}")


@schedule_group.command("remove")
@click.argument("schedule_id")
def schedule_remove(schedule_id: str) -> None:
    """Remove a schedule by id."""
    sdd = _sdd_dir()
    store = ScheduleStore(sdd)
    if store.remove(schedule_id):
        click.echo(f"Removed schedule {schedule_id}")
    else:
        click.echo(f"error: schedule {schedule_id!r} not found", err=True)
        raise SystemExit(1)


@schedule_group.command("audit")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def schedule_audit(as_json: bool) -> None:
    """Re-derive and chain-check persisted fire receipts (verification).

    For every non-counterfactual receipt the verb re-runs the
    deterministic projection from the receipt's persisted inputs and
    confirms the recomputed ``projection_hash`` equals the recorded one,
    then cross-checks the receipt against the matching ``schedule.fire``
    audit-chain entry and verifies the receipt-to-receipt chain linkage.
    A tampered or chain-inconsistent receipt makes the command exit
    non-zero and names the offending receipt, so the verb is safe to run
    as a CI gate rather than only a human-readable table.
    """
    sdd = _sdd_dir()
    report = verify_receipts(sdd)

    if as_json:
        click.echo(
            _json.dumps(
                {"receipts": report.to_json(), "ok": report.ok, "failures": list(report.failures)},
                sort_keys=True,
                indent=2,
            ),
        )
        raise SystemExit(0 if report.ok else 1)

    if not report.results:
        click.echo("(no schedule fires recorded)")
        return

    click.echo(f"{'FIRE_TIME':<20} {'SCHEDULE':<24} {'PROJECTION':<18} {'STATUS':<10} CHAIN")
    for r in report.results:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r.fire_time))
        proj_short = (r.recorded_projection_hash or "-")[:16]
        if r.counterfactual:
            chain_short = "-"
            status = "skip"
        else:
            chain_short = "ok" if r.chain_match else "MISMATCH"
            if r.rev_skipped:
                status = "rev-skip"
            elif r.verified:
                status = "verified"
            else:
                status = "MISMATCH"
        click.echo(f"{ts:<20} {r.schedule_id:<24} {proj_short:<18} {status:<10} {chain_short}")

    if not report.ok:
        click.echo("", err=True)
        click.echo("audit FAILED - the following receipts did not verify:", err=True)
        for failure in report.failures:
            click.echo(f"  - {failure}", err=True)
        raise SystemExit(1)


@schedule_group.command("verify")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def schedule_verify(as_json: bool) -> None:
    """Replay every recorded fire and confirm its graph hash (verification).

    For each ``schedule.fire_projection`` row recorded in the event journal
    this re-runs the deterministic projection from the row's persisted
    inputs and confirms the recomputed ``graph_hash`` equals the recorded
    one. A divergence names the fire and exits non-zero, so the verb is
    safe as a CI gate: it proves a past fire is byte-identically
    reproducible from ``(schedule, fire_time, state)``.
    """
    from bernstein.core.orchestration.schedule_fire_record import verify_all_fires

    sdd = _sdd_dir()
    results = verify_all_fires(sdd)

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "fires": [
                        {
                            "schedule_id": r.schedule_id,
                            "fire_time": r.fire_time,
                            "recorded_graph_hash": r.recorded_graph_hash,
                            "recomputed_graph_hash": r.recomputed_graph_hash,
                            "match": r.match,
                            "reason": r.reason,
                        }
                        for r in results
                    ],
                    "ok": all(r.match for r in results),
                },
                sort_keys=True,
                indent=2,
            )
        )
        raise SystemExit(0 if all(r.match for r in results) else 1)

    if not results:
        click.echo("(no schedule fires recorded)")
        return

    click.echo(f"{'FIRE_TIME':<20} {'SCHEDULE':<24} {'GRAPH':<18} STATUS")
    for r in results:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r.fire_time))
        graph_short = (r.recorded_graph_hash or "-")[:16]
        status = "ok" if r.match else "MISMATCH"
        click.echo(f"{ts:<20} {r.schedule_id:<24} {graph_short:<18} {status}")

    failures = [r for r in results if not r.match]
    if failures:
        click.echo("", err=True)
        click.echo("verify FAILED - the following fires did not reproduce:", err=True)
        for r in failures:
            click.echo(f"  - {r.schedule_id}@{r.fire_time}: {r.reason}", err=True)
        raise SystemExit(1)


@schedule_group.command("run")
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_TICK_INTERVAL_S,
    show_default=True,
    help="Tick interval (seconds) for the standalone supervisor worker.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single tick and exit (useful for testing or cron-driven harnesses).",
)
def schedule_run(interval: float, once: bool) -> None:
    """Run the schedule supervisor in the foreground.

    Provides the long-running worker called out in the AC. The same
    supervisor logic is also wired into the ``bernstein daemon`` hook so
    operators may choose either lifecycle.
    """
    sdd = _sdd_dir()
    store = ScheduleStore(sdd)

    # The audit chain wiring uses the existing AuditLog primitives.
    audit_dir = sdd / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    try:
        from bernstein.core.security.audit import AuditLog

        audit_writer: Any | None = AuditLog(audit_dir=audit_dir)
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"warning: audit log unavailable ({exc}); fires will not chain", err=True)
        audit_writer = None

    # Wire the production TriggerManager if available. The worker runs
    # outside the orchestrator process, so TriggerManager.evaluate is the
    # only seam back into the regular trigger pipeline. When the manager
    # cannot load (e.g. no triggers.yaml) we fall back to recording the
    # TriggerEvent on disk; the next orchestrator tick will pick it up.
    trigger_manager: Any | None = None
    try:
        from bernstein.core.orchestration.trigger_manager import TriggerManager

        trigger_manager = TriggerManager(sdd)
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(
            f"warning: trigger manager unavailable ({exc}); fires will queue locally",
            err=True,
        )

    fires_dispatched: list[Any] = []

    def _dispatch(event: Any) -> None:
        fires_dispatched.append(event)
        # Push the event through the existing trigger pipeline so the
        # downstream task store sees a normal trigger fire. If no
        # manager wired, the receipt + audit-chain entry stand on
        # their own for later replay.
        if trigger_manager is not None:
            try:
                trigger_manager.evaluate(event)
            except Exception:  # pragma: no cover - defensive
                logger = __import__("logging").getLogger(__name__)
                logger.exception("Trigger pipeline dispatch failed for %s", event.metadata)

    supervisor = ScheduleSupervisor(store, _dispatch, audit_writer)

    if once:
        receipts = supervisor.tick()
        click.echo(f"tick complete: {len(receipts)} receipt(s), {len(fires_dispatched)} fire(s)")
        return

    click.echo(f"schedule supervisor started (interval={interval}s)")
    try:
        while True:
            supervisor.tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nsupervisor stopped")


@schedule_group.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def schedule_doctor(as_json: bool) -> None:
    """Report supervisor liveness, last fire, and next fire.

    Surfaces the doctor check required by the AC. Designed to be
    composable with the main ``bernstein doctor`` runner; the standalone
    command is provided so operators can poll the schedule subsystem
    independently of the full doctor report.
    """
    sdd = _sdd_dir()
    store = ScheduleStore(sdd)
    status = ScheduleSupervisor(store, lambda _evt: None, None).status()
    payload = {
        "alive": status.alive,
        "last_tick_at": status.last_tick_at,
        "last_fire_at": status.last_fire_at,
        "next_fire_at": status.next_fire_at,
        "next_fire_schedule_id": status.next_fire_schedule_id,
        "schedules_total": status.schedules_total,
    }
    if as_json:
        click.echo(_json.dumps(payload, sort_keys=True, indent=2))
        return

    click.echo(f"schedules registered: {status.schedules_total}")
    click.echo(f"supervisor alive:     {status.alive}")
    if status.last_fire_at:
        click.echo(f"last fire at:         {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(status.last_fire_at))}")
    else:
        click.echo("last fire at:         (never)")
    if status.next_fire_at:
        click.echo(
            f"next fire at:         "
            f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(status.next_fire_at))} "
            f"(schedule {status.next_fire_schedule_id})"
        )
    else:
        click.echo("next fire at:         (no schedules)")
