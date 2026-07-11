"""``bernstein auth dashboard-token``: scoped dashboard credential grants.

Issue #2366. The dashboard accepts two credential scopes: ``viewer`` (read
every surface, change nothing) and ``operator`` (read and trigger
state-changing actions). Tokens are issued here, printed exactly once, and
recorded in an append-only journal of HMAC-signed rows -- only the token's
SHA-256 digest is stored, so the journal never holds a usable credential.
Each grant and revocation is also mirrored onto the audit chain, and every
write the token later authorizes lands in the ``dashboard-auth`` governance
run (``bernstein governance verify dashboard-auth``).

    bernstein auth dashboard-token issue --principal alice --scope viewer
    bernstein auth dashboard-token list
    bernstein auth dashboard-token revoke <token-id>
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.server.dashboard_tokens import DashboardTokenRegistry


def _registry_for(workdir: str) -> tuple[DashboardTokenRegistry, AuditChainStore]:
    """Build the (registry, chain) pair rooted at *workdir*'s .sdd."""
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.server.dashboard_tokens import (
        DashboardTokenRegistry,
        resolve_dashboard_hmac_key,
    )

    sdd_dir = Path(workdir).resolve() / ".sdd"
    key = resolve_dashboard_hmac_key(sdd_dir)
    registry = DashboardTokenRegistry(sdd_dir / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    chain = AuditChainStore(sdd_dir / "audit", key=key)
    return registry, chain


_WORKDIR_OPTION = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)


@click.group("dashboard-token")
def dashboard_token_group() -> None:
    """Issue, list, and revoke scoped dashboard tokens.

    \b
      bernstein auth dashboard-token issue --principal alice --scope viewer
      bernstein auth dashboard-token list
      bernstein auth dashboard-token revoke <token-id>
    """


@dashboard_token_group.command("issue")
@click.option("--principal", required=True, help="The person / seat the token attributes actions to.")
@click.option(
    "--scope",
    type=click.Choice(["viewer", "operator"]),
    default="viewer",
    show_default=True,
    help="viewer = read-only; operator = may trigger state-changing actions.",
)
@_WORKDIR_OPTION
def issue_cmd(principal: str, scope: str, workdir: str) -> None:
    """Issue a scoped dashboard token (printed once, never stored)."""
    from bernstein.core.security.audit_chain import record_dashboard_token_grant

    registry, chain = _registry_for(workdir)
    raw, record = registry.issue(principal=principal, scope=scope, now=int(time.time()))
    record_dashboard_token_grant(
        chain=chain,
        grant="issue",
        token_id=record.token_id,
        token_sha256=record.token_sha256,
        principal=record.principal,
        scope=record.scope,
    )
    console.print(f"Issued dashboard token for [bold]{principal}[/bold] (scope: {scope})")
    console.print(f"  Token: {raw}")
    console.print(f"  Id:    {record.token_id}")
    console.print(
        "The token is shown once and only its digest is journaled. "
        "Pass it as `Authorization: Bearer <token>` or in the dashboard login form."
    )


@dashboard_token_group.command("list")
@_WORKDIR_OPTION
def list_cmd(workdir: str) -> None:
    """List journal rows (grants and revocations); never prints tokens."""
    registry, _chain = _registry_for(workdir)
    records = registry.records()
    if not records:
        console.print("No dashboard tokens issued.")
        return
    console.print(f"{'ID':<18} {'KIND':<8} {'PRINCIPAL':<24} {'SCOPE':<10} ISSUED_AT")
    for r in records:
        console.print(f"{r.token_id:<18} {r.kind:<8} {r.principal:<24} {r.scope or '-':<10} {r.issued_at}")


@dashboard_token_group.command("revoke")
@click.argument("token_id", required=True)
@_WORKDIR_OPTION
def revoke_cmd(token_id: str, workdir: str) -> None:
    """Revoke a token by its id (see `list`); appends a signed revocation."""
    from bernstein.core.security.audit_chain import record_dashboard_token_grant

    registry, chain = _registry_for(workdir)
    live = {r.token_id: r for r in registry.records() if r.kind == "issue"}
    if not registry.revoke(token_id, now=int(time.time())):
        console.print(f"[yellow]No live token with id {token_id}[/yellow]")
        raise SystemExit(1)
    target = live[token_id]
    record_dashboard_token_grant(
        chain=chain,
        grant="revoke",
        token_id=target.token_id,
        token_sha256=target.token_sha256,
        principal=target.principal,
        scope="",
    )
    console.print(f"Revoked dashboard token {token_id} (principal: {target.principal})")
