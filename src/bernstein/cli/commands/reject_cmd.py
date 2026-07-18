"""``bernstein reject`` - refuse a pending approval gate.

Mirror of :mod:`bernstein.cli.commands.approve_cmd`: writes
``<workdir>/.sdd/runtime/approvals/<task_id>.rejected`` so the
post-completion review gate or the pre-spawn ``ApprovalSpec`` gate
(#1110) unblocks with a refusal. Idempotent under concurrent
invocations: the first writer wins via ``os.replace`` and subsequent
callers see the existing decision and report ``already resolved``.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.commands.approve_cmd import _atomic_write_text, _foreground_confirm
from bernstein.cli.helpers import console


@click.command("reject")
@click.argument("task_id")
@click.option(
    "--workdir",
    default=".",
    help="Project root directory (parent of .sdd/).",
    type=click.Path(),
)
@click.option(
    "--prompt/--no-prompt",
    default=True,
    show_default=True,
    help="Foreground TTY prompts confirm before writing the sentinel.",
)
def reject(task_id: str, workdir: str, prompt: bool) -> None:
    """Reject a pending task (review gate or pre-spawn approval gate).

    Writes a ``<task_id>.rejected`` decision file under
    ``.sdd/runtime/approvals/``. The orchestrator marks the task failed
    and skips the agent body (or, post-completion, discards the work
    without merging).

    Concurrent ``bernstein reject`` calls are idempotent: the first one
    creates the file, subsequent invocations exit with ``already
    resolved``.

    \b
    Example:
      bernstein reject T-abc123
    """
    from bernstein.core.orchestration.approval_gate import UnsafeApprovalIdError, approval_path

    # Same rule as the approve and read sides; validated before mkdir.
    try:
        decision_file = approval_path(Path(workdir), task_id, ".rejected")
        approved_file = approval_path(Path(workdir), task_id, ".approved")
    except UnsafeApprovalIdError as exc:
        console.print(f"[red]Refusing to reject:[/red] {exc}")
        raise SystemExit(1) from exc

    approvals_dir = decision_file.parent
    approvals_dir.mkdir(parents=True, exist_ok=True)

    if approved_file.exists():
        console.print(
            f"[yellow]Already resolved:[/yellow] task [bold]{task_id}[/bold] was approved; "
            "leaving the approval in place."
        )
        return

    if decision_file.exists():
        console.print(f"[dim]Already rejected:[/dim] task [bold]{task_id}[/bold] (no-op)")
        return

    if prompt and not _foreground_confirm(f"Reject task {task_id}?"):
        console.print(f"[dim]Skipped[/dim] rejection for [bold]{task_id}[/bold]")
        return

    created = _atomic_write_text(decision_file, "rejected")
    if created:
        console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold]: work will be discarded.")
    else:
        console.print(f"[dim]Already rejected:[/dim] task [bold]{task_id}[/bold] (no-op)")


__all__ = ["reject"]
