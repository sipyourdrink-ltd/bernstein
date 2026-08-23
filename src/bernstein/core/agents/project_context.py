"""Project-context resolution for spawned agents.

A repository may carry a top-level ``.sdd/project.md`` plus subtree-scoped
ones. An agent working inside a subtree should see that subtree's file rather
than only the root's, so the lookup walks up from the files a task owns.

This lives in its own module because both ``spawn_prompt`` and
``spawner_core`` build agent prompts and need the identical answer; keeping
one copy is what stops the two from drifting apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import Task

# mtime-keyed, so an edit during a run is picked up without a restart.
_FILE_CACHE: dict[str, tuple[float, str]] = {}


def read_cached(path: Path) -> str:
    """Return file contents, re-reading only when mtime changes.

    Args:
        path: File to read.

    Returns:
        File contents, or empty string if the file does not exist.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _FILE_CACHE.pop(key, None)
        return ""
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    content = path.read_text(encoding="utf-8")
    _FILE_CACHE[key] = (mtime, content)
    return content


def _scoped_start(root: Path, owned: str) -> Path | None:
    """Return the directory to start the upward walk from, or None.

    ``root / owned`` is not enough on its own: an absolute ``owned`` makes
    that expression return ``owned`` unchanged, and ``..`` segments or a
    symlink can climb out of the project the same way. A start directory
    outside ``root`` would never reach it by taking ``.parent``, so the
    caller's walk would run to the filesystem root and spin there forever.
    Rejecting those paths here is what makes the walk terminate.
    """
    try:
        start = (root / owned).resolve().parent
    except OSError:
        return None
    return start if start.is_relative_to(root) else None


def resolve_project_context(tasks: list[Task], workdir: Path) -> str:
    """Resolve project context, preferring the nearest subtree-scoped file.

    Walks up from each task's owned files looking for a scoped
    ``.sdd/project.md``; the nearest ancestor wins. When a scoped file is
    found it supplements the top-level file (scoped content first). When none
    is found, only the top-level file is returned (byte-identical to the
    pre-scoping behaviour).

    A batch owning files in several subtrees resolves to the first scoped
    file found, in task then owned-file order — one agent gets one project
    context, and a batch spanning subtrees has no single right answer to
    concatenate.

    Args:
        tasks: Batch of tasks whose owned files scope the lookup.
        workdir: Project working directory.

    Returns:
        Combined project context string.
    """
    root = workdir.resolve()
    top_level = read_cached(workdir / ".sdd" / "project.md")

    scoped: str | None = None
    for t in tasks:
        for f in t.owned_files:
            d = _scoped_start(root, f)
            if d is None:
                continue
            # Terminates: d is inside root, and .parent strictly ascends.
            while d != root:
                content = read_cached(d / ".sdd" / "project.md")
                if content:
                    scoped = content
                    break
                d = d.parent
            if scoped is not None:
                break
        if scoped is not None:
            break

    if scoped is None:
        return top_level
    if top_level:
        return scoped + "\n\n" + top_level
    return scoped
