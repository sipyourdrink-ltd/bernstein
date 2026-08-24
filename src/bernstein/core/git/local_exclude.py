"""Run-scoped git excludes for in-tree files a run has to write anyway.

Run configuration belongs in the untracked overlay
(:mod:`bernstein.core.config.run_overlay`), which lives inside the git
directory and therefore cannot reach a commit.  One file cannot follow that
rule.

**Why ``.claude/mcp.json`` is exempt.**  It is the bridge manifest Claude
Code reads to discover Bernstein's MCP tools, and Claude Code looks for it at
exactly one project-local path: ``<workdir>/.claude/mcp.json``.  The child
process is not ours to reconfigure, it accepts no path argument for this
file, and it resolves the path relative to the directory the operator opened
- which is the work tree.  So the file has to be written inside the work
tree, and the invariant has to be enforced a second way for it: the path is
registered in the repository's ``info/exclude`` for the duration of the run,
so an agent's broad ``git add -A`` cannot stage it and ``git commit -a``
cannot carry it.

``info/exclude`` is used rather than ``.gitignore`` because ``.gitignore`` is
itself a tracked file - writing the exclusion into the work tree would swap
one leaked configuration file for another.

**What this does not cover.**  Git ignore rules apply to untracked paths
only.  If a target repository already tracks ``.claude/mcp.json``, an exclude
entry has no effect on it, and the run-configuration commit gate
(:mod:`bernstein.core.quality.run_config_gate`) is what stops the change from
being published.  The two layers are deliberate: the exclude keeps the file
out of the index in the common case, the gate fails loudly in the case the
exclude cannot reach.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S: Final[int] = 10

#: Header written above the block this module manages, so an operator reading
#: ``info/exclude`` can tell where the entries came from.
EXCLUDE_BLOCK_HEADER: Final[str] = "# bernstein: run-scoped excludes (see core/git/local_exclude.py)"

#: In-tree paths a run must write for a child process to find, anchored to the
#: repository root with a leading ``/`` so they cannot shadow a same-named
#: path a target project legitimately keeps elsewhere.
RUN_EXCLUDE_ENTRIES: Final[tuple[str, ...]] = ("/.claude/mcp.json",)


def resolve_info_exclude_path(workdir: Path) -> Path | None:
    """Resolve the ``info/exclude`` file that applies to *workdir*.

    Uses ``git rev-parse --git-path`` rather than assuming
    ``workdir/.git/info/exclude``: inside a linked worktree ``.git`` is a
    file, and git reads ``info/exclude`` from the *common* git directory
    shared by every worktree of the clone, not from the per-worktree one.
    ``--git-path`` resolves both cases the way git itself does.

    Returns ``None`` - logged at debug level - when *workdir* is not inside a
    git repository or ``git`` is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not resolve git info/exclude for %s: %s", workdir, exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        logger.debug(
            "git rev-parse --git-path info/exclude failed for %s: %s",
            workdir,
            result.stderr.strip(),
        )
        return None
    exclude_path = Path(result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = workdir / exclude_path
    return exclude_path


def register_run_excludes(
    workdir: Path,
    entries: Iterable[str] = RUN_EXCLUDE_ENTRIES,
) -> tuple[str, ...]:
    """Idempotently add *entries* to *workdir*'s ``info/exclude``.

    Args:
        workdir: A directory inside the repository whose index must not pick
            up the run's in-tree files.
        entries: Exclude patterns, already anchored (leading ``/``).

    Returns:
        The entries this call actually appended - empty when they were all
        present already, or when the exclude file could not be resolved or
        written.  Registration is best-effort by design: it is a hardening
        layer in front of the commit gate, and failing to harden must never
        abort a run that the gate will still protect.
    """
    wanted = [entry for entry in entries if entry.strip()]
    if not wanted:
        return ()

    exclude_path = resolve_info_exclude_path(workdir)
    if exclude_path is None:
        return ()

    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    except OSError as exc:
        logger.debug("Cannot read git exclude file %s: %s", exclude_path, exc)
        return ()

    present = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in wanted if entry not in present]
    if not missing:
        return ()

    lines: list[str] = []
    if existing and not existing.endswith("\n"):
        lines.append("")
    if EXCLUDE_BLOCK_HEADER not in present:
        lines.append(EXCLUDE_BLOCK_HEADER)
    lines.extend(missing)

    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        logger.debug("Cannot append to git exclude file %s: %s", exclude_path, exc)
        return ()

    logger.debug("Registered run-scoped git excludes in %s: %s", exclude_path, ", ".join(missing))
    return tuple(missing)
