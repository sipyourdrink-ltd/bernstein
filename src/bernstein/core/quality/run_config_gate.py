"""Quality gate: a change must never contain run configuration.

A run reads the project's committed configuration and writes its own
overrides to an untracked overlay
(:mod:`bernstein.core.config.run_overlay`), so under normal operation no
commit can carry a configuration file.  This gate is the backstop for the
cases the overlay cannot reach on its own:

* a target repository that already **tracks** ``.claude/mcp.json`` - git
  ignore rules do not apply to tracked paths, so the run-scoped exclude in
  :mod:`bernstein.core.git.local_exclude` has no effect there;
* an agent that edits ``bernstein.yaml`` itself, believing it is doing the
  work it was asked to do;
* any future code path that writes a configuration file into the work tree
  before committing.

The check is a set membership test over the names git already reports for
the change, so it costs one ``git diff`` and no file reads.  It is cheap
enough to run on every commit the orchestrator makes, which is the point: a
compensating "restore the file before publishing" step only ever protected
the one publishing path that remembered to call it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.config.run_overlay import describe_overlay_remedy, find_run_config_paths
from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 15

#: Placeholder recorded when git could not be asked what the change contains.
UNREADABLE = "<diff-read-failed>"

PASS_DETAILS = "No run-configuration path in the change."


@dataclass(frozen=True)
class RunConfigGateResult:
    """Verdict of the run-configuration gate.

    Attributes:
        ok: True when the change contains no run-configuration path.
        offending_paths: The run-configuration paths found, in the order git
            reported them.  Holds :data:`UNREADABLE` when the diff could not
            be read at all.
        details: Message naming every offending file and how to fix it.
    """

    ok: bool
    offending_paths: tuple[str, ...]
    details: str


def _passed() -> RunConfigGateResult:
    return RunConfigGateResult(ok=True, offending_paths=(), details=PASS_DETAILS)


def _failed(paths: Sequence[str]) -> RunConfigGateResult:
    return RunConfigGateResult(ok=False, offending_paths=tuple(paths), details=describe_overlay_remedy(paths))


def check_paths(paths: Iterable[str]) -> RunConfigGateResult:
    """Verdict for an already-known set of changed paths.

    Pure, so the pipeline gate and the commit-time checks below share one
    definition of what counts as a violation.
    """
    offending = find_run_config_paths(paths)
    return _passed() if not offending else _failed(offending)


def _name_only(cwd: Path, args: list[str], *, what: str) -> list[str] | None:
    """Run a name-only git query, or ``None`` when the answer is unavailable."""
    try:
        result = run_git(args, cwd, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("run_config gate: could not read %s in %s: %s", what, cwd, exc)
        return None
    if not result.ok:
        logger.warning(
            "run_config gate: could not read %s in %s (returncode=%d): %s",
            what,
            cwd,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_staged(cwd: Path) -> RunConfigGateResult:
    """Verdict for what is staged in *cwd* right now.

    Fails closed: a staged set that cannot be read is not a clean bill of
    health, because the whole reason this gate exists is that an unnoticed
    configuration file gets published.
    """
    names = _name_only(
        cwd,
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        what="the staged set",
    )
    if names is None:
        return _failed([UNREADABLE])
    return check_paths(names)


def check_commit(cwd: Path, rev: str = "HEAD") -> RunConfigGateResult:
    """Verdict for the diff of commit *rev*.

    Fails closed for the same reason as :func:`check_staged`.
    """
    names = _name_only(
        cwd,
        ["show", "--no-commit-id", "--name-only", "--pretty=format:", rev],
        what=f"the diff of {rev}",
    )
    if names is None:
        return _failed([UNREADABLE])
    return check_paths(names)
