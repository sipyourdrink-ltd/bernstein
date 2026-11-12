"""``bernstein adapters contract-check`` - capability check for adapter CLIs.

Loads ``tests/contract/contracts/<name>.yaml``, runs the upstream CLI's
``--help`` in a sandboxed subprocess, and asserts every entry in
``required_flags`` and ``required_subcommands`` appears in that output.
When the contract names a secret in ``auth.secret_env`` and the secret
is set, the configured model-list command is also run and
``expected_models.required_present`` is verified.

Exit codes:

* ``0`` - contract holds. (Or binary not installed locally -
  informational for developer machines; the CI workflow installs the
  CLI first.)
* ``2`` - capability failure (missing flag/subcommand or model). Drift
  is a hard fail per the refined design in #1291.
* ``3`` - upstream CLI runtime failure (``--help`` exited non-zero with
  empty output or no required tokens). The contract has not been
  evaluated; an operator should investigate the CLI install. The
  workflow treats this as a checker error rather than drift.

Refs: #1291.
"""

from __future__ import annotations

import json
import sys

import click

from bernstein.adapters._contract import ContractSpec, check_contract, list_contracts


@click.command("contract-check")
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    default=False,
    help="List adapters that have a contract on disk and exit.",
)
def contract_check_cmd(name: str | None, as_json: bool, list_only: bool) -> None:
    """Run the adapter contract check for NAME."""
    if list_only:
        names = list_contracts()
        if as_json:
            click.echo(json.dumps({"count": len(names), "contracts": names}, indent=2))
        else:
            for entry in names:
                click.echo(entry)
        return

    if not name:
        raise click.UsageError("Provide an adapter NAME or use --list.")

    try:
        spec = ContractSpec.load(name)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    result = check_contract(spec)
    payload = result.to_dict()

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(f"adapter:   {result.adapter}")
        click.echo(f"binary:    {result.binary}")
        click.echo(f"installed: {result.binary_installed}")
        if result.skipped_reason:
            click.echo(f"note:      {result.skipped_reason}")
        if result.capability_failures:
            click.echo("capability failures:")
            for failure in result.capability_failures:
                click.echo(f"  - {failure}")
        if result.model_failures:
            click.echo("model failures:")
            for failure in result.model_failures:
                click.echo(f"  - {failure}")
        if result.runtime_failure:
            click.echo(f"runtime failure: {result.runtime_failure}")
        click.echo(f"passed:    {result.passed}")

    # Exit-code policy:
    #   2 -> capability or model drift (hard fail by design).
    #   3 -> upstream CLI runtime failure (--help broken). Surface as a
    #        distinct "checker degraded" code so the workflow does not
    #        misattribute it to contract drift.
    # A missing binary is informational for developer machines - the CI
    # workflow installs the CLI before invocation, so it never reaches
    # the capability-failure branches.
    if result.capability_failures or result.model_failures:
        sys.exit(2)
    if result.runtime_failure:
        sys.exit(3)
    sys.exit(0)
