"""PR Size Governor — auto-split large agent PRs into reviewable chunks.

Addresses the 'bottleneck is verification' problem (Addy Osmani) by enforcing
a maximum PR size of 400 changed lines and creating chained PRs with correct
dependency ordering so each PR is independently reviewable.

Usage::

    from bernstein.core.git.pr_size_governor import split_pr_if_needed

    result = split_pr_if_needed(
        cwd,
        task_id="abc123",
        task_title="feat(auth): OAuth support",
        base_ref="main",
        head_ref="HEAD",
    )
    if result is None:
        # PR is small enough — create a normal single PR
        create_github_pr(cwd, ...)
    else:
        # Split PRs were created
        print(result.pr_urls)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.git.git_basic import GitResult

from bernstein.core.git.git_ops import (
    create_github_pr,
    push_branch,
    run_git,
)

logger = logging.getLogger(__name__)

MAX_PR_LINES: int = 400  # Default maximum changed lines per PR


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PRChunk:
    """A group of files destined for a single PR.

    Attributes:
        files: File paths included in this chunk.
        line_count: Total changed lines (insertions + deletions).
        branch_name: Branch name for this chunk (e.g. bernstein/task-abc-p1).
        base_branch: Branch this PR targets (main or previous chunk branch).
        part_number: 1-based index of this chunk in the split plan.
    """

    files: list[str]
    line_count: int
    branch_name: str
    base_branch: str
    part_number: int


@dataclass
class SplitPlan:
    """Plan for splitting a large changeset into multiple PRs.

    Attributes:
        chunks: Ordered list of PR chunks (dependencies first).
        total_lines: Total changed lines across all chunks.
        needs_split: True if total_lines > max_lines.
    """

    chunks: list[PRChunk]
    total_lines: int
    needs_split: bool


@dataclass
class SplitResult:
    """Outcome of executing a SplitPlan.

    Attributes:
        pr_urls: URLs of created PRs in dependency order.
        chunk_count: Number of PRs created.
        success: True if all PRs were created successfully.
        error: Error message on failure (empty on success).
    """

    pr_urls: list[str]
    chunk_count: int
    success: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Diff measurement
# ---------------------------------------------------------------------------


def count_diff_lines_per_file(
    cwd: Path,
    base_ref: str = "main",
    head_ref: str = "HEAD",
) -> dict[str, int]:
    """Return total changed lines (insertions + deletions) per file.

    Uses ``git diff --numstat`` which gives stable numeric output across
    platforms.  Binary files (reported as ``-``) are counted as 0.

    Args:
        cwd: Repository root.
        base_ref: Base branch or ref for the diff.
        head_ref: Head branch or ref (default ``"HEAD"``).

    Returns:
        Mapping of file path → total lines changed.  Files with no
        numstat entry are absent from the dict.
    """
    r = run_git(["diff", "--numstat", base_ref, head_ref], cwd, timeout=30)
    if not r.ok or not r.stdout.strip():
        return {}

    lines_per_file: dict[str, int] = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_str, deleted_str, filepath = parts
        try:
            added = int(added_str) if added_str.strip() != "-" else 0
            deleted = int(deleted_str) if deleted_str.strip() != "-" else 0
        except ValueError:
            continue
        lines_per_file[filepath.strip()] = added + deleted

    return lines_per_file


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------


def _parse_python_imports(source: str) -> set[str]:
    """Extract top-level module names from Python import statements.

    Handles ``import X``, ``import X.Y``, and ``from X import Y`` forms.
    Returns only the first dotted component (e.g. ``bernstein`` from
    ``from bernstein.core import models``).

    Args:
        source: Python source code text.

    Returns:
        Set of top-level module name strings.
    """
    modules: set[str] = set()
    for m in re.finditer(r"^\s*import\s+([\w.]+)", source, re.MULTILINE):
        modules.add(m.group(1).split(".")[0])
    for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import", source, re.MULTILINE):
        modules.add(m.group(1).split(".")[0])
    return modules


def _file_to_module(filepath: str) -> str:
    """Convert a file path to its Python stem (module name approximation).

    Examples::

        "src/bernstein/core/foo.py" → "foo"
        "tests/unit/test_foo.py"   → "test_foo"

    Args:
        filepath: Relative or absolute file path string.

    Returns:
        Stem of the filename (basename without extension).
    """
    return Path(filepath).stem


def _build_intra_changeset_deps(files: list[str], cwd: Path) -> dict[str, set[str]]:
    """Build dependency sets from intra-changeset Python imports.

    Args:
        files: File paths in the changeset.
        cwd: Repository root for reading file contents.

    Returns:
        Mapping of file -> set of files it depends on.
    """
    module_to_file: dict[str, str] = {_file_to_module(f): f for f in files}
    deps: dict[str, set[str]] = {f: set() for f in files}

    for filepath in files:
        if not filepath.endswith(".py"):
            continue
        try:
            source = (cwd / filepath).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for mod in _parse_python_imports(source):
            dep_file = module_to_file.get(mod)
            if dep_file and dep_file != filepath:
                deps[filepath].add(dep_file)
    return deps


def _topo_sort(files: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Kahn's topological sort with cycle-safe fallback.

    Args:
        files: All file paths.
        deps: Dependency mapping (file -> set of prerequisites).

    Returns:
        Files in topological order.
    """
    in_degree: dict[str, int] = dict.fromkeys(files, 0)
    for f, dep_set in deps.items():
        in_degree[f] += len(dep_set)

    queue = sorted(f for f in files if in_degree[f] == 0)
    result: list[str] = []
    visited: set[str] = set()

    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        result.append(node)
        for f, dep_set in deps.items():
            if node in dep_set and f not in visited:
                in_degree[f] -= 1
                if in_degree[f] == 0:
                    queue.append(f)

    # Append remaining (cycles or disconnected)
    for f in files:
        if f not in visited:
            result.append(f)
    return result


