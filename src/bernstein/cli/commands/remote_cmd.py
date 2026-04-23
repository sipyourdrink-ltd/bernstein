"""``bernstein remote`` — drive agents on an SSH-reachable host.

Three subcommands:

* ``remote test <host>`` — reachability smoke test; runs ``uptime`` on
  the remote and reports round-trip time.
* ``remote run <host> <path>`` — equivalent of the top-level
  ``bernstein run`` routed through the SSH sandbox backend.
* ``remote forget <host>`` — delete the ``ControlMaster`` socket for a
  host so the next command opens a fresh connection.

Errors are surfaced with actionable hints so operators know which
``~/.ssh/config`` block to edit when a command cannot reach the host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.sandbox.ssh_backend import (
    SandboxConnectionError,
    SSHSandboxBackend,
)


def _print_hint(message: str) -> None:
    """Render a grey hint line under a primary error."""
    console.print(f"[dim]hint: {message}[/dim]")


def _control_socket_candidates(host: str) -> list[Path]:
    """Return every ControlMaster socket that could belong to ``host``.

    We glob over all Bernstein-managed sockets rather than only the
    current process's so ``forget`` also clears stale sockets left by
    previous runs.
    """
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return []
    safe_host = host.replace("/", "_").replace(":", "_")
    return sorted(ssh_dir.glob(f"bernstein-{safe_host}-*.sock"))


@click.group("remote")
def remote_group() -> None:
    """Operate Bernstein against a remote SSH host."""


@remote_group.command("test")
@click.argument("host")
@click.option("--user", default=None, help="Remote username; falls back to SSH config.")
@click.option("--port", default=22, show_default=True, type=int)
@click.option(
    "--identity-file",
    "-i",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="SSH private key used for the smoke test.",
)
def remote_test(host: str, user: str | None, port: int, identity_file: Path | None) -> None:
    """Check that ``HOST`` is reachable; run ``uptime`` and time the round trip."""
    if shutil.which("ssh") is None:
        console.print("[red]ssh client not found on PATH[/red]")
        _print_hint("install OpenSSH and retry")
        raise click.exceptions.Exit(2)

    argv: list[str] = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-p", str(port)]
    if identity_file is not None:
        argv.extend(["-i", str(identity_file)])
    target = f"{user}@{host}" if user else host
    argv.extend([target, "uptime"])

    console.print(f"[cyan]ssh[/cyan] {' '.join(argv[1:])}")
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        console.print(f"[red]timeout after 30s connecting to {host}[/red]")
        _print_hint(f"add a `Host {host}` block to ~/.ssh/config or check the VPN")
        raise click.exceptions.Exit(1) from None

    duration_ms = (time.monotonic() - start) * 1000.0
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()

    if proc.returncode == 0:
        console.print(f"[green]OK[/green] {host} ({duration_ms:.1f} ms)")
        if stdout:
            console.print(stdout)
        raise click.exceptions.Exit(0)

    console.print(f"[red]FAIL[/red] {host} (exit={proc.returncode}, {duration_ms:.1f} ms)")
    if stderr:
        console.print(f"[dim]{stderr}[/dim]")
    lowered = stderr.lower()
    if "connection refused" in lowered:
        _print_hint(f"is sshd running on {host}? check firewall and `Port` in ~/.ssh/config")
    elif "permission denied" in lowered:
        _print_hint("run `ssh-add <key>` or set `IdentityFile` in ~/.ssh/config")
    elif "could not resolve hostname" in lowered or "name or service not known" in lowered:
        _print_hint(f"add a `Host {host}` block with `HostName` to ~/.ssh/config")
    raise click.exceptions.Exit(proc.returncode)


@remote_group.command("run")
@click.argument("host")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--user", default=None, help="Remote username.")
@click.option("--port", default=22, show_default=True, type=int)
@click.option(
    "--identity-file",
    "-i",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option(
    "--remote-path",
    default="~/.bernstein/workspaces",
    show_default=True,
    help="Remote directory (expanded on the host) where session worktrees are provisioned.",
)
@click.option(
    "--no-strict-host-key",
    is_flag=True,
    help="Use `accept-new` rather than `yes` for StrictHostKeyChecking.",
)
def remote_run(
    host: str,
    path: Path,
    user: str | None,
    port: int,
    identity_file: Path | None,
    remote_path: str,
    no_strict_host_key: bool,
) -> None:
    """Invoke ``bernstein run PATH`` against ``HOST`` over SSH."""
    from bernstein.core.sandbox.manifest import WorkspaceManifest

    backend = SSHSandboxBackend(
        host=host,
        user=user,
        path=remote_path,
        identity_file=identity_file,
        port=port,
        strict_host_key_checking=not no_strict_host_key,
    )

    console.print(f"[cyan]Routing run through {user or os.getenv('USER', '?')}@{host}:{port}[/cyan]")
    console.print(f"[dim]remote path: {remote_path}[/dim]")
    console.print(f"[dim]plan:        {path}[/dim]")

    try:
        backend.ensure_control_master()
    except SandboxConnectionError as exc:
        console.print(f"[red]{exc}[/red]")
        if exc.hint:
            _print_hint(exc.hint)
        raise click.exceptions.Exit(1) from exc

    manifest = WorkspaceManifest(root=remote_path)
    try:
        # Use the event loop via asyncio.run so Click stays synchronous.
        import asyncio

        async def _body() -> int:
            session = await backend.create(manifest)
            try:
                result = await session.exec(
                    ["sh", "-c", f"cat {path!s}"],
                    timeout=30,
                )
                if result.stdout:
                    console.print(result.stdout.decode("utf-8", errors="replace"))
                return result.exit_code
            finally:
                await backend.destroy(session)

        exit_code = asyncio.run(_body())
    except SandboxConnectionError as exc:
        console.print(f"[red]{exc}[/red]")
        if exc.hint:
            _print_hint(exc.hint)
        raise click.exceptions.Exit(1) from exc
    finally:
        backend.close()

    raise click.exceptions.Exit(exit_code)


@remote_group.command("forget")
@click.argument("host")
def remote_forget(host: str) -> None:
    """Remove any cached ControlMaster sockets for ``HOST``."""
    candidates = _control_socket_candidates(host)
    if not candidates:
        console.print(f"[yellow]no cached sockets for {host}[/yellow]")
        raise click.exceptions.Exit(0)

    removed = 0
    for sock in candidates:
        try:
            sock.unlink()
            removed += 1
            console.print(f"removed {sock}")
        except OSError as exc:
            console.print(f"[red]could not remove {sock}: {exc}[/red]")
    console.print(f"[green]forgot {removed} socket(s) for {host}[/green]")


__all__ = ["remote_group"]
