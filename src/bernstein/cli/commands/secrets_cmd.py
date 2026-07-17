"""``bernstein secrets ...`` CLI commands.

Operator surface for the short-lived-token broker. The CLI is intentionally
small: ``list`` enumerates backend-visible secret names, and ``mint`` issues
a one-shot token for an out-of-band agent invocation. Routine in-process
minting happens via the orchestrator's broker instance, not this CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import yaml

from bernstein.core.security.redactor import mask
from bernstein.core.security.secrets_broker import (
    SecretsBrokerError,
    build_broker_from_config,
)

__all__ = ["secrets_group"]


def _load_secrets_block(config_path: Path) -> dict[str, Any]:
    """Load and validate the ``security.secrets`` block from a YAML file."""
    if not config_path.exists():
        raise click.ClickException(f"config not found: {config_path}")
    try:
        raw: object = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise click.ClickException(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise click.ClickException(f"top-level YAML in {config_path} must be a mapping")
    security: object = raw.get("security") or {}
    if not isinstance(security, dict):
        raise click.ClickException("security block must be a mapping")
    secrets_block: object = security.get("secrets")
    if not isinstance(secrets_block, dict):
        raise click.ClickException("security.secrets block is missing or not a mapping")
    return {str(k): v for k, v in secrets_block.items()}


@click.group(name="secrets")
def secrets_group() -> None:
    """Short-lived-token broker commands."""


@secrets_group.command(name="list")
@click.option(
    "--config",
    "config_path",
    default="bernstein.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def secrets_list(config_path: Path) -> None:
    """List secret names the configured backend can enumerate."""
    block = _load_secrets_block(config_path)
    try:
        broker = build_broker_from_config(block)
    except SecretsBrokerError as exc:
        raise click.ClickException(str(exc)) from exc
    names = broker.list_backend_secrets()
    if not names:
        click.echo("(backend does not enumerate secret names)")
        return
    for name in names:
        click.echo(name)


@secrets_group.command(name="mint")
@click.option(
    "--config",
    "config_path",
    default="bernstein.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
@click.option("--task", "task_id", required=True, help="Bernstein task id that owns the token.")
@click.option("--secret", "secret_name", required=True, help="Backing secret name in the configured backend.")
@click.option(
    "--ttl",
    "ttl_seconds",
    type=int,
    default=None,
    help="TTL in seconds. Defaults to mint.ttl_seconds_default.",
)
@click.option(
    "--reveal",
    is_flag=True,
    default=False,
    help="Print the raw token value. Off by default: only metadata is printed.",
)
def secrets_mint(
    config_path: Path,
    task_id: str,
    secret_name: str,
    ttl_seconds: int | None,
    reveal: bool,
) -> None:
    """Mint a short-lived token for a backing secret."""
    block = _load_secrets_block(config_path)
    try:
        broker = build_broker_from_config(block)
        token = broker.mint(secret_name=secret_name, task_id=task_id, ttl_seconds=ttl_seconds)
    except SecretsBrokerError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "token_id": token.token_id,
        "secret_name": token.secret_name,
        "task_id": token.task_id,
        "ttl_seconds": token.ttl_seconds,
        "expires_at": token.expires_at,
        "value": token.value if reveal else mask(token.value, keep=4),
    }
    click.echo(json.dumps(payload, sort_keys=True))


# ---------------------------------------------------------------------------
# ``bernstein secrets grants ...`` - chain-anchored per-task credential grants
# ---------------------------------------------------------------------------


@secrets_group.group(name="grants")
def grants_group() -> None:
    """Reconstruct and verify chain-anchored credential grants (issue #2516).

    \b
    A scoped grant (task id, secret name, audience, expiry, capability ceiling)
    is an Ed25519-signed record anchored in the HMAC audit chain. The broker
    refuses to mint a downstream token without a grant that verifies, and a
    run's full issue / exchange / revoke history reconstructs offline from the
    chain alone.

    \b
    Examples:
      bernstein secrets grants list run-42
      bernstein secrets grants verify run-42 --json
    """


def _grant_root(root: Path | None) -> Path:
    from bernstein.core.identity import grants as _grants

    return root if root is not None else _grants.DEFAULT_ROOT


@grants_group.command(name="verify")
@click.argument("run")
@click.option(
    "--root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Grant-record root (default: .sdd/audit).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the byte-identical JSON report.")
def grants_verify(run: str, root: Path | None, as_json: bool) -> None:
    """Reconstruct and verify the grant chain for RUN offline.

    Exits 0 when the chain is intact (at least one record, every record
    verifies from genesis to tail with a valid HMAC and Ed25519 signature); 1
    otherwise, naming the first failing record.
    """
    from bernstein.core.identity import grants as _grants

    result = _grants.verify_grant_chain(root=_grant_root(root), run_id=run, key=_grants._audit_key())

    if as_json:
        click.echo(_grants.render_report(result, run_id=run))
        raise SystemExit(0 if result.valid else 1)

    if not result.records and not result.errors:
        click.echo(f"no grant records for run {run}", err=True)
        raise SystemExit(1)
    for r in result.records:
        click.echo(
            f"  record {r.record_index}: {r.kind}  grant={r.grant_id[:12]}  task={r.task_id}  secret={r.secret_name}"
        )
    if result.valid:
        click.echo(f"grant chain intact ({len(result.records)} record(s))")
        raise SystemExit(0)
    for err in result.errors:
        click.echo(f"  {err}", err=True)
    click.echo("grant chain verification failed", err=True)
    raise SystemExit(1)


@grants_group.command(name="list")
@click.argument("run")
@click.option(
    "--root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Grant-record root (default: .sdd/audit).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def grants_list(run: str, root: Path | None, as_json: bool) -> None:
    """List the grants recorded for RUN with their reconstructed lifecycle."""
    from bernstein.core.identity import grants as _grants

    result = _grants.verify_grant_chain(root=_grant_root(root), run_id=run, key=_grants._audit_key())
    life = result.lifecycles()

    if as_json:
        payload = {
            "run": run,
            "valid": result.valid,
            "grants": [
                {
                    "grant_id": gid,
                    "task_id": state["task_id"],
                    "secret_name": state["secret_name"],
                    "audience": state["audience"],
                    "expiry": state["expiry"],
                    "revoked": state["revoked"],
                    "token_ids": list(state["token_ids"]),
                }
                for gid, state in sorted(life.items())
            ],
        }
        click.echo(json.dumps(payload, sort_keys=True))
        raise SystemExit(0 if result.valid else 1)

    if not life:
        click.echo(f"no grants for run {run}", err=True)
        raise SystemExit(1)
    for gid, state in sorted(life.items()):
        status = "revoked" if state["revoked"] else "active"
        click.echo(
            f"  {gid[:12]}  task={state['task_id']}  secret={state['secret_name']}  "
            f"audience={state['audience']}  {status}  tokens={len(state['token_ids'])}"
        )
    if not result.valid:
        raise SystemExit(1)
