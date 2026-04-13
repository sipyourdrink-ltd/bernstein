"""CLI authentication commands for Bernstein SSO."""

from __future__ import annotations

import json
import os
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console

from bernstein.cli.helpers import SERVER_URL
from bernstein.core.auth import extract_jwt_expiry

console = Console()

_TOKEN_DIR = Path.home() / ".bernstein"
_TOKEN_FILE = _TOKEN_DIR / "token.json"


@dataclass(frozen=True)
class CachedToken:
    """Locally cached CLI token metadata."""

    token: str
    server_url: str
    saved_at: float
    expires_at: float | None = None
    refresh_token: str | None = None

    @property
    def is_expired(self) -> bool:
        """Return whether the cached token is expired."""
        return self.expires_at is not None and time.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize the token cache entry."""
        return {
            "token": self.token,
            "server_url": self.server_url,
            "saved_at": self.saved_at,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CachedToken:
        """Deserialize a token cache entry from JSON."""
        expires_at_raw = payload.get("expires_at")
        expires_at = float(expires_at_raw) if isinstance(expires_at_raw, int | float) else None
        refresh_token = payload.get("refresh_token")
        return cls(
            token=str(payload["token"]),
            server_url=str(payload.get("server_url", SERVER_URL)),
            saved_at=float(payload.get("saved_at", 0.0)),
            expires_at=expires_at,
            refresh_token=str(refresh_token) if isinstance(refresh_token, str) else None,
        )


def _save_token(
    token: str,
    server_url: str = "",
    *,
    expires_at: float | None = None,
    refresh_token: str | None = None,
) -> CachedToken:
    """Save a JWT token and metadata to the local cache."""
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    cached = CachedToken(
        token=token,
        server_url=server_url or SERVER_URL,
        saved_at=time.time(),
        expires_at=expires_at if expires_at is not None else extract_jwt_expiry(token),
        refresh_token=refresh_token,
    )
    _TOKEN_FILE.write_text(json.dumps(cached.to_dict(), indent=2))
    _TOKEN_FILE.chmod(0o600)
    return cached


def _refresh_token_entry(cached: CachedToken) -> CachedToken | None:
    """Refresh an expired token when a refresh token is available."""
    if not cached.refresh_token:
        return None
    try:
        response = httpx.post(
            f"{cached.server_url}/auth/cli/refresh",
            json={"refresh_token": cached.refresh_token},
            timeout=5.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expires_at_raw = payload.get("expires_at")
    expires_at = float(expires_at_raw) if isinstance(expires_at_raw, int | float) else None
    refresh_token = payload.get("refresh_token")
    return _save_token(
        token,
        cached.server_url,
        expires_at=expires_at,
        refresh_token=str(refresh_token) if isinstance(refresh_token, str) else cached.refresh_token,
    )


def _load_token() -> CachedToken | None:
    """Load the cached JWT token, refreshing if possible."""
    if not _TOKEN_FILE.exists():
        return None
    try:
        cached = CachedToken.from_dict(json.loads(_TOKEN_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return None
    if not cached.is_expired:
        return cached
    refreshed = _refresh_token_entry(cached)
    if refreshed is not None:
        return refreshed
    _clear_token()
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
@click.option("--sso", is_flag=True, help="Open the browser automatically for the SSO flow.")
def auth_login(server: str | None, sso: bool) -> None:
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
    verification_uri = str(device["verification_uri"])
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
    if sso:
        if webbrowser.open(verification_uri):
            console.print("[dim]Opened browser for SSO login.[/dim]")
        else:
            console.print("[dim]Could not open a browser automatically; open the URL manually.[/dim]")

    # Poll for authorization
    _poll_for_token(target, device_code, expires_in, interval)


def _show_profile(target: str, token: str) -> None:
    """Display profile info for the authenticated user (best-effort)."""
    try:
        resp = httpx.get(
            f"{target}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            profile = resp.json()
            console.print(f"  User:  {profile.get('display_name', 'unknown')}")
            console.print(f"  Email: {profile.get('email', 'unknown')}")
            console.print(f"  Role:  {profile.get('role', 'unknown')}")
    except Exception:
        pass


def _poll_for_token(target: str, device_code: str, expires_in: int, interval: int) -> None:
    """Poll the token endpoint until authorized, expired, or timed out."""
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
            expires_at = result.get("expires_at")
            refresh_token = result.get("refresh_token")
            _save_token(
                token,
                server_url=target,
                expires_at=float(expires_at) if isinstance(expires_at, int | float) else None,
                refresh_token=str(refresh_token) if isinstance(refresh_token, str) else None,
            )
            console.print()
            console.print("[bold green]Authenticated successfully![/bold green]")
            _show_profile(target, token)
            console.print(f"\n[dim]Token cached at {_TOKEN_FILE}[/dim]")
            return

        if status == "expired":
            console.print("\n[red]Device code expired. Please try again.[/red]")
            raise SystemExit(1)

        console.print("[dim].[/dim]", end="", highlight=False)

    console.print("\n[red]Timed out waiting for authorization.[/red]")
    raise SystemExit(1)


@auth_group.command("status")
def auth_status() -> None:
    """Show current authentication status."""
    # Check for SSO token
    cached = _load_token()
    if cached:
        token = cached.token
        target = cached.server_url
        saved_at = cached.saved_at

        console.print("[bold]SSO Authentication[/bold]")
        console.print(f"  Server:  {target}")
        console.print(f"  Cached:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved_at))}")
        if cached.expires_at is not None:
            console.print(f"  Expires: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cached.expires_at))}")

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

    token = cached.token
    target = cached.server_url

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
