"""``bernstein resume <task-id>`` - pick up a task from its last checkpoint.

Loads the per-task checkpoint written by the orchestrator after every
successful step transition, or before an automatic stall kill (heartbeat
staleness or identical-progress detection; issue #3376) discards the
worker's state, validates it, bumps ``resume_count``, and writes a local
resume signal. A checkpoint written at a stall kill carries its
``stall_reason``.

See ``feat-resume-from-checkpoint`` spec for the full contract. v1 scope
is local-only - cross-machine resume, distributed checkpoint storage,
and resuming across role-definition changes are explicitly out of scope.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.adapters._contract import resume_capability, strategy_for
from bernstein.cli.helpers import console
from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleContext, LifecycleEvent
from bernstein.core.persistence.agent_checkpoint import (
    AmbiguousAgentCheckpointError,
    discard_checkpoint,
    evaluate_observations,
    find_checkpoint_for_task,
    is_checkpoint_recoverable,
)
from bernstein.core.persistence.resume_prompt import build_resume_context
from bernstein.core.persistence.task_resume import (
    CheckpointCorruptError,
    CheckpointMissingError,
    ResumeDispatchStatus,
    ResumeRequestCorruptError,
    ResumeRequestRecord,
    TaskResumeCheckpoint,
    bump_resume_count,
    checkpoint_path_for,
    checkpoint_source_digest,
    load_checkpoint,
    load_resume_request,
    publish_resume_request,
    resume_request_id,
)

# Exit codes used by `bernstein resume`. Kept tight so operators (and the
# dashboard) can branch on the specific failure mode.
EXIT_OK: int = 0
EXIT_IO_ERROR: int = 1
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
    parsing CLI output. The separate task checkpoint retains the saved
    step and adapter session. Lookup or deletion I/O errors propagate.
    """
    runtime_dir = workdir / ".sdd" / "runtime"
    checkpoint = find_checkpoint_for_task(task_id, runtime_dir, for_discard=True)
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
    #: Deterministic digest and request id for this attempt (#7854). Excludes
    #: ``resume_count``/``updated_at`` so a duplicate command reuses the id.
    source_digest: str = ""
    request_id: str = ""


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
    source_digest = checkpoint_source_digest(checkpoint)
    request_id = resume_request_id(task_id, source_digest)

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

    existing_request = load_resume_request(workdir, task_id, request_id)
    # A second command for a still-queued request must not double-bump; a
    # claimed/resumed attempt is a new instruction and is counted again.
    # This stays after the authority gate so a refusal has no side effects.
    if existing_request is None or existing_request.dispatch_status != ResumeDispatchStatus.QUEUED.value:
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
        source_digest=source_digest,
        request_id=request_id,
    )


