"""Automated changelog generation from agent-produced diffs.

Generates a human-readable changelog from all changes made during a Bernstein
run.  Groups changes by component, summarizes each change in plain English,
flags breaking changes, and links back to the originating task.

This is distinct from ``changelog_cmd.py`` (which generates changelogs from
conventional commits for Bernstein itself) — this module generates changelogs
for the *target project* based on what agents actually changed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Component detection
# ---------------------------------------------------------------------------

# Maps path prefixes (most-specific first) to human-readable component names.
_COMPONENT_MAP: list[tuple[str, str]] = [
    ("src/bernstein/cli/", "CLI"),
    ("src/bernstein/core/routes/", "API Routes"),
    ("src/bernstein/core/", "Core Engine"),
    ("src/bernstein/adapters/", "Adapters"),
    ("src/bernstein/", "Bernstein Package"),
    ("templates/roles/", "Role Templates"),
    ("templates/prompts/", "Prompt Templates"),
    ("templates/", "Templates"),
    ("tests/unit/", "Unit Tests"),
    ("tests/integration/", "Integration Tests"),
    ("tests/", "Tests"),
    ("docs/", "Documentation"),
    (".github/", "CI/CD"),
    ("scripts/", "Scripts"),
]

_DEFAULT_COMPONENT = "Other"


def _component_for_file(path: str) -> str:
    """Map a file path to a component name."""
    for prefix, name in _COMPONENT_MAP:
        if path.startswith(prefix):
            return name
    return _DEFAULT_COMPONENT


def _dominant_component(files: list[str]) -> str:
    """Return the component that owns the most files in *files*."""
    if not files:
        return _DEFAULT_COMPONENT
    counts: dict[str, int] = defaultdict(int)
    for f in files:
        counts[_component_for_file(f)] += 1
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Breaking change detection
# ---------------------------------------------------------------------------

# Patterns that strongly suggest a breaking change in a diff hunk.
_BREAKING_DIFF_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^-\s*(def |class )\w[^_]", re.MULTILINE),  # removed public symbol
    re.compile(r"^-\s*@(click\.command|app\.route|router\.(get|post|put|delete|patch))", re.MULTILINE),
    re.compile(r"BREAKING CHANGE", re.IGNORECASE),
]


def _is_breaking_diff(diff: str) -> bool:
    """Heuristically detect whether *diff* contains a breaking change."""
    return any(p.search(diff) for p in _BREAKING_DIFF_PATTERNS)


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path, timeout: int = 30) -> str:
    """Run a git command and return stdout, or empty string on error."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _commits_for_task(task_id: str, cwd: Path, since_ref: str | None = None) -> list[str]:
    """Return commit SHAs that reference *task_id* via ``Refs: #<id>`` footer.

    Args:
        task_id: Bernstein task ID (hex string).
        cwd: Git repository root.
        since_ref: Optional commit ref to start searching from (e.g. a tag).

    Returns:
        List of full commit SHAs, newest-first.
    """
    rev_range = f"{since_ref}..HEAD" if since_ref else "HEAD"
    # Use --grep to find commits whose body mentions the task id
    raw = _run_git(
        [
            "log",
            rev_range,
            "--pretty=format:%H",
            f"--grep=Refs: #{task_id}",
            "--no-merges",
        ],
        cwd,
    )
    return [sha.strip() for sha in raw.splitlines() if sha.strip()]


def _files_in_commit(sha: str, cwd: Path) -> list[str]:
    """Return list of files changed in *sha*."""
    raw = _run_git(["diff-tree", "--no-commit-id", "-r", "--name-only", sha], cwd)
    return [f.strip() for f in raw.splitlines() if f.strip()]


def _diff_for_commit(sha: str, cwd: Path) -> str:
    """Return the full diff for *sha*."""
    return _run_git(["show", "--no-color", sha], cwd, timeout=60)


def _subject_for_commit(sha: str, cwd: Path) -> str:
    """Return the subject line for *sha*."""
    return _run_git(["log", "-1", "--pretty=format:%s", sha], cwd).strip()


# ---------------------------------------------------------------------------
# Task data fetching
# ---------------------------------------------------------------------------


