"""``bernstein worktrees`` - inspect and reap orphan worktrees.

Two subcommands::

    bernstein worktrees list           # tabular dump of every worktree
    bernstein worktrees gc [--yes] [--dry]

The classifier in :mod:`bernstein.core.worktrees.classifier` is the
source of truth for state. This module only handles I/O: rendering the
table, holding the GC lock, prompting the operator, and emitting the
``worktree.gc`` lifecycle event for plugins.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.table import Table

from bernstein.core.process_utils import is_process_alive
from bernstein.core.worktrees.classifier import (
    GC_LOCK_RELPATH,
    WORKTREE_GC_LIFECYCLE_EVENT,
    WORKTREE_REAP_EVENT,
    ClassifiedWorktree,
    WorktreeState,
    classify_worktrees,
    format_size,
    reap_worktree,
    worktree_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)

#: Actor recorded on every ``worktree.reap`` audit event - the GC surface.
_AUDIT_ACTOR = "worktrees-gc"

__all__ = ["format_age", "lock_gc", "render_worktrees_table", "worktrees_group"]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


_STATE_STYLE: dict[WorktreeState, str] = {
    WorktreeState.ACTIVE: "green",
    WorktreeState.ORPHAN: "yellow",
    WorktreeState.STALE: "red",
    WorktreeState.CORRUPT: "magenta",
}


def format_age(seconds: float) -> str:
    """Render a wall-clock duration as ``"3d 04h"`` / ``"5m"`` / ``"42s"``."""
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86_400:
        hours = secs // 3600
        mins = (secs % 3600) // 60
        return f"{hours}h {mins:02d}m"
    days = secs // 86_400
    hours = (secs % 86_400) // 3600
    return f"{days}d {hours:02d}h"


def render_worktrees_table(rows: Iterable[ClassifiedWorktree]) -> Table:
    """Build a Rich table for ``bernstein worktrees list``."""
    table = Table(title="Bernstein worktrees", header_style="bold cyan")
    table.add_column("Path", overflow="fold", no_wrap=False)
    table.add_column("Task")
    table.add_column("State")
    table.add_column("Age", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("PID", justify="right")

    for row in rows:
        style = _STATE_STYLE.get(row.state, "white")
        task_display = row.task_id[:12] if row.task_id else "-"
        if row.pid is None:
            pid_display = "-"
        elif row.pid_alive:
            pid_display = str(row.pid)
        else:
            pid_display = f"{row.pid}✗"
        table.add_row(
            str(row.path),
            task_display,
            f"[{style}]{row.state.value}[/{style}]",
            format_age(row.age_seconds),
            format_size(row.size_bytes),
            pid_display,
        )
    return table


def _rows_to_json(rows: Iterable[ClassifiedWorktree]) -> list[dict[str, object]]:
    return [
        {
            "path": str(r.path),
            "session_id": r.session_id,
            "task_id": r.task_id,
            "state": r.state.value,
            "age_seconds": int(r.age_seconds),
            "size_bytes": r.size_bytes,
            "pid": r.pid,
            "pid_alive": r.pid_alive,
            "last_trace_mtime": r.last_trace_mtime,
            "reapable": r.is_reapable,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class GcLockError(RuntimeError):
    """Raised when the GC lock cannot be acquired."""


# A GC that has "owned" the lock longer than this is treated as a crashed
# leftover. Generous so a legitimately long sweep is never reclaimed under it.
_GC_LOCK_MAX_AGE_S = 6 * 3600


def _read_gc_lock(lock_path: Path) -> dict[str, object] | None:
    """Return the lock's recorded ``{pid, started_at}`` payload, or None."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _gc_lock_is_stale(meta: dict[str, object] | None) -> bool:
    """True when the lock's owning process is gone or the lock is too old.

    An unreadable / mid-write payload is NOT treated as stale, so a lock another
    process just created (between ``O_EXCL`` and the write) is never reclaimed.
    The crashed-GC case the recovery targets always leaves a fully written
    ``{pid, started_at}`` payload, which the pid-liveness check below detects.
    """
    if not isinstance(meta, dict):
        return False
    pid = meta.get("pid")
    if isinstance(pid, int) and pid > 0 and not is_process_alive(pid):
        return True
    started = meta.get("started_at")
    return isinstance(started, (int, float)) and (time.time() - started) > _GC_LOCK_MAX_AGE_S


