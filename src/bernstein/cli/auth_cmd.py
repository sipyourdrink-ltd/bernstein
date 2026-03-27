"""CLI authentication commands for Bernstein SSO.

Provides:
- bernstein auth login   — authenticate via device flow or browser
- bernstein auth status  — show current auth status
- bernstein auth logout  — revoke the current token
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console

from bernstein.cli.helpers import SERVER_URL

console = Console()

# Token is cached at ~/.bernstein/token.json
_TOKEN_DIR = Path.home() / ".bernstein"
_TOKEN_FILE = _TOKEN_DIR / "token.json"


def _save_token(token: str, server_url: str = "") -> None:
    """Save JWT token to local cache."""
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(
        json.dumps(
            {
                "token": token,
                "server_url": server_url or SERVER_URL,
                "saved_at": time.time(),
            },
            indent=2,
        )
    )
    # Restrict permissions (owner read/write only)
    _TOKEN_FILE.chmod(0o600)


def _load_token() -> dict[str, Any] | None:
    """Load cached JWT token."""
    if not _TOKEN_FILE.exists():
        return None
    try:
        return json.loads(_TOKEN_FILE.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _clear_token() -> None:
    """Remove cached token."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()


@click.group("auth")
def auth_group() -> None:
    """Manage authentication (SSO / token-based)."""


@auth_group.command("login")
@click.option("--server", default=None, help="Server URL (default: $BERNSTEIN_SERVER_URL or localhost:8052)")
def auth_login(server: str | None) -> None:
    """Authenticate with the Bernstein server via device authorization flow.

    This initiates a device code flow:
    1. The CLI requests a device code from the server
    2. You open the server URL in your browser and log in via SSO
    3. Enter the displayed user code to authorize the CLI
    4. The CLI polls until authorized and caches the token
    """
    target = server or SERVER_URL

    # Check server availability
    try:
        resp = httpx.get(f"{target}/auth/providers", timeout=5.0)
        resp.raise_for_status()
        providers = resp.json()
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach server at {target}[/red]")
        console.print("[dim]Is the Bernstein server running? Try: bernstein start[/dim]")
        raise SystemExit(1)  # noqa: B904
    except Exception as exc:
        console.print(f"[red]Error checking auth providers: {exc}[/red]")
        raise SystemExit(1)  # noqa: B904

    if not providers.get("device_flow_enabled"):
        console.print("[yellow]SSO authentication is not configured on this server.[/yellow]")
        console.print("[dim]Set BERNSTEIN_AUTH_TOKEN for simple token auth instead.[/dim]")
        raise SystemExit(1)

    # Initiate device flow
    try:
        resp = httpx.post(
            f"{target}/auth/cli/device",
            json={"client_name": "bernstein-cli"},
            timeout=5.0,
        )
        resp.raise_for_status()
        device = resp.json()
    except Exception as exc:
        console.print(f"[red]Failed to initiate device auth: {exc}[/red]")
        raise SystemExit(1)  # noqa: B904

    user_code = device["user_code"]
    verification_uri = device["verification_uri"]
    expires_in = device["expires_in"]
    interval = device["interval"]
    device_code = device["device_code"]

    console.print()
    console.print("[bold]Device Authorization[/bold]")
    console.print()
    console.print(f"  1. Open: [bold cyan]{verification_uri}[/bold cyan]")
    console.print("  2. Log in via SSO (OIDC or SAML)")
    console.print(f"  3. Enter code: [bold green]{user_code}[/bold green]")
    console.print()
    console.print(f"[dim]Code expires in {expires_in // 60} minutes. Waiting for authorization...[/dim]")

    # Poll for authorization
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)

        try:
            resp = httpx.post(
                f"{target}/auth/cli/token",
                json={"device_code": device_code},
                timeout=5.0,
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception:
            console.print("[dim].[/dim]", end="")
            continue

        status = result.get("status", "pending")
        if status == "complete":
            token = result["access_token"]
            _save_token(token, server_url=target)
            console.print()
            console.print("[bold green]Authenticated successfully![/bold green]")

            # Show profile
            try:
                profile_resp = httpx.get(
                    f"{target}/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5.0,
                )
                if profile_resp.status_code == 200:
                    profile = profile_resp.json()
                    console.print(f"  User:  {profile.get('display_name', 'unknown')}")
                    console.print(f"  Email: {profile.get('email', 'unknown')}")
                    console.print(f"  Role:  {profile.get('role', 'unknown')}")
            except Exception:
                pass

            console.print(f"\n[dim]Token cached at {_TOKEN_FILE}[/dim]")
            return

        if status == "expired":
            console.print("\n[red]Device code expired. Please try again.[/red]")
            raise SystemExit(1)

        # Still pending
        console.print("[dim].[/dim]", end="", highlight=False)

    console.print("\n[red]Timed out waiting for authorization.[/red]")
    raise SystemExit(1)


@auth_group.command("status")
def auth_status() -> None:
    """Show current authentication status."""
    # Check for SSO token
    cached = _load_token()
    if cached:
        token = cached.get("token", "")
        target = cached.get("server_url", SERVER_URL)
        saved_at = cached.get("saved_at", 0)

        console.print("[bold]SSO Authentication[/bold]")
        console.print(f"  Server:  {target}")
        console.print(f"  Cached:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved_at))}")

        # Validate token
        try:
            resp = httpx.get(
                f"{target}/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                profile = resp.json()
                console.print("  Status:  [green]valid[/green]")
                console.print(f"  User:    {profile.get('display_name', 'unknown')}")
                console.print(f"  Email:   {profile.get('email', 'unknown')}")
                console.print(f"  Role:    {profile.get('role', 'unknown')}")
                groups = profile.get("sso_groups", [])
                if groups:
                    console.print(f"  Groups:  {', '.join(groups)}")
                return
            console.print("  Status:  [red]expired or invalid[/red]")
            console.print("[dim]Run 'bernstein auth login' to re-authenticate.[/dim]")
        except httpx.ConnectError:
            console.print("  Status:  [yellow]server unreachable[/yellow]")
        except Exception as exc:
            console.print(f"  Status:  [red]error: {exc}[/red]")
        return

    # Check for legacy token
    legacy = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if legacy:
        console.print("[bold]Legacy Token Authentication[/bold]")
        console.print(f"  Token:   {'*' * 8}...{legacy[-4:]}" if len(legacy) > 4 else "  Token:   ****")
        console.print(f"  Server:  {SERVER_URL}")
        console.print("[dim]Using BERNSTEIN_AUTH_TOKEN environment variable.[/dim]")
        return

    console.print("[yellow]Not authenticated.[/yellow]")
    console.print("[dim]Run 'bernstein auth login' for SSO, or set BERNSTEIN_AUTH_TOKEN.[/dim]")


@auth_group.command("logout")
def auth_logout() -> None:
    """Revoke current session and clear cached token."""
    cached = _load_token()
    if not cached:
        console.print("[dim]No cached token found.[/dim]")
        return

    token = cached.get("token", "")
    target = cached.get("server_url", SERVER_URL)

    # Try to revoke server-side
    try:
        resp = httpx.post(
            f"{target}/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            console.print("[green]Session revoked on server.[/green]")
    except Exception:
        console.print("[dim]Could not reach server to revoke session.[/dim]")

    _clear_token()
    console.print("[green]Local token cleared.[/green]")
