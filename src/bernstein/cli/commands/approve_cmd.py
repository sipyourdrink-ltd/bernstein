"""``bernstein approve`` - resolve the human-in-the-loop approval gate.

Originally lived in :mod:`bernstein.cli.commands.task_cmd`; pulled into a
dedicated module for #1110 so the pre-spawn ``ApprovalSpec`` gate has a
clear ownership boundary distinct from the post-completion review queue
and the tool-call queue (``bernstein approve-tool``).

The command writes ``<workdir>/.sdd/runtime/approvals/<task_id>.approved``
which the orchestrator's pre-spawn gate (or the legacy review gate)
detects via filesystem polling. Atomic writes (``os.replace``) make it
idempotent: a second concurrent ``bernstein approve <task_id>`` call
either lands a byte-identical payload or finds the file already
resolved and exits with a "already resolved" message.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _atomic_write_text(path: Path, content: str) -> bool:
    """Atomically write *content* to *path*; return ``True`` if newly created.

    Uses ``os.replace`` so concurrent writers cannot interleave content,
    and exposes a "first writer wins" hint (the return value) to the
    caller so we can differentiate fresh resolutions from no-op repeats.

    Args:
        path: Target file path.
        content: Text payload to persist.

    Returns:
        ``True`` if the path did not exist prior to the call (the caller
        is the first writer); ``False`` when an existing file was simply
        overwritten - useful for the idempotent "already resolved"
        message used by both ``bernstein approve`` and ``bernstein
        reject``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pre_exists = path.exists()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
    return not pre_exists


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
def approve(task_id: str, workdir: str, prompt: bool) -> None:
    """Approve a pending task (review gate or pre-spawn approval gate).

    Writes a ``<task_id>.approved`` decision file under
    ``.sdd/runtime/approvals/`` so a paused orchestrator (post-completion
    review or pre-spawn ``ApprovalSpec`` gate, #1110) can move on.

    Concurrent ``bernstein approve`` calls are idempotent: the first one
    creates the file, subsequent invocations report ``already resolved``
    and exit without rewriting state.

    \b
    Example:
      bernstein approve T-abc123
    """
    from bernstein.core.orchestration.approval_gate import UnsafeApprovalIdError, approval_path

    # The decision file name is derived from task_id, so the id goes through the
    # same rule the read side uses. Validated before mkdir: an unchecked id here
    # created directories and wrote a decision file anywhere on disk.
    try:
        decision_file = approval_path(Path(workdir), task_id, ".approved")
        rejected_file = approval_path(Path(workdir), task_id, ".rejected")
    except UnsafeApprovalIdError as exc:
        console.print(f"[red]Refusing to approve:[/red] {exc}")
        raise SystemExit(1) from exc

    approvals_dir = decision_file.parent
    approvals_dir.mkdir(parents=True, exist_ok=True)

    if rejected_file.exists():
        console.print(
            f"[yellow]Already resolved:[/yellow] task [bold]{task_id}[/bold] was rejected; "
            "leaving the rejection in place."
        )
        return

    if decision_file.exists():
        console.print(f"[dim]Already approved:[/dim] task [bold]{task_id}[/bold] (no-op)")
        return

    if prompt and not _foreground_confirm(f"Approve task {task_id}?"):
        console.print(f"[dim]Skipped[/dim] approval for [bold]{task_id}[/bold]")
        return

    created = _atomic_write_text(decision_file, "approved")
    if created:
        console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold]: Bernstein will continue.")
    else:
        console.print(f"[dim]Already approved:[/dim] task [bold]{task_id}[/bold] (no-op)")


__all__ = ["approve"]
