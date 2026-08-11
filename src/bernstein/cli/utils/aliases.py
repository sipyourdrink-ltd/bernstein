"""Command aliases and shortcuts for Bernstein CLI.

e.g. ``bernstein s`` = ``bernstein status``.

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
    "s": "status",
    "r": "run",
    "d": "doctor",
    "l": "live",
    "p": "plan",
    "c": "cost",
    "w": "watch",
    # ``init-wizard`` is folded into ``init --wizard`` (#3140). An alias value
    # may carry flags, so the shortcut keeps landing on the interactive setup
    # path it has always opened instead of silently becoming plain ``init``.
    "i": "init --wizard",
    "st": "stop",
    "rc": "recap",
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
    return ALIASES.copy()


def expand_alias(name: str) -> list[str] | None:
    """Split an alias into the argv tokens it stands for.

    An alias value is a command line, not just a command name: folding a
    top-level command into a flag on another one (``init-wizard`` into
    ``init --wizard``, #3140) leaves the shortcut with nothing to point at
    unless the value can carry the flag too.

    Args:
        name: Potential alias string.

    Returns:
        The argv tokens the alias expands to, or None when ``name`` is not a
        registered alias. The first token is always the command name.
    """
    resolved = ALIASES.get(name)
    if resolved is None:
        return None
    tokens = resolved.split()
    return tokens or None


def _load_user_aliases() -> dict[str, str]:
    """Load user-defined aliases from ~/.bernstein/aliases.yaml."""
    if not _USER_ALIASES_PATH.is_file():
        return {}
    try:
        with _USER_ALIASES_PATH.open() as f:
            raw: object = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            return {}
        from typing import cast

        entries = cast("dict[str, object]", raw)
        return {k: str(v) for k, v in entries.items() if isinstance(v, str)}
    except Exception:
        logger.debug("Failed to load user aliases from %s", _USER_ALIASES_PATH, exc_info=True)
        return {}


def _merge_aliases() -> None:
    """Merge user aliases into the global registry (user overrides built-in)."""
    global _user_aliases
    _user_aliases = _load_user_aliases()
    ALIASES.update(_user_aliases)


# Call at module load time
_merge_aliases()


class AliasGroup(click.Group):
    """Click Group that resolves short aliases to full command names.

    Falls back to standard prefix matching if no alias is found.
    """

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Check alias registry first. Only the command name is a lookup key:
        # any flags the alias carries are applied by resolve_command.
        tokens = expand_alias(cmd_name)
        if tokens is not None:
            return super().get_command(ctx, tokens[0])
        # Standard lookup
        return super().get_command(ctx, cmd_name)

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Check if first arg is an alias
        if args:
            tokens = expand_alias(args[0])
            if tokens is not None:
                args = [*tokens, *args[1:]]
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
        "i": "Interactive workspace setup wizard",
        "st": "Graceful shutdown",
        "ps": "Running agent processes",
        "rc": "Post-run summary",
    }

    for alias, command in sorted(ALIASES.items()):
        desc = _descriptions.get(alias, "")
        source = "[cyan]user[/cyan]" if alias in _user_aliases else "[dim]built-in[/dim]"
        table.add_row(alias, command, source, desc)

    console.print(table)
    console.print("\n[dim]Usage: bernstein <alias> [options][/dim]")
