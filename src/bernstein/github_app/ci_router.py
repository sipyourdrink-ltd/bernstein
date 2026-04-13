"""CI failure routing: blame attribution and enriched fix-task generation.

Implements the 334f pipeline:
  1. ``blame_ci_failures`` — maps failing files to the commit that triggered CI.
  2. ``build_ci_routing_payload`` — builds a fix-task description that includes
     the triggering commit diff so the agent has immediate context.

The module purposely contains only pure logic (git subprocess calls + data
transformation).  Side effects (HTTP calls, store access) live in the caller.
"""

from __future__ import annotations

import logging
import subprocess
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.ci_fix import CIFailure

logger = logging.getLogger(__name__)

# Maximum auto-retries before the system stops creating new ci-fix tasks.
MAX_CI_RETRIES: int = 3

# Size limits to keep task descriptions digestible.
_DIFF_MAX_CHARS: int = 3000


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def get_commit_files(head_sha: str, cwd: Path | None = None) -> list[str]:
    """List files changed in *head_sha*.

    Args:
        head_sha: Git commit SHA.
        cwd: Repository root; ``None`` means the current working directory.

    Returns:
        Sorted list of changed file paths, or an empty list on failure.
    """
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", head_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=cwd,
        )
        if result.returncode == 0:
            return sorted(f.strip() for f in result.stdout.splitlines() if f.strip())
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git diff-tree %s failed: %s", head_sha[:8], exc)
    return []


def get_commit_diff(head_sha: str, cwd: Path | None = None) -> str:
    """Return the diff for *head_sha*, truncated to ``_DIFF_MAX_CHARS``.

    Uses ``git show --stat --patch`` so the caller gets both the file summary
    and the actual changes in one call.

    Args:
        head_sha: Git commit SHA.
        cwd: Repository root.

    Returns:
        Diff text, or an empty string on failure.
    """
    try:
        result = subprocess.run(
            ["git", "show", "--stat", "--patch", head_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout[:_DIFF_MAX_CHARS]
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git show %s failed: %s", head_sha[:8], exc)
    return ""


def get_commit_message(head_sha: str, cwd: Path | None = None) -> str:
    """Return the subject line of *head_sha*.

    Args:
        head_sha: Git commit SHA.
        cwd: Repository root.

    Returns:
        Subject line, or an empty string on failure.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=%s", "-1", head_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git log %s failed: %s", head_sha[:8], exc)
    return ""


# ---------------------------------------------------------------------------
# Blame attribution
# ---------------------------------------------------------------------------


@dataclass
class CIBlameResult:
    """Attribution of a CI failure to a specific commit.

    Attributes:
        head_sha: The commit that triggered the CI run.
        responsible_files: Files that appear in both the CI failure output and
            the commit diff.  Falls back to *all* files changed in the commit
            when the intersection is empty (e.g. transitive type errors).
        diff_context: Truncated diff for ``head_sha`` (max ``_DIFF_MAX_CHARS``).
        commit_message: Subject line of ``head_sha``.
    """

    head_sha: str
    responsible_files: list[str] = field(default_factory=list[str])
    diff_context: str = ""
    commit_message: str = ""


def blame_ci_failures(
    failures: list[CIFailure],
    head_sha: str,
    cwd: Path | None = None,
) -> CIBlameResult:
    """Attribute CI failures to the triggering commit.

    Collects every ``affected_file`` from *failures* and intersects that set
    with the files changed in *head_sha*.  When there is no overlap the full
    commit file list is used as context — this handles the common case where a
    type error in file B is caused by a change in file A.

    Args:
        failures: Parsed ``CIFailure`` objects from the log.
        head_sha: SHA of the commit that triggered the CI run.
        cwd: Repository root.

    Returns:
        ``CIBlameResult`` with git context for *head_sha*.
    """
    failing_set: set[str] = set()
    for failure in failures:
        failing_set.update(failure.affected_files)

    commit_files = get_commit_files(head_sha, cwd)

    # Direct overlap first — these are the most likely culprits.
    responsible = [f for f in commit_files if f in failing_set]
    if not responsible and commit_files:
        # No direct match: the commit introduced a transitive breakage.
        responsible = commit_files[:5]

    logger.debug(
        "blame_ci_failures: sha=%s commit_files=%d failing=%d responsible=%d",
        head_sha[:8],
        len(commit_files),
        len(failing_set),
        len(responsible),
    )

    return CIBlameResult(
        head_sha=head_sha,
        responsible_files=responsible,
        diff_context=get_commit_diff(head_sha, cwd),
        commit_message=get_commit_message(head_sha, cwd),
    )


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def build_ci_routing_payload(
    failures: list[CIFailure],
    blame: CIBlameResult,
    workflow_name: str,
    run_url: str = "",
    retry_count: int = 0,
) -> dict[str, Any]:
    """Build a fix-task payload enriched with CI log and commit diff context.

    Model and effort escalate on repeat failures so that harder problems get
    more capable agents:
    - Attempt 1-2: ``sonnet`` / ``high``
    - Attempt 3+:  ``opus``   / ``max``

    Args:
        failures: Parsed CI failures.
        blame: Blame attribution from :func:`blame_ci_failures`.
        workflow_name: Human-readable workflow name (e.g. ``"CI"``).
        run_url: URL of the failing CI run.
        retry_count: Number of *previous* fix attempts for this branch
            (0 means this is the first attempt).

    Returns:
        Task creation dict compatible with ``TaskCreate`` fields.
    """
    model = "opus" if retry_count >= 2 else "sonnet"
    effort = "max" if retry_count >= 2 else "high"

    failure_kinds = ", ".join(sorted({f.kind.value for f in failures}))
    failure_summaries = "\n".join(f"- [{f.job}] {f.summary}" for f in failures)
    hints = "\n".join(f"  {f.fix_hint}" for f in failures if f.fix_hint)

    files_block = (
        "\n".join(f"  - {f}" for f in blame.responsible_files) if blame.responsible_files else "  (could not determine)"
    )
    run_link = f"\nCI run: {run_url}" if run_url else ""
    retry_note = f"\n\n**Retry attempt {retry_count + 1}/{MAX_CI_RETRIES}**" if retry_count > 0 else ""
    diff_section = (
        f"\n## Triggering commit diff ({blame.head_sha[:8]})\n```diff\n{blame.diff_context}\n```"
        if blame.diff_context
        else ""
    )

    description = textwrap.dedent(f"""\
        CI workflow "{workflow_name}" failed.{retry_note}
        Commit: {blame.head_sha[:8]} — {blame.commit_message}
        Failures: {failure_kinds}
        {run_link}

        ## Files to investigate
        {files_block}

        ## Failure summaries
        {failure_summaries}

        ## Suggested fixes
        {hints}
        {diff_section}

        ## Instructions
        1. Review the triggering commit diff above.
        2. Run the suggested fix commands locally.
        3. Verify with: uv run ruff check src/ && uv run python scripts/run_tests.py -x
        4. Commit and push the fix.
    """)

    title = f"[ci-fix][{blame.head_sha[:8]}] {workflow_name}: {failure_kinds}"[:120]

    return {
        "title": title,
        "description": description,
        "role": "qa",
        "priority": 1,
        "scope": "small",
        "task_type": "fix",
        "model": model,
        "effort": effort,
    }