def _fetch_tasks_from_server(server_url: str, status: str = "done") -> list[dict[str, object]]:
    """Fetch tasks from the task server REST API.

    Args:
        server_url: Base URL of the Bernstein task server.
        status: Task status filter (default ``"done"``).

    Returns:
        List of task dicts.  Empty list if server is unreachable.
    """
    try:
        import httpx

        resp = httpx.get(f"{server_url}/tasks", params={"status": status}, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        if isinstance(data, dict) and "tasks" in data:
            return data["tasks"]  # type: ignore[return-value]
        return []
    except Exception:  # noqa: BLE001
        return []


def _fetch_tasks_from_metrics(metrics_dir: Path, since_ts: float = 0.0) -> list[dict[str, object]]:
    """Read completed tasks from daily metric JSONL files.

    Args:
        metrics_dir: Path to ``.sdd/metrics/``.
        since_ts: Unix timestamp; only include records at or after this time.

    Returns:
        List of task-like dicts with at minimum ``task_id``, ``title``, ``role``.
    """
    tasks: list[dict[str, object]] = []
    if not metrics_dir.is_dir():
        return tasks

    # Daily metric files are named YYYY-MM-DD.jsonl and contain one record per
    # agent run, each with task_id, role, success, etc.
    for jsonl in sorted(metrics_dir.glob("*.jsonl")):
        # Skip non-date files (agent_success_*.jsonl, api_usage_*.jsonl, etc.)
        if not re.match(r"^\d{4}-\d{2}-\d{2}\.jsonl$", jsonl.name):
            continue
        try:
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp", 0)
                    if isinstance(ts, str):
                        # ISO format "2026-04-11T..."
                        import datetime

                        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    if float(ts) < since_ts:
                        continue
                    if rec.get("success") is True:
                        tasks.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
        except OSError:
            continue

    return tasks


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------


@dataclass
class TaskChange:
    """A single task's contribution to the changelog.

    Attributes:
        task_id: Bernstein task identifier.
        title: Human-readable task title.
        role: Agent role that executed the task (backend, cli, qa, …).
        result_summary: What the agent reported on completion.
        component: Dominant component affected.
        files: All files touched by this task.
        is_breaking: Whether any commit for this task contains a breaking change.
        commit_shas: Git commits linked to this task.
        summary: One-sentence plain-English summary of the change.
    """

    task_id: str
    title: str
    role: str
    result_summary: str
    component: str
    files: list[str] = field(default_factory=list)
    is_breaking: bool = False
    commit_shas: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class RunChangelog:
    """Structured changelog for a Bernstein run.

    Attributes:
        generated_at: Unix timestamp when changelog was created.
        since_ref: Git ref used as the run boundary (tag or commit SHA).
        tasks_total: Total completed tasks analysed.
        changes: All task changes, grouped by component.
        breaking_changes: Subset of changes that are breaking.
    """

    generated_at: float
    since_ref: str | None
    tasks_total: int
    changes: dict[str, list[TaskChange]]  # component → changes
    breaking_changes: list[TaskChange]


# ---------------------------------------------------------------------------
# Summary generation (deterministic, no LLM)
# ---------------------------------------------------------------------------

_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r":\s*(?P<desc>.+)$",
    re.IGNORECASE,
)

_TYPE_TO_VERB: dict[str, str] = {
    "feat": "Add",
    "fix": "Fix",
    "perf": "Optimize",
    "refactor": "Refactor",
    "docs": "Document",
    "test": "Test",
    "build": "Update build for",
    "ci": "Configure CI for",
    "chore": "Chore:",
    "style": "Style",
    "revert": "Revert",
}


