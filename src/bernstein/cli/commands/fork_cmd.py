"""``bernstein fork --run <id> --from-step N`` - fork a run from a step (#2295).

Reads the parent run's canonical event journal, resumes the snapshot
commit recorded at step ``N`` into a fresh isolated worktree, and starts
a new run whose journal parent-links the fork point. Prints the fork
lineage (parent run, fork step, snapshot sha, new run id, worktree path).

Snapshot support is provided by the worktree sandbox backend; cloud
sandbox backends still raise ``NotImplementedError``, so the worktree is
the supported snapshot backend for fork-from-step.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.replay.fork import ForkError, fork_run


@click.command("fork")
@click.option("--run", "run_id", required=True, help="Parent run id to fork from.")
@click.option(
    "--from-step",
    "from_step",
    required=True,
    type=int,
    help="Journal step index to fork at. A snapshot must have been recorded there.",
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Repository root that owns the snapshot refs (defaults to current directory).",
)
@click.option(
    "--sdd-dir",
    "sdd_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Run state directory holding runs/<run_id>/journal.jsonl (defaults to <repo-root>/.sdd).",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Audit chain directory; when present the fork is HMAC-chain attested.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def fork_cmd(
    run_id: str,
    from_step: int,
    repo_root: Path | None,
    sdd_dir: Path | None,
    audit_dir: Path | None,
    as_json: bool,
) -> None:
    """Fork a run at a journal step into a new isolated run.

    \b
    Exit codes:
        0  fork created
        1  fork failed (no snapshot at step, tampered ref, or checkout error)
    """
    root = repo_root or Path.cwd()
    sdd = sdd_dir or (root / ".sdd")

    chain = None
    if audit_dir is not None and audit_dir.is_dir():
        from bernstein.core.security.audit_chain import AuditChainStore

        chain = AuditChainStore(audit_dir)

    try:
        result = fork_run(sdd, run_id, from_step=from_step, repo_root=root, chain=chain)
    except ForkError as exc:
        console.print(f"[red]Fork failed:[/red] {exc}")
        raise SystemExit(1) from None

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "new_run_id": result.new_run_id,
                    "parent_run_id": result.parent_run_id,
                    "from_step": result.from_step,
                    "snapshot_sha": result.snapshot_sha,
                    "worktree_path": result.worktree_path,
                    "child_head": result.child_head,
                }
            )
        )
        return

    console.print(f"[green]Forked[/green] [cyan]{result.parent_run_id}[/cyan] at step {result.from_step}")
    console.print(f"  new run:   [bold]{result.new_run_id}[/bold]")
    console.print(f"  snapshot:  {result.snapshot_sha[:12]} [dim]({result.snapshot_sha})[/dim]")
    console.print(f"  worktree:  {result.worktree_path}")
    console.print(f"  child head: {result.child_head}")


__all__ = ["fork_cmd"]
