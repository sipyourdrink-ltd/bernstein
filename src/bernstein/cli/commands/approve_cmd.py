"""``bernstein approve`` - resolve the human-in-the-loop approval gate.

Originally lived in :mod:`bernstein.cli.commands.task_cmd`; pulled into a
dedicated module for #1110 so the pre-spawn ``ApprovalSpec`` gate has a
clear ownership boundary distinct from the post-completion review queue
and the tool-call queue (``bernstein approve-tool``).

The command writes ``<workdir>/.sdd/runtime/approvals/<task_id>.approved``
which the orchestrator's pre-spawn gate (or the legacy review gate)
detects via filesystem polling. The file is a signed decision record
(:mod:`bernstein.core.security.approval_decision`) naming the task, the
outcome, the ``cli`` decision path, the local-shell principal, and the
nonce of the open approval request; the gates honour nothing else.
Atomic writes (``os.replace``) make it idempotent: a second concurrent
``bernstein approve <task_id>`` call finds the decision already recorded
for the open request and exits with an "already resolved" message.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import click

from bernstein.cli.helpers import console


def _foreground_confirm(prompt: str) -> bool:
    """Ask ``prompt y/n?`` on the controlling TTY and return ``True`` for yes.

    Returns ``True`` only on an interactive ``stdin`` where the operator
    presses ``y`` (case-insensitive). Background runs (where ``stdin``
    is not a TTY) skip the prompt and return ``True`` immediately so
    operator scripts and CI pipelines are not blocked on the read.
    """
    if not sys.stdin.isatty():
        return True
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


@click.command("approve")
@click.argument("task_id", required=False)
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
@click.option(
    "--tool",
    "tool_id",
    default=None,
    help="Resolve a pending tool-call approval by id instead of a task (flag form of ``approve-tool``).",
)
def approve(task_id: str | None, workdir: str, prompt: bool, tool_id: str | None) -> None:
    """Approve a pending task (review gate or pre-spawn approval gate).

    Writes a ``<task_id>.approved`` decision file under
    ``.sdd/runtime/approvals/`` so a paused orchestrator (post-completion
    review or pre-spawn ``ApprovalSpec`` gate, #1110) can move on.

    Concurrent ``bernstein approve`` calls are idempotent: the first one
    creates the file, subsequent invocations report ``already resolved``
    and exit without rewriting state.

    Pass ``--tool <id>`` instead to resolve a pending tool-call approval
    from the interactive approval queue; ``approve-tool`` remains as an
    alias for this flag form.

    \b
    Examples:
      bernstein approve T-abc123
      bernstein approve --tool ap-1a2b3c4d5e6f
    """
    if tool_id is not None:
        from bernstein.cli.commands.approval_cmd import approve_tool

        approve_tool(latest=False, approval_id=tool_id, always=False, workdir=workdir)
        return

    if task_id is None:
        raise click.UsageError(
            "Missing argument 'TASK_ID'; pass a task id, or --tool <id> to resolve a pending tool-call approval."
        )

    _resolve_task_decision(task_id, Path(workdir), prompt=prompt, outcome="approved")


def _resolve_task_decision(
    task_id: str,
    workdir: Path,
    *,
    prompt: bool,
    outcome: Literal["approved", "rejected"],
) -> None:
    """Record an operator decision for *task_id* (shared by approve/reject).

    The decision is written as a signed decision record bound to the task's
    open approval request. A decision slot that does not hold an authentic
    record for that request does not count as "already resolved": the gate
    would reject it, so the operator's decision is written over it.
    """
    from bernstein.core.approval.models import ApprovalPrincipalRequired, local_shell_principal
    from bernstein.core.orchestration.approval_gate import (
        UnsafeApprovalIdError,
        approval_path,
        record_decision,
        request_pending_paths,
    )
    from bernstein.core.security.approval_decision import check_decision_file, read_request_nonce

    verb = "approve" if outcome == "approved" else "reject"
    opposite: Literal["approved", "rejected"] = "rejected" if outcome == "approved" else "approved"

    # The decision file name is derived from task_id, so the id goes through the
    # same rule the read side uses. Validated before mkdir: an unchecked id here
    # created directories and wrote a decision file anywhere on disk.
    try:
        decision_file = approval_path(workdir, task_id, f".{outcome}")
        opposite_file = approval_path(workdir, task_id, f".{opposite}")
        nonce = read_request_nonce(request_pending_paths(workdir, task_id))
    except UnsafeApprovalIdError as exc:
        console.print(f"[red]Refusing to {verb}:[/red] {exc}")
        raise SystemExit(1) from exc

    decision_file.parent.mkdir(parents=True, exist_ok=True)

    def _recorded(path: Path, slot: Literal["approved", "rejected"]) -> bool:
        if not path.exists():
            return False
        return check_decision_file(path, slot=slot, task_id=task_id, expected_nonce=nonce).verified

    if _recorded(opposite_file, opposite):
        console.print(
            f"[yellow]Already resolved:[/yellow] task [bold]{task_id}[/bold] was {opposite}; "
            f"leaving the {'rejection' if opposite == 'rejected' else 'approval'} in place."
        )
        return

    if _recorded(decision_file, outcome):
        console.print(f"[dim]Already {outcome}:[/dim] task [bold]{task_id}[/bold] (no-op)")
        return

    if prompt and not _foreground_confirm(f"{verb.capitalize()} task {task_id}?"):
        console.print(
            f"[dim]Skipped[/dim] {'approval' if outcome == 'approved' else 'rejection'} for [bold]{task_id}[/bold]"
        )
        return

    try:
        principal = local_shell_principal()
    except ApprovalPrincipalRequired as exc:
        console.print(f"[red]Refusing to {verb}:[/red] {exc}")
        raise SystemExit(1) from exc

    try:
        _path, created, bound_nonce = record_decision(
            workdir,
            task_id,
            outcome,
            source="cli",
            principal=principal.to_dict(),
        )
    except UnsafeApprovalIdError as exc:
        console.print(f"[red]Refusing to {verb}:[/red] {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:
        # Without the decision key the record cannot be signed, and an
        # unsigned file would be rejected by the gate anyway.
        console.print(f"[red]Could not sign the decision record:[/red] {exc}")
        raise SystemExit(1) from exc

    if not bound_nonce:
        console.print(
            f"[yellow]Note:[/yellow] task [bold]{task_id}[/bold] has no open approval request; "
            "a gate that opens later will not accept this decision."
        )
    if outcome == "approved":
        if created:
            console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold]: Bernstein will continue.")
        else:
            console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold] (decision record replaced)")
    elif created:
        console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold]: work will be discarded.")
    else:
        console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold] (decision record replaced)")


__all__ = ["approve"]
