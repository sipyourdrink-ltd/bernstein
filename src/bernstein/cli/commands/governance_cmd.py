"""``bernstein governance``: verify RBAC + budget decisions as chain projections.

Issue #2309. Recomputes every access and budget decision recorded for a run from
the signed lineage spine and confirms the recorded verdicts:

    bernstein governance verify <run> --bindings <file> [--ledger <file>]

Access decisions re-resolve the subject's role from the presented signed role
bindings and re-project the role's permissions onto the action. Budget decisions
recompute per-subject spend from the cost ledger (never a stored counter) and
re-derive the verdict. A tampered verdict, a widened permission binding, or a
diverged ledger fails the check.

    bernstein govern plan --playbook <file> --inventory <file> [--workdir <path>]

Generate a signed, lineage-bearing govern plan representing the diff between
declared posture (playbook) and enumerated environment (inventory). The plan
contains one entry per mismatch (FORBIDDEN, ABSENT, WIDER_CEILING, UNKNOWN)
and is anchored in the lineage spine for offline verification.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.govern import compute_plan as _compute_plan


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
      bernstein govern plan --playbook p.json --inventory i.json [--workdir w]
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


@governance_group.command("plan")
@click.option(
    "--playbook",
    "playbook_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON file describing the declared posture (see compute_plan schema).",
)
@click.option(
    "--inventory",
    "inventory_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON file describing the enumerated environment (see compute_plan schema).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def governance_plan_cmd(playbook_file: str, inventory_file: str, workdir: str) -> None:
    """Generate a signed, lineage-bearing govern plan.

    Exit 0 always (a signed empty plan is valid).
    """
    root = Path(workdir).resolve()

    playbook = json.loads(Path(playbook_file).read_text(encoding="utf-8"))
    inventory = json.loads(Path(inventory_file).read_text(encoding="utf-8"))

    timestamp = int(time.time())

    plan = _compute_plan(
        playbook=playbook,
        inventory=inventory,
        run_id="govern-plan",
        timestamp=timestamp,
    )

    # Anchoring in the lineage spine
    from bernstein.core.lineage.spine import LineageSpine

    hmac_key = _load_hmac_key()
    lineage_root = _lineage_root(root)
    spine = LineageSpine(lineage_root, run_id="govern-plan", hmac_key=hmac_key)

    # Write the plan to the lineage spine
    # We use the plan's canonical bytes as the content to anchor
    artifact_path = "governance-plan.json"
    anchor_hash = spine.record(
        artifact_path=artifact_path,
        content=plan.to_canonical_bytes(),
        actor="bernstein.govern",
        step_id=plan.inputs_hash,
        model="none",
        timestamp=timestamp,
    )

    # Also persist the plan JSON to a file in the governance decisions dir
    decisions_dir = lineage_root / "govern-plan"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    plan_path = decisions_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Print a Rich table summarizing the plan
    console_obj = Console()
    console_obj.print()
    console_obj.print("[bold]Governance plan[/bold] run=govern-plan")
    console_obj.print(f"  Plan entries: {len(plan.entries)}")
    console_obj.print(f"  Inputs hash: {plan.inputs_hash}")
    console_obj.print(f"  Timestamp: {timestamp}")
    console_obj.print(f"  Journal anchor: {anchor_hash}")

    table = Table(title="Plan Entries", show_header=True, header_style="bold magenta")
    table.add_column("Kind")
    table.add_column("Surface")
    table.add_column("Playbook Clause")
    table.add_column("Observed Value")
    table.add_column("Declared Value")
    table.add_column("Evidence Ref")

    for entry in plan.entries:
        table.add_row(
            entry.kind.value,
            entry.surface,
            entry.playbook_clause,
            str(entry.observed_value) if entry.observed_value is not None else "",
            str(entry.declared_value) if entry.declared_value is not None else "",
            entry.evidence_ref,
        )

    console_obj.print(table)
    raise SystemExit(0)
