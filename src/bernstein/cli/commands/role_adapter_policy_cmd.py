"""CLI commands for the per-role adapter deny-list policy.

Wires :mod:`bernstein.core.security.role_adapter_policy` into the user-facing
``bernstein security role-adapter-policy show|set|test`` verbs.

The policy is persisted to ``.sdd/security/role_adapter_policy.json`` (per
the module's :data:`DEFAULT_POLICY_PATH`) and reloaded on each invocation -
the CLI does not assume the orchestrator is running.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.core.security.role_adapter_policy import (
    DEFAULT_POLICY_PATH,
    RolePolicy,
    load_policy_file,
    save_policy_file,
)


@click.group("security")
def security_group() -> None:
    """Security policy inspection and editing commands."""


@security_group.group("role-adapter-policy")
def role_adapter_policy_group() -> None:
    """Inspect and edit the per-role adapter allow-list."""


@role_adapter_policy_group.command("show")
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to the JSON policy file.",
)
@click.option(
    "--json-output",
    "as_json",
    is_flag=True,
    help="Emit raw JSON instead of a readable table.",
)
def show_policy(policy_file: Path, as_json: bool) -> None:
    """Print the current role → allow-list mapping.

    An empty allow-list for a role means **all adapters allowed** (the
    back-compat default). The optional ``*`` entry is the default for unlisted
    roles; an empty default means no adapters allowed.
    """
    payload = load_policy_file(policy_file).to_dict()
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not payload:
        click.echo(f"role-adapter-policy: empty (no restrictions). file={policy_file}")
        return

    click.echo(f"role-adapter-policy from {policy_file}:")
    for role, allowed in payload.items():
        if allowed:
            click.echo(f"  {role}: {', '.join(allowed)}")
        elif role == "*":
            click.echo("  *: (empty default allow-list - no adapters allowed)")
        else:
            click.echo(f"  {role}: (empty allow-list - all adapters allowed)")


@role_adapter_policy_group.command("set")
@click.option(
    "--role",
    required=True,
    help="Role name (e.g. backend, security, docs), or '*' for unlisted roles.",
)
@click.option(
    "--allow",
    "allow",
    multiple=True,
    help=(
        "Adapter id allowed for this role. Pass --allow multiple times for a "
        "list. Pass with no values to clear the allow-list (back-compat: "
        "everything allowed for a named role; no adapters allowed for '*')."
    ),
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to the JSON policy file. Created if absent.",
)
def set_policy(role: str, allow: tuple[str, ...], policy_file: Path) -> None:
    """Set the allow-list for *role*.

    Examples:

    \b
        bernstein security role-adapter-policy set --role security \\
            --allow claude --allow aider
        bernstein security role-adapter-policy set --role security
        # ^ clears the allow-list for the role (back to unrestricted)
        bernstein security role-adapter-policy set --role '*'
        # ^ denies every adapter for roles with no named entry
    """
    policy = load_policy_file(policy_file)
    new_map = dict(policy.per_role_allowlists)
    new_map[role] = tuple(sorted({a.strip() for a in allow if a.strip()}))
    new_policy = RolePolicy(per_role_allowlists=new_map)
    written = save_policy_file(new_policy, policy_file)
    if new_map[role]:
        click.echo(f"role={role!r} allow-list set to {sorted(new_map[role])}; saved to {written}")
    elif role == "*":
        click.echo(f"default allow-list set: no adapters allowed for unlisted roles; saved to {written}")
    else:
        click.echo(f"role={role!r} allow-list cleared (back-compat: all adapters allowed); saved to {written}")


@role_adapter_policy_group.command("test")
@click.option(
    "--role",
    required=True,
    help="Role to test.",
)
@click.option(
    "--adapter",
    required=True,
    help="Adapter id to test (e.g. claude, aider, mock).",
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_POLICY_PATH,
    show_default=True,
    help="Path to the JSON policy file.",
)
def test_policy(role: str, adapter: str, policy_file: Path) -> None:
    """Print whether *role* may spawn *adapter* under the current policy.

    Exit code is 0 when allowed, 1 when denied - useful for shell guards.
    """
    policy = load_policy_file(policy_file)
    if policy.is_allowed(role, adapter):
        click.echo(f"ALLOW: role={role!r} adapter={adapter!r}")
        return
    allowed = policy.allowed_for(role)
    click.echo(f"DENY:  role={role!r} adapter={adapter!r}; allowed={sorted(allowed)}")
    raise click.exceptions.Exit(code=1)
