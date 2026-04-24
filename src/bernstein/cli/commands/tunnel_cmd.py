"""``bernstein tunnel`` — one wrapper around four tunnel providers.

Subcommands:

* ``tunnel start <port>`` — launch a tunnel using ``--provider`` or
  auto-detection (prefers cloudflared, then bore, ngrok, tailscale).
* ``tunnel list`` — show every active tunnel tracked in
  ``.sdd/runtime/tunnels.json``.
* ``tunnel stop <name>`` / ``tunnel stop --all`` — tear tunnels down,
  sending SIGTERM to the owning PID.
"""

from __future__ import annotations

import os
import signal

import click

from bernstein.cli.helpers import console
from bernstein.core.tunnels.drivers import register_default_drivers
from bernstein.core.tunnels.protocol import ProviderNotAvailable
from bernstein.core.tunnels.registry import TunnelRegistry

_PROVIDER_CHOICES = ["auto", "cloudflared", "ngrok", "bore", "tailscale"]


def _build_registry() -> TunnelRegistry:
    """Construct a registry with the four shipped drivers registered.

    Returns:
        A :class:`TunnelRegistry` ready to create / list tunnels.
    """
    reg = TunnelRegistry()
    register_default_drivers(reg)
    return reg


def _sigterm_pid(pid: int) -> bool:
    """Send ``SIGTERM`` to ``pid``; return ``True`` if the syscall succeeded.

    Args:
        pid: Target process id.  Ignored if ``<= 0``.

    Returns:
        ``True`` if the signal was delivered, ``False`` otherwise.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    return True


@click.group("tunnel")
def tunnel_group() -> None:
    """Manage local-to-public tunnels (cloudflared / ngrok / bore / tailscale)."""


@tunnel_group.command("start")
@click.argument("port", type=int)
@click.option(
    "--provider",
    type=click.Choice(_PROVIDER_CHOICES),
    default="auto",
    show_default=True,
    help="Tunnel provider; 'auto' picks the first binary found on PATH.",
)
@click.option(
    "--name",
    default=None,
    help="Tunnel name; auto-generated if omitted.",
)
def start_cmd(port: int, provider: str, name: str | None) -> None:
    """Start a tunnel exposing ``localhost:<PORT>`` publicly."""
    reg = _build_registry()
    try:
        handle = reg.create(port=port, provider=provider, name=name)
    except ProviderNotAvailable as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(f"[dim]hint: {exc.hint}[/dim]")
        raise SystemExit(1) from exc
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc
    console.print(
        f"[green]Started tunnel[/green] [bold]{handle.name}[/bold] "
        f"({handle.provider}): {handle.public_url} -> localhost:{handle.port} "
        f"[dim](pid={handle.pid})[/dim]"
    )


@tunnel_group.command("list")
def list_cmd() -> None:
    """Show every active tunnel."""
    reg = _build_registry()
    handles = reg.list_active()
    if not handles:
        console.print("[dim]No active tunnels.[/dim]")
        return
    header = f"{'NAME':<24} {'PORT':>6} {'PROVIDER':<12} {'PUBLIC_URL':<48} {'PID':>7}"
    console.print(header)
    console.print("-" * len(header))
    for h in handles:
        console.print(f"{h.name:<24} {h.port:>6} {h.provider:<12} {h.public_url:<48} {h.pid:>7}")


@tunnel_group.command("stop")
@click.argument("name", required=False)
@click.option("--all", "stop_all", is_flag=True, default=False, help="Stop every active tunnel.")
def stop_cmd(name: str | None, stop_all: bool) -> None:
    """Stop a named tunnel or (with ``--all``) every active tunnel."""
    if not stop_all and not name:
        raise click.UsageError("Provide a NAME or use --all.")
    reg = _build_registry()
    if stop_all:
        handles = reg.list_active()
        if not handles:
            console.print("[dim]No active tunnels to stop.[/dim]")
            return
        for h in handles:
            _sigterm_pid(h.pid)
            reg.destroy(h.name)
            console.print(f"[yellow]Stopped[/yellow] {h.name} ({h.provider})")
        return
    # Single tunnel
    assert name is not None
    handle = reg.get(name)
    if handle is None:
        console.print(f"[red]No tunnel named '{name}'.[/red]")
        raise SystemExit(1)
    _sigterm_pid(handle.pid)
    reg.destroy(handle.name)
    console.print(f"[yellow]Stopped[/yellow] {handle.name} ({handle.provider})")
