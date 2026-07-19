"""``bernstein integrations list`` - enumerate wired-in CLI adapters.

Users currently have to read ``src/bernstein/adapters/`` to discover
which CLI tools Bernstein can drive. This command surfaces the same
information directly from the registry:

* ``bernstein integrations list`` - one line per adapter (name + headline).
* ``--details`` - per-adapter block with binary, config knob, docs link.
* ``--json`` - stable machine-readable payload for CI dashboards.
* ``--installed`` - filter to adapters whose binary is on ``$PATH``.

Per-adapter copy is sourced from :mod:`bernstein.adapters.use_cases`
which sits alongside the adapter implementations. When an entry is
absent we fall back to the first line of the adapter module docstring
so newly-added adapters still appear with a sensible summary.
"""

from __future__ import annotations

import importlib
import inspect
import json
import shutil
import sys

import click

from bernstein.adapters.use_cases import USE_CASES, AdapterUseCase
from bernstein.cli.helpers import console

FORMAT_TABLE = "table"
FORMAT_JSON = "json"
DOCS_INDEX = "docs/adapters/index.md"
CONFIG_KNOB = "cli"


def _fallback_headline(adapter_obj: object) -> str:
    """Use the first line of the module docstring when no curated copy exists.

    Strips the trailing "CLI adapter" boilerplate so the line reads as a
    use case rather than a tautology.
    """
    target = adapter_obj if inspect.isclass(adapter_obj) else type(adapter_obj)
    try:
        module = inspect.getmodule(target)
    except Exception:  # pragma: no cover - defensive
        module = None
    raw = (getattr(module, "__doc__", None) or "").strip()
    if not raw:
        return ""
    first_line = raw.splitlines()[0].strip().rstrip(".")
    # Strip the common "X CLI adapter" / "X adapter" suffix.
    for suffix in (" CLI adapter", " adapter for Bernstein", " adapter"):
        if first_line.lower().endswith(suffix.lower()):
            first_line = first_line[: -len(suffix)].rstrip(" -")
            break
    return first_line


def _docs_link(name: str, use_case: AdapterUseCase | None) -> str:
    """Resolve the per-adapter doc link, falling back to the index page."""
    if use_case and use_case.docs_path:
        return use_case.docs_path
    return DOCS_INDEX


def _binary_for(name: str, use_case: AdapterUseCase | None) -> str:
    """Pick the binary name to probe via ``shutil.which``.

    Priority: explicit use-case entry, then the override table in
    :mod:`bernstein.cli.commands.adapter_cmd`, then the adapter's
    capability profile (which already declares its binary), then the
    registry key itself. Empty string means "no external binary"
    (in-process or SDK-only adapters).

    Consulting the profile keeps this surface consistent with
    ``report._binary_for_adapter`` and ``adapter_cmd._binary_for_adapter``:
    a factory-built adapter whose binary differs from its registry key
    (for example ``pydantic_ai`` -> ``clai``) is probed under its real
    binary even without a curated use-case entry, so it is not silently
    misreported as not installed.
    """
    if use_case is not None:
        return use_case.binary
    # Lazy import keeps the dependency graph one-way: integrations_cmd
    # depends on adapter_cmd, never the reverse.
    overrides = importlib.import_module("bernstein.cli.commands.adapter_cmd")._BINARY_OVERRIDES
    if name in overrides:
        return overrides[name]
    from bernstein.adapters.capability_profile import profile_binary_for

    return profile_binary_for(name) or name


def _enumerate_rows() -> list[dict[str, object]]:
    """Snapshot the live registry plus the synthetic ``generic`` adapter."""
    registry = importlib.import_module("bernstein.adapters.registry")
    registry._load_entrypoint_adapters()

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for name, adapter in sorted(registry._ADAPTERS.items()):
        rows.append(_row_for(name, adapter))
        seen.add(name)

    # ``generic`` is constructed lazily inside ``get_adapter`` and not
    # registered in ``_ADAPTERS`` on import. Surface it so the listing
    # matches what ``--help`` would suggest.
    if "generic" not in seen:
        generic_mod = importlib.import_module("bernstein.adapters.generic")
        rows.append(_row_for("generic", generic_mod.GenericAdapter))

    rows.sort(key=lambda r: str(r["name"]))
    return rows


