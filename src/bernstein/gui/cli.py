"""CLI commands for the Bernstein web GUI.

The GUI ships with the wheel: pre-built static SPA in ``src/bernstein/gui/static/``
plus the Python mount in ``bernstein.gui``. The ``[gui]`` extras label is kept
in pyproject for forward-compat (so the install spec stays stable), but no
runtime gate is needed today - ``sse-starlette`` arrives transitively via core
deps and ``fastapi`` / ``uvicorn`` are already required.

Subcommands:

* ``serve`` - boot the FastAPI app, optionally publish via a tunnel
* ``qr``    - print a QR code for an existing tunnel URL with onboarding
              credentials (use without ``--tunnel`` after ``bernstein
              tunnel start`` so the operator picks the provider explicitly)
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from bernstein.core.tunnels.protocol import TunnelHandle
    from bernstein.core.tunnels.registry import TunnelRegistry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: File where the latest onboarding credentials are persisted.
#: Permissions are tightened to owner-only on write.
PASSPHRASE_FILE = Path.home() / ".bernstein" / "dashboard.passphrase"

#: Provider choices accepted by ``serve --tunnel-provider`` and ``qr``.
PROVIDER_CHOICES = ("auto", "cloudflared", "ngrok", "bore", "tailscale")


# ---------------------------------------------------------------------------
# Helpers (kept module-level so tests can drive them directly)
# ---------------------------------------------------------------------------


def _build_tunnel_registry() -> TunnelRegistry:
    """Construct a registry with the four shipped drivers registered.

    Imports happen here (not at module load) so importing this module
    stays cheap when ``serve`` is invoked without ``--tunnel``.
    """
    from bernstein.core.tunnels.drivers import register_default_drivers
    from bernstein.core.tunnels.registry import TunnelRegistry

    reg = TunnelRegistry()
    register_default_drivers(reg)
    return reg


def write_passphrase_file(path: Path, payload: dict[str, str]) -> None:
    """Persist the onboarding payload to disk with 0600 permissions.

    Args:
        path: Destination file path.
        payload: Dict of credentials (token + passphrase + url).

    The function creates parent directories as needed and overwrites any
    previous credentials atomically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    # ``os.open`` with explicit mode 0o600 avoids the race window between
    # ``write_text`` and ``chmod`` where the file is briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def read_passphrase_file(path: Path) -> dict[str, str] | None:
    """Read an existing onboarding payload, returning ``None`` if absent."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed: object = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    # narrow type for the type checker - only return string-string entries
    result: dict[str, str] = {}
    items: list[tuple[object, object]] = list(parsed.items())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    for k, v in items:
        result[str(k)] = str(v)
    return result


def _print_onboarding(url: str, passphrase: str, *, echo: Callable[[str], object] | None = None) -> str:
    """Format the operator-facing onboarding block and return the rendered text.

    The QR is rendered alongside a short instruction block.  The function
    returns the full multi-line string so callers can also dump it to a
    log or test against it.
    """
    from bernstein.gui import qr as _qr

    qr_text = _qr.render_ascii_qr(url)
    out = (
        "\nBernstein PWA onboarding\n"
        f"  URL:        {url}\n"
        f"  Passphrase: {passphrase}\n"
        "  Scan the QR with your phone, tap 'Add to Home Screen' (iOS) or\n"
        "  'Install app' (Android), then enter the passphrase once.\n\n"
        f"{qr_text}\n"
    )
    if echo is None:
        click.echo(out)
    else:
        # Test-injection seam: callers can pass a list-append or similar.
        echo(out)
    return out


def _start_tunnel(port: int, provider: str) -> TunnelHandle:
    """Start a tunnel for ``port`` using ``provider`` ("auto" allowed).

    The caller is responsible for tearing the tunnel down (SIGTERM on the
    handle's PID + registry destroy).
    """
    return _build_tunnel_registry().create(port=port, provider=provider)


def _stop_tunnel(name: str) -> None:
    """Stop a previously started tunnel by name; idempotent on missing."""
    reg = _build_tunnel_registry()
    handle = reg.get(name)
    if handle is None:
        return
    with contextlib.suppress(OSError):
        # OSError swallowed: the tunnel process may already be gone.
        os.kill(handle.pid, signal.SIGTERM)
    reg.destroy(name)


def _enforce_dashboard_posture(host: str, workdir: Path) -> None:
    """Apply the dashboard startup posture for a bind on *host* (#2366).

    Non-loopback binds refuse to start until dashboard auth is configured
    (a scoped token or a password) - there is no silent open mode on a
    routable interface. Unconfigured loopback binds get an operator token
    issued into the signed token journal and printed once.

    Raises:
        SystemExit: On an unconfigured non-loopback bind.
    """
    import time

    from bernstein.core.security.audit_chain import AuditChainStore, record_dashboard_token_grant
    from bernstein.core.server.dashboard_tokens import (
        SCOPE_OPERATOR,
        DashboardTokenRegistry,
        resolve_dashboard_hmac_key,
        resolve_dashboard_posture,
    )

    sdd_dir = workdir / ".sdd"
    key = resolve_dashboard_hmac_key(sdd_dir)
    registry = DashboardTokenRegistry(sdd_dir / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    configured = bool(os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")) or registry.has_tokens()

    posture = resolve_dashboard_posture(host, auth_configured=configured)
    if posture == "refuse":
        raise SystemExit(
            f"Refusing to bind the dashboard on non-loopback host {host!r} without auth configured.\n"
            "Issue a scoped credential first:\n"
            "  bernstein auth dashboard-token issue --principal <you> --scope operator\n"
            "or set BERNSTEIN_DASHBOARD_PASSWORD, then re-run."
        )
    if posture == "generate":
        raw, record = registry.issue(principal="local-operator", scope=SCOPE_OPERATOR, now=int(time.time()))
        record_dashboard_token_grant(
            chain=AuditChainStore(sdd_dir / "audit", key=key),
            grant="issue",
            token_id=record.token_id,
            token_sha256=record.token_sha256,
            principal=record.principal,
            scope=record.scope,
        )
        click.echo("Dashboard token (operator scope, printed once - the journal keeps only its digest):")
        click.echo(f"  Token: {raw}")
        click.echo("  Use it as `Authorization: Bearer <token>` or in the dashboard login form.")
        return
    click.echo(
        "Dashboard auth: configured (scoped token or password). Manage tokens with `bernstein auth dashboard-token`."
    )


# ---------------------------------------------------------------------------
# Click surface
# ---------------------------------------------------------------------------


@click.group("gui")
def gui_group() -> None:
    """Bernstein web GUI - operator dashboard.

    ``bernstein gui serve`` boots a FastAPI server with the SPA mounted at
    ``/ui`` and the full ``/api/v1/*`` surface attached. Pass ``--tunnel``
    to publish the app over a Cloudflare / ngrok / bore / Tailscale tunnel
    and print a QR for phone onboarding.

    ``bernstein gui qr`` prints a QR for an already-running tunnel or an
    arbitrary URL - handy when reissuing the passphrase or re-pairing a
    second device.
    """


@gui_group.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option(
    "--port",
    default=8052,
    show_default=True,
    type=int,
    help="Bind port. Defaults to 8052 (canonical Bernstein orchestrator port).",
)
@click.option("--no-open", is_flag=True, help="Do not auto-open the browser.")
@click.option(
    "--dev",
    is_flag=True,
    help=(
        "Dev mode - skip browser auto-open. Vite's dev port is governed by "
        "``web/vite.config.ts`` (currently ``strictPort: 5173``); override at "
        "the Vite command line if your smoke / GUI dev workflow uses a "
        "different port (e.g. ``cd web && npm run dev -- --port 3000``)."
    ),
)
@click.option(
    "--minimal",
    is_flag=True,
    help="Mount only the GUI + /gui-meta (skip the full Bernstein API). Useful for smoke tests.",
)
@click.option(
    "--tunnel",
    is_flag=True,
    default=False,
    help="Publish the GUI through a tunnel and print a QR + passphrase for phone onboarding.",
)
@click.option(
    "--tunnel-provider",
    type=click.Choice(PROVIDER_CHOICES),
    default="auto",
    show_default=True,
    help="Tunnel provider when --tunnel is set. 'auto' picks the first installed binary.",
)
def serve(
    host: str,
    port: int,
    no_open: bool,
    dev: bool,
    minimal: bool,
    tunnel: bool,
    tunnel_provider: str,
) -> None:
    """Start a FastAPI server with the GUI mounted at /ui.

    By default also mounts the full Bernstein API surface from
    ``bernstein.core.server.server_app.create_app``. Pass ``--minimal`` to
    skip the full API (faster boot for smoke tests).
    """
    import uvicorn
    from fastapi import FastAPI

    from bernstein.gui import mount, pwa

    # Startup posture (#2366): non-loopback binds refuse to start without
    # dashboard auth configured; unconfigured loopback binds get a scoped
    # operator token issued and printed once.
    _enforce_dashboard_posture(host, Path.cwd())

    if minimal:
        app = FastAPI(title="Bernstein", description="Operator GUI (minimal)")
    else:
        try:
            from bernstein.core.server.server_app import create_app  # pyright: ignore[reportUnknownVariableType]
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(f"Failed to import Bernstein API factory: {exc}") from exc
        app = create_app()

    mount(app)

    local_url = f"http://{host}:{port}/ui/"
    click.echo(f"Bernstein GUI - {local_url}")
    if dev:
        click.echo(
            "Dev mode: run `cd web && npm run dev` in a second terminal for HMR. "
            "Vite's port is set in web/vite.config.ts (default 5173, strictPort); "
            "override with `npm run dev -- --port <port>` if you need a different one."
        )

    tunnel_name: str | None = None
    if tunnel:
        from bernstein.core.tunnels.protocol import ProviderNotAvailable

        try:
            handle = _start_tunnel(port=port, provider=tunnel_provider)
        except ProviderNotAvailable as exc:
            click.echo(f"Tunnel start failed: {exc}", err=True)
            click.echo(f"hint: {exc.hint}", err=True)
            raise SystemExit(1) from exc
        tunnel_name = handle.name
        issue = pwa.new_auth_issue()
        onboarding_url = pwa.compose_onboarding_url(handle.public_url, issue.token)
        payload = {
            "url": onboarding_url,
            "public_url": handle.public_url,
            "token": issue.token,
            "passphrase": issue.passphrase,
            "tunnel_name": handle.name,
            "provider": handle.provider,
        }
        write_passphrase_file(PASSPHRASE_FILE, payload)
        click.echo(f"Tunnel ({handle.provider}) up: {handle.public_url}")
        _print_onboarding(onboarding_url, issue.passphrase)

    if not no_open and not dev and not tunnel:
        with contextlib.suppress(Exception):
            import webbrowser

            webbrowser.open(local_url)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if tunnel_name is not None:
            _stop_tunnel(tunnel_name)


@gui_group.command("qr")
@click.option(
    "--url",
    default=None,
    help="Public URL to encode. Omit to re-print the QR for the last persisted tunnel.",
)
@click.option(
    "--rotate",
    is_flag=True,
    default=False,
    help="Issue a new auth token + passphrase instead of reusing the persisted ones.",
)
@click.option(
    "--passphrase-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the persisted-credentials location (defaults to ~/.bernstein/dashboard.passphrase).",
)
def qr_cmd(url: str | None, rotate: bool, passphrase_file: Path | None) -> None:
    """Print a QR for the current (or specified) tunnel URL."""
    from bernstein.gui import pwa

    path = passphrase_file if passphrase_file is not None else PASSPHRASE_FILE
    existing = read_passphrase_file(path)

    if url is None:
        if existing is None:
            raise click.UsageError(
                "No persisted onboarding credentials. Run `bernstein gui serve --tunnel` "
                "first, or pass --url to encode an arbitrary URL."
            )
        url = existing.get("url") or existing.get("public_url")
        if not url:
            raise click.UsageError(f"Persisted file at {path} has no URL field.")

    if rotate or existing is None:
        issue = pwa.new_auth_issue()
        # Re-attach the token to the URL when we issued a fresh one. The URL
        # may already have a fragment; strip it before composing.
        base_url = url.split("#", 1)[0]
        url = pwa.compose_onboarding_url(base_url.rstrip("/"), issue.token)
        payload = {
            "url": url,
            "token": issue.token,
            "passphrase": issue.passphrase,
        }
        if existing is not None:
            # Preserve provider / tunnel_name metadata if we are rotating
            # in place against an existing tunnel.
            for k in ("public_url", "tunnel_name", "provider"):
                if k in existing:
                    payload[k] = existing[k]
        write_passphrase_file(path, payload)
        passphrase = issue.passphrase
    else:
        passphrase = existing.get("passphrase", "")

    _print_onboarding(url, passphrase)
