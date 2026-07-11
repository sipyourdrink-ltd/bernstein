"""Compaction CLI -- print and verify a task's compaction receipt chain.

Every context compaction (proactive or reactive) is receipted into the
HMAC-chained audit log as a ``compaction.receipt`` event and registered
as a step in the worker's replay journal (issue #2246). This command
group is the operator surface over those records:

  bernstein compaction log --task <id>            Print the receipt chain.
  bernstein compaction log --task <id> --json     Structured output.
  bernstein compaction log --task <id> --verify   Re-verify chain + journal.

``--verify`` exits non-zero when any journaled compaction step lacks a
chain-verifiable receipt or when the receipt hashes disagree with the
journal -- the same check the run's audit verification applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console, print_json

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tokens.compaction_receipt import CompactionReceipt


@click.group("compaction")
def compaction_group() -> None:
    """Compaction receipt tools (HMAC-chained, journal-anchored)."""


@compaction_group.command("log")
@click.option("--task", "task_id", required=True, help="Task id whose compaction receipts to print.")
@click.option(
    "--audit-dir",
    "audit_dir",
    type=str,
    default=".sdd/audit",
    show_default=True,
    help="Audit chain directory holding the receipt events.",
)
@click.option(
    "--sdd-dir",
    "sdd_dir",
    type=str,
    default=".sdd",
    show_default=True,
    help="Run state directory (replay journals live under it).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option(
    "--verify",
    "do_verify",
    is_flag=True,
    default=False,
    help="Re-verify the chain and cross-check replay-journal compaction steps; exit 1 on failure.",
)
def compaction_log_cmd(task_id: str, audit_dir: str, sdd_dir: str, as_json: bool, do_verify: bool) -> None:
    """Print the compaction receipt chain for a task."""
    from pathlib import Path

    audit_path = Path(audit_dir)
    if not audit_path.is_dir():
        console.print(
            "[yellow]No audit chain found.[/yellow]  Compaction receipts are recorded under "
            f"[bold]{audit_path}[/bold] once a run compacts a worker's context."
        )
        return

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tokens.compaction_receipt import load_receipts

    chain = AuditChainStore(audit_path)
    receipts = load_receipts(chain, task_id=task_id)

    verify_ok = True
    verify_errors: list[str] = []
    if do_verify:
        verify_ok, verify_errors = _verify_task(chain, task_id=task_id, sdd_dir=Path(sdd_dir))

    if as_json:
        print_json(
            {
                "task_id": task_id,
                "receipts": [receipt.to_details() for receipt in receipts],
                "verify": ({"ok": verify_ok, "errors": verify_errors} if do_verify else None),
            }
        )
    elif not receipts:
        console.print(f"[yellow]No compaction receipts for task [bold]{task_id}[/bold].[/yellow]")
    else:
        _print_receipt_table(task_id, receipts)

    if do_verify and not as_json:
        if verify_ok:
            console.print("[green]Receipt verification: OK[/green]")
        else:
            console.print("[red]Receipt verification: FAILED[/red]")
            for err in verify_errors:
                console.print(f"  [red]-[/red] {err}")
    if do_verify and not verify_ok:
        raise SystemExit(1)


def _verify_task(chain: AuditChainStore, *, task_id: str, sdd_dir: Path) -> tuple[bool, list[str]]:
    """Run receipt verification for *task_id* across every worker journal.

    Walks each per-agent replay journal under ``<sdd_dir>/runtime/journal``
    and cross-checks its compaction steps against the chain receipts via
    :func:`bernstein.core.tokens.compaction_receipt.verify_compaction_receipts`.
    Journals without compaction steps for this task are skipped.

    Args:
        chain: The audit chain store holding the receipt events.
        task_id: Task whose compactions are being verified.
        sdd_dir: Run state directory the journals live under.

    Returns:
        ``(ok, errors)`` aggregated over the chain and every journal.
    """
    from bernstein.core.persistence.journal import JournalReader, default_journal_root
    from bernstein.core.tokens.compaction_receipt import (
        find_compaction_steps,
        verify_compaction_receipts,
    )

    ok, errors = verify_compaction_receipts(chain, task_id=task_id)

    journal_root = default_journal_root(sdd_dir)
    for agent_dir in sorted(path for path in journal_root.iterdir() if path.is_dir()):
        reader = JournalReader(agent_dir)
        steps = [step for step in find_compaction_steps(reader) if str(step.tool_call.get("task_id", "")) == task_id]
        if not steps:
            continue
        journal_ok, journal_errors = verify_compaction_receipts(chain, journal_reader=reader, task_id=task_id)
        ok = ok and journal_ok
        errors.extend(err for err in journal_errors if err not in errors)
    return ok, errors


def _print_receipt_table(task_id: str, receipts: list[CompactionReceipt]) -> None:
    """Render the receipt chain as a Rich table."""
    from datetime import UTC, datetime

    from rich.table import Table

    table = Table(
        title=f"Compaction receipts for task {task_id}",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Trigger")
    table.add_column("Tokens", justify="right")
    table.add_column("Validators")
    table.add_column("Retries", justify="right")
    table.add_column("Gate")
    table.add_column("Pre/Post SHA-256", no_wrap=True)
    table.add_column("Correlation", no_wrap=True)

    for receipt in receipts:
        ts = datetime.fromtimestamp(receipt.ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        failed = [name for name, passed in receipt.validators if not passed]
        validators = "[green]all pass[/green]" if not failed else "[red]fail: " + ", ".join(failed) + "[/red]"
        table.add_row(
            ts,
            receipt.trigger,
            f"{receipt.tokens_before} -> {receipt.tokens_after}",
            validators,
            str(receipt.retry_count),
            receipt.gate_action,
            f"{receipt.pre_sha256[:12]}../{receipt.post_sha256[:12]}..",
            receipt.correlation_id,
        )
    console.print(table)


__all__ = ["compaction_group", "compaction_log_cmd"]