def build_dependency_order(files: list[str], cwd: Path) -> list[str]:
    """Order files so that imported files precede their importers.

    Only considers intra-changeset dependencies -- if file A (in the
    changeset) imports file B (also in the changeset), B appears before A.
    External imports are ignored.  Falls back to original order on cycles
    or parse failures.

    Non-Python files have no detected dependencies and appear first.

    Args:
        files: File paths in the changeset.
        cwd: Repository root (used for reading file contents).

    Returns:
        Files in topological order (dependency-first).
    """
    if not files:
        return []

    deps = _build_intra_changeset_deps(files, cwd)
    return _topo_sort(files, deps)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_split(
    cwd: Path,
    files: list[str],
    *,
    base_ref: str = "main",
    head_ref: str = "HEAD",
    max_lines: int = MAX_PR_LINES,
    task_branch: str = "bernstein/task",
) -> SplitPlan:
    """Plan how to split a changeset into PRs of at most *max_lines* each.

    Steps:

    1. Count changed lines per file via ``git diff --numstat``.
    2. Topologically sort files by intra-changeset import dependencies.
    3. Greedily fill chunks up to *max_lines*.
    4. Assign chained branch names (p1 → main, p2 → p1, etc.).

    A single file that exceeds *max_lines* on its own is placed in its own
    chunk (cannot be split further at this granularity).

    Args:
        cwd: Repository root.
        files: Changed files to consider.  Auto-detected from diff if empty.
        base_ref: Base branch/ref for diff measurement.
        head_ref: Head branch/ref (default ``"HEAD"``).
        max_lines: Maximum changed lines per PR chunk.
        task_branch: Branch name prefix (e.g. ``"bernstein/task-abc123"``).

    Returns:
        :class:`SplitPlan` with ordered chunks and metadata.
    """
    # Auto-detect changed files if not provided
    if not files:
        r = run_git(["diff", "--name-only", base_ref, head_ref], cwd, timeout=30)
        files = [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]

    if not files:
        return SplitPlan(chunks=[], total_lines=0, needs_split=False)

    # Measure lines per file
    lines_map = count_diff_lines_per_file(cwd, base_ref, head_ref)
    for f in files:
        lines_map.setdefault(f, 0)

    # Dependency-ordered file list
    ordered = build_dependency_order(files, cwd)
    total_lines = sum(lines_map[f] for f in ordered)

    if total_lines <= max_lines:
        return SplitPlan(
            chunks=[
                PRChunk(
                    files=ordered,
                    line_count=total_lines,
                    branch_name=f"{task_branch}-p1",
                    base_branch=base_ref,
                    part_number=1,
                )
            ],
            total_lines=total_lines,
            needs_split=False,
        )

    # Greedy chunking
    raw_chunks: list[tuple[list[str], int]] = []  # (files, line_count)
    current_files: list[str] = []
    current_lines = 0

    for filepath in ordered:
        file_lines = lines_map[filepath]
        if current_files and current_lines + file_lines > max_lines:
            raw_chunks.append((current_files, current_lines))
            current_files = []
            current_lines = 0
        current_files.append(filepath)
        current_lines += file_lines

    if current_files:
        raw_chunks.append((current_files, current_lines))

    # Build PRChunk objects with chained base branches
    chunks: list[PRChunk] = []
    for i, (chunk_files, chunk_lines) in enumerate(raw_chunks):
        part = i + 1
        base = base_ref if part == 1 else chunks[-1].branch_name
        chunks.append(
            PRChunk(
                files=chunk_files,
                line_count=chunk_lines,
                branch_name=f"{task_branch}-p{part}",
                base_branch=base,
                part_number=part,
            )
        )

    return SplitPlan(chunks=chunks, total_lines=total_lines, needs_split=True)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_split(
    cwd: Path,
    plan: SplitPlan,
    *,
    head_ref: str = "HEAD",
    base_ref: str = "main",
    pr_title_prefix: str = "feat",
    pr_body_prefix: str = "",
    labels: list[str] | None = None,
) -> SplitResult:
    """Execute a :class:`SplitPlan` by creating branches and GitHub PRs.

    For each chunk in order:

    1. Creates (or resets) a branch from the chunk's ``base_branch``.
    2. Applies the diff for the chunk's files (``head_ref`` vs ``base_ref``).
    3. Commits, pushes, and opens a GitHub PR.

    The chain of PRs (p1 → main, p2 → p1, …) encodes the dependency
    ordering: later PRs cannot be merged until earlier ones land.

    Args:
        cwd: Repository root.
        plan: Split plan from :func:`plan_split`.
        head_ref: Branch/ref with all changes (e.g. ``"agent/session-123"``).
        base_ref: Original base branch (default ``"main"``).
        pr_title_prefix: Prefix for PR titles.
        pr_body_prefix: Text prepended to each PR body (e.g. task summary).
        labels: GitHub labels to attach to each PR.

    Returns:
        :class:`SplitResult` with PR URLs and success status.
    """
    if not plan.chunks:
        return SplitResult(pr_urls=[], chunk_count=0, success=True)

    if not plan.needs_split:
        return SplitResult(pr_urls=[], chunk_count=0, success=True, error="no split needed")

    current_r = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    original_branch = current_r.stdout.strip() if current_r.ok else head_ref

    pr_urls: list[str] = []
    total = len(plan.chunks)

    for chunk in plan.chunks:
        result = _execute_chunk(cwd, chunk, plan, base_ref, head_ref, pr_title_prefix, pr_body_prefix, labels)
        if result is None:
            continue  # Empty diff, skipped
        if isinstance(result, SplitResult):
            # Fatal error — merge partial pr_urls and return
            _restore(cwd, original_branch)
            return SplitResult(
                pr_urls=pr_urls + result.pr_urls,
                chunk_count=len(pr_urls) + result.chunk_count,
                success=False,
                error=result.error,
            )
        # Success — result is a PR URL or empty string
        if result:
            pr_urls.append(result)

    _restore(cwd, original_branch)
    return SplitResult(
        pr_urls=pr_urls,
        chunk_count=len(pr_urls),
        success=len(pr_urls) == total,
    )