def _acquire_gc_lock_fd(lock_path: Path) -> int:
    """Open the GC lock with ``O_EXCL``, reclaiming a provably stale lock once."""
    try:
        return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        meta = _read_gc_lock(lock_path)
        if not _gc_lock_is_stale(meta):
            owner = f" held by pid {meta['pid']}" if isinstance(meta, dict) and meta.get("pid") else ""
            raise GcLockError(f"another worktree GC is already running ({lock_path}{owner})") from exc
        logger.warning("Reclaiming stale worktree GC lock %s (previous owner gone): %s", lock_path, meta)
        # A competing acquirer may win the race and re-create the lock; the
        # retry below resolves that case.
        with contextlib.suppress(OSError):
            lock_path.unlink()
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as retry_exc:
            raise GcLockError(f"another worktree GC is already running ({lock_path})") from retry_exc


@contextlib.contextmanager
def lock_gc(repo_root: Path):  # type: ignore[no-untyped-def]
    """Acquire :data:`GC_LOCK_RELPATH` exclusively, yielding the lock path.

    The lock file is created with ``O_EXCL``; a concurrent invocation by a live
    process raises :class:`GcLockError`. A lock left behind by a crashed or
    killed GC (its recorded pid is no longer alive, or it is older than
    :data:`_GC_LOCK_MAX_AGE_S`) is reclaimed once instead of wedging every future
    run. The lock is removed on context exit even when the body raises.
    """
    lock_path = repo_root / GC_LOCK_RELPATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = _acquire_gc_lock_fd(lock_path)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            payload = {"pid": os.getpid(), "started_at": time.time()}
            fh.write(json.dumps(payload))
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except OSError:
            logger.debug("lock release: %s already removed", lock_path)


# ---------------------------------------------------------------------------
# Lifecycle event helper
# ---------------------------------------------------------------------------


def _emit_worktree_gc(repo_root: Path, row: ClassifiedWorktree, dry_run: bool, *, reaped: bool = True) -> None:
    """Notify plugins that ``row`` was reaped (or preserved for safety).

    We import lazily so importing this CLI module never drags in pluggy
    when the operator only ran ``--help``.

    Args:
        reaped: ``True`` for an actual reap, ``False`` for a safety-skip so
            subscribers can distinguish a deletion from a preserved worktree.
    """
    try:
        from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleContext, LifecycleEvent
    except Exception:
        return

    registry = _shared_registry()
    if registry is None:
        return

    # ``LifecycleEvent`` is a closed StrEnum and we deliberately do not
    # add a new entry to avoid rippling through the notify bridge. We
    # piggyback on ``POST_ARCHIVE`` semantically (lifecycle-end) and pass
    # the canonical event id via the ``env`` payload.
    ctx = LifecycleContext(
        event=LifecycleEvent.POST_ARCHIVE,
        task=row.task_id,
        session_id=row.session_id,
        workdir=repo_root,
        env={
            "BERNSTEIN_WORKTREE_GC_EVENT": WORKTREE_GC_LIFECYCLE_EVENT,
            "BERNSTEIN_WORKTREE_GC_STATE": row.state.value,
            "BERNSTEIN_WORKTREE_GC_PATH": str(row.path),
            "BERNSTEIN_WORKTREE_GC_DRY_RUN": "1" if dry_run else "0",
            "BERNSTEIN_WORKTREE_GC_REAPED": "1" if reaped else "0",
            "BERNSTEIN_WORKTREE_GC_UNSAVED": "1" if row.has_unsaved_work else "0",
        },
    )
    try:
        # ``HookRegistry`` may not implement an event-name-string overload;
        # the shared registry uses canonical enum events. We call the
        # standard fire path so any plugin that subscribes to
        # ``post_archive`` sees the env payload above and can filter by
        # ``BERNSTEIN_WORKTREE_GC_EVENT``.
        registry.run(LifecycleEvent.POST_ARCHIVE, ctx)
    except Exception as exc:
        logger.debug("lifecycle emit failed: %s", exc)
    _ = HookRegistry  # silence unused import warning when registry is None