def _make_summary(task_title: str, commit_subjects: list[str], files: list[str]) -> str:
    """Generate a plain-English one-sentence summary for a task change.

    Uses the task title as the primary source, augmented by commit messages.
    Falls back to file-based description when no useful commit message exists.

    Args:
        task_title: Human-readable task title from the task server.
        commit_subjects: Subject lines of commits linked to this task.
        files: Files changed.

    Returns:
        One-sentence summary string.
    """
    # Try to extract a clean description from the best commit subject
    best_desc: str | None = None
    for subject in commit_subjects:
        m = _CONVENTIONAL_RE.match(subject)
        if m:
            verb = _TYPE_TO_VERB.get(m.group("type").lower(), "Update")
            desc = m.group("desc").strip()
            best_desc = f"{verb} {desc}"
            break

    if best_desc:
        return best_desc

    # Fall back to task title (cleaned up)
    title = task_title.strip()
    if title:
        # Capitalise and trim trailing punctuation
        title = title[0].upper() + title[1:] if title else title
        return title.rstrip(".")

    # Last resort: describe files
    if files:
        names = ", ".join(Path(f).name for f in files[:3])
        suffix = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
        return f"Modify {names}{suffix}"

    return "Miscellaneous change"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_run_changelog(
    workdir: Path,
    *,
    server_url: str = "http://localhost:8052",
    since_ref: str | None = None,
    since_hours: float | None = None,
    include_no_commits: bool = False,
) -> RunChangelog:
    """Generate a changelog from agent-produced diffs for *workdir*.

    Queries the task server for completed tasks, maps each to its git commits
    (via ``Refs: #<task_id>`` footer), and derives per-component change records.

    Args:
        workdir: Root of the target git repository.
        server_url: Bernstein task server base URL.
        since_ref: Git ref (tag/SHA) marking the start of the run window.
            Only commits *after* this ref are considered.
        since_hours: If *since_ref* is not provided, limit to tasks completed
            in the last N hours.  Defaults to 24 hours.
        include_no_commits: If True, include tasks that have no matching git
            commits (useful for reporting tasks that failed to commit).

    Returns:
        A :class:`RunChangelog` with all changes grouped by component.
    """
    if since_hours is None and since_ref is None:
        since_hours = 24.0

    since_ts = time.time() - (since_hours * 3600) if since_hours else 0.0

    # ------------------------------------------------------------------
    # 1. Fetch completed tasks
    # ------------------------------------------------------------------
    tasks: list[dict[str, object]] = _fetch_tasks_from_server(server_url)
    if not tasks:
        # Fall back to metrics files (server may be down after run completes)
        metrics_dir = workdir / ".sdd" / "metrics"
        tasks = _fetch_tasks_from_metrics(metrics_dir, since_ts=since_ts)

    # ------------------------------------------------------------------
    # 2. For each task, find git commits and diff
    # ------------------------------------------------------------------
    changes_by_component: dict[str, list[TaskChange]] = defaultdict(list)
    breaking_changes: list[TaskChange] = []

    for task in tasks:
        task_id = str(task.get("task_id") or task.get("id") or "")
        title = str(task.get("title") or "Untitled")
        role = str(task.get("role") or "")
        result_summary = str(task.get("result_summary") or "")

        if not task_id:
            continue

        # Find associated commits
        commit_shas = _commits_for_task(task_id, workdir, since_ref=since_ref)

        if not commit_shas and not include_no_commits:
            continue

        # Aggregate files and check for breaking changes
        all_files: list[str] = []
        is_breaking = False
        commit_subjects: list[str] = []

        for sha in commit_shas:
            files = _files_in_commit(sha, workdir)
            all_files.extend(files)
            subject = _subject_for_commit(sha, workdir)
            commit_subjects.append(subject)

            # Check commit subject for breaking marker
            if "!" in subject or "BREAKING CHANGE" in subject.upper():
                is_breaking = True

            # Check diff content for structural breaking changes
            if not is_breaking:
                diff = _diff_for_commit(sha, workdir)
                if _is_breaking_diff(diff):
                    is_breaking = True

        # Deduplicate files preserving order
        seen: set[str] = set()
        unique_files: list[str] = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        component = _dominant_component(unique_files) if unique_files else _component_for_role(role)
        summary = _make_summary(title, commit_subjects, unique_files)

        change = TaskChange(
            task_id=task_id,
            title=title,
            role=role,
            result_summary=result_summary,
            component=component,
            files=unique_files,
            is_breaking=is_breaking,
            commit_shas=commit_shas,
            summary=summary,
        )
        changes_by_component[component].append(change)
        if is_breaking:
            breaking_changes.append(change)

    return RunChangelog(
        generated_at=time.time(),
        since_ref=since_ref,
        tasks_total=len(tasks),
        changes=dict(changes_by_component),
        breaking_changes=breaking_changes,
    )


