"""``bernstein daemon`` CLI group.

Installs Bernstein as an auto-start service on the host:

* Linux (systemd): writes a user unit to ``~/.config/systemd/user``
  (or a system unit to ``/etc/systemd/system`` when ``--system`` is
  supplied).
* macOS (launchd): writes a user agent plist to
  ``~/Library/LaunchAgents``.

The command also exposes ``start``, ``stop``, ``restart``, ``status``,
and ``uninstall`` subcommands.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from bernstein.core.daemon import (
    detect_init_system,
)
from bernstein.core.daemon import launchd as launchd_mod
from bernstein.core.daemon import systemd as systemd_mod
from bernstein.core.daemon.errors import UnitExistsError

__all__ = ["daemon_group"]

# Single-source the error message so the five call sites stay in sync
# (Sonar python:S1192).
_UNSUPPORTED_INIT_ERR = "Unsupported init system (need systemd or launchd)."

DEFAULT_COMMAND = "bernstein dashboard --headless"


def _parse_env_pairs(pairs: tuple[str, ...]) -> dict[str, str]:
    """Parse ``--env KEY=VAL`` tuples into a dict.

    Args:
        pairs: Click-collected ``KEY=VAL`` strings.

    Returns:
        A ``dict[str, str]`` of environment overrides.

    Raises:
        click.BadParameter: When any entry is malformed.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"--env values must be KEY=VAL (got: {pair!r})")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            raise click.BadParameter(f"--env key must not be empty (got: {pair!r})")
        out[key] = value
    return out


def _resolve_scope(user: bool, system: bool) -> str:
    """Return ``"user"`` or ``"system"`` per the flags and platform."""
    if user and system:
        raise click.UsageError("Cannot combine --user and --system.")
    init = detect_init_system()
    if init == "launchd":
        if system:
            raise click.UsageError("--system is not supported on macOS (launchd agents are user-scope).")
        return "user"
    if system:
        return "system"
    return "user"


@click.group("daemon")
def daemon_group() -> None:
    """Install Bernstein as an auto-start service (systemd / launchd)."""


@daemon_group.command("install")
@click.option("--user", "user_scope", is_flag=True, default=False, help="Install as a user-scope unit (default).")
@click.option("--system", "system_scope", is_flag=True, default=False, help="Install as a system-scope unit (Linux).")
@click.option(
    "--command",
    "command",
    default=DEFAULT_COMMAND,
    show_default=True,
    help="The command line to run as the daemon.",
)
@click.option(
    "--env",
    "env_pairs",
    multiple=True,
    metavar="KEY=VAL",
    help="Extra environment variable to embed in the unit. Repeat as needed.",
)
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing unit.")
def install(
    user_scope: bool,
    system_scope: bool,
    command: str,
    env_pairs: tuple[str, ...],
    force: bool,
) -> None:
    """Install the Bernstein daemon for auto-start at boot/login."""
    scope = _resolve_scope(user_scope, system_scope)
    env = _parse_env_pairs(env_pairs)
    init = detect_init_system()
    path_env = os.environ.get("PATH", "")
    workdir = os.environ.get("HOME", str(Path.home()))
    try:
        if init == "launchd":
            path = launchd_mod.install_launchd_plist(command, env=env, workdir=workdir, path_env=path_env, force=force)
        elif init == "systemd":
            if scope == "system":
                path = systemd_mod.install_systemd_system_unit(
                    command, env=env, workdir=workdir, path_env=path_env, force=force
                )
            else:
                path = systemd_mod.install_systemd_user_unit(
                    command, env=env, workdir=workdir, path_env=path_env, force=force
                )
        else:
            raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    except UnitExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Installed daemon unit at {path}")


@daemon_group.command("start")
def start_cmd() -> None:
    """Start the Bernstein daemon."""
    init = detect_init_system()
    if init == "launchd":
        plist = launchd_mod.default_plist_dir() / launchd_mod.DEFAULT_PLIST_NAME
        launchd_mod.load(plist)
        launchd_mod.start()
    elif init == "systemd":
        systemd_mod.start()
    else:
        raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    click.echo("Daemon started.")


@daemon_group.command("stop")
def stop_cmd() -> None:
    """Stop the Bernstein daemon."""
    init = detect_init_system()
    if init == "launchd":
        plist = launchd_mod.default_plist_dir() / launchd_mod.DEFAULT_PLIST_NAME
        launchd_mod.stop()
        launchd_mod.unload(plist)
    elif init == "systemd":
        systemd_mod.stop()
    else:
        raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    click.echo("Daemon stopped.")


@daemon_group.command("restart")
def restart_cmd() -> None:
    """Restart the Bernstein daemon."""
    init = detect_init_system()
    if init == "launchd":
        plist = launchd_mod.default_plist_dir() / launchd_mod.DEFAULT_PLIST_NAME
        launchd_mod.stop()
        launchd_mod.unload(plist)
        launchd_mod.load(plist)
        launchd_mod.start()
    elif init == "systemd":
        systemd_mod.restart()
    else:
        raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    click.echo("Daemon restarted.")


@daemon_group.command("status")
def status_cmd() -> None:
    """Print the daemon status (Running / Stopped / Failed)."""
    init = detect_init_system()
    if init == "launchd":
        text = launchd_mod.status()
    elif init == "systemd":
        text = systemd_mod.status()
    else:
        raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    click.echo(text)


@daemon_group.command("uninstall")
@click.option("--force", is_flag=True, default=False, help="Suppress errors if the unit is missing.")
def uninstall_cmd(force: bool) -> None:
    """Uninstall the Bernstein daemon unit."""
    init = detect_init_system()
    if init == "launchd":
        removed = launchd_mod.uninstall()
    elif init == "systemd":
        removed = systemd_mod.uninstall()
    else:
        raise click.ClickException(_UNSUPPORTED_INIT_ERR)
    if not removed and not force:
        click.echo("No daemon unit installed.")
        return
    click.echo("Daemon uninstalled.")