def _shared_registry():  # type: ignore[no-untyped-def]
    """Return the process-wide :class:`HookRegistry`, if one is installed.

    Bernstein bootstrap stashes a singleton on a module-level attribute.
    The lookup is intentionally defensive - running the CLI as a
    standalone script should not require the orchestrator to be alive.
    """
    try:
        from bernstein.core.lifecycle import hooks as _hooks_mod
    except Exception:
        return None
    return getattr(_hooks_mod, "GLOBAL_REGISTRY", None)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@click.group("worktrees")
def worktrees_group() -> None:
    """Inspect and reap Bernstein agent worktrees."""


@worktrees_group.command("list")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def list_cmd(workdir: Path, as_json: bool) -> None:
    """List every Bernstein worktree and its state."""
    repo_root = workdir.resolve()
    rows = classify_worktrees(repo_root)
    if as_json:
        click.echo(json.dumps(_rows_to_json(rows), indent=2, default=str))
        return

    console = Console()
    if not rows:
        console.print(f"[dim]No Bernstein worktrees found under {repo_root}/.sdd/.[/dim]")
        return
    console.print(render_worktrees_table(rows))
    reapable = sum(1 for r in rows if r.is_reapable)
    if reapable:
        console.print(
            f"[yellow]{reapable} worktree(s) reapable - run [bold]bernstein worktrees gc[/bold] to clean up.[/yellow]"
        )


@worktrees_group.command("gc")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option(
    "--dry",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be deleted without touching disk.",
)
@click.option(
    "--force-unsaved",
    is_flag=True,
    default=False,
    help=(
        "Also reap worktrees that hold UNSAVED work (a dirty working tree or "
        "commits not merged into the integration branch). Dangerous: this "
        "destroys the only copy of that work. Requires an extra confirmation."
    ),
)
def gc_cmd(workdir: Path, yes: bool, dry_run: bool, force_unsaved: bool) -> None:
    """Delete orphan, stale, and corrupt worktrees.

    Worktrees that still hold unsaved work (uncommitted changes or unmerged
    commits) are preserved and reported as safety-skips unless
    ``--force-unsaved`` is passed. The safety decision - reap or skip - is
    recorded in the HMAC-chained audit log either way, so a skip is never
    silent.
    """
    repo_root = workdir.resolve()
    rows = classify_worktrees(repo_root)
    reapable = [r for r in rows if r.is_reapable]
    # Terminal-state worktrees the classifier vetoed because they hold
    # unsaved work. These never enter ``reapable`` (``is_reapable`` is False).
    unsaved = [r for r in rows if r.has_unsaved_work and r.state is not WorktreeState.ACTIVE]

    console = Console()
    if not reapable and not unsaved:
        console.print("[green]No reapable worktrees - nothing to do.[/green]")
        return

    targets = reapable.copy()
    if force_unsaved:
        targets.extend(unsaved)

    if reapable:
        console.print(render_worktrees_table(reapable))
    if unsaved and not force_unsaved:
        _report_safety_skips(console, unsaved)
    if unsaved and force_unsaved:
        console.print(render_worktrees_table(unsaved))

    if not targets:
        # Everything reapable was actually an unsaved worktree we are
        # preserving; record the skips for audit and stop.
        try:
            run_gc(repo_root, [], dry_run=dry_run, skipped=unsaved)
        except GcLockError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(2) from exc
        return

    if not yes and not dry_run:
        if not click.confirm(f"Reap {len(targets)} worktree(s)?", default=False):
            click.echo("Aborted.")
            raise SystemExit(1)
        if (
            force_unsaved
            and unsaved
            and not click.confirm(
                f"--force-unsaved will DESTROY unsaved work in {len(unsaved)} worktree(s). Continue?",
                default=False,
            )
        ):
            click.echo("Aborted.")
            raise SystemExit(1)

    try:
        run_gc(
            repo_root,
            targets,
            dry_run=dry_run,
            force_unsaved=force_unsaved,
            skipped=[] if force_unsaved else unsaved,
            on_progress=lambda row, removed: _print_reap_progress(console, row, removed, dry_run=dry_run),
        )
    except GcLockError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc


