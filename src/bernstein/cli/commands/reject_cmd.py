"""``bernstein reject`` - refuse a pending approval gate.

Mirror of :mod:`bernstein.cli.commands.approve_cmd`: writes a signed
decision record to ``<workdir>/.sdd/runtime/approvals/<task_id>.rejected``
so the post-completion review gate or the pre-spawn ``ApprovalSpec`` gate
(#1110) unblocks with a refusal. Idempotent under concurrent
invocations: the first writer wins via ``os.replace`` and subsequent
callers see the existing decision and report ``already resolved``.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.commands.approve_cmd import _resolve_task_decision


@click.command("reject")
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
    help="Resolve a pending tool-call approval by id instead of a task (flag form of ``reject-tool``).",
)
def reject(task_id: str | None, workdir: str, prompt: bool, tool_id: str | None) -> None:
    """Reject a pending task (review gate or pre-spawn approval gate).

    Writes a ``<task_id>.rejected`` decision file under
    ``.sdd/runtime/approvals/``. The orchestrator marks the task failed
    and skips the agent body (or, post-completion, discards the work
    without merging).

    Concurrent ``bernstein reject`` calls are idempotent: the first one
    creates the file, subsequent invocations exit with ``already
    resolved``.

    Pass ``--tool <id>`` instead to refuse a pending tool-call approval
    from the interactive approval queue; ``reject-tool`` remains as an
    alias for this flag form.

    \b
    Examples:
      bernstein reject T-abc123
      bernstein reject --tool ap-1a2b3c4d5e6f
    """
    if tool_id is not None:
        from bernstein.cli.commands.approval_cmd import reject_tool

        reject_tool(latest=False, approval_id=tool_id, workdir=workdir)
        return

    if task_id is None:
        raise click.UsageError(
            "Missing argument 'TASK_ID'; pass a task id, or --tool <id> to resolve a pending tool-call approval."
        )

    _resolve_task_decision(task_id, Path(workdir), prompt=prompt, outcome="rejected")


__all__ = ["reject"]
