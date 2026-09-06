"""``bernstein resume <task-id>`` - pick up a task from its last checkpoint.

Loads the per-task checkpoint written by the orchestrator after every
successful step transition, or before an automatic stall kill (heartbeat
staleness or identical-progress detection; issue #3376) discards the
worker's state, validates it, bumps ``resume_count``, fires the
``task.resume`` lifecycle event, and hands control back so the
orchestrator can re-spawn the task from the next step boundary. A
checkpoint written at a stall kill carries its ``stall_reason``.

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
from bernstein.core.persistence.agent_checkpoint import (
    discard_checkpoint,
    evaluate_observations,
    find_checkpoint_for_task,
    is_checkpoint_recoverable,
)
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
EXIT_GRANT_REFUSED: int = 5
EXIT_OBSERVATIONS_MOVED: int = 6


class GrantRefusedError(RuntimeError):
    """Raised when the agent's grant no longer matches current configuration.

    The message names the grant bindings the stored hash covers (role
    permissions, task, parent run, chain head) so the operator can act
    without reading source.
    """


class ObservationsMovedError(RuntimeError):
    """Raised when the bytes the suspended work was derived from have moved.

    Distinct from :class:`GrantRefusedError` on purpose: the grant refusal
    says the run may no longer act, while this says the run would act on a
    world model that is no longer true. Discarding the checkpoint and
    respawning is the expected answer; ``--override-observations`` resumes
    anyway and records that it did.
    """


def discard_agent_checkpoint(workdir: Path, task_id: str) -> bool:
    """Drop the agent checkpoint for ``task_id``; return whether one existed.

    Exposed so the dashboard and the API can take the discard path without
    parsing CLI output. After the drop the next run of the task carries no
    continuation entry, so the chain reads it as a new run.
    """
    runtime_dir = workdir / ".sdd" / "runtime"
    checkpoint = find_checkpoint_for_task(task_id, runtime_dir)
    if checkpoint is None:
        return False
    return discard_checkpoint(checkpoint.agent_id, runtime_dir)


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
    override_interpreter: bool = False,
    override_observations: bool = False,
) -> ResumePlan:
    """Load + validate the checkpoint, bump ``resume_count``, fire the hook.

    Args:
        workdir: Project root containing ``.sdd/runtime/checkpoints``.
        task_id: Task to resume.
        hooks: Optional registry; ``task.resume`` fires on it when given.
        override_interpreter: When ``True``, bypass the interpreter mismatch
            check (``--override-interpreter``) so a resume proceeds even
            though the adapter or resolved model moved.
        override_observations: When ``True``, resume even though the bytes the
            suspended work was derived from moved, instead of discarding the
            checkpoint and respawning.

    Returns:
        A :class:`ResumePlan` ready for the orchestrator.

    Raises:
        CheckpointMissingError: No checkpoint on disk.
        CheckpointCorruptError: File exists but is invalid.
        GrantRefusedError: The grant the checkpoint was written under moved.
        ObservationsMovedError: The observations the checkpoint bound moved.
        HookFailure: The ``task.resume`` hook rejected the resume.
    """
    # Reading once before the bump gives us a clear error path: if the
    # file is corrupt we exit before incrementing the counter.
    checkpoint = load_checkpoint(workdir, task_id)

    # --- Grant + interpreter authority checks (issues #3649, #3852) ---
    # Look up the AgentCheckpoint for this task (written by the orchestrator
    # at suspend time).  Checkpoints are stored per agent, so the lookup
    # scans for the task rather than treating the task id as an agent id.
    # If the checkpoint carries a grant_hash we verify the current
    # configuration still matches before taking any side effect (bump,
    # hook, signal); a stale grant refuses with the bindings named.  The
    # same pre-side-effect gate verifies the interpreter (adapter + resolved
    # model) still matches, unless the operator overrides it.
    _runtime_dir = workdir / ".sdd" / "runtime"
    _agent_checkpoint = find_checkpoint_for_task(task_id, _runtime_dir)
    if _agent_checkpoint is not None:
        _ok, _reason = is_checkpoint_recoverable(
            _agent_checkpoint,
            current_task_id=task_id,
            current_adapter=checkpoint.adapter or None,
            current_model=checkpoint.meta.get("model") or None,
            override_interpreter=override_interpreter,
        )
        if not _ok:
            raise GrantRefusedError(_reason)
        # The authority question is settled; the world-model question is not.
        # Both are asked before the first side effect, and in this order: a
        # run that may not act at all is never offered the discard choice.
        if not override_observations:
            _verdict = evaluate_observations(_agent_checkpoint)
            if _verdict.discard_candidate:
                raise ObservationsMovedError(_verdict.reason)

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
@click.option(
    "--override-interpreter",
    is_flag=True,
    default=False,
    help="Resume even though the adapter or resolved model moved since suspend.",
)
@click.option(
    "--override-observations",
    is_flag=True,
    default=False,
    help="Resume even though the bytes the suspended work was derived from moved.",
)
@click.option(
    "--discard",
    is_flag=True,
    default=False,
    help="Drop the checkpoint first, so the task runs fresh instead of continuing.",
)
def resume_cmd(
    task_id: str,
    workdir: Path | None,
    output_json: bool,
    dry_run: bool,
    override_interpreter: bool,
    override_observations: bool,
    discard: bool,
) -> None:
    """Pick up a paused/killed/crashed task from its last checkpoint.

    \b
    Exit codes:
        0  resume prepared (and, when --dry-run is off, dispatched)
        2  no checkpoint on disk
        3  checkpoint corrupt / failed schema validation
        4  task.resume lifecycle hook failed
        5  grant mismatch — role narrowed, task reassigned, or parent cancelled
        6  observations moved — discard and respawn, or --override-observations
    """
    project_root = workdir or Path.cwd()
    if discard:
        dropped = discard_agent_checkpoint(project_root, task_id)
        console.print(
            f"[yellow]Checkpoint discarded for {task_id!r}.[/yellow] The task runs fresh; "
            "the chain records a new run, not a continuation."
            if dropped
            else f"[dim]No agent checkpoint to discard for {task_id!r}.[/dim]"
        )
    try:
        plan = prepare_resume(
            project_root,
            task_id,
            override_interpreter=override_interpreter,
            override_observations=override_observations,
        )
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
    except GrantRefusedError as exc:
        console.print(f"[red]Grant mismatch — resume refused:[/red] {exc}")
        console.print(
            "[dim]The role's permissions, task assignment, or parent run changed since this"
            " checkpoint was written. Re-run the task from scratch or restore the original grant.[/dim]"
        )
        raise SystemExit(EXIT_GRANT_REFUSED) from None
    except ObservationsMovedError as exc:
        console.print(f"[red]Observations moved — resume is not a continuation:[/red] {exc}")
        console.print(
            "[dim]Respawn the task from scratch with 'bernstein resume"
            f" {task_id} --discard', or resume onto the changed bytes with"
            " --override-observations (recorded in the chain).[/dim]"
        )
        raise SystemExit(EXIT_OBSERVATIONS_MOVED) from None

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