def _report_safety_skips(console: Console, skipped: list[ClassifiedWorktree]) -> None:
    """Print an operator-visible line per worktree preserved for safety."""
    console.print(
        f"[yellow]Preserving {len(skipped)} worktree(s) with unsaved work "
        f"(pass [bold]--force-unsaved[/bold] to override):[/yellow]"
    )
    for row in skipped:
        console.print(f"[yellow]  skipped[/yellow] {row.path} ({row.state.value}) - holds unsaved work")


def _print_reap_progress(
    console: Console,
    row: ClassifiedWorktree,
    removed: bool,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        verb = "Would remove"
    elif removed:
        verb = "Removed"
    else:
        verb = "Skipped"
    console.print(f"[dim]{verb}[/dim] {row.path} ({row.state.value})")


def run_gc(
    repo_root: Path,
    rows: list[ClassifiedWorktree],
    *,
    dry_run: bool,
    force_unsaved: bool = False,
    skipped: list[ClassifiedWorktree] | None = None,
    on_progress: Callable[[ClassifiedWorktree, bool], None] | None = None,
    audit_log: AuditLog | None = None,
) -> int:
    """Reap ``rows`` under the GC lock, anchoring each reap to the audit chain.

    For every row, in order, *inside the GC lock*:

    1. Capture a pre-deletion fingerprint (git HEAD sha + dirty flag) while
       the worktree still exists.
    2. Append one ``worktree.reap`` event to the HMAC-chained audit log
       (issue #1833). This is **fail-closed**: if the append raises (e.g.
       audit key permission error, full disk) the exception propagates and
       the worktree is *not* reaped - we never destroy a worktree we could
       not record. The audit write does not depend on any plugin
       ``HookRegistry``; the lifecycle notification below is separate and
       best-effort.
    3. Reap the directory (skipped in ``--dry`` mode).
    4. Fire the best-effort lifecycle event for plugins.

    Worktrees in ``skipped`` are NOT deleted; instead each gets one
    ``worktree.reap`` event flagged ``reaped=false`` with the unsaved-work
    reason, so a safety-skip is recorded in the same forensic chain as a
    real reap rather than being silent (issue #1847).

    Args:
        repo_root: Absolute repository root.
        rows: Reapable classifier rows to process. When ``force_unsaved`` is
            set this may include rows whose ``has_unsaved_work`` is ``True``.
        dry_run: When ``True``, record the event flagged ``dry_run=true``
            and perform no filesystem mutation.
        force_unsaved: When ``True``, the operator explicitly opted in to
            destroying unsaved work; recorded as ``forced=true`` on any
            reaped row that held unsaved work.
        skipped: Worktrees preserved for safety (unsaved work, no force).
            Recorded as ``reaped=false`` and never deleted.
        on_progress: Optional per-row progress callback ``(row, removed)``.
        audit_log: Optional pre-opened :class:`AuditLog` (used by tests to
            inject a fixed key). When ``None`` a project log rooted at
            ``<repo_root>/.sdd/audit`` is opened once for the whole sweep.

    Returns:
        The number of worktrees actually removed (always 0 in ``--dry``
        mode after the lock work completes).
    """
    removed_count = 0
    with lock_gc(repo_root):
        log = audit_log if audit_log is not None else _open_audit_log(repo_root)
        # Record safety-skips first so the audit chain shows what was
        # deliberately preserved before any destruction in this sweep.
        for row in skipped or []:
            _append_reap_event(log, row, dry_run=dry_run, reaped=False, forced=False)
            _emit_worktree_gc(repo_root, row, dry_run, reaped=False)
        for row in rows:
            # 1-2: fingerprint then record BEFORE any destruction. A raised
            # exception here aborts the sweep with the worktree intact.
            _append_reap_event(log, row, dry_run=dry_run, reaped=True, forced=force_unsaved)
            # 3: only now is it safe to destroy.
            removed = reap_worktree(repo_root, row, dry_run=dry_run)
            if on_progress is not None:
                on_progress(row, removed)
            if removed and not dry_run:
                removed_count += 1
            # 4: best-effort plugin notification (independent of the audit).
            _emit_worktree_gc(repo_root, row, dry_run, reaped=True)
    return removed_count


def _open_audit_log(repo_root: Path) -> AuditLog:
    """Open the project HMAC audit log rooted at ``<repo_root>/.sdd/audit``.

    Imported lazily so ``bernstein worktrees --help`` never drags in the
    security/audit module (and its key resolution) unnecessarily.
    """
    from bernstein.core.security.audit import AuditLog

    return AuditLog(audit_dir=repo_root / ".sdd" / "audit")


def _append_reap_event(
    log: AuditLog,
    row: ClassifiedWorktree,
    *,
    dry_run: bool,
    reaped: bool,
    forced: bool,
) -> None:
    """Append one ``worktree.reap`` event capturing the pre-deletion state.

    The fingerprint (git HEAD sha + dirty flag) is captured here, before
    the caller reaps the directory. The ``details`` payload is restricted
    to the fields the issue enumerates so the daily JSONL does not bloat.

    Args:
        log: Open HMAC-chained audit log.
        row: Classifier row being reaped or preserved.
        dry_run: Whether this is a ``--dry`` sweep (no filesystem mutation).
        reaped: ``True`` when the directory is being deleted; ``False`` for
            a safety-skip (unsaved work preserved without ``--force-unsaved``).
        forced: ``True`` when ``--force-unsaved`` overrode the unsaved-work
            veto for this reap.

    Fail-closed: any exception raised by :meth:`AuditLog.log` propagates to
    the caller, which then skips the reap.
    """
    fingerprint = worktree_fingerprint(row.path)
    log.log(
        event_type=WORKTREE_REAP_EVENT,
        actor=_AUDIT_ACTOR,
        resource_type="worktree",
        resource_id=row.session_id,
        details={
            "state": row.state.value,
            "task_id": row.task_id,
            "path": str(row.path),
            "size_bytes": row.size_bytes,
            "age_seconds": int(row.age_seconds),
            "last_trace_mtime": row.last_trace_mtime,
            "head_sha": fingerprint.head_sha,
            "dirty": fingerprint.dirty,
            "has_unsaved_work": row.has_unsaved_work,
            "reaped": reaped,
            "forced": forced,
            "dry_run": dry_run,
        },
    )


# ---------------------------------------------------------------------------
# Lock inspection + recovery (`worktrees unlock`)
# ---------------------------------------------------------------------------

#: Audit event-type appended to the HMAC-chained log when an operator clears
#: a stuck GC lock, so a recovery is forensically reconstructable rather than a
#: silent file deletion.
_WORKTREE_GC_UNLOCK_EVENT = "worktree.gc_unlock"


def _gc_lock_status(repo_root: Path) -> dict[str, object]:
    """Return the current GC lock status without mutating it."""
    lock_path = repo_root / GC_LOCK_RELPATH
    if not lock_path.exists():
        return {"held": False}
    meta = _read_gc_lock(lock_path)
    pid = meta.get("pid") if isinstance(meta, dict) else None
    started = meta.get("started_at") if isinstance(meta, dict) else None
    age: float | None = None
    if isinstance(started, (int, float)):
        age = max(0.0, time.time() - started)
    alive: bool | None = None
    if isinstance(pid, int) and pid > 0:
        alive = is_process_alive(pid)
    return {
        "held": True,
        "path": str(lock_path),
        "pid": pid,
        "alive": alive,
        "age_seconds": age,
        "stale": _gc_lock_is_stale(meta),
        "readable": isinstance(meta, dict),
    }


def _append_gc_unlock_event(repo_root: Path, status: dict[str, object], *, forced: bool) -> None:
    """Record the unlock on the HMAC-chained audit log.

    Unlike a reap (which destroys work and is fail-closed), clearing a lock
    file destroys nothing, so the caller treats a failed append as a warning
    and still recovers: refusing to clear the lock when the audit log is
    unavailable would recreate the very wedge this command exists to fix.
    """
    age = status.get("age_seconds")
    log = _open_audit_log(repo_root)
    log.log(
        event_type=_WORKTREE_GC_UNLOCK_EVENT,
        actor=_AUDIT_ACTOR,
        resource_type="worktree_gc_lock",
        resource_id=str(repo_root / GC_LOCK_RELPATH),
        details={
            "previous_pid": status.get("pid"),
            "previous_owner_alive": status.get("alive"),
            "age_seconds": int(age) if isinstance(age, (int, float)) else None,
            "stale": status.get("stale"),
            "forced": forced,
        },
    )


@worktrees_group.command("unlock")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Remove the lock even when its owner process still looks alive.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def unlock_cmd(workdir: Path, force: bool, as_json: bool) -> None:
    """Inspect and clear a stuck worktree GC lock.

    ``worktrees gc`` takes an exclusive lock; a crashed or killed run can leave
    it behind and wedge every later gc. This reports who holds the lock and
    removes it when the owner process is gone (or with ``--force``), recording
    the unlock as a tamper-evident ``worktree.gc_unlock`` audit event so the
    recovery is never silent. A lock held by a live, recent process is refused
    unless ``--force`` is passed.
    """
    repo_root = workdir.resolve()
    status = _gc_lock_status(repo_root)
    console = Console()

    if not status["held"]:
        if as_json:
            click.echo(json.dumps({"held": False, "removed": False}, default=str))
        else:
            console.print("[green]No worktree GC lock is held.[/green]")
        return

    removable = bool(status["stale"]) or force
    removed = False
    if removable:
        forced_live = force and not status["stale"]
        try:
            _append_gc_unlock_event(repo_root, status, forced=forced_live)
        except Exception as exc:  # best-effort: never wedge recovery on audit failure
            logger.warning("worktree.gc_unlock audit append failed (continuing): %s", exc)
        lock_path = repo_root / GC_LOCK_RELPATH
        with contextlib.suppress(OSError):
            lock_path.unlink()
        removed = not lock_path.exists()

    pid = status.get("pid")
    alive = status.get("alive")
    age = status.get("age_seconds")
    age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "unknown age"
    owner = f"pid {pid}" if pid else "an unknown process"
    liveness = {True: "alive", False: "not running", None: "liveness unknown"}[alive]

    if as_json:
        click.echo(json.dumps(status | {"removed": removed}, default=str))
        if not removable:
            raise SystemExit(1)
        return

    if removed:
        console.print(f"[green]Cleared worktree GC lock[/green] (previous owner {owner}, {liveness}, {age_str}).")
        console.print("[dim]Recorded as a worktree.gc_unlock audit event.[/dim]")
    elif removable:
        console.print("[yellow]GC lock was already gone.[/yellow]")
    else:
        console.print(
            f"[red]A worktree GC appears to be running ({owner}, {liveness}, {age_str}).[/red]\n"
            "Pass --force only if you are sure that process is dead."
        )
        raise SystemExit(1)
