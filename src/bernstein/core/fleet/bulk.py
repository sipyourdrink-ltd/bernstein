"""Bulk-action dispatcher for the fleet view.

Every bulk action delegates to the per-project ``bernstein`` CLI command
so the orchestrator's existing audit/logging path handles each individual
operation. We never reach into the per-project task server directly for
state-changing operations — that would split the audit chain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.fleet.aggregator import ProjectSnapshot
    from bernstein.core.fleet.config import ProjectConfig

logger = logging.getLogger(__name__)

# Filter expression grammar (whitespace-insensitive):
#   <field> <op> <value>
# where field ∈ {cost, agents, approvals}, op ∈ {<, <=, >, >=, ==, !=}.
_FILTER_RE = re.compile(
    r"^\s*(?P<field>cost|agents|approvals)\s*"
    r"(?P<op><=|>=|==|!=|<|>)\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*$"
)


@dataclass(slots=True)
class BulkActionResult:
    """Outcome of dispatching a bulk action to one or more projects.

    Attributes:
        action: Name of the action (``stop`` / ``pause`` / ``resume`` / ``cost``).
        succeeded: Project names where the per-project CLI returned 0.
        failed: Mapping ``project -> stderr-or-error`` for failures.
        outputs: Mapping ``project -> stdout`` for inspection.
    """

    action: str
    succeeded: list[str] = field(default_factory=list[str])
    failed: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


def _evaluate_filter(snapshot: ProjectSnapshot, expression: str) -> bool:
    match = _FILTER_RE.match(expression)
    if match is None:
        raise ValueError(f"unrecognised filter expression: {expression!r}")
    field_name, op, raw = match.group("field"), match.group("op"), match.group("value")
    threshold = float(raw)
    if field_name == "cost":
        actual = snapshot.cost_usd
    elif field_name == "agents":
        actual = float(snapshot.agents)
    elif field_name == "approvals":
        actual = float(snapshot.pending_approvals)
    else:  # pragma: no cover - regex guarantees membership
        raise ValueError(f"unsupported field: {field_name}")
    return {
        "<": actual < threshold,
        "<=": actual <= threshold,
        ">": actual > threshold,
        ">=": actual >= threshold,
        "==": actual == threshold,
        "!=": actual != threshold,
    }[op]


def select_projects(
    projects: list[ProjectConfig],
    snapshots: list[ProjectSnapshot],
    *,
    names: list[str] | None = None,
    filter_expression: str | None = None,
) -> list[ProjectConfig]:
    """Resolve a ``names`` list and/or filter into a project subset.

    Args:
        projects: Full project list.
        snapshots: Aligned (or named) snapshot list. Order need not match.
        names: Optional explicit project names.
        filter_expression: Optional ``cost>5``-style filter.

    Returns:
        The subset of projects matching the request. When both ``names``
        and ``filter_expression`` are supplied, both must match.

    Raises:
        ValueError: If the filter expression is malformed.
    """
    by_name: dict[str, ProjectConfig] = {p.name: p for p in projects}
    snap_by_name: dict[str, ProjectSnapshot] = {s.name: s for s in snapshots}

    candidates = [by_name[n] for n in names if n in by_name] if names else list(projects)

    if not filter_expression:
        return candidates

    selected: list[ProjectConfig] = []
    for project in candidates:
        snap = snap_by_name.get(project.name)
        if snap is None:
            continue
        if _evaluate_filter(snap, filter_expression):
            selected.append(project)
    return selected


# -- Subprocess plumbing ---------------------------------------------------


SubprocessRunner = Callable[[list[str], Path, dict[str, str]], Awaitable[tuple[int, str, str]]]


async def _default_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
    """Default implementation: spawn ``cmd`` with ``cwd`` and capture output."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await process.communicate()
    return (
        int(process.returncode or 0),
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _bernstein_command() -> list[str]:
    """Pick the executable used to invoke per-project ``bernstein`` commands.

    ``$BERNSTEIN_BIN`` overrides everything (useful for tests that need to
    point at a stub script). Otherwise we use the current Python's
    ``-m bernstein`` so the dispatch always targets the same install.
    """
    override = os.environ.get("BERNSTEIN_BIN")
    if override:
        return shlex.split(override)
    import sys

    return [sys.executable, "-m", "bernstein"]


async def _dispatch_one(
    project: ProjectConfig,
    args: list[str],
    runner: SubprocessRunner,
) -> tuple[str, int, str, str]:
    cmd = [*_bernstein_command(), *args]
    env = os.environ.copy()
    # Make the project's task server reachable for sub-process commands that
    # talk to the API rather than the filesystem.
    env["BERNSTEIN_TASK_SERVER_URL"] = project.task_server_url
    rc, stdout, stderr = await runner(cmd, project.path, env)
    return project.name, rc, stdout, stderr


async def _bulk_dispatch(
    action: str,
    projects: list[ProjectConfig],
    cli_args: list[str],
    *,
    runner: SubprocessRunner | None = None,
) -> BulkActionResult:
    runner_fn = runner or _default_runner
    result = BulkActionResult(action=action)
    if not projects:
        return result
    coros = [_dispatch_one(p, cli_args, runner_fn) for p in projects]
    rows = await asyncio.gather(*coros, return_exceptions=True)
    for row in rows:
        if isinstance(row, BaseException):
            logger.warning("fleet bulk %s: dispatch error %s", action, row)
            continue
        name, rc, stdout, stderr = row
        result.outputs[name] = stdout
        if rc == 0:
            result.succeeded.append(name)
        else:
            result.failed[name] = stderr.strip() or stdout.strip() or f"exit {rc}"
    return result


async def bulk_stop(
    projects: list[ProjectConfig],
    *,
    runner: SubprocessRunner | None = None,
) -> BulkActionResult:
    """Send ``bernstein stop`` to every selected project."""
    return await _bulk_dispatch("stop", projects, ["stop"], runner=runner)


async def bulk_pause(
    projects: list[ProjectConfig],
    *,
    runner: SubprocessRunner | None = None,
) -> BulkActionResult:
    """Pause every selected project via the per-project CLI.

    Maps to ``bernstein daemon stop`` so the daemon (and thus the run)
    halts; this preserves the per-project audit chain because the call
    goes through the project's existing supervised stop path.
    """
    return await _bulk_dispatch("pause", projects, ["daemon", "stop"], runner=runner)


async def bulk_resume(
    projects: list[ProjectConfig],
    *,
    runner: SubprocessRunner | None = None,
) -> BulkActionResult:
    """Resume every selected project via the per-project CLI.

    Maps to ``bernstein daemon start`` (the inverse of ``bulk_pause``).
    """
    return await _bulk_dispatch("resume", projects, ["daemon", "start"], runner=runner)


async def bulk_cost_report(
    projects: list[ProjectConfig],
    *,
    runner: SubprocessRunner | None = None,
    extra_args: list[str] | None = None,
) -> BulkActionResult:
    """Run ``bernstein cost report`` on every selected project."""
    args = ["cost", "report", *(extra_args or [])]
    return await _bulk_dispatch("cost-report", projects, args, runner=runner)


# Public for fleet web view + tests to evaluate filters without dispatching.
def evaluate_filter(snapshot: ProjectSnapshot, expression: str) -> bool:
    """Public wrapper for the internal filter parser."""
    return _evaluate_filter(snapshot, expression)


def expose_runtime_for_tests() -> dict[str, Any]:
    """Return private hooks for test-only injection.

    Tests can monkeypatch these by importing the module and reassigning the
    attributes; they are documented here so readers know the surface.
    """
    return {
        "default_runner": _default_runner,
        "bernstein_command": _bernstein_command,
    }
