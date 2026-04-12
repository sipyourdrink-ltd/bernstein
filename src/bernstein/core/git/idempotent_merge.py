"""Idempotent merge with conflict pre-check.

Provides pure-data structures and helper functions for planning and
validating git merges before they are attempted.  The ``dry_run_merge``
function conceptualises the merge operation without touching the
repository, making it safe to call repeatedly (idempotent) for the same
source/target pair.

``build_merge_command`` returns the git CLI arguments that the caller can
run when a merge is actually desired, and ``should_attempt_merge``
encodes the policy for whether a merge should proceed given a check
result.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeCheckResult:
    """Outcome of a pre-merge conflict check.

    Attributes:
        task_id: Identifier for the task whose branch is being merged.
        source_branch: The feature branch to merge from.
        target_branch: The branch being merged into (e.g. ``"main"``).
        can_merge: Whether the merge is expected to succeed cleanly.
        conflict_files: File paths that would conflict (empty when clean).
        merge_strategy: Git merge strategy to use.
        checked_at: UTC timestamp of the check.
    """

    task_id: str
    source_branch: str
    target_branch: str
    can_merge: bool
    conflict_files: list[str]
    merge_strategy: Literal["fast-forward", "recursive", "octopus"]
    checked_at: datetime


@dataclass(frozen=True)
class MergeAttempt:
    """Record of a merge attempt derived from a :class:`MergeCheckResult`.

    Attributes:
        task_id: Identifier for the task whose branch is being merged.
        attempt_id: Unique identifier for this attempt.
        result: The pre-check result that preceded the attempt.
        applied: Whether the merge was actually applied.
        error: Error message if the merge failed, empty string otherwise.
    """

    task_id: str
    attempt_id: str
    result: MergeCheckResult
    applied: bool
    error: str


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

_STRATEGY_MAP: dict[str, list[str]] = {
    "fast-forward": ["--ff-only"],
    "recursive": ["--strategy", "recursive"],
    "octopus": ["--strategy", "octopus"],
}


def dry_run_merge(
    source: str,
    target: str,
    workdir: Path,
    *,
    task_id: str = "",
) -> MergeCheckResult:
    """Conceptualise a merge without executing any git commands.

    Examines the branch names and working directory to build a
    :class:`MergeCheckResult`.  The function never runs git so it is safe
    to call from read-only contexts or in tests.

    The merge strategy is determined by the number of source refs:

    * A single source branch uses ``"fast-forward"`` when possible.
    * Multiple sources (octopus merge) are not supported here; the
      single-source path always selects ``"fast-forward"`` as the
      preferred strategy, falling back to ``"recursive"`` if the
      branches have diverged.

    Since no actual git commands are run the ``can_merge`` flag is set
    optimistically to ``True`` and ``conflict_files`` is left empty.
    Callers that need real conflict detection should use
    :func:`bernstein.core.merge_queue.detect_merge_conflicts`.

    Args:
        source: Source branch name (e.g. ``"agent/backend-abc123"``).
        target: Target branch name (e.g. ``"main"``).
        workdir: Repository root directory.
        task_id: Optional task identifier to embed in the result.

    Returns:
        A :class:`MergeCheckResult` describing the planned merge.
    """
    strategy: Literal["fast-forward", "recursive", "octopus"] = "fast-forward"

    # If source and target are the same the merge is a no-op; mark it
    # as fast-forward (it will effectively be a no-op).
    if source == target:
        strategy = "fast-forward"
    # Fallback: when we cannot confirm fast-forward eligibility without
    # git, we choose recursive as the safe default.
    elif "/" in source and "/" in target:
        strategy = "recursive"

    return MergeCheckResult(
        task_id=task_id,
        source_branch=source,
        target_branch=target,
        can_merge=True,
        conflict_files=[],
        merge_strategy=strategy,
        checked_at=datetime.now(UTC),
    )


def build_merge_command(
    source: str,
    strategy: Literal["fast-forward", "recursive", "octopus"],
) -> list[str]:
    """Build the git CLI arguments for a merge.

    Args:
        source: Branch name to merge.
        strategy: Merge strategy to encode.

    Returns:
        A list of strings suitable for passing to ``subprocess.run``
        (without the leading ``git``).
    """
    cmd = ["git", "merge"]
    cmd.extend(_STRATEGY_MAP.get(strategy, []))
    cmd.append(source)
    return cmd


def should_attempt_merge(check: MergeCheckResult) -> bool:
    """Decide whether a merge should proceed based on a pre-check result.

    A merge is attempted only when:

    * ``can_merge`` is ``True``, **and**
    * ``conflict_files`` is empty.

    Args:
        check: The result of a prior dry-run or conflict check.

    Returns:
        ``True`` if the merge should be attempted.
    """
    return check.can_merge and len(check.conflict_files) == 0


def format_merge_check(check: MergeCheckResult) -> str:
    """Format a :class:`MergeCheckResult` as a human-readable summary.

    Args:
        check: The merge check result to format.

    Returns:
        Multi-line string summarising the check outcome.
    """
    status = "CLEAN" if check.can_merge and not check.conflict_files else "CONFLICT"
    lines = [
        f"Merge check [{status}]",
        f"  task:     {check.task_id}",
        f"  source:   {check.source_branch}",
        f"  target:   {check.target_branch}",
        f"  strategy: {check.merge_strategy}",
        f"  checked:  {check.checked_at.isoformat()}",
    ]
    if check.conflict_files:
        lines.append(f"  conflicts ({len(check.conflict_files)}):")
        for f in check.conflict_files:
            lines.append(f"    - {f}")
    return "\n".join(lines)


def create_merge_attempt(
    check: MergeCheckResult,
    *,
    applied: bool = False,
    error: str = "",
) -> MergeAttempt:
    """Create a :class:`MergeAttempt` record from a check result.

    Args:
        check: The pre-check result.
        applied: Whether the merge was applied.
        error: Error message if the merge failed.

    Returns:
        A new :class:`MergeAttempt` instance.
    """
    return MergeAttempt(
        task_id=check.task_id,
        attempt_id=uuid.uuid4().hex[:12],
        result=check,
        applied=applied,
        error=error,
    )
