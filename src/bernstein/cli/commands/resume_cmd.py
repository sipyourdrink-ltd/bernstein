"""``bernstein resume <task-id>`` - pick up a task from its last checkpoint.

Loads the per-task checkpoint written by the orchestrator after every
successful step transition, validates it, bumps ``resume_count``, fires
the ``task.resume`` lifecycle event, and hands control back so the
orchestrator can re-spawn the task from the next step boundary.

See ``feat-resume-from-checkpoint`` spec for the full contract. v1 scope
is local-only - cross-machine resume, distributed checkpoint storage,
and resuming across role-definition changes are explicitly out of scope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.adapters._contract import resume_capability, strategy_for
from bernstein.cli.helpers import console
from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleContext, LifecycleEvent
from bernstein.core.persistence.resume_prompt import build_resume_context
from bernstein.core.persistence.task_resume import (
    CheckpointCorruptError,
    CheckpointMissingError,
    TaskResumeCheckpoint,
    bump_resume_count,
    checkpoint_path_for,
    load_checkpoint,
)

# Exit codes used by `bernstein resume`. Kept tight so operators (and the
# dashboard) can branch on the specific failure mode.
EXIT_OK: int = 0
EXIT_NO_CHECKPOINT: int = 2
EXIT_CORRUPT: int = 3
EXIT_HOOK_FAILED: int = 4


@dataclass(frozen=True)
class ResumePlan:
    """Outcome of the resume-prepare phase before the actual re-spawn.

    Exposed so other entry points (server, dashboard "Resume" button) can
    reuse the same preflight logic without parsing CLI output.
    """

    checkpoint: TaskResumeCheckpoint
    capability: str
    resume_context: str
    #: The typed resume strategy (issue #1627). Dispatch sites branch on this
    #: enum rather than the adapter name; ``capability`` is the legacy
    #: two-state view retained for the ``bernstein resume`` env contract.
    resume_strategy: str = ""


def prepare_resume(
    workdir: Path,
    task_id: str,
    *,
    hooks: HookRegistry | None = None,
) -> ResumePlan:
    """Load + validate the checkpoint, bump ``resume_count``, fire the hook.

    Args:
        workdir: Project root containing ``.sdd/runtime/checkpoints``.
        task_id: Task to resume.
        hooks: Optional registry; ``task.resume`` fires on it when given.

    Returns:
        A :class:`ResumePlan` ready for the orchestrator.

    Raises:
        CheckpointMissingError: No checkpoint on disk.
        CheckpointCorruptError: File exists but is invalid.
        HookFailure: The ``task.resume`` hook rejected the resume.
    """
    # Reading once before the bump gives us a clear error path: if the
    # file is corrupt we exit before incrementing the counter.
    load_checkpoint(workdir, task_id)
    checkpoint = bump_resume_count(workdir, task_id)
    adapter_name = checkpoint.adapter or ""
    capability = resume_capability(adapter_name)
    resume_strategy = str(strategy_for(adapter_name).resume)
    resume_context = build_resume_context(checkpoint)
    if hooks is not None:
        hooks.run(
            LifecycleEvent.TASK_RESUME,
            LifecycleContext(
                event=LifecycleEvent.TASK_RESUME,
                task=task_id,
                session_id=checkpoint.adapter_session_id or None,
                workdir=workdir,
                env={
                    "BERNSTEIN_RESUME_COUNT": str(checkpoint.resume_count),
                    "BERNSTEIN_RESUME_CAPABILITY": capability,
                    "BERNSTEIN_RESUME_STRATEGY": resume_strategy,
                },
            ),
        )
    return ResumePlan(
        checkpoint=checkpoint,
        capability=capability,
        resume_context=resume_context,
        resume_strategy=resume_strategy,
    )


def _render_plan(workdir: Path, plan: ResumePlan, *, output_json: bool) -> None:
    """Pretty-print the resume plan to the operator."""
    cp = plan.checkpoint
    if output_json:
        payload = {
            "task_id": cp.task_id,
            "resume_count": cp.resume_count,
            "last_completed_step_id": cp.last_completed_step_id,
            "trace_cursor": cp.trace_cursor,
            "adapter": cp.adapter,
            "adapter_session_id": cp.adapter_session_id,
            "capability": plan.capability,
            "resume_strategy": plan.resume_strategy,
            "worktree_path": cp.worktree_path,
            "checkpoint_path": str(checkpoint_path_for(workdir, cp.task_id)),
        }
        console.print_json(json.dumps(payload))
        return

    console.print()
    console.print(
        Panel(
            f"[bold]Resuming task[/bold] [cyan]{cp.task_id}[/cyan]",
            border_style="green",
            expand=False,
        )
    )
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=22)
    table.add_column("Value")
    table.add_row("Resume attempt", str(cp.resume_count))
    table.add_row("Last completed step", cp.last_completed_step_id or "[dim]<none>[/dim]")
    table.add_row("Trace cursor (bytes)", str(cp.trace_cursor))
    table.add_row("Adapter", cp.adapter or "[dim]<unknown>[/dim]")
    table.add_row("Adapter session id", cp.adapter_session_id or "[dim]<none>[/dim]")
    table.add_row("Capability", plan.capability)
    if cp.worktree_path:
        table.add_row("Worktree", cp.worktree_path)
    table.add_row("Checkpoint file", str(checkpoint_path_for(workdir, cp.task_id)))
    console.print(table)
    console.print()
    console.print("[dim]Adapter prompt will receive recovered scratchpad as preamble.[/dim]")
    console.print()


@click.command("resume")
@click.argument("task_id")
@click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the Rich summary.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate + bump resume_count + print plan; do not re-spawn.",
)
def resume_cmd(task_id: str, workdir: Path | None, output_json: bool, dry_run: bool) -> None:
    """Pick up a paused/killed/crashed task from its last checkpoint.

    \b
    Exit codes:
        0  resume prepared (and, when --dry-run is off, dispatched)
        2  no checkpoint on disk
        3  checkpoint corrupt / failed schema validation
        4  task.resume lifecycle hook failed
    """
    project_root = workdir or Path.cwd()
    try:
        plan = prepare_resume(project_root, task_id)
    except CheckpointMissingError as exc:
        console.print(f"[red]No checkpoint:[/red] {exc}")
        _hint_work_ledger(project_root, task_id)
        raise SystemExit(EXIT_NO_CHECKPOINT) from None
    except CheckpointCorruptError as exc:
        console.print(f"[red]Corrupt checkpoint for {task_id!r}:[/red] {exc}")
        console.print(
            "[dim]Inspect the file under .sdd/runtime/checkpoints/<task-id>/ and remove it to run the task fresh.[/dim]"
        )
        raise SystemExit(EXIT_CORRUPT) from None

    _render_plan(project_root, plan, output_json=output_json)

    if dry_run:
        return

    # Re-spawn integration with the orchestrator. We keep the actual
    # spawn deferred to the orchestrator path so this command is safe to
    # call from the dashboard / API and stays unit-testable. The CLI
    # signals intent by writing a one-line marker the spawner watches.
    _write_resume_signal(project_root, plan)


def _hint_work_ledger(workdir: Path, resume_id: str) -> None:
    """Point the operator at the durable work ledger when one matches.

    A per-task checkpoint is machine-local; a run whose state was anchored
    to the work-ledger ref (#2358) resumes on any clone via
    ``bernstein ledger resume``. When the id the operator passed matches a
    local or anchored ledger, say so instead of dead-ending.
    """
    from bernstein.core.persistence.ledger_git import LedgerGitError, ledger_ref, list_ledger_runs
    from bernstein.core.persistence.work_ledger import LedgerReader, run_ledger_dir

    try:
        ledger_ref(resume_id)
    except LedgerGitError:
        return
    has_local = LedgerReader(run_ledger_dir(workdir / ".sdd", resume_id)).exists()
    has_anchor = False
    if not has_local:
        try:
            has_anchor = resume_id in list_ledger_runs(workdir)
        except LedgerGitError:
            has_anchor = False
    if has_local or has_anchor:
        console.print(
            f"[yellow]A durable work ledger exists for {resume_id!r}.[/yellow] "
            f"Resume the run from its verified chain with: bernstein ledger resume {resume_id}"
        )


def _write_resume_signal(workdir: Path, plan: ResumePlan) -> None:
    """Drop a signal file the orchestrator's resume watcher picks up.

    Kept tiny: any worker watching ``.sdd/runtime/resume/`` claims the
    task by atomically renaming the signal. If no worker is running the
    file persists until ``bernstein run`` starts.
    """
    signal_dir = workdir / ".sdd" / "runtime" / "resume"
    signal_dir.mkdir(parents=True, exist_ok=True)
    target = signal_dir / f"{plan.checkpoint.task_id}.signal"
    payload = {
        "task_id": plan.checkpoint.task_id,
        "resume_count": plan.checkpoint.resume_count,
        "capability": plan.capability,
        "adapter": plan.checkpoint.adapter,
        "adapter_session_id": plan.checkpoint.adapter_session_id,
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"[green]Resume signal written:[/green] {target}")
