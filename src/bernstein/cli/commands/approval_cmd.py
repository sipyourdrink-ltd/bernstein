"""CLI resolvers for the interactive tool-call approval queue (op-002).

``bernstein approve`` and ``bernstein reject`` pop the oldest (or a
specified) pending approval from ``.sdd/runtime/approvals/`` and record
the operator's decision. The commands work headlessly so CI runners and
pairing operators can unblock agents over SSH without a TUI.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from bernstein.core.approval.models import (
    ApprovalDecision,
    PendingApproval,
    local_shell_principal,
)
from bernstein.core.approval.queue import ApprovalQueue, promote_to_always_allow

console = Console()

_RUNTIME_REL = Path(".sdd") / "runtime" / "approvals"


def _queue_for(workdir: str) -> ApprovalQueue:
    """Return an :class:`ApprovalQueue` rooted at *workdir*."""
    return ApprovalQueue(base_dir=Path(workdir) / _RUNTIME_REL)


def _select_approval(
    queue: ApprovalQueue,
    *,
    latest: bool,
    approval_id: str | None,
) -> PendingApproval | None:
    """Pick the approval to act on given the CLI flags."""
    pending = queue.list_pending()
    if not pending:
        return None
    if approval_id is not None:
        for approval in pending:
            if approval.id == approval_id:
                return approval
        return None
    # FIFO by default; ``--latest`` flips to most recent.
    return pending[-1] if latest else pending[0]


def _render(approval: PendingApproval) -> str:
    """Format a pending approval for the operator."""
    args_preview = ", ".join(f"{k}={v!r}" for k, v in approval.tool_args.items())
    return (
        f"[bold bright_yellow]{approval.id}[/] "
        f"tool=[cyan]{approval.tool_name}[/] "
        f"role=[magenta]{approval.agent_role}[/] "
        f"session=[dim]{approval.session_id}[/]\n"
        f"  args: {args_preview or '[dim](none)[/]'}"
    )


@click.command("approve-tool")
@click.option(
    "--latest",
    is_flag=True,
    default=False,
    help="Resolve the newest pending approval instead of the oldest.",
)
@click.option("--id", "approval_id", default=None, help="Resolve a specific approval by id.")
@click.option(
    "--always",
    is_flag=True,
    default=False,
    help="Allow and promote the pattern into always-allow rules.",
)
@click.option("--workdir", default=".", type=click.Path(), help="Project root directory.")
def approve_tool_cmd(
    latest: bool,
    approval_id: str | None,
    always: bool,
    workdir: str,
) -> None:
    """Approve a pending tool-call approval.

    Pops the oldest pending approval from ``.sdd/runtime/approvals/`` and
    records an ``allow`` decision. Pass ``--latest`` to resolve the most
    recent entry instead, ``--id`` to target a specific approval, or
    ``--always`` to promote the matched pattern into the user's
    always-allow rules so future calls never block.

    \b
    Examples:
      bernstein approve-tool
      bernstein approve-tool --always
      bernstein approve-tool --id ap-1a2b3c4d5e6f
    """
    approve_tool(latest=latest, approval_id=approval_id, always=always, workdir=workdir)


def approve_tool(
    *,
    latest: bool,
    approval_id: str | None,
    always: bool,
    workdir: str,
) -> None:
    """Resolve a pending tool-call approval.

    Shared implementation behind the ``approve-tool`` command and the
    ``bernstein approve --tool <id>`` flag form.
    """
    queue = _queue_for(workdir)
    approval = _select_approval(queue, latest=latest, approval_id=approval_id)
    if approval is None:
        if approval_id is not None:
            console.print(f"[red]No pending approval with id {approval_id}[/]")
            raise click.exceptions.Exit(1)
        console.print("[dim]No pending approvals.[/]")
        return

    console.print(_render(approval))
    decision = ApprovalDecision.ALWAYS if always else ApprovalDecision.ALLOW
    # The CLI is a human-channel surface: it reads the on-disk record
    # which carries the nonce, and threads it back so the gate enforces
    # the single-use binding uniformly across TUI, HTTP, and CLI paths.
    queue.resolve(
        approval.id,
        decision,
        principal=local_shell_principal(),
        reason="cli",
        nonce=approval.nonce,
    )
    if always:
        try:
            target = promote_to_always_allow(approval, workdir=Path(workdir))
            console.print(f"[green]Approved and promoted to {target}[/]")
        except OSError as exc:
            console.print(f"[yellow]Approved, but could not write always-allow rule: {exc}[/]")
    else:
        console.print(f"[green]Approved {approval.id}[/]")


@click.command("reject-tool")
@click.option(
    "--latest",
    is_flag=True,
    default=False,
    help="Resolve the newest pending approval instead of the oldest.",
)
@click.option("--id", "approval_id", default=None, help="Resolve a specific approval by id.")
@click.option("--workdir", default=".", type=click.Path(), help="Project root directory.")
def reject_tool_cmd(
    latest: bool,
    approval_id: str | None,
    workdir: str,
) -> None:
    """Reject a pending tool-call approval.

    Records a ``reject`` decision so the blocked agent surfaces a
    permission error. With no flags the oldest pending approval is
    resolved.

    \b
    Examples:
      bernstein reject-tool
      bernstein reject-tool --id ap-1a2b3c4d5e6f
    """
    reject_tool(latest=latest, approval_id=approval_id, workdir=workdir)


def reject_tool(
    *,
    latest: bool,
    approval_id: str | None,
    workdir: str,
) -> None:
    """Resolve a pending tool-call approval as rejected.

    Shared implementation behind the ``reject-tool`` command and the
    ``bernstein reject --tool <id>`` flag form.
    """
    queue = _queue_for(workdir)
    approval = _select_approval(queue, latest=latest, approval_id=approval_id)
    if approval is None:
        if approval_id is not None:
            console.print(f"[red]No pending approval with id {approval_id}[/]")
            raise click.exceptions.Exit(1)
        console.print("[dim]No pending approvals.[/]")
        return

    console.print(_render(approval))
    queue.resolve(
        approval.id,
        ApprovalDecision.REJECT,
        principal=local_shell_principal(),
        reason="cli",
        nonce=approval.nonce,
    )
    console.print(f"[red]Rejected {approval.id}[/]")


__all__ = ["approve_tool", "approve_tool_cmd", "reject_tool", "reject_tool_cmd"]