def _create_chunk_branch(cwd: Path, chunk: PRChunk) -> GitResult:
    """Create or reset the branch for a chunk.

    Args:
        cwd: Repository root.
        chunk: PR chunk with branch_name and base_branch.

    Returns:
        GitResult from the checkout.
    """
    branch_r = run_git(["checkout", "-B", chunk.branch_name, chunk.base_branch], cwd, timeout=30)
    if not branch_r.ok:
        run_git(["branch", "-D", chunk.branch_name], cwd)
        branch_r = run_git(["checkout", "-B", chunk.branch_name, chunk.base_branch], cwd, timeout=30)
    return branch_r


def _execute_chunk(
    cwd: Path,
    chunk: PRChunk,
    plan: SplitPlan,
    base_ref: str,
    head_ref: str,
    pr_title_prefix: str,
    pr_body_prefix: str,
    labels: list[str] | None,
) -> str | SplitResult | None:
    """Execute a single chunk: create branch, apply diff, push, open PR.

    Args:
        cwd: Repository root.
        chunk: The PR chunk to execute.
        plan: Full split plan (for PR body generation).
        base_ref: Original base ref for diffs.
        head_ref: Head ref with all changes.
        pr_title_prefix: PR title prefix.
        pr_body_prefix: Text prepended to each PR body.
        labels: GitHub labels.

    Returns:
        PR URL string on success, None if skipped (empty diff),
        or SplitResult on fatal error (caller should abort).
    """
    total = len(plan.chunks)
    part_label = f"[{chunk.part_number}/{total}]"

    branch_r = _create_chunk_branch(cwd, chunk)
    if not branch_r.ok:
        return SplitResult(
            pr_urls=[],
            chunk_count=0,
            success=False,
            error=f"Cannot create branch {chunk.branch_name}: {branch_r.stderr}",
        )

    diff_r = run_git(["diff", base_ref, head_ref, "--", *chunk.files], cwd, timeout=60)
    if not diff_r.ok or not diff_r.stdout.strip():
        logger.warning("Empty diff for chunk %d, skipping", chunk.part_number)
        return None

    apply_r = run_git(["apply", "--allow-empty", "-"], cwd, input_data=diff_r.stdout, timeout=30)
    if not apply_r.ok:
        return SplitResult(
            pr_urls=[],
            chunk_count=0,
            success=False,
            error=f"Chunk {chunk.part_number}: git apply failed: {apply_r.stderr}",
        )

    run_git(["add", "--", *chunk.files], cwd)
    short_names = [Path(f).name for f in chunk.files[:3]]
    file_summary = ", ".join(short_names)
    if len(chunk.files) > 3:
        file_summary += f" (+{len(chunk.files) - 3} more)"
    commit_msg = (
        f"{pr_title_prefix}: {file_summary} {part_label}\n\n"
        f"Part {chunk.part_number} of {total} — {chunk.line_count} lines changed\n\n"
        f"Files:\n"
        + "\n".join(f"  {f}" for f in chunk.files)
        + "\n\nCo-Authored-By: bernstein[bot] <noreply@bernstein.dev>"
    )
    commit_r = run_git(["commit", "-m", commit_msg], cwd)
    if not commit_r.ok:
        run_git(["commit", "--allow-empty", "-m", commit_msg], cwd)

    push_r = push_branch(cwd, chunk.branch_name)
    if not push_r.ok:
        return SplitResult(
            pr_urls=[],
            chunk_count=0,
            success=False,
            error=f"Push failed for {chunk.branch_name}: {push_r.stderr}",
        )

    body = _build_pr_body(chunk, plan, pr_body_prefix)
    pr_result = create_github_pr(
        cwd,
        title=f"{pr_title_prefix} {part_label}",
        body=body,
        head=chunk.branch_name,
        base=chunk.base_branch,
        labels=labels,
    )
    if pr_result.success:
        logger.info("Created PR %d/%d: %s", chunk.part_number, total, pr_result.pr_url)
        return pr_result.pr_url
    logger.warning("PR creation failed for chunk %d: %s", chunk.part_number, pr_result.error)
    return ""


