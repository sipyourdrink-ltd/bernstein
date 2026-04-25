"""CLI surface for the credential vault.

Two top-level commands are exposed:

* ``bernstein connect <provider>`` — guided OAuth or token-paste flow that
  validates the credential against the provider's whoami endpoint and
  stores it in the OS keychain (default) or an AES-GCM file blob.
* ``bernstein creds <list|revoke|test>`` — list metadata, revoke a stored
  credential (and call the provider's revoke endpoint when configured),
  or re-validate an existing entry.

The CLI never prints secrets — every UI affordance uses
:func:`bernstein.core.security.vault.resolver.mask_secret` and
:func:`bernstein.core.security.vault.resolver.fingerprint`.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import webbrowser
from typing import TYPE_CHECKING

import click
from rich.table import Table

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.security.vault.protocol import CredentialVault
from bernstein.core.security.vault.connect import (
    perform_connect,
    perform_revoke,
    perform_test,
)
from bernstein.core.security.vault.factory import (
    BackendChoice,
    open_vault,
)
from bernstein.core.security.vault.oauth_device import (
    OAuthDeviceCancelled,
    OAuthDeviceTimeout,
    begin_device_code,
    poll_device_code,
)
from bernstein.core.security.vault.protocol import VaultError
from bernstein.core.security.vault.providers import (
    AuthMode,
    ProviderConfig,
    list_providers,
    require_provider,
)

__all__ = ["connect_cmd", "creds_group"]


# ---------------------------------------------------------------------------
# Common backend options
# ---------------------------------------------------------------------------


def _backend_options(func: object) -> object:
    """Apply ``--backend`` and ``--passphrase-env`` to a click command."""
    func = click.option(  # type: ignore[assignment]
        "--passphrase-env",
        "passphrase_env",
        default=None,
        help="Env var holding the passphrase for the file backend.",
    )(func)
    func = click.option(  # type: ignore[assignment]
        "--backend",
        type=click.Choice(["keyring", "file"]),
        default=None,
        help="Vault backend (default: keyring; file requires --passphrase-env).",
    )(func)
    return func


def _open_vault_or_exit(
    backend: BackendChoice | None,
    passphrase_env: str | None,
) -> CredentialVault:
    try:
        return open_vault(backend=backend, passphrase_env=passphrase_env)
    except VaultError as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# bernstein connect <provider>
# ---------------------------------------------------------------------------


@click.command("connect")
@click.argument("provider_id", metavar="PROVIDER")
@click.option(
    "--oauth",
    is_flag=True,
    default=False,
    help="Use OAuth device-code flow (Linear only). Default is token paste.",
)
@_backend_options
def connect_cmd(
    provider_id: str,
    oauth: bool,
    backend: BackendChoice | None,
    passphrase_env: str | None,
) -> None:
    """Connect Bernstein to a third-party provider and store the credential.

    \b
      bernstein connect github           # paste a PAT
      bernstein connect linear --oauth   # OAuth device-code flow
      bernstein connect jira             # email + base URL + API token
      bernstein connect slack            # paste a bot token
      bernstein connect telegram         # paste a bot token
    """
    try:
        provider = require_provider(provider_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    vault = _open_vault_or_exit(backend, passphrase_env)

    if oauth and provider.auth_mode is AuthMode.TOKEN_PASTE and provider.oauth_device_code is None:
        raise click.UsageError(f"{provider.display_name} does not support OAuth.")

    if oauth or provider.auth_mode is AuthMode.OAUTH_DEVICE_CODE:
        fields = _run_oauth_device_code(provider)
    else:
        fields = _prompt_paste(provider)

    result = perform_connect(provider, fields, vault=vault)
    if not result.success:
        console.print(f"[red]Validation failed:[/red] {result.error}")
        if result.masked_secret:
            console.print(f"[dim]Masked token: {result.masked_secret} (fingerprint {result.fingerprint})[/dim]")
        raise SystemExit(1)

    console.print(f"[green]Stored {provider.display_name} credentials[/green] for [bold]{result.account}[/bold]")
    console.print(f"  fingerprint: [cyan]{result.fingerprint}[/cyan]   masked: {result.masked_secret}")
    if provider.notes:
        console.print(f"  [dim]note: {provider.notes}[/dim]")


def _prompt_paste(provider: ProviderConfig) -> dict[str, str]:
    """Walk the user through the provider's paste prompts.

    Secret prompts use :func:`getpass.getpass` so the token never echoes.
    Non-secret fields (Jira email, base URL) use :func:`click.prompt`.
    """
    fields: dict[str, str] = {}
    for prompt in provider.paste_prompts:
        value = (
            getpass.getpass(f"{prompt.label}: ")
            if prompt.is_secret
            else click.prompt(prompt.label, type=str)
        )
        if not value:
            raise click.UsageError(f"{prompt.label} cannot be empty.")
        fields[prompt.field] = value.strip()
    return fields


def _run_oauth_device_code(provider: ProviderConfig) -> dict[str, str]:
    """Drive the OAuth device-code flow and return ``{"token": access_token}``."""
    spec = provider.oauth_device_code
    if spec is None:
        raise click.UsageError(f"{provider.display_name} has no OAuth device-code config.")

    try:
        challenge = begin_device_code(
            device_endpoint=spec.device_code_endpoint,
            client_id=spec.client_id,
            scope=spec.scope,
        )
    except Exception as exc:
        raise click.ClickException(f"Could not start OAuth flow: {exc}") from exc

    console.print(
        f"\nVisit [bold cyan]{challenge.verification_url}[/bold cyan] and enter code "
        f"[bold]{challenge.user_code}[/bold]\n",
    )
    with contextlib.suppress(Exception):
        webbrowser.open(challenge.verification_url)

    async def _await_token() -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: poll_device_code(
                challenge,
                token_endpoint=spec.token_endpoint,
                client_id=spec.client_id,
            ).access_token,
        )

    try:
        token = asyncio.run(_await_token())
    except OAuthDeviceCancelled as exc:
        raise click.ClickException(f"OAuth cancelled: {exc}") from exc
    except OAuthDeviceTimeout as exc:
        raise click.ClickException(f"OAuth timeout: {exc}") from exc
    except Exception as exc:
        raise click.ClickException(f"OAuth failed: {exc}") from exc

    return {"token": token}


# ---------------------------------------------------------------------------
# bernstein creds list / revoke / test
# ---------------------------------------------------------------------------


@click.group("creds")
def creds_group() -> None:
    """Inspect and manage credentials stored in the Bernstein vault."""


@creds_group.command("list")
@_backend_options
def creds_list(backend: BackendChoice | None, passphrase_env: str | None) -> None:
    """List stored credentials. Never prints the secret itself."""
    vault = _open_vault_or_exit(backend, passphrase_env)
    records = vault.list()
    if not records:
        console.print("[yellow]No credentials stored.[/yellow]  Run [bold]bernstein connect <provider>[/bold] first.")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Account")
    table.add_column("Fingerprint", style="dim")
    table.add_column("Created", style="dim")
    table.add_column("Last used", style="dim")
    known_ids = {p.id for p in list_providers()}
    for rec in sorted(records, key=lambda r: r.provider_id):
        if rec.provider_id not in known_ids:
            continue
        table.add_row(
            rec.provider_id,
            rec.account or "-",
            rec.fingerprint or "-",
            rec.created_at[:19] if rec.created_at else "-",
            rec.last_used_at[:19] if rec.last_used_at else "never",
        )
    console.print(table)


@creds_group.command("revoke")
@click.argument("provider_id", metavar="PROVIDER")
@_backend_options
def creds_revoke(
    provider_id: str,
    backend: BackendChoice | None,
    passphrase_env: str | None,
) -> None:
    """Remove a credential locally and call the provider's revoke endpoint."""
    try:
        provider = require_provider(provider_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    vault = _open_vault_or_exit(backend, passphrase_env)
    result = perform_revoke(provider, vault=vault)

    if not result.removed_local and not result.revoked_remote:
        console.print(f"[yellow]No vault entry for {provider.id}; nothing to revoke.[/yellow]")
        return

    if result.removed_local:
        console.print(f"[green]Removed local vault entry for {provider.display_name}.[/green]")
    if provider.revoke is not None:
        if result.revoked_remote:
            console.print(f"[green]Revoked remote credential at {provider.display_name}.[/green]")
        else:
            console.print(f"[yellow]Remote revoke not confirmed:[/yellow] {result.error}")
    elif provider.notes:
        console.print(f"[dim]Note: {provider.notes}[/dim]")


@creds_group.command("test")
@click.argument("provider_id", metavar="PROVIDER")
@_backend_options
def creds_test(
    provider_id: str,
    backend: BackendChoice | None,
    passphrase_env: str | None,
) -> None:
    """Re-validate a stored credential against the provider's whoami."""
    try:
        provider = require_provider(provider_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    vault = _open_vault_or_exit(backend, passphrase_env)
    result = perform_test(provider, vault=vault)
    if result.success:
        console.print(f"[green]OK[/green] {provider.display_name} → [bold]{result.account}[/bold]")
        return
    console.print(f"[red]Test failed:[/red] {result.error}")
    raise SystemExit(1)
