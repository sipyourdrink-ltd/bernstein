"""``bernstein ledger`` - the durable work ledger surface (#2358).

The work ledger is a hash-chained JSONL record of a run's task graph and
every state transition, anchored to a dedicated git ref so it travels with
the repository. This command group is the operator surface over it:

* ``verify``   - walk the chain and recompute every entry hash; a tampered
  entry is named at its exact position.
* ``anchor``   - publish the validated chain to the ledger ref and mirror
  the anchor into the HMAC audit chain.
* ``fetch``    - pull an anchored chain from a remote (after a clone) and
  materialize it locally, refusing divergence.
* ``resume``   - verify end to end, rebuild scheduler state by replay,
  and hand the frontier tasks to the resume watcher.
* ``runs``     - list anchored runs.
* ``gc``       - squash a ref's anchor history to bound repo growth.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.persistence.ledger_git import (
    LedgerDivergenceError,
    LedgerGitError,
    anchor_ledger,
    fetch_ledger_ref,
    gc_ledger_ref,
    list_ledger_runs,
    materialize_ledger,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_RESUMED,
    LedgerError,
    LedgerReader,
    LedgerState,
    WorkLedger,
    replay_state,
    run_ledger_dir,
)
from bernstein.core.security.audit_chain import AuditChainStore, record_work_ledger_anchor

# Exit codes shared across the group so operators (and the dashboard) can
# branch on the specific failure mode.
EXIT_OK = 0
EXIT_NO_LEDGER = 1
EXIT_VERIFY_FAILED = 2
EXIT_DIVERGED = 3

_WORKDIR_OPTION = click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
_JSON_OPTION = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the Rich summary.",
)


def _ledger_dir(workdir: Path | None, run_id: str) -> Path:
    root = (workdir or Path.cwd()).resolve()
    return run_ledger_dir(root / ".sdd", run_id)


def _print_errors(errors: list[str]) -> None:
    for error in errors:
        console.print(f"  [red]-[/red] {error}")


@click.group("ledger")
def ledger_group() -> None:
    """Durable work ledger: verify, anchor, fetch, resume, gc."""


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@ledger_group.command("verify")
@click.argument("run_id")
@_WORKDIR_OPTION
@click.option(
    "--expected-head",
    default=None,
    help="Fail unless the walked head equals this hash (e.g. from an anchor receipt).",
)
@_JSON_OPTION
def ledger_verify_cmd(run_id: str, workdir: Path | None, expected_head: str | None, output_json: bool) -> None:
    """Walk the run's ledger chain and recompute every entry hash.

    \b
    Exit codes:
        0  chain verifies end to end
        1  no ledger on disk for this run
        2  verification failed (the exact entry position is named)
    """
    ledger_dir = _ledger_dir(workdir, run_id)
    reader = LedgerReader(ledger_dir)
    if not reader.exists():
        console.print(f"[red]No work ledger for run {run_id!r}[/red] at {ledger_dir}")
        raise SystemExit(EXIT_NO_LEDGER)

    result = reader.verify(expected_head=expected_head)
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "run_id": run_id,
                    "ok": result.ok,
                    "head_hash": result.head_hash,
                    "entries": result.entries,
                    "errors": result.errors,
                }
            )
        )
    elif result.ok:
        console.print(
            f"[green]Ledger verified:[/green] run {run_id}, {result.entries} entries, head {result.head_hash[:16]}..."
        )
    else:
        console.print(f"[red]Ledger verification failed for run {run_id!r}:[/red]")
        _print_errors(result.errors)
    if not result.ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


# ---------------------------------------------------------------------------
# anchor
# ---------------------------------------------------------------------------


@ledger_group.command("anchor")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def ledger_anchor_cmd(run_id: str, workdir: Path | None, output_json: bool) -> None:
    """Anchor the run's validated chain to its git ref.

    The chain is verified before anything reaches the ref, and the anchor is
    mirrored into the HMAC audit chain as a ``work_ledger.anchor`` event.

    \b
    Exit codes:
        0  anchored (or already anchored at this head)
        1  no ledger on disk for this run
        2  the chain does not verify or git refused
        3  the anchored chain diverges from the local one
    """
    root = (workdir or Path.cwd()).resolve()
    ledger_dir = _ledger_dir(workdir, run_id)
    if not LedgerReader(ledger_dir).exists():
        console.print(f"[red]No work ledger for run {run_id!r}[/red] at {ledger_dir}")
        raise SystemExit(EXIT_NO_LEDGER)

    try:
        anchor = anchor_ledger(root, ledger_dir, run_id=run_id)
    except LedgerDivergenceError as exc:
        console.print(f"[red]Divergence detected:[/red] {exc}")
        raise SystemExit(EXIT_DIVERGED) from None
    except LedgerGitError as exc:
        console.print(f"[red]Anchor failed:[/red] {exc}")
        raise SystemExit(EXIT_VERIFY_FAILED) from None

    chain = AuditChainStore(root / ".sdd" / "audit")
    record_work_ledger_anchor(
        chain=chain,
        run_id=run_id,
        head_hash=anchor.head_hash,
        entry_count=anchor.entry_count,
        chunk_count=anchor.chunk_count,
        ref=anchor.ref,
        tree_sha=anchor.tree_sha,
    )

    if output_json:
        console.print_json(json.dumps(anchor.to_dict()))
    else:
        console.print(
            f"[green]Anchored:[/green] run {run_id}, {anchor.entry_count} entries "
            f"-> {anchor.ref} (tree {anchor.tree_sha[:12]}, head {anchor.head_hash[:16]}...)"
        )


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@ledger_group.command("fetch")
@click.argument("run_id")
@_WORKDIR_OPTION
@click.option("--remote", default="origin", show_default=True, help="Git remote to fetch the ledger ref from.")
@_JSON_OPTION
def ledger_fetch_cmd(run_id: str, workdir: Path | None, remote: str, output_json: bool) -> None:
    """Fetch the run's anchored ledger from a remote and materialize it.

    Use after cloning the repository on a new machine: side refs are not
    fetched by a default clone. Divergent local state is refused, never
    merged.

    \b
    Exit codes:
        0  ledger materialized (created, fast-forwarded, or unchanged)
        1  the remote has no anchored ledger for this run
        2  the anchored chain does not verify
        3  local and anchored chains diverge
    """
    root = (workdir or Path.cwd()).resolve()
    ledger_dir = _ledger_dir(workdir, run_id)
    try:
        fetch_ledger_ref(root, run_id, remote=remote)
        result = materialize_ledger(root, run_id, ledger_dir)
    except LedgerDivergenceError as exc:
        console.print(f"[red]Divergence detected:[/red] {exc}")
        raise SystemExit(EXIT_DIVERGED) from None
    except LedgerGitError as exc:
        message = str(exc)
        console.print(f"[red]Fetch failed:[/red] {message}")
        raise SystemExit(EXIT_NO_LEDGER if "no anchored ledger" in message else EXIT_VERIFY_FAILED) from None

    if output_json:
        console.print_json(json.dumps({"run_id": run_id, **result.to_dict()}))
    else:
        console.print(
            f"[green]Ledger {result.action}:[/green] run {run_id}, {result.entry_count} entries, "
            f"head {result.head_hash[:16]}..."
        )


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def _render_state(run_id: str, ledger_dir: Path, state: LedgerState) -> None:
    console.print()
    console.print(
        Panel(
            f"[bold]Resuming run[/bold] [cyan]{run_id}[/cyan] from its work ledger",
            border_style="green",
            expand=False,
        )
    )
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=22)
    table.add_column("Value")
    table.add_row("Verified entries", str(state.entries))
    table.add_row("Chain head", f"{state.head_hash[:32]}...")
    table.add_row("Completed tasks", ", ".join(state.completed_tasks) or "[dim]<none>[/dim]")
    table.add_row("In-flight tasks", ", ".join(state.in_flight_tasks) or "[dim]<none>[/dim]")
    table.add_row("Scheduled tasks", ", ".join(state.scheduled_tasks) or "[dim]<none>[/dim]")
    table.add_row("Failed tasks", ", ".join(state.failed_tasks) or "[dim]<none>[/dim]")
    table.add_row("Prior resumes", str(state.resumes))
    table.add_row("Ledger", str(ledger_dir))
    console.print(table)
    console.print()


def _write_resume_signals(root: Path, run_id: str, state: LedgerState) -> list[Path]:
    """Drop one signal file per frontier task for the resume watcher.

    The signal shape matches ``bernstein resume`` so any worker watching
    ``.sdd/runtime/resume/`` claims a ledger-resumed task the same way it
    claims a checkpoint-resumed one.
    """
    signal_dir = root / ".sdd" / "runtime" / "resume"
    signal_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for task_id in state.resume_frontier():
        target = signal_dir / f"{task_id}.signal"
        payload = {
            "task_id": task_id,
            "run_id": run_id,
            "source": "work_ledger",
            "ledger_head": state.head_hash,
            "resume_count": state.resumes,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written.append(target)
    return written


@ledger_group.command("resume")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Verify + replay + print the plan; do not record the resume or write signals.",
)
def ledger_resume_cmd(run_id: str, workdir: Path | None, output_json: bool, dry_run: bool) -> None:
    """Resume a run from its work ledger on any clone.

    Verifies the chain end to end, rebuilds scheduler state by replaying
    every transition, refuses divergent chains, then records the resume as
    a new chain entry and hands the frontier tasks to the resume watcher.

    \b
    Exit codes:
        0  resume prepared (state rebuilt from the verified chain)
        1  no ledger on disk (and none anchored) for this run
        2  chain verification failed (exact entry position reported)
        3  local and anchored chains diverge (two divergent resumes)
    """
    root = (workdir or Path.cwd()).resolve()
    ledger_dir = _ledger_dir(workdir, run_id)
    reader = LedgerReader(ledger_dir)

    # A fresh clone has the anchored ref but no materialized file yet.
    if not reader.exists():
        try:
            materialize_ledger(root, run_id, ledger_dir)
        except LedgerDivergenceError as exc:
            console.print(f"[red]Divergence detected:[/red] {exc}")
            raise SystemExit(EXIT_DIVERGED) from None
        except LedgerGitError as exc:
            console.print(f"[red]No work ledger for run {run_id!r}:[/red] {exc}")
            console.print("[dim]After a clone, run 'bernstein ledger fetch <run-id>' to pull the anchored chain.[/dim]")
            raise SystemExit(EXIT_NO_LEDGER) from None

    result = reader.verify()
    if not result.ok:
        console.print(f"[red]Refusing to resume run {run_id!r}: the chain does not verify.[/red]")
        _print_errors(result.errors)
        raise SystemExit(EXIT_VERIFY_FAILED)

    # Divergence gate: when an anchored chain exists it must be identical
    # to, an extension of, or an ancestor of the local chain. Two chains
    # extending the same parent entry are two divergent resumes.
    try:
        materialize_result = materialize_ledger(root, run_id, ledger_dir)
        materialized_action = materialize_result.action
    except LedgerDivergenceError as exc:
        console.print(f"[red]Divergence detected:[/red] {exc}")
        raise SystemExit(EXIT_DIVERGED) from None
    except LedgerGitError:
        # No anchored ref, or local ahead of the anchor: both fine to resume.
        materialized_action = "none"

    state = replay_state(reader.entries(), run_id=run_id)

    if output_json:
        console.print_json(
            json.dumps(
                {
                    **state.to_dict(),
                    "run_id": run_id,
                    "ledger_dir": str(ledger_dir),
                    "materialized": materialized_action,
                    "dry_run": dry_run,
                }
            )
        )
    else:
        _render_state(run_id, ledger_dir, state)

    if dry_run:
        return

    # Record the resume as a chain entry: the nonce makes two independent
    # resumes of the same head structurally divergent at the next entry, so
    # the anchor/fetch gate catches them instead of a silent merge.
    try:
        ledger = WorkLedger.open(ledger_dir)
        ledger.append(
            kind=KIND_RUN_RESUMED,
            payload={"from_head": state.head_hash, "resume_nonce": uuid.uuid4().hex},
        )
    except LedgerError as exc:
        console.print(f"[red]Failed to record the resume entry:[/red] {exc}")
        raise SystemExit(EXIT_VERIFY_FAILED) from None

    signals = _write_resume_signals(root, run_id, state)
    if not output_json:
        if signals:
            for signal in signals:
                console.print(f"[green]Resume signal written:[/green] {signal}")
        else:
            console.print("[yellow]Nothing to resume:[/yellow] no in-flight or scheduled tasks in the ledger.")
        console.print("[dim]Re-anchor after the run advances: bernstein ledger anchor " + run_id + "[/dim]")


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@ledger_group.command("runs")
@_WORKDIR_OPTION
@_JSON_OPTION
def ledger_runs_cmd(workdir: Path | None, output_json: bool) -> None:
    """List runs with an anchored work ledger in this repository."""
    root = (workdir or Path.cwd()).resolve()
    try:
        runs = list_ledger_runs(root)
    except LedgerGitError as exc:
        console.print(f"[red]Failed to list ledger refs:[/red] {exc}")
        raise SystemExit(EXIT_VERIFY_FAILED) from None
    if output_json:
        console.print_json(json.dumps({"runs": runs}))
        return
    if not runs:
        console.print("[dim]No anchored work ledgers in this repository.[/dim]")
        return
    for run_id in runs:
        console.print(f"  {run_id}")


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


@ledger_group.command("gc")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def ledger_gc_cmd(run_id: str, workdir: Path | None, output_json: bool) -> None:
    """Squash the run's anchor history to a single commit.

    Bounds repository growth for long runs: superseded chunk blobs become
    unreachable and a normal ``git gc`` reclaims them. The current anchored
    tree (the verifiable identity) is preserved byte for byte.

    \b
    Exit codes:
        0  history squashed (or already minimal)
        1  no anchored ledger for this run
    """
    root = (workdir or Path.cwd()).resolve()
    try:
        result = gc_ledger_ref(root, run_id)
    except LedgerGitError as exc:
        console.print(f"[red]gc failed:[/red] {exc}")
        raise SystemExit(EXIT_NO_LEDGER) from None
    if output_json:
        console.print_json(json.dumps({"run_id": run_id, **result.to_dict()}))
    else:
        console.print(
            f"[green]gc done:[/green] run {run_id}, dropped {result.dropped_commits} "
            f"anchor commit(s); ref now at {result.commit_sha[:12]}"
        )


__all__ = [
    "EXIT_DIVERGED",
    "EXIT_NO_LEDGER",
    "EXIT_OK",
    "EXIT_VERIFY_FAILED",
    "ledger_group",
]
