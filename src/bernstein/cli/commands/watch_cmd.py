"""Watch command — monitor file changes and re-run affected tasks."""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console, server_get

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".sdd",
        ".git",
        "__pycache__",
        "node_modules",
        ".tox",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".eggs",
    }
)

_DEBOUNCE_SECS: float = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_ignored(path: Path, workdir: Path) -> bool:
    """Return True if *path* is inside a directory that should never trigger a re-run.

    Args:
        path: Absolute path of the changed file.
        workdir: Root directory being watched.

    Returns:
        True when the path is under an ignored top-level directory.
    """
    try:
        rel = path.relative_to(workdir)
    except ValueError:
        return False
    return bool(rel.parts and rel.parts[0] in _IGNORED_DIRS)


def _matches_glob(path: Path, workdir: Path, glob_pattern: str | None) -> bool:
    """Return True if *path* matches *glob_pattern* (or no pattern was given).

    Args:
        path: Absolute path of the changed file.
        workdir: Root directory being watched.
        glob_pattern: Optional glob string, e.g. ``"src/**/*.py"``.

    Returns:
        True when the path is accepted by the pattern filter.
    """
    if not glob_pattern:
        return True
    try:
        rel = str(path.relative_to(workdir))
    except ValueError:
        rel = str(path)
    return fnmatch.fnmatch(rel, glob_pattern)


def _query_open_tasks() -> list[dict[str, Any]]:
    """Fetch open tasks from the task server.

    Returns:
        List of task dicts, or an empty list when the server is unreachable.
    """
    data: Any = server_get("/tasks?status=open")
    if isinstance(data, list):
        return data  # type: ignore[return-value]
    return []