def _restore(cwd: Path, branch: str) -> None:
    """Best-effort checkout of *branch* (swallows errors)."""
    run_git(["checkout", branch], cwd, timeout=10)


def _build_pr_body(chunk: PRChunk, plan: SplitPlan, prefix: str) -> str:
    """Compose the PR body for a chunk."""
    total = len(plan.chunks)
    parts: list[str] = []

    if prefix:
        parts.append(prefix)

    parts.append(
        f"**Part {chunk.part_number} of {total}** — {chunk.line_count} lines changed\n\n"
        f"Files in this PR:\n" + "\n".join(f"- `{f}`" for f in chunk.files)
    )

    if chunk.part_number < total:
        next_branch = plan.chunks[chunk.part_number].branch_name
        parts.append(f"_Next: part {chunk.part_number + 1} of {total} (`{next_branch}` → `{chunk.branch_name}`)_")
    else:
        parts.append("_This is the final part of the split PR chain._")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


def split_pr_if_needed(
    cwd: Path,
    *,
    files: list[str] | None = None,
    task_id: str = "",
    task_title: str = "",
    base_ref: str = "main",
    head_ref: str = "HEAD",
    max_lines: int = MAX_PR_LINES,
    labels: list[str] | None = None,
) -> SplitResult | None:
    """Split a large agent PR into reviewable chunks if it exceeds *max_lines*.

    Call this in the merge/PR-creation flow.  Returns ``None`` when the
    changeset fits in a single PR (caller proceeds normally).  Returns a
    :class:`SplitResult` when the changeset was split into multiple PRs.

    Args:
        cwd: Repository root.
        files: Explicit file list.  Auto-detected from diff when ``None``.
        task_id: Task ID used for branch naming (e.g. ``"abc123"``).
        task_title: Human-readable task title used as PR title prefix.
        base_ref: Base branch (default ``"main"``).
        head_ref: Branch/ref with all changes (default ``"HEAD"``).
        max_lines: Maximum changed lines per PR (default :data:`MAX_PR_LINES`).
        labels: GitHub labels to attach to each PR.

    Returns:
        ``None`` if no split was needed, otherwise the :class:`SplitResult`.
    """
    branch_prefix = f"bernstein/task-{task_id}" if task_id else "bernstein/split"
    plan = plan_split(
        cwd,
        files or [],
        base_ref=base_ref,
        head_ref=head_ref,
        max_lines=max_lines,
        task_branch=branch_prefix,
    )

    if not plan.needs_split:
        return None

    return execute_split(
        cwd,
        plan,
        head_ref=head_ref,
        base_ref=base_ref,
        pr_title_prefix=task_title or "feat",
        labels=labels,
    )
