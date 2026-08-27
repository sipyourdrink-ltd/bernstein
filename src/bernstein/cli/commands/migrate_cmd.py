"""``bernstein migrate`` - fan out a deterministic file-level migration.

Wraps :mod:`bernstein.core.tasks.swarm_migration` so an operator can
kick off a swarm without writing Python.  Tasks are created via the
running task server (``POST /tasks``); when the server is offline the
command exits non-zero with a clear message.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import SERVER_URL, console, server_get, server_post
from bernstein.core.tasks.lifecycle import (
    SUCCESSFUL_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    UNSUCCESSFUL_TERMINAL_STATUSES,
)
from bernstein.core.tasks.swarm_migration import (
    MigrationPlan,
    chunk_targets,
    effective_max_parallel,
    enumerate_targets,
    spawn_swarm,
)

_DRY_RUN_PREVIEW_LIMIT = 10

# A swarm chunk whose task reports one of these has stopped touching its files,
# so re-running the plan must respawn it rather than wait on it forever.
# Anything else - open, claimed, in progress, blocked, awaiting approval - may
# still be editing the chunk, so the plan reuses it rather than spawning a
# duplicate owner (issue #4624).
#
# Three lifecycle sets are unioned because each answers a different question and
# neither one alone covers every status that has stopped:
#
# * SUCCESSFUL/UNSUCCESSFUL_TERMINAL answer "will this dependency ever deliver".
#   DONE, FAILED and ORPHANED keep a recovery edge back to OPEN, so they are
#   absent from TERMINAL_TASK_STATUSES while having certainly stopped editing.
# * TERMINAL_TASK_STATUSES answers "can this status transition at all". SUSPENDED
#   is in it and in neither of the others: it has no outgoing transition, so a
#   chunk left in it would report active forever and never respawn - #4541's
#   "never return a dead id forever" bug, reintroduced through a status neither
#   dependency set names.
#
# A status absent from all three still defaults to "still active", which is the
# safe side of the duplicate-owner bug. test_terminal_status_values_covers_every
# _unresumable_status pins that gap shut for statuses added to only one set.
_TERMINAL_STATUS_VALUES = frozenset(
    s.value for s in (SUCCESSFUL_TASK_STATUSES | UNSUCCESSFUL_TERMINAL_STATUSES | TERMINAL_TASK_STATUSES)
)


class _ServerTaskStore:
    """Adapter that bridges :class:`spawn_swarm` to ``POST /tasks``."""

    def create_sync(self, body: dict[str, Any]) -> str:
        resp = server_post("/tasks", body)
        if resp is None:
            raise click.ClickException(f"Task server at {SERVER_URL} is unreachable; start `bernstein run` first.")
        task_id = resp.get("id")
        if not isinstance(task_id, str):
            raise click.ClickException(f"Task server returned no task id: {resp!r}")
        return task_id

    def is_task_active(self, task_id: str) -> bool:
        """Return ``True`` while the server still reports *task_id* non-terminal.

        A ``404`` or an unreachable server yields ``None`` from
        :func:`server_get`; both are treated as "not active" so the chunk
        respawns, matching the pre-#4624 behaviour when the task is genuinely
        gone. Only a live, non-terminal status suppresses the respawn.
        """
        resp = server_get(f"/tasks/{task_id}")
        if resp is None:
            return False
        status = resp.get("status")
        return isinstance(status, str) and status not in _TERMINAL_STATUS_VALUES


def _slugify(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in value.strip().lower())
    return safe.strip("-") or "swarm"


def _print_plan_summary(
    plan: MigrationPlan,
    targets: list[Path],
    chunks: list[list[Path]],
    cap: int,
) -> None:
    console.print(f"[bold]Migration plan:[/bold] {plan.id}")
    console.print(f"  glob:           {plan.glob}")
    console.print(f"  files:          {len(targets)}")
    console.print(f"  chunks:         {len(chunks)} (chunk_size={plan.chunk_size})")
    console.print(f"  max_parallel:   {cap} (configured {plan.max_parallel})")
    console.print(f"  role:           {plan.role}")


def _print_dry_run_preview(targets: list[Path], chunks: list[list[Path]]) -> None:
    console.print("\n[bold]Targets (preview):[/bold]")
    for p in targets[:_DRY_RUN_PREVIEW_LIMIT]:
        console.print(f"  - {p}")
    if len(targets) > _DRY_RUN_PREVIEW_LIMIT:
        console.print(f"  ... and {len(targets) - _DRY_RUN_PREVIEW_LIMIT} more")
    console.print("\n[bold]First chunk:[/bold]")
    if chunks:
        for p in chunks[0]:
            console.print(f"  - {p}")


@click.command("migrate")
@click.option("--glob", "glob_pattern", required=True, help="Repo-relative glob, e.g. 'src/**/*.py'.")
@click.option("--transform", "transform_prompt", required=True, help="Instruction passed to each subagent.")
@click.option("--id", "plan_id", default=None, help="Stable plan id used for idempotent re-runs.")
@click.option("--chunk-size", type=int, default=5, show_default=True, help="Files per subagent.")
@click.option("--max-parallel", type=int, default=20, show_default=True, help="Concurrent subagent cap.")
@click.option("--role", default="backend", show_default=True, help="Role for spawned tasks.")
@click.option(
    "--exclude",
    "excludes",
    multiple=True,
    help="Repo-relative glob to exclude (may be repeated).",
)
@click.option(
    "--repo-root",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=Path.cwd(),
    show_default="cwd",
    help="Repository root used to resolve --glob.",
)
@click.option("--dry-run", is_flag=True, help="Show enumerated chunks without spawning tasks.")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON instead of rich text.")
def migrate_cmd(
    glob_pattern: str,
    transform_prompt: str,
    plan_id: str | None,
    chunk_size: int,
    max_parallel: int,
    role: str,
    excludes: tuple[str, ...],
    repo_root: Path,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Spawn one task per chunk for a deterministic migration."""
    resolved_id = plan_id or _slugify(transform_prompt)[:48]
    plan = MigrationPlan(
        id=resolved_id,
        glob=glob_pattern,
        transform_prompt=transform_prompt,
        chunk_size=chunk_size,
        max_parallel=max_parallel,
        role=role,
        excludes=excludes,
    )
    repo_root = repo_root.resolve()
    targets = enumerate_targets(plan, repo_root)
    chunks = chunk_targets(targets, plan.chunk_size)
    cap = effective_max_parallel(plan, len(chunks))

    if not targets:
        msg = f"No files matched glob {glob_pattern!r} under {repo_root}"
        if json_output:
            console.print(json.dumps({"plan_id": plan.id, "spawned": [], "warning": msg}))
        else:
            console.print(f"[yellow]{msg}[/yellow]")
        return

    if dry_run:
        if json_output:
            console.print(
                json.dumps(
                    {
                        "plan": asdict(plan),
                        "files": [str(p) for p in targets],
                        "chunks": [[str(p) for p in c] for c in chunks],
                        "effective_max_parallel": cap,
                    },
                    indent=2,
                )
            )
            return
        _print_plan_summary(plan, targets, chunks, cap)
        _print_dry_run_preview(targets, chunks)
        console.print("\n[dim]Dry run: no tasks spawned.[/dim]")
        return

    store = _ServerTaskStore()
    spawned = spawn_swarm(plan, store, repo_root)
    if json_output:
        console.print(json.dumps({"plan_id": plan.id, "spawned": spawned, "chunks": len(chunks)}))
        return
    _print_plan_summary(plan, targets, chunks, cap)
    console.print(f"\n[green]Spawned {len(spawned)} task(s).[/green]")
    for tid in spawned:
        console.print(f"  - {tid}")
