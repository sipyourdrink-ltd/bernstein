"""``bernstein preview`` — sandboxed dev-server with public tunnel link.

Subcommands:

* ``preview start`` — auto-discover (or override) the dev-server
  command, boot it inside the originating session's sandbox, expose it
  via the existing ``bernstein tunnel`` wrapper, and print a shareable
  HTTPS URL.
* ``preview list`` — print every active preview as a table or JSON.
* ``preview status <id>`` — print details for a single preview.
* ``preview stop <id>|--all`` — tear the preview down.

The CLI is intentionally thin: every meaningful decision lives in
:class:`bernstein.core.preview.PreviewManager`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.cli.helpers import console
from bernstein.core.preview import (
    AuthMode,
    PreviewError,
    PreviewManager,
    PreviewState,
    list_candidates,
)

if TYPE_CHECKING:
    from bernstein.core.sandbox.backend import SandboxSession

logger = logging.getLogger(__name__)


_AUTH_CHOICES = ["basic", "token", "none"]
_PROVIDER_CHOICES = ["auto", "cloudflared", "ngrok", "bore", "tailscale"]


@click.group("preview")
def preview_group() -> None:
    """Sandboxed dev-server preview with public tunnel link."""


# ---------------------------------------------------------------------------
# preview start
# ---------------------------------------------------------------------------


@preview_group.command("start")
@click.option(
    "--cwd",
    "cwd_arg",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Working directory for the dev server. Defaults to the most recent session worktree.",
)
@click.option(
    "--command",
    "command_arg",
    default=None,
    help='Override the auto-discovered command (e.g. "pnpm dev").',
)
@click.option(
    "--list-commands",
    "list_only",
    is_flag=True,
    default=False,
    help="Print every discovered candidate command instead of starting.",
)
@click.option(
    "--provider",
    type=click.Choice(_PROVIDER_CHOICES),
    default="auto",
    show_default=True,
    help="Tunnel provider; falls back to cloudflared when 'auto' has nothing.",
)
@click.option(
    "--auth",
    "auth_mode",
    type=click.Choice(_AUTH_CHOICES),
    default="token",
    show_default=True,
    help="Auth mode for the public link.",
)
@click.option(
    "--expire",
    "expire_arg",
    default="4h",
    show_default=True,
    help="Link expiry (e.g. 30m, 4h, 1d).",
)
@click.option(
    "--no-clipboard",
    "no_clipboard",
    is_flag=True,
    default=False,
    help="Do not attempt to copy the URL to the clipboard.",
)
def start_cmd(
    cwd_arg: Path | None,
    command_arg: str | None,
    list_only: bool,
    provider: str,
    auth_mode: str,
    expire_arg: str,
    no_clipboard: bool,
) -> None:
    """Boot the dev server and expose it through the existing tunnel wrapper."""
    cwd = (cwd_arg or _resolve_default_cwd()).resolve()
    if not cwd.is_dir():
        console.print(f"[red]--cwd does not exist:[/red] {cwd}")
        raise SystemExit(2)

    if list_only:
        _print_candidates(cwd)
        return

    try:
        sandbox_session = asyncio.run(_create_sandbox_session(cwd))
    except Exception as exc:  # pragma: no cover - sandbox-specific
        console.print(f"[red]Could not provision sandbox:[/red] {exc}")
        raise SystemExit(1) from exc

    manager = PreviewManager()
    try:
        preview = manager.start(
            cwd=cwd,
            sandbox_session=sandbox_session,
            command=command_arg,
            provider=provider,
            auth_mode=AuthMode(auth_mode),
            expire_seconds=expire_arg,
        )
    except PreviewError as exc:
        console.print(f"[red]preview start failed:[/red] {exc}")
        raise SystemExit(1) from exc

    state = preview.state
    console.print(
        f"[green]Started preview[/green] [bold]{state.preview_id}[/bold] "
        f"({state.tunnel_provider} -> localhost:{state.port})"
    )
    console.print(f"[bold]URL:[/bold] {state.share_url}")
    console.print(
        f"[dim]auth={state.auth_mode}  sandbox={state.sandbox_backend}/"
        f"{state.sandbox_session_id}  expires_epoch={int(state.expires_at_epoch)}[/dim]"
    )

    if not no_clipboard:
        _maybe_copy_to_clipboard(state.share_url)


# ---------------------------------------------------------------------------
# preview list
# ---------------------------------------------------------------------------


@preview_group.command("list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def list_cmd(as_json: bool) -> None:
    """List every active preview."""
    states = PreviewManager().list()
    if as_json:
        click.echo(json.dumps([_state_to_payload(s) for s in states], indent=2, sort_keys=True))
        return

    if not states:
        console.print("[dim]No active previews.[/dim]")
        return

    header = (
        f"{'ID':<14} {'COMMAND':<28} {'PORT':>6} {'SANDBOX':<14} "
        f"{'PROVIDER':<12} {'AUTH':<6} {'EXPIRES':>10} URL"
    )
    console.print(header)
    console.print("-" * len(header))
    for s in states:
        console.print(
            f"{s.preview_id:<14} {(s.command or '')[:28]:<28} {s.port:>6} "
            f"{s.sandbox_backend:<14} {s.tunnel_provider:<12} {s.auth_mode:<6} "
            f"{int(s.expires_at_epoch):>10} {s.share_url}"
        )


# ---------------------------------------------------------------------------
# preview status
# ---------------------------------------------------------------------------


@preview_group.command("status")
@click.argument("preview_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def status_cmd(preview_id: str, as_json: bool) -> None:
    """Print details for one preview."""
    state = PreviewManager().status(preview_id)
    if state is None:
        console.print(f"[red]No preview with id {preview_id!r}.[/red]")
        raise SystemExit(1)
    if as_json:
        click.echo(json.dumps(_state_to_payload(state), indent=2, sort_keys=True))
        return
    payload = _state_to_payload(state)
    for key, value in payload.items():
        console.print(f"  [bold]{key}[/bold]: {value}")


# ---------------------------------------------------------------------------
# preview stop
# ---------------------------------------------------------------------------


@preview_group.command("stop")
@click.argument("preview_id", required=False)
@click.option("--all", "stop_all", is_flag=True, default=False, help="Stop every active preview.")
def stop_cmd(preview_id: str | None, stop_all: bool) -> None:
    """Stop one preview or every active preview."""
    if not stop_all and not preview_id:
        raise click.UsageError("Provide a PREVIEW_ID or use --all.")
    manager = PreviewManager()
    if stop_all:
        n = manager.stop_all()
        console.print(f"[yellow]Stopped {n} preview(s).[/yellow]")
        return
    assert preview_id is not None
    if not manager.stop(preview_id):
        console.print(f"[red]No preview with id {preview_id!r}.[/red]")
        raise SystemExit(1)
    console.print(f"[yellow]Stopped[/yellow] {preview_id}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_candidates(cwd: Path) -> None:
    """Render every discovered candidate to the console."""
    candidates = list_candidates(cwd)
    if not candidates:
        console.print(f"[yellow]No preview candidates found under {cwd}.[/yellow]")
        return
    console.print(f"[bold]Candidates discovered under[/bold] {cwd}")
    for c in candidates:
        marker = "*" if c.is_runnable() else "-"
        cmd = c.command or "(metadata only)"
        suffix = f"  [dim]{c.details}[/dim]" if c.details else ""
        console.print(f"  {marker} [cyan]{c.source:<24}[/cyan] {cmd}{suffix}")


def _resolve_default_cwd() -> Path:
    """Pick the most recent session worktree for ``--cwd`` defaults.

    Walks ``.sdd/worktrees/`` and picks the entry with the most recent
    mtime. Falls back to the current working directory.
    """
    base = Path(".sdd/worktrees")
    if base.is_dir():
        candidates = [p for p in base.iterdir() if p.is_dir() and p.name not in {".locks", ".graveyard"}]
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]
    return Path.cwd()


async def _create_sandbox_session(cwd: Path) -> SandboxSession:
    """Provision a worktree-backed sandbox session anchored at *cwd*.

    Reuses the existing :class:`WorktreeSandboxBackend` so the dev
    server runs in the same isolation primitive the originating
    session used. We deliberately don't try to "re-attach" — the
    backend either reuses a warm worktree (when *cwd* already lives
    under ``.sdd/worktrees/``) or carves out a new lightweight one.
    """
    from bernstein.core.sandbox import WorkspaceManifest, get_backend

    backend = get_backend("worktree")
    manifest = WorkspaceManifest(root=str(cwd))
    return await backend.create(manifest, options={"repo_root": str(cwd)})


def _state_to_payload(state: PreviewState) -> dict[str, Any]:
    """Render a :class:`PreviewState` as a public-facing dict.

    Sensitive fields (signed token query strings, basic-auth passwords)
    live on the ``share_url``; the JSON output is suitable for
    operators but should still be treated as confidential.
    """
    return {
        "id": state.preview_id,
        "command": state.command,
        "cwd": state.cwd,
        "port": state.port,
        "sandbox": state.sandbox_backend,
        "session": state.sandbox_session_id,
        "provider": state.tunnel_provider,
        "tunnel_name": state.tunnel_name,
        "url": state.share_url,
        "public_url": state.public_url,
        "auth_mode": state.auth_mode,
        "expires_at": int(state.expires_at_epoch),
        "pid": state.process_pid,
    }


def _maybe_copy_to_clipboard(text: str) -> None:
    """Best-effort clipboard copy; failures are silent."""
    try:
        from bernstein.tui.clipboard import copy_to_clipboard

        result = copy_to_clipboard(text)
        if result.success:
            console.print("[dim]URL copied to clipboard.[/dim]")
    except Exception as exc:  # pragma: no cover - clipboard backends are platform-specific
        logger.debug("clipboard copy failed: %s", exc)


__all__ = ["preview_group"]