def _render_plan(workdir: Path, plan: ResumePlan) -> None:
    """Pretty-print the resume plan to the operator."""
    cp = plan.checkpoint
    console.print()
    console.print(
        Panel(
            f"[bold]Resume plan for task[/bold] [cyan]{cp.task_id}[/cyan]",
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
    help="Discard the agent checkpoint before preparing the resume.",
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
        0  resume prepared (and, when --dry-run is off, signal queued)
        1  resume preparation or signal write failed with an I/O error
        2  no checkpoint on disk
        3  invalid checkpoint data or ambiguous agent checkpoint for --discard
        4  task.resume lifecycle hook failed
        5  grant mismatch — role narrowed, task reassigned, or parent cancelled
        6  observations moved — see the hint for resume and fresh-run options
    """
    project_root = workdir or Path.cwd()
    report: dict[str, object] = {
        "task_id": task_id,
        "dry_run": dry_run,
        "dispatch_status": "not_dispatched",
        "signal_paths": [],
        "runtime_acknowledged": False,
    }

    def fail(exit_code: int, message: str, hint: str | None = None) -> NoReturn:
        if output_json:
            report["error"] = message
            if hint:
                report["hint"] = hint
            click.echo(json.dumps(report, indent=2))
        else:
            console.print(message, style="red", markup=False)
            if hint:
                console.print(hint, style="dim", markup=False, soft_wrap=True)
        raise SystemExit(exit_code) from None

    try:
        if discard:
            report["checkpoint_discarded"] = False
            dropped = discard_agent_checkpoint(project_root, task_id)
            report["checkpoint_discarded"] = dropped
            if not output_json:
                console.print(
                    f"[yellow]Agent checkpoint discarded for {task_id!r}.[/yellow]"
                    if dropped
                    else f"[dim]No agent checkpoint to discard for {task_id!r}.[/dim]"
                )
        plan = prepare_resume(
            project_root,
            task_id,
            override_interpreter=override_interpreter,
            override_observations=override_observations,
        )
    except CheckpointMissingError as exc:
        fail(EXIT_NO_CHECKPOINT, f"No checkpoint: {exc}", _work_ledger_hint(project_root, task_id))
    except AmbiguousAgentCheckpointError as exc:
        fail(
            EXIT_CORRUPT,
            str(exc),
            "Inspect the checkpoints under .sdd/runtime/agents/ or start a new session with 'bernstein run --fresh'.",
        )
    except CheckpointCorruptError as exc:
        fail(
            EXIT_CORRUPT,
            f"Corrupt checkpoint for {task_id!r}: {exc}",
            "Inspect the file under .sdd/runtime/checkpoints/<task-id>/ and remove it to run the task fresh.",
        )
    except GrantRefusedError as exc:
        fail(
            EXIT_GRANT_REFUSED,
            f"Grant mismatch — resume refused: {exc}",
            "The role's permissions, task assignment, or parent run changed since this checkpoint was written. "
            "Re-run the task from scratch or restore the original grant.",
        )
    except ObservationsMovedError as exc:
        fail(
            EXIT_OBSERVATIONS_MOVED,
            f"Observations moved — resume refused: {exc}",
            "Start a new session with 'bernstein run --fresh'. "
            "To resume from the task checkpoint, --discard removes the agent checkpoint, "
            "or --override-observations keeps it and accepts the changed bytes.",
        )
    except OSError as exc:
        fail(EXIT_IO_ERROR, f"Could not prepare resume: {exc}")
    except ResumeRequestCorruptError as exc:
        fail(EXIT_IO_ERROR, f"Could not read resume requests: {exc}")

    cp = plan.checkpoint
    report.update(
        task_id=cp.task_id,
        resume_count=cp.resume_count,
        last_completed_step_id=cp.last_completed_step_id,
        trace_cursor=cp.trace_cursor,
        adapter=cp.adapter,
        adapter_session_id=cp.adapter_session_id,
        capability=plan.capability,
        resume_strategy=plan.resume_strategy,
        worktree_path=cp.worktree_path,
        checkpoint_path=str(checkpoint_path_for(project_root, cp.task_id)),
        request_id=plan.request_id,
        source_digest=plan.source_digest,
    )
    record: ResumeRequestRecord | None = None
    signal: Path | None = None
    if not dry_run:
        try:
            signal, record = _write_resume_signal(project_root, plan)
        except (OSError, ResumeRequestCorruptError) as exc:
            fail(EXIT_IO_ERROR, f"Could not write resume signal: {exc}")
        report.update(
            dispatch_status=record.dispatch_status,
            signal_paths=[str(signal)],
            request_id=record.request_id,
            runtime_acknowledged=record.dispatch_status == ResumeDispatchStatus.RESUMED.value,
        )

    if output_json:
        click.echo(json.dumps(report, indent=2))
        return

    _render_plan(project_root, plan)
    if signal is not None:
        console.print(f"[green]Resume signal queued:[/green] {signal}")
        if record is not None and record.dispatch_status == ResumeDispatchStatus.RESUMED.value:
            console.print(f"[green]Runtime acknowledgement: resumed for request {record.request_id}.[/green]")
        else:
            console.print("[dim]Runtime acknowledgement: unavailable.[/dim]")
    else:
        console.print("[dim]Dry run completed.[/dim]")


def _work_ledger_hint(workdir: Path, resume_id: str) -> str | None:
    """Point the operator at the durable work ledger when one matches.

    A per-task checkpoint is machine-local; a run whose state was anchored
    to the work-ledger ref (#2358) resumes on any clone via
    ``bernstein ledger resume``. When the id the operator passed matches a
    local or anchored ledger, say so instead of dead-ending.
    """
    from bernstein.core.persistence.ledger_git import LedgerGitError, ledger_ref, list_ledger_runs
    from bernstein.core.persistence.work_ledger import LedgerReader, run_ledger_dir
    from bernstein.core.security.path_containment import PathContainmentError

    try:
        ledger_ref(resume_id)
    except LedgerGitError:
        return None
    try:
        has_local = LedgerReader(run_ledger_dir(workdir / ".sdd", resume_id)).exists()
    except (OSError, PathContainmentError):
        has_local = False
    has_anchor = False
    if not has_local:
        try:
            has_anchor = resume_id in list_ledger_runs(workdir)
        except (OSError, subprocess.TimeoutExpired, LedgerGitError):
            has_anchor = False
    if has_local or has_anchor:
        return (
            f"A durable work ledger exists for {resume_id!r}. "
            f"Resume the run with 'bernstein ledger resume {resume_id}'."
        )
    return None


def _write_resume_signal(workdir: Path, plan: ResumePlan) -> tuple[Path, ResumeRequestRecord]:
    """Publish the wake-up signal and return its path with the request record.

    The signal keeps its legacy fields so existing readers still work; the
    companion ``<task_id>.requests.json`` holds the claim/outcome state.
    """
    cp = plan.checkpoint
    record = publish_resume_request(
        workdir,
        cp.task_id,
        source_digest=plan.source_digest,
        signal_fields={
            "resume_count": cp.resume_count,
            "capability": plan.capability,
            "adapter": cp.adapter,
            "adapter_session_id": cp.adapter_session_id,
        },
    )
    signal = workdir / ".sdd" / "runtime" / "resume" / f"{cp.task_id}.signal"
    return signal, record
