"""On-disk layout for the detached run service (#2352).

Every path a supervisor writes lives under ``<sdd>/runtime/run-service/<run-id>``.
Run ids share the git-ref-safe alphabet the persistence layer uses for
worktree and ledger ids, and each resolved run directory is realpath-contained
under the service root so a crafted id can never escape the runtime tree
(CodeQL path-injection hardening).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Run ids must match the alphabet the ledger uses for run/worktree ids.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RunServicePathError(ValueError):
    """Raised for an unsafe run id or a path that escapes the service root."""


def service_root(sdd_dir: Path) -> Path:
    """Return ``<sdd_dir>/runtime/run-service`` (created on first use)."""
    root = sdd_dir / "runtime" / "run-service"
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_run_id(run_id: str) -> str:
    """Return *run_id* unchanged when safe, else raise :class:`RunServicePathError`."""
    if not _RUN_ID_RE.match(run_id):
        msg = f"invalid run id {run_id!r}: must match {_RUN_ID_RE.pattern}"
        raise RunServicePathError(msg)
    return run_id


def run_dir(sdd_dir: Path, run_id: str) -> Path:
    """Return the per-run runtime directory, realpath-contained under the root.

    Raises:
        RunServicePathError: If *run_id* is malformed or the resolved
            directory would fall outside the service root.
    """
    validate_run_id(run_id)
    root = service_root(sdd_dir)
    candidate = root / run_id
    root_real = root.resolve()
    candidate_real = candidate.resolve()
    if candidate_real != root_real and root_real not in candidate_real.parents:
        msg = f"run directory for {run_id!r} escapes the service root"
        raise RunServicePathError(msg)
    return candidate


def descriptor_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the run descriptor JSON path."""
    return run_dir(sdd_dir, run_id) / "descriptor.json"


def pidfile_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the supervisor pidfile path."""
    return run_dir(sdd_dir, run_id) / "supervisor.pid"


def heartbeat_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the supervisor heartbeat path."""
    return run_dir(sdd_dir, run_id) / "supervisor.heartbeat"


def supervisor_log_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the supervisor stdout/stderr log path."""
    return run_dir(sdd_dir, run_id) / "supervisor.log"


def list_run_ids(sdd_dir: Path) -> list[str]:
    """Return the sorted run ids that have a runtime directory."""
    root = service_root(sdd_dir)
    return sorted(child.name for child in root.iterdir() if child.is_dir() and _RUN_ID_RE.match(child.name))


__all__ = [
    "RunServicePathError",
    "descriptor_path",
    "heartbeat_path",
    "list_run_ids",
    "pidfile_path",
    "run_dir",
    "service_root",
    "supervisor_log_path",
    "validate_run_id",
]
