"""``bernstein trackers`` -- inspect and smoke-test registered tracker adapters.

Surfaces the tracker registry to operators:

* ``bernstein trackers list`` -- one row per registered adapter (name,
  source, summary, capabilities).
* ``bernstein trackers list --json`` -- stable machine-readable payload.
* ``bernstein trackers test <name>`` -- construct the adapter (using
  ``trackers.<name>`` from ``bernstein.yaml`` when available) and call
  ``pull_open_tickets`` with an empty filter as a smoke test.

The ``test`` subcommand is deliberately read-only: it never claims,
comments, or transitions a ticket. It exits non-zero on any failure so
CI workflows can rely on it. Adapters without runtime credentials in
the local environment are reported as ``skipped`` with a reason rather
than failing the command -- this lets the same invocation work in
ephemeral test environments.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.trackers.registry import TrackerRegistration


__all__ = ["register", "trackers_group"]


def _load_registry_with_plugins() -> Any:
    """Read the workspace tracker registrations without writing quarantine state."""
    from bernstein.core.trackers.registry import discover_workspace_trackers

    return discover_workspace_trackers(Path.cwd(), record_failures=False)


def _row_for(entry: TrackerRegistration) -> dict[str, Any]:
    """Serialise a registration into the JSON row schema."""
    return {
        "name": entry.name,
        "source": entry.source,
        "summary": entry.summary,
        "capabilities": list(entry.capabilities),
        "provenance": entry.provenance,
    }


def _render_table(rows: list[dict[str, Any]]) -> None:
    """Pretty-print one row per adapter."""
    if not rows:
        console.print("[yellow]No tracker adapters registered.[/yellow]")
        return

    name_width = max(len(r["name"]) for r in rows)
    source_width = max(len(r["source"]) for r in rows)
    header_name = "NAME".ljust(name_width)
    header_source = "SOURCE".ljust(source_width)
    console.print(f"[bold]{header_name}  {header_source}  CAPABILITIES  SUMMARY[/bold]")
    for row in rows:
        caps = ",".join(row["capabilities"]) or "-"
        console.print(
            f"{row['name'].ljust(name_width)}  {row['source'].ljust(source_width)}  {caps:<13}  {row['summary']}"
        )


def _load_tracker_config(name: str) -> dict[str, Any]:
    """Best-effort load of ``trackers.<name>`` from ``bernstein.yaml``.

    Returns an empty dict when no config is present or the loader is
    unavailable. The ``test`` subcommand falls back to constructing the
    adapter with no arguments in that case, which is fine for adapters
    whose factory accepts purely-default construction (e.g. an
    in-memory fake).
    """
    try:
        from bernstein.core.config import load_settings  # type: ignore[attr-defined]
    except Exception:
        return {}

    try:
        settings = load_settings()
    except Exception:
        return {}

    raw = getattr(settings, "trackers", None)
    if raw is None and isinstance(settings, dict):
        raw = settings.get("trackers")
    if not isinstance(raw, dict):
        return {}
    per_tracker = raw.get(name)
    if not isinstance(per_tracker, dict):
        return {}
    return per_tracker


@click.group("trackers")
def trackers_group() -> None:
    """Inspect and smoke-test registered tracker adapters."""


@trackers_group.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of a human-readable table.",
)
@click.option(
    "--source",
    type=click.Choice(["builtin", "plugin", "programmatic"]),
    default=None,
    help="Filter to a single registration source.",
)
def trackers_list_cmd(as_json: bool, source: str | None) -> None:
    """Enumerate every tracker adapter the orchestrator knows about."""
    registry = _load_registry_with_plugins()
    entries = list(registry)
    if source is not None:
        entries = [e for e in entries if e.source == source]
    rows = [_row_for(e) for e in entries]

    if as_json:
        click.echo(json.dumps({"count": len(rows), "trackers": rows}, indent=2, sort_keys=True))
        return

    _render_table(rows)


@trackers_group.command("test")
@click.argument("name")
@click.option(
    "--limit",
    type=int,
    default=1,
    show_default=True,
    help="Maximum number of open tickets to fetch during the smoke test.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON result document instead of human-readable lines.",
)
def trackers_test_cmd(name: str, limit: int, as_json: bool) -> None:
    """Construct ``<name>`` and call ``pull_open_tickets`` as a smoke test.

    Read-only: never claims, comments, transitions, or attaches.
    """
    registry = _load_registry_with_plugins()
    try:
        entry = registry.get(name)
    except KeyError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)

    cfg = _load_tracker_config(name)
    result: dict[str, Any] = {
        "name": name,
        "source": entry.source,
        "status": "unknown",
        "fetched": 0,
        "limit": limit,
        "reason": None,
    }
    try:
        adapter = entry.factory(**cfg)
    except TypeError as exc:
        # Adapter requires more arguments than we supplied; treat as skip
        # rather than fail so an operator running the command in a bare
        # environment gets a clear hint.
        result["status"] = "skipped"
        result["reason"] = f"adapter construction needs config: {exc}"
        _emit_test_result(result, as_json)
        return
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = f"construction failed: {exc}"
        _emit_test_result(result, as_json)
        sys.exit(1)

    try:
        fetched = 0
        for _ in adapter.pull_open_tickets({}):
            fetched += 1
            if fetched >= limit:
                break
        result["status"] = "ok"
        result["fetched"] = fetched
    except NotImplementedError as exc:
        result["status"] = "unsupported"
        result["reason"] = str(exc) or "pull_open_tickets not implemented"
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = f"pull_open_tickets failed: {exc}"
        _emit_test_result(result, as_json)
        sys.exit(1)

    _emit_test_result(result, as_json)


def _emit_test_result(result: dict[str, Any], as_json: bool) -> None:
    """Print the smoke-test result document."""
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
        return

    console.print(f"tracker: [bold]{result['name']}[/bold]  ({result['source']})")
    console.print(f"status : {result['status']}")
    console.print(f"fetched: {result['fetched']} (limit {result['limit']})")
    if result["reason"]:
        console.print(f"reason : {result['reason']}")


def register(group: click.Group) -> None:
    """Attach the trackers group to the top-level CLI."""
    group.add_command(trackers_group, "trackers")
