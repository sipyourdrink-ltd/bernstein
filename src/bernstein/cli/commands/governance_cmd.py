"""``bernstein governance``: verify RBAC + budget decisions as chain projections.

Issue #2309. Recomputes every access and budget decision recorded for a run from
the signed lineage spine and confirms the recorded verdicts:

    bernstein governance verify <run> --bindings <file> [--ledger <file>]

Access decisions re-resolve the subject's role from the presented signed role
bindings and re-project the role's permissions onto the action. Budget decisions
recompute per-subject spend from the cost ledger (never a stored counter) and
re-derive the verdict. A tampered verdict, a widened permission binding, or a
diverged ledger fails the check.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("governance")
def governance_group() -> None:
    """Verify RBAC and budget decisions as projections over the audit chain.

    \b
      bernstein governance verify <run> --bindings b.json --ledger ledger.jsonl
    """


@governance_group.command("verify")
@click.argument("run_id", required=True)
@click.option(
    "--bindings",
    "bindings_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Signed role-bindings JSON the access decisions project over.",
)
@click.option(
    "--ledger",
    "ledger_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Spend ledger JSONL for recomputing budget decisions (required when the run has budget rows).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def governance_verify_cmd(run_id: str, bindings_file: str, ledger_file: str | None, workdir: str) -> None:
    """Recompute every access and budget verdict for *run_id* and match them.

    Exit codes: 0 = verified, 1 = no records / bad input, 2 = mismatch.
    """
    from bernstein.core.security.governance import RoleBindings, verify_governance

    root = Path(workdir).resolve()
    bindings = RoleBindings.from_dict(json.loads(Path(bindings_file).read_text(encoding="utf-8")))
    ledger_path = Path(ledger_file).resolve() if ledger_file else None

    result = verify_governance(
        run_id=run_id,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        bindings=bindings,
        ledger_path=ledger_path,
    )

    console.print()
    console.print(f"[bold]Governance verify[/bold] run={run_id}")
    console.print(f"  decisions checked  {result.checked}")
    if result.ok:
        console.print("[green]OK[/green] -- every access and budget verdict recomputes from the chain.")
        raise SystemExit(0)
    if result.checked == 0:
        console.print(f"[yellow]NO RECORDS[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
