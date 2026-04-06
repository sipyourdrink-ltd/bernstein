"""Command aliases and shortcuts for Bernstein CLI.

CLI-013: e.g. ``bernstein s`` = ``bernstein status``.

Provides a Click Group subclass that resolves short aliases to
full command names, plus a registry of built-in aliases.
User-defined aliases can be loaded from ``~/.bernstein/aliases.yaml``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alias registry
# ---------------------------------------------------------------------------

ALIASES: dict[str, str] = {
    "s": "score",  # status (hidden name: score)
    "r": "run",
    "d": "doctor",
    "l": "live",
    "p": "plan",
    "c": "cost",
    "w": "watch",
    "i": "overture",  # init (hidden name: overture)
}

# Track which aliases are user-defined (populated at load time)
_user_aliases: dict[str, str] = {}

_USER_ALIASES_PATH = Path.home() / ".bernstein" / "aliases.yaml"


def get_alias(name: str) -> str | None:
    """Return the full command name for an alias, or None.

    Args:
        name: Potential alias string.

    Returns:
        Full command name, or None if not an alias.
    """
    return ALIASES.get(name)


def get_all_aliases() -> dict[str, str]:
    """Return a copy of the alias registry."""
    return dict(ALIASES)


def _load_user_aliases() -> dict[str, str]:
    """Load user-defined aliases from ~/.bernstein/aliases.yaml."""
    if not _USER_ALIASES_PATH.is_file():
        return {}
    try:
        with open(_USER_ALIASES_PATH) as f:
            raw: object = yaml.safe_load(f) or {}
        data = raw if isinstance(raw, dict) else {}
        return {k: str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        logger.debug("Failed to load user aliases from %s", _USER_ALIASES_PATH, exc_info=True)
        return {}


def _merge_aliases() -> None:
    """Merge user aliases into the global registry (user overrides built-in)."""
    global _user_aliases  # noqa: PLW0603
    _user_aliases = _load_user_aliases()
    ALIASES.update(_user_aliases)


# Call at module load time
_merge_aliases()


class AliasGroup(click.Group):
    """Click Group that resolves short aliases to full command names.

    Falls back to standard prefix matching if no alias is found.
    """

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Check alias registry first
        resolved = ALIASES.get(cmd_name)
        if resolved is not None:
            return super().get_command(ctx, resolved)
        # Standard lookup
        return super().get_command(ctx, cmd_name)

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Check if first arg is an alias
        if args:
            resolved = ALIASES.get(args[0])
            if resolved is not None:
                args = [resolved, *args[1:]]
        return super().resolve_command(ctx, args)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Append alias table to help output."""
        super().format_help(ctx, formatter)


@click.command("aliases")
def aliases_cmd() -> None:
    """Show command aliases and shortcuts.

    \b
    Bernstein supports short aliases for common commands:
      bernstein s  ->  bernstein status
      bernstein r  ->  bernstein run
      bernstein d  ->  bernstein doctor
      etc.
    """
    from rich.table import Table

    from bernstein.cli.helpers import console

    table = Table(title="Command Aliases", show_header=True, header_style="bold cyan")
    table.add_column("Alias", style="green", width=10)
    table.add_column("Command", style="white", width=20)
    table.add_column("Source", style="dim", width=10)
    table.add_column("Description", style="dim")

    _descriptions: dict[str, str] = {
        "s": "Task summary and agent health",
        "r": "Start orchestrating agents",
        "d": "Run self-diagnostics",
        "l": "Interactive TUI dashboard",
        "p": "Show task backlog",
        "c": "Spend breakdown",
        "w": "Watch for file changes",
        "i": "Initialize workspace",
    }

    for alias, command in sorted(ALIASES.items()):
        desc = _descriptions.get(alias, "")
        source = "[cyan]user[/cyan]" if alias in _user_aliases else "[dim]built-in[/dim]"
        table.add_row(alias, command, source, desc)

    console.print(table)
    console.print("\n[dim]Usage: bernstein <alias> [options][/dim]")
