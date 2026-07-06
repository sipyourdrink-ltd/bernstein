"""``bernstein delegation`` - verify per-hop delegation receipts for a run.

Each run authorizes a ``principal -> orchestrator -> sub-agent`` chain, one
HMAC-chained receipt per hop (see
:mod:`bernstein.core.identity.delegation`). ``delegation verify <run>``
reconstructs that chain offline and confirms it is intact - which principal
authorized which sub-agent action - exiting non-zero on any tamper, deleted
hop, or missing chain (issue #2305 AC4).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.identity import delegation


@click.group(name="delegation")
def delegation_group() -> None:
    """Delegation-receipt tooling for the principal->orchestrator->sub-agent chain.

    \b
    Examples:
      bernstein delegation verify run-42
      bernstein delegation verify run-42 --json
    """


@delegation_group.command("verify")
@click.argument("run")
@click.option(
    "--root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Delegation-receipt root (default: .sdd/audit).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def verify_cmd(run: str, root: Path | None, as_json: bool) -> None:
    """Reconstruct and verify the delegation chain for RUN.

    Exits 0 when the chain is intact (at least one hop, every hop verifies
    from genesis to tail); 1 otherwise.
    """
    result = delegation.verify_run(run, root=root)

    if as_json:
        payload = {
            "run": run,
            "valid": result.valid,
            "hops": result.hops,
            "errors": result.errors,
            "receipts": [
                {
                    "hop_index": r.hop_index,
                    "issuer": r.issuer,
                    "subject": r.subject,
                    "audience": r.audience,
                    "act": r.act,
                }
                for r in result.receipts
            ],
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise SystemExit(0 if result.valid else 1)

    if result.hops == 0:
        console.print(f"[red]No delegation receipts for run[/red] {run}")
        raise SystemExit(1)

    for r in result.receipts:
        console.print(f"  hop {r.hop_index}: {r.issuer} -> {r.audience}  [dim]({r.act})[/dim]")
    if result.valid:
        console.print(f"[green]delegation chain intact[/green] ({result.hops} hop(s))")
        raise SystemExit(0)
    for err in result.errors:
        console.print(f"[red]  {err}[/red]")
    console.print("[red]delegation chain verification failed[/red]")
    raise SystemExit(1)
