"""CLI command group for impact analysis (issue #3139).

Consolidates API compatibility checking, dependency call-site analysis,
and blast-radius scoring under ``bernstein impact``:

- ``bernstein impact api`` -- also reachable as top-level ``api-check``,
  which an earlier change registered after #3139 measured the module as
  documented-but-orphaned.  The top-level spelling is left in place and is
  not deprecated here; this change gives the command a home in the group.
- ``bernstein impact deps`` (was ``dep-impact``, now a deprecated alias)
- ``bernstein impact blast`` (was ``blast-radius``, now a deprecated alias)
"""

from __future__ import annotations

import click

from bernstein.cli.commands.api_check_cmd import api_check_cmd
from bernstein.cli.commands.blast_radius_cmd import blast_radius_group
from bernstein.cli.commands.dep_impact_cmd import dep_impact_cmd


@click.group("impact")
def impact_group() -> None:
    """Analyse impact of code changes (API compatibility, caller sites, blast radius)."""


# Subcommands under `bernstein impact`
impact_group.add_command(api_check_cmd, "api")
impact_group.add_command(dep_impact_cmd, "deps")
impact_group.add_command(blast_radius_group, "blast")


# ---------------------------------------------------------------------------
# Deprecated top-level aliases for 3.x back-compat
# ---------------------------------------------------------------------------


@click.command("dep-impact", help="[Deprecated] Analyse call-site impacts (use 'bernstein impact deps').")
@click.option("--base", default="HEAD~1", show_default=True, metavar="REF", help="Git ref to compare against.")
@click.option(
    "--workdir", default=None, type=click.Path(exists=True, file_okay=False, resolve_path=True), help="Repository root."
)
@click.option("--strict", is_flag=True, default=False, help="Exit 1 when any call-site impact is found.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output results as JSON.")
@click.pass_context
def dep_impact_alias_cmd(ctx: click.Context, base: str, workdir: str | None, strict: bool, output_json: bool) -> None:
    """[Deprecated] Use 'bernstein impact deps' instead."""
    click.echo(
        "WARNING: 'bernstein dep-impact' is deprecated and will be removed in v4.0.0 (#3139): "
        "use 'bernstein impact deps' instead.",
        err=True,
    )
    ctx.invoke(dep_impact_cmd, base=base, workdir=workdir, strict=strict, output_json=output_json)


@click.group("blast-radius", help="[Deprecated] Inspect blast-radius scores (use 'bernstein impact blast').")
@click.pass_context
def blast_radius_alias_group(ctx: click.Context) -> None:
    """[Deprecated] Use 'bernstein impact blast' instead."""
    if ctx.invoked_subcommand is not None:
        click.echo(
            "WARNING: 'bernstein blast-radius' is deprecated and will be removed in v4.0.0 (#3139): "
            "use 'bernstein impact blast' instead.",
            err=True,
        )


for _cmd_name, _cmd_obj in blast_radius_group.commands.items():
    blast_radius_alias_group.add_command(_cmd_obj, _cmd_name)
