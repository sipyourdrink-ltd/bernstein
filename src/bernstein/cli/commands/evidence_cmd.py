"""``bernstein evidence``: verification evidence bundles (issue #2362).

A completed task's proof-of-done artefacts (test-runner output, coverage, lint,
optional screenshot / recording) are content-addressed and bound into a signed
bundle anchored in the evidence lineage spine and sealed by the HMAC audit
chain. The bundle *is* the receipt: strip the spine and the signature and it is
just a file; anchored and signed it recomputes offline from the stored evidence
alone.

    bernstein evidence show <task>     Render a sealed bundle.
    bernstein evidence verify <task>   Recompute + re-verify it offline.

``bernstein audit verify`` runs the same integrity check across every bundle, so
a tampered evidence file is detected exactly like a tampered chain entry.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@click.group("evidence")
def evidence_group() -> None:
    """Render and verify verification evidence bundles.

    \b
      bernstein evidence show <task>
      bernstein evidence verify <task>
    """


@evidence_group.command("show")
@click.argument("task")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def evidence_show_cmd(task: str, workdir: str) -> None:
    """Render the sealed evidence bundle for TASK.

    Exit code 0 when a bundle exists, 1 when there is none.
    """
    from rich.table import Table

    from bernstein.core.evidence.bundle import read_evidence_bundle

    root = Path(workdir).resolve()
    bundle = read_evidence_bundle(root, task)
    if bundle is None:
        console.print(f"[yellow]No evidence bundle found for task[/yellow] {task}")
        raise SystemExit(1)

    anchor = bundle.journal_entry_hash or ""
    anchor_short = anchor.split(":", 1)[-1][:16] if anchor else "unanchored"
    verdict = "[green]pass[/green]" if bundle.gate_passed else "[red]fail[/red]"

    console.print()
    console.print(f"[bold]Evidence bundle[/bold] task={bundle.task_id}")
    console.print(f"  gate            {verdict}")
    console.print(f"  bundle_hash     {bundle.bundle_hash()}")
    console.print(f"  anchor          {anchor_short}")
    console.print(f"  items           {len(bundle.items)} ({bundle.passed_count} pass / {bundle.failed_count} fail)")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Producer", style="bold")
    table.add_column("Kind")
    table.add_column("Gate")
    table.add_column("Status")
    table.add_column("Exit", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Content hash", style="dim")
    for item in bundle.items:
        status = "[green]pass[/green]" if item.status == "pass" else "[red]fail[/red]"
        gate = "required" if item.required else "advisory"
        size = f"{item.size}{'+' if item.truncated else ''}"
        cred = " +cred" if item.content_credential_hash else ""
        table.add_row(
            item.name,
            item.kind + cred,
            gate,
            status,
            str(item.exit_code),
            size,
            item.content_hash.split(":", 1)[-1][:16] + "…",
        )
    console.print(table)
    console.print(f"\n[dim]Verify offline:[/dim] bernstein evidence verify {bundle.task_id}\n")


@evidence_group.command("verify")
@click.argument("task")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def evidence_verify_cmd(task: str, workdir: str) -> None:
    """Recompute and re-verify TASK's evidence bundle offline.

    Checks the Ed25519 signature, the evidence spine anchor, and the content
    hash of every stored blob. Exit codes: 0 = verified, 1 = no bundle,
    2 = mismatch (a tampered evidence file, bundle, or spine).
    """
    from bernstein.core.evidence.bundle import verify_evidence_bundle

    root = Path(workdir).resolve()
    result = verify_evidence_bundle(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=_load_hmac_key(),
        task_id=task,
    )
    console.print()
    console.print(f"[bold]Evidence verify[/bold] task={task}")
    if result.ok:
        assert result.bundle is not None
        verdict = "pass" if result.bundle.gate_passed else "fail"
        console.print(f"  gate {verdict}")
        console.print("[green]OK[/green] -- bundle and every evidence file recompute from the seal.")
        raise SystemExit(0)
    if result.bundle is None:
        console.print(f"[yellow]NO BUNDLE[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
