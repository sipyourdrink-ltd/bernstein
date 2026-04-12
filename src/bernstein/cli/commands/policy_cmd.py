"""CLI commands for policy-as-code audits."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.core.policy_engine import PolicySubject, build_policy_diff, load_policy_engine


@click.group("policy")
def policy_group() -> None:
    """Inspect and run policy-as-code checks."""


@policy_group.command("check")
def check_policies() -> None:
    """Run YAML/Rego policies against the current repository diff."""

    workdir = Path.cwd()
    engine = load_policy_engine(workdir)
    if engine is None:
        click.echo("No policies configured.")
        return

    subject = PolicySubject(
        id="manual",
        title="Manual policy audit",
        description="Manual policy audit from bernstein policy check.",
        role="manual",
    )
    violations = engine.check(subject, build_policy_diff(workdir))
    if not violations:
        click.echo("Policy check passed.")
        return

    for violation in violations:
        prefix = "BLOCK" if violation.blocked else "WARN"
        click.echo(f"[{prefix}] {violation.policy_name}: {violation.detail}")

    if any(violation.blocked for violation in violations):
        raise click.ClickException("Policy violations blocked the audit.")