def _component_for_role(role: str) -> str:
    """Fallback component from agent role when no files are available."""
    role_map: dict[str, str] = {
        "cli": "CLI",
        "backend": "Core Engine",
        "frontend": "CLI",
        "qa": "Tests",
        "devops": "CI/CD",
        "docs": "Documentation",
        "security": "Core Engine",
        "architect": "Core Engine",
    }
    return role_map.get(role.lower(), _DEFAULT_COMPONENT)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_markdown(cl: RunChangelog, *, repo_url: str | None = None) -> str:
    """Render *cl* as a GitHub-flavoured Markdown changelog.

    Args:
        cl: The changelog to render.
        repo_url: Optional repository URL; used to generate task links.

    Returns:
        Markdown string.
    """
    import datetime

    lines: list[str] = ["# Run Changelog", ""]

    ts = datetime.datetime.fromtimestamp(cl.generated_at).strftime("%Y-%m-%d %H:%M")
    since_label = cl.since_ref if cl.since_ref else "last 24 hours"
    lines.append(f"_Generated {ts} · {cl.tasks_total} tasks · since {since_label}_")
    lines.append("")

    if cl.breaking_changes:
        lines.append("## ⚠️ Breaking Changes")
        lines.append("")
        for ch in cl.breaking_changes:
            task_link = _task_link(ch.task_id, repo_url)
            lines.append(f"- **[{ch.component}]** {ch.summary} {task_link}")
        lines.append("")

    # Ordered component sections (most changes first)
    ordered = sorted(cl.changes.items(), key=lambda kv: -len(kv[1]))
    for component, changes in ordered:
        count = len(changes)
        lines.append(f"## {component} ({count} {'change' if count == 1 else 'changes'})")
        lines.append("")
        for ch in changes:
            breaking_flag = " ⚠️ **BREAKING**" if ch.is_breaking else ""
            task_link = _task_link(ch.task_id, repo_url)
            lines.append(f"- {ch.summary}{breaking_flag} {task_link}")
            if ch.result_summary:
                # Indent the agent's own summary as a sub-bullet
                lines.append(f"  _{ch.result_summary[:200]}_")
        lines.append("")

    if not cl.changes:
        lines.append("_No agent-produced changes found for this run window._")
        lines.append("")

    return "\n".join(lines)


def format_console(cl: RunChangelog, *, repo_url: str | None = None) -> str:
    """Render *cl* as Rich-markup console output.

    Args:
        cl: The changelog to render.
        repo_url: Optional repository URL; used to generate task links.

    Returns:
        Rich markup string suitable for ``console.print()``.
    """
    import datetime

    parts: list[str] = []

    ts = datetime.datetime.fromtimestamp(cl.generated_at).strftime("%Y-%m-%d %H:%M")
    since_label = cl.since_ref if cl.since_ref else "last 24 hours"
    parts.append(f"[dim]Generated {ts} · {cl.tasks_total} tasks · since {since_label}[/dim]")
    parts.append("")

    if cl.breaking_changes:
        parts.append("[bold red]⚠️  Breaking Changes[/bold red]")
        for ch in cl.breaking_changes:
            parts.append(f"  [red]![/red] [bold]{ch.component}:[/bold] {ch.summary}  [dim]#{ch.task_id[:8]}[/dim]")
        parts.append("")

    ordered = sorted(cl.changes.items(), key=lambda kv: -len(kv[1]))
    for component, changes in ordered:
        count = len(changes)
        parts.append(f"[bold blue]{component}[/bold blue] [dim]({count})[/dim]")
        for ch in changes:
            breaking = " [bold red][BREAKING][/bold red]" if ch.is_breaking else ""
            parts.append(f"  • {ch.summary}{breaking}  [dim]#{ch.task_id[:8]}[/dim]")
        parts.append("")

    if not cl.changes:
        parts.append("[yellow]No agent-produced changes found for this run window.[/yellow]")

    return "\n".join(parts)


def _task_link(task_id: str, repo_url: str | None) -> str:
    """Return a markdown link or plain reference for *task_id*."""
    short = task_id[:8]
    if repo_url:
        return f"([`#{short}`]({repo_url}/issues/{task_id}))"
    return f"[`#{short}`]"
