"""Config diff command: show settings that differ from defaults.

CFG-010: CLI wrapper around core.config_drift_cmd.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_current_config() -> dict[str, object] | None:
    """Load the current mapping or report why there is nothing to compare."""
    from bernstein.cli.helpers import console

    for name in ("bernstein.yaml", "bernstein.yml"):
        p = Path.cwd() / name
        if p.is_file():
            with p.open() as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                return dict(raw)  # type: ignore[arg-type]
            console.print(f"[dim]{name} must contain a YAML mapping.[/dim]")
            return None
    console.print("[dim]No bernstein.yaml found in current directory.[/dim]")
    return None


def config_diff_cmd() -> None:
    """Show settings that differ from defaults."""
    from rich.table import Table

    from bernstein.cli.helpers import console
    from bernstein.core.config_drift_cmd import diff_against_defaults

    current_yaml = _load_current_config()

    if current_yaml is None:
        return

    report = diff_against_defaults(current_yaml)

    if not report.has_deviations:
        console.print("[green]All settings match defaults.[/green]")
        return

    table = Table(title="Configuration diff", show_header=True, header_style="bold cyan")
    table.add_column("Setting", style="cyan")
    table.add_column("Kind", style="dim")
    table.add_column("Default", style="dim")
    table.add_column("Current", style="bold green")

    for dev in report.deviations:
        kind_style = {
            "changed": "yellow",
            "added": "green",
            "removed": "red",
        }.get(dev.kind, "dim")
        table.add_row(
            dev.key,
            f"[{kind_style}]{dev.kind}[/{kind_style}]",
            str(dev.default_value) if dev.default_value is not None else "",
            str(dev.current_value) if dev.current_value is not None else "",
        )

    console.print(table)
    console.print(
        f"\n{report.changed_count} changed, "
        f"{report.added_count} added, "
        f"{report.removed_count} removed "
        f"out of {report.total_keys} total settings."
    )