def _row_for(name: str, adapter_obj: object) -> dict[str, object]:
    """Build the JSON-shaped row for a single adapter."""
    use_case = USE_CASES.get(name)
    headline = (use_case.headline if use_case else "") or _fallback_headline(adapter_obj)
    binary = _binary_for(name, use_case)
    installed = bool(binary) and shutil.which(binary) is not None
    return {
        "name": name,
        "headline": headline,
        "binary": binary,
        "installed": installed,
        "config_knob": CONFIG_KNOB,
        "docs": _docs_link(name, use_case),
        "details": use_case.details if use_case else "",
    }


def _filter_installed(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop rows whose binary is not currently on ``$PATH``."""
    return [row for row in rows if row["installed"]]


def _render_summary_table(rows: list[dict[str, object]]) -> None:
    """Default ``integrations list`` view: name + headline."""
    from rich.table import Table

    table = Table(title=f"Bernstein integrations ({len(rows)})", show_lines=False)
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("installed", style="green", no_wrap=True)
    table.add_column("headline", style="white")
    for row in rows:
        installed_marker = "[green]yes[/green]" if row["installed"] else "[dim]no[/dim]"
        if not row["binary"]:
            installed_marker = "[dim]n/a[/dim]"
        table.add_row(str(row["name"]), installed_marker, str(row["headline"] or "-"))
    console.print(table)


def _render_details(rows: list[dict[str, object]]) -> None:
    """``--details`` view: per-adapter block with binary, config, docs."""
    for row in rows:
        console.print(f"[cyan bold]{row['name']}[/cyan bold]")
        console.print(f"  headline   : {row['headline'] or '-'}")
        binary = str(row["binary"]) or "-"
        installed = "installed" if row["installed"] else "missing"
        if not row["binary"]:
            installed = "n/a"
        console.print(f"  binary     : {binary}  ({installed})")
        console.print(f"  config knob: {row['config_knob']}: {row['name']}")
        console.print(f"  docs       : {row['docs']}")
        if row["details"]:
            console.print(f"  notes      : {row['details']}")
        console.print()


@click.group("integrations")
def integrations_group() -> None:
    """List and inspect the CLI agents Bernstein knows how to drive."""


@integrations_group.command("list")
@click.option(
    "--details",
    is_flag=True,
    default=False,
    help="Show a fuller per-adapter block instead of one line per adapter.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a stable JSON document for programmatic consumption.",
)
@click.option(
    "--installed",
    "installed_only",
    is_flag=True,
    default=False,
    help="Filter to adapters whose binary is currently on $PATH.",
)
def integrations_list_cmd(details: bool, as_json: bool, installed_only: bool) -> None:
    """Enumerate every CLI adapter Bernstein knows about.

    With no flags, prints a one-line summary per adapter. Use ``--details``
    for per-adapter blocks, ``--json`` for CI dashboards, and
    ``--installed`` to filter to adapters that are usable right now.
    """
    rows = _enumerate_rows()
    if installed_only:
        rows = _filter_installed(rows)

    if as_json:
        payload = {"count": len(rows), "adapters": rows}
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if details:
        _render_details(rows)
        return

    _render_summary_table(rows)


def register(group: click.Group) -> None:
    """Attach the integrations group to the top-level CLI."""
    group.add_command(integrations_group, "integrations")


__all__ = [
    "CONFIG_KNOB",
    "DOCS_INDEX",
    "FORMAT_JSON",
    "FORMAT_TABLE",
    "_enumerate_rows",
    "_filter_installed",
    "_row_for",
    "integrations_group",
    "integrations_list_cmd",
    "register",
]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """Standalone entry point - used by direct ``python -m`` invocation."""
    integrations_list_cmd.main(args=argv, standalone_mode=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