def _find_affected_tasks(changed_path: Path, workdir: Path, open_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the subset of *open_tasks* that mention the changed file.

    Matching is done by checking whether the relative path or bare filename
    appears in a task's title or description.

    Args:
        changed_path: Absolute path of the file that changed.
        workdir: Root directory being watched.
        open_tasks: All currently open tasks from the server.

    Returns:
        Tasks that reference the changed file; the full list when none match.
    """
    try:
        rel_str = str(changed_path.relative_to(workdir))
    except ValueError:
        rel_str = str(changed_path)

    filename = changed_path.name
    matched: list[dict[str, Any]] = []
    for task in open_tasks:
        title: str = task.get("title") or ""
        description: str = task.get("description") or ""
        haystack = f"{title} {description}"
        if rel_str in haystack or filename in haystack:
            matched.append(task)

    return matched if matched else open_tasks


# ---------------------------------------------------------------------------
# Change handler with debounce
# ---------------------------------------------------------------------------


class _DebounceHandler:
    """Collect filesystem events and fire a single callback after *debounce_secs*.

    Args:
        workdir: Root directory being watched.
        glob_pattern: Optional glob pattern to restrict accepted files.
        on_changes: Callback invoked with the set of changed paths.
        debounce_secs: Seconds to wait after the last event before firing.
    """

    def __init__(
        self,
        workdir: Path,
        glob_pattern: str | None,
        on_changes: Any,
        debounce_secs: float = _DEBOUNCE_SECS,
    ) -> None:
        self._workdir = workdir
        self._glob_pattern = glob_pattern
        self._on_changes = on_changes
        self._debounce_secs = debounce_secs
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending: set[Path] = set()

    def push(self, path: Path) -> None:
        """Register a filesystem change; reset the debounce window.

        Args:
            path: Absolute path of the changed file.
        """
        if _is_ignored(path, self._workdir):
            return
        if not _matches_glob(path, self._workdir, self._glob_pattern):
            return

        with self._lock:
            self._pending.add(path)
            if self._timer is not None:
                self._timer.cancel()
            t = threading.Timer(self._debounce_secs, self._fire)
            t.daemon = True
            self._timer = t
            t.start()

    def _fire(self) -> None:
        """Drain the pending set and invoke the callback."""
        with self._lock:
            paths = set(self._pending)
            self._pending.clear()
            self._timer = None
        if paths:
            self._on_changes(paths)


# ---------------------------------------------------------------------------
# Re-run logic
# ---------------------------------------------------------------------------


def _trigger_rerun(affected: list[dict[str, Any]]) -> None:
    """Spawn a ``bernstein run --auto-approve`` subprocess to process open tasks.

    Args:
        affected: Tasks that are expected to be re-run (used only for display).
    """
    if not affected:
        console.print("  [dim]No open tasks to re-run.[/dim]")
        return

    cmd = [sys.executable, "-m", "bernstein", "run", "--auto-approve"]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        console.print(f"  [yellow]Warning: could not spawn re-run: {exc}[/yellow]")


def _handle_changes(changed_paths: set[Path], workdir: Path) -> None:
    """React to a batch of debounced file-change events.

    Prints a summary line per changed file, queries the task server for open
    tasks, finds which are affected, and triggers a re-run.

    Args:
        changed_paths: Set of absolute paths that changed in the last window.
        workdir: Root directory being watched.
    """
    open_tasks = _query_open_tasks()

    for path in sorted(changed_paths):
        try:
            rel = path.relative_to(workdir)
        except ValueError:
            rel = path  # type: ignore[assignment]

        affected = _find_affected_tasks(path, workdir, open_tasks)

        if not open_tasks:
            task_info = "[dim]no open tasks[/dim]"
        elif len(affected) == len(open_tasks):
            task_info = f"re-running [bold]{len(affected)}[/bold] open task{'s' if len(affected) != 1 else ''}"
        else:
            ids = ", ".join(f"#{(t.get('id') or '')[:8]}" for t in affected[:3])
            suffix = f" +{len(affected) - 3} more" if len(affected) > 3 else ""
            task_info = f"re-running task {ids}{suffix}"

        console.print(f"[cyan]Detected change in[/cyan] {rel} [dim]→[/dim] {task_info}")

    _trigger_rerun(open_tasks)


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("watch")
@click.option(
    "--glob",
    "glob_pattern",
    default=None,
    metavar="PATTERN",
    help="Restrict watching to files matching this glob (e.g. 'src/**/*.py').",
)
@click.argument("directory", default=".", type=click.Path(exists=True, file_okay=False))
def watch_cmd(glob_pattern: str | None, directory: str) -> None:
    """Monitor the working directory and re-run tasks on file changes.

    \b
    Watches for source file changes and automatically re-runs affected
    Bernstein tasks.  Changes inside .sdd/, .git/, __pycache__/, and
    node_modules/ are ignored.  A 2-second debounce prevents rapid
    successive saves from triggering multiple runs.

    \b
      bernstein watch                         # watch current directory
      bernstein watch src/                    # watch a subdirectory
      bernstein watch --glob "src/**/*.py"    # only Python source files
    """
    try:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError as exc:
        console.print(
            "[red]Error:[/red] 'watchdog' is not installed.\nInstall it with: [bold]pip install watchdog[/bold]"
        )
        raise SystemExit(1) from exc

    workdir = Path(directory).resolve()

    def _on_changes(paths: set[Path]) -> None:
        _handle_changes(paths, workdir)

    handler_wrapper = _DebounceHandler(
        workdir=workdir,
        glob_pattern=glob_pattern,
        on_changes=_on_changes,
    )

    class _Handler(FileSystemEventHandler):
        def _push_if_file(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                handler_wrapper.push(Path(str(event.src_path)))

        on_modified = _push_if_file
        on_created = _push_if_file
        on_deleted = _push_if_file

        def on_moved(self, event: FileSystemEvent) -> None:
            # dest_path exists on MoveEvent subtype; fall back gracefully
            dest = getattr(event, "dest_path", None)
            if not event.is_directory and dest:
                handler_wrapper.push(Path(str(dest)))

    glob_hint = f" [dim](filter: {glob_pattern})[/dim]" if glob_pattern else ""
    console.print(f"[bold green]Watching for changes…[/bold green]  {workdir}{glob_hint}")
    console.print("[dim]Press Ctrl+C to exit.[/dim]")

    observer = Observer()
    observer.schedule(_Handler(), str(workdir), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        console.print("\n[dim]Watch stopped.[/dim]")
