"""Gather project context for the manager's planning prompt.

Reads the file tree, README, and .sdd/project.md to give the LLM
enough context to decompose a goal into well-scoped tasks.

Also provides ``TaskContextBuilder`` for enriching task prompts with
file summaries, dependency graphs, related changes, and subsystem
context — so spawned agents skip the "orientation" phase.
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git_context import (
    cochange_files as _gc_cochange_files,
)
from bernstein.core.git_context import (
    ls_files as _gc_ls_files,
)
from bernstein.core.git_context import (
    ls_files_pattern as _gc_ls_files_pattern,
)
from bernstein.core.git_context import (
    recent_changes_multi as _gc_recent_changes_multi,
)

if TYPE_CHECKING:
    from bernstein.core.models import ApiTier, Task

logger = logging.getLogger(__name__)

_file_tree_cache: dict[str, tuple[float, str]] = {}

_IGNORED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "dist",
        "build",
        ".egg-info",
        ".tox",
        ".sdd/runtime",
    }
)

_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info"})


def _should_skip(path: Path) -> bool:
    """Return True if *path* should be excluded from the file tree."""
    for part in path.parts:
        if part in _IGNORED_DIRS:
            return True
    return path.suffix in _IGNORED_SUFFIXES


_FILE_TREE_TTL = 60.0  # seconds


def clear_caches() -> None:
    """Reset all module-level caches. Useful for tests."""
    _file_tree_cache.clear()


def file_tree(workdir: Path, max_lines: int = 50) -> str:
    """Build a compact file-tree listing of the project.

    Uses ``git ls-files`` when inside a git repo (fast, respects
    .gitignore). Falls back to a recursive walk with heuristic filters.

    Results are cached per *workdir* for 60 seconds to avoid repeated
    subprocess calls during a single orchestrator run.

    Args:
        workdir: Project root directory.
        max_lines: Maximum number of lines to include.

    Returns:
        A newline-separated file listing, truncated to *max_lines*.
    """
    cache_key = str(workdir)
    now = time.monotonic()
    if cache_key in _file_tree_cache:
        cached_time, cached_result = _file_tree_cache[cache_key]
        if now - cached_time < _FILE_TREE_TTL:
            return cached_result

    lines: list[str] = []

    # Try git ls-files first — fast and .gitignore-aware.
    lines = _gc_ls_files(workdir)

    # Fallback: walk the directory tree.
    if not lines:
        for path in sorted(workdir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(workdir)
            if _should_skip(rel):
                continue
            lines.append(str(rel))

    if len(lines) > max_lines:
        truncated = lines[:max_lines]
        truncated.append(f"... ({len(lines) - max_lines} more files)")
        output = "\n".join(truncated)
    else:
        output = "\n".join(lines)

    _file_tree_cache[cache_key] = (now, output)
    return output


def _read_if_exists(path: Path, max_chars: int = 4000) -> str | None:
    """Read a text file, returning None if it doesn't exist.

    Args:
        path: File to read.
        max_chars: Truncate content to this many characters.

    Returns:
        File content (possibly truncated) or None.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text


def available_roles(templates_dir: Path) -> list[str]:
    """Discover available specialist roles from the templates directory.

    Each subdirectory of *templates_dir* that contains a
    ``system_prompt.md`` is treated as a valid role.

    Args:
        templates_dir: Path to ``templates/roles/``.

    Returns:
        Sorted list of role names.
    """
    if not templates_dir.is_dir():
        return []
    roles: list[str] = []
    for child in sorted(templates_dir.iterdir()):
        if child.is_dir() and (child / "system_prompt.md").exists():
            roles.append(child.name)
    return roles


def gather_project_context(workdir: Path, max_lines: int = 100) -> str:
    """Gather project context for the manager: file tree, README, .sdd/project.md.

    Args:
        workdir: Project root directory.
        max_lines: Maximum file-tree lines.

    Returns:
        Formatted context string ready for prompt injection.
    """
    sections: list[str] = []

    # File tree
    tree = file_tree(workdir, max_lines=max_lines)
    if tree:
        sections.append(f"## File tree\n```\n{tree}\n```")

    # README
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = _read_if_exists(workdir / name)
        if readme:
            sections.append(f"## README\n{readme}")
            break

    # .sdd/project.md
    project_md = _read_if_exists(workdir / ".sdd" / "project.md")
    if project_md:
        sections.append(f"## Project description (.sdd/project.md)\n{project_md}")

    return "\n\n".join(sections) if sections else "(no project context available)"


# ---------------------------------------------------------------------------
# Task Context Builder — rich context injection for spawned agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSummary:
    """AST-derived summary of a Python file.

    Attributes:
        path: Relative path from project root.
        docstring: Module-level docstring (first line, truncated).
        classes: List of class names with their method names.
        functions: Top-level function names.
        imports: Module names imported by this file.
    """

    path: str
    docstring: str
    classes: list[tuple[str, list[str]]]  # (class_name, [method_names])
    functions: list[str]
    imports: list[str]


def _parse_python_file(filepath: Path) -> FileSummary | None:
    """Parse a Python file and extract structural summary via AST.

    Args:
        filepath: Absolute path to the Python file.

    Returns:
        FileSummary or None if parsing fails.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None

    # Module docstring
    docstring = ""
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        raw = tree.body[0].value.value.strip()
        # First line only, truncated
        docstring = raw.split("\n")[0][:120]

    classes: list[tuple[str, list[str]]] = []
    functions: list[str] = []
    imports: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
            ]
            classes.append((node.name, methods))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])

    # Deduplicate imports
    imports = sorted(set(imports))

    return FileSummary(
        path=str(filepath),
        docstring=docstring,
        classes=classes,
        functions=functions,
        imports=imports,
    )


def _find_importers(target_rel: str, workdir: Path) -> list[str]:
    """Find Python files that import the given module (by relative path).

    Uses a fast grep for the module name rather than full AST parsing
    of the entire project.

    Args:
        target_rel: Relative path like ``src/bernstein/core/spawner.py``.
        workdir: Project root.

    Returns:
        List of relative paths that import the target module.
    """
    # Convert path to module-style name for grep: "bernstein.core.spawner"
    # Also try the last component for relative imports
    basename = Path(target_rel).stem

    importers: list[str] = []
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include=*.py", basename, "."],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                rel = line.lstrip("./")
                if rel != target_rel:
                    importers.append(rel)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return sorted(set(importers))[:10]  # Cap at 10


@functools.lru_cache(maxsize=256)
def _git_cochanged_files(target_rel: str, workdir: Path, max_results: int = 5) -> list[str]:
    """Find files frequently co-modified with the target in git history.

    Delegates to ``git_context.cochange_files`` for the actual git queries.
    Results are cached via ``lru_cache`` so repeated calls within the same
    process are free.

    Args:
        target_rel: Relative path of the file to analyze.
        workdir: Project root (git repo).
        max_results: Maximum number of co-changed files to return.

    Returns:
        List of relative paths, most frequently co-changed first.
    """
    pairs = _gc_cochange_files(workdir, target_rel, depth=20, max_results=max_results)
    return [f for f, _ in pairs]


def _recent_git_changes(files: list[str], workdir: Path, max_entries: int = 5) -> list[str]:
    """Get recent git commit summaries touching any of the given files.

    Delegates to ``git_context.recent_changes_multi``.

    Args:
        files: Relative file paths to check.
        workdir: Project root (git repo).
        max_entries: Maximum number of log entries.

    Returns:
        List of formatted commit lines: "hash: subject".
    """
    return _gc_recent_changes_multi(workdir, files, max_entries=max_entries)


def _subsystem_context(filepath: str, workdir: Path) -> str:
    """Get subsystem-level context from directory README or __init__.py docstring.

    Args:
        filepath: Relative path of a file.
        workdir: Project root.

    Returns:
        Context string or empty.
    """
    parent = (workdir / filepath).parent
    # Try README.md in the directory
    readme = parent / "README.md"
    if readme.is_file():
        text = _read_if_exists(readme, max_chars=1000)
        if text:
            return text

    # Try __init__.py docstring
    init = parent / "__init__.py"
    if init.is_file():
        summary = _parse_python_file(init)
        if summary and summary.docstring:
            return summary.docstring

    return ""


class TaskContextBuilder:
    """Enriches task descriptions with file summaries, dependencies, and history.

    Uses AST parsing (not LLM) to generate structural context so spawned
    agents can skip codebase exploration and start working immediately.

    Args:
        workdir: Project working directory.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    def build_context(self, tasks: list[Task]) -> str:
        """Build rich context block for a batch of tasks.

        Analyzes owned_files from all tasks, generates file summaries,
        dependency info, co-change history, and subsystem context.

        Args:
            tasks: Batch of tasks (typically same role).

        Returns:
            Formatted context string ready for prompt injection.
        """
        # Collect all owned files across the batch
        owned: list[str] = []
        for task in tasks:
            owned.extend(task.owned_files)
        owned = sorted(set(owned))

        if not owned:
            return ""

        sections: list[str] = []
        sections.append("### Context (auto-generated)")
        sections.append("#### Files you'll work with:")

        all_related: list[str] = []
        for rel_path in owned:
            abs_path = self._workdir / rel_path
            file_section = self._build_file_section(rel_path, abs_path)
            if file_section:
                sections.append(file_section)

            # Collect related files for recent changes
            all_related.extend(_git_cochanged_files(rel_path, self._workdir, max_results=3))

        # Recent changes across all owned + related files
        all_files = owned + sorted(set(all_related))
        recent = _recent_git_changes(all_files, self._workdir, max_entries=5)
        if recent:
            sections.append("\n#### Related recent changes:")
            for entry in recent:
                sections.append(f"- commit {entry}")

        # Subsystem context (deduplicated by directory)
        seen_dirs: set[str] = set()
        subsystem_notes: list[str] = []
        for rel_path in owned:
            parent_str = str(Path(rel_path).parent)
            if parent_str not in seen_dirs:
                seen_dirs.add(parent_str)
                ctx = _subsystem_context(rel_path, self._workdir)
                if ctx:
                    subsystem_notes.append(f"**{parent_str}/**: {ctx}")

        if subsystem_notes:
            sections.append("\n#### Architecture notes:")
            for note in subsystem_notes:
                sections.append(f"- {note}")

        # Knowledge base: recent decisions
        decisions = self._load_recent_decisions()
        if decisions:
            sections.append("\n#### Recent decisions from other agents:")
            sections.append(decisions)

        return "\n".join(sections)

    def _build_file_section(self, rel_path: str, abs_path: Path) -> str:
        """Build context section for a single file.

        Args:
            rel_path: Relative path from project root.
            abs_path: Absolute path to the file.

        Returns:
            Formatted file section string.
        """
        lines: list[str] = [f"- **{rel_path}**"]

        if abs_path.suffix == ".py" and abs_path.is_file():
            summary = _parse_python_file(abs_path)
            if summary:
                if summary.docstring:
                    lines[0] += f": {summary.docstring}"
                if summary.classes:
                    for cls_name, methods in summary.classes:
                        method_str = ", ".join(methods[:8])
                        if len(methods) > 8:
                            method_str += ", ..."
                        lines.append(f"  - Class `{cls_name}`: {method_str}")
                if summary.functions:
                    func_str = ", ".join(f"`{f}`" for f in summary.functions[:10])
                    lines.append(f"  - Functions: {func_str}")
                if summary.imports:
                    lines.append(f"  - Imports: {', '.join(summary.imports[:10])}")

            importers = _find_importers(rel_path, self._workdir)
            if importers:
                lines.append(f"  - Imported by: {', '.join(importers[:5])}")
        elif not abs_path.is_file():
            lines[0] += " (file not found)"

        return "\n".join(lines)

    def _load_recent_decisions(self) -> str:
        """Load recent decisions from knowledge base.

        Returns:
            Formatted recent decisions or empty string.
        """
        decisions_path = self._workdir / ".sdd" / "knowledge" / "recent_decisions.md"
        if not decisions_path.is_file():
            return ""
        text = _read_if_exists(decisions_path, max_chars=2000)
        return text or ""


# ---------------------------------------------------------------------------
# Knowledge Base — .sdd/knowledge/ generation and maintenance
# ---------------------------------------------------------------------------


@dataclass
class FileIndexEntry:
    """Entry in the file index knowledge base.

    Attributes:
        path: Relative file path.
        summary: Module docstring (first line).
        exports: List of public class/function names.
        imports: List of imported module names.
        last_modified: ISO timestamp of last modification.
    """

    path: str
    summary: str
    exports: list[str]
    imports: list[str]
    last_modified: str


def build_file_index(workdir: Path) -> dict[str, dict[str, object]]:
    """Build a file index mapping Python files to their structural info.

    Scans all Python files tracked by git and extracts AST summaries.

    Args:
        workdir: Project root directory.

    Returns:
        Dict mapping relative path to index entry data.
    """
    index: dict[str, dict[str, object]] = {}

    # Get Python files from git
    py_files = _gc_ls_files_pattern(workdir, "*.py")
    if not py_files:
        return index

    for rel_path in py_files:
        abs_path = workdir / rel_path
        if not abs_path.is_file():
            continue

        summary = _parse_python_file(abs_path)
        if summary is None:
            continue

        exports: list[str] = []
        for cls_name, _methods in summary.classes:
            exports.append(cls_name)
        for func_name in summary.functions:
            if not func_name.startswith("_"):
                exports.append(func_name)

        # Get last modified time
        try:
            mtime = datetime.fromtimestamp(abs_path.stat().st_mtime).isoformat()
        except OSError:
            mtime = ""

        index[rel_path] = {
            "summary": summary.docstring,
            "exports": exports,
            "imports": summary.imports,
            "last_modified": mtime,
        }

    return index


def build_architecture_md(workdir: Path) -> str:
    """Generate an architecture.md summarizing the project's module structure.

    Args:
        workdir: Project root directory.

    Returns:
        Markdown string with module map and key signatures.
    """
    sections: list[str] = ["# Architecture Map (auto-generated)\n"]

    # Find Python packages (directories with __init__.py)
    init_files = sorted(_gc_ls_files_pattern(workdir, "*/__init__.py"))
    if not init_files:
        return ""

    for init_path in init_files:
        pkg_dir = str(Path(init_path).parent)
        sections.append(f"## {pkg_dir}/")

        # Parse __init__.py for package docstring
        abs_init = workdir / init_path
        summary = _parse_python_file(abs_init)
        if summary and summary.docstring:
            sections.append(f"_{summary.docstring}_\n")

        # List modules in this package
        pkg_py_files = _gc_ls_files_pattern(workdir, f"{pkg_dir}/*.py")
        for mod_path in sorted(pkg_py_files):
            if mod_path == init_path:
                continue
            mod_name = Path(mod_path).stem
            mod_summary = _parse_python_file(workdir / mod_path)
            if mod_summary:
                doc = f" — {mod_summary.docstring}" if mod_summary.docstring else ""
                exports: list[str] = []
                for cls_name, _ in mod_summary.classes:
                    exports.append(cls_name)
                for fn in mod_summary.functions:
                    if not fn.startswith("_"):
                        exports.append(fn)
                export_str = f" [{', '.join(exports[:6])}]" if exports else ""
                sections.append(f"- `{mod_name}`{doc}{export_str}")
            else:
                sections.append(f"- `{mod_name}`")

        sections.append("")

    return "\n".join(sections)


def refresh_knowledge_base(workdir: Path) -> None:
    """Regenerate the .sdd/knowledge/ files.

    Creates or updates:
    - file_index.json — per-file structural summaries
    - architecture.md — module map with signatures

    Args:
        workdir: Project root directory.
    """
    knowledge_dir = workdir / ".sdd" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # file_index.json
    index = build_file_index(workdir)
    index_path = knowledge_dir / "file_index.json"
    with index_path.open("w") as f:
        json.dump(index, f, indent=2)

    # architecture.md
    arch = build_architecture_md(workdir)
    if arch:
        (knowledge_dir / "architecture.md").write_text(arch, encoding="utf-8")

    # Ensure recent_decisions.md exists
    decisions_path = knowledge_dir / "recent_decisions.md"
    if not decisions_path.exists():
        decisions_path.write_text(
            "# Recent Decisions\n\nNo decisions recorded yet.\n",
            encoding="utf-8",
        )

    logger.info("Knowledge base refreshed at %s", knowledge_dir)


def append_decision(workdir: Path, task_id: str, title: str, summary: str) -> None:
    """Append a decision/finding from a completed task to the knowledge base.

    Keeps the last 15 entries to prevent unbounded growth.

    Args:
        workdir: Project root directory.
        task_id: Task identifier.
        title: Task title.
        summary: Result summary from the agent.
    """
    decisions_path = workdir / ".sdd" / "knowledge" / "recent_decisions.md"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{timestamp}] {title} ({task_id})\n{summary}\n"

    # Read existing content
    existing = ""
    if decisions_path.is_file():
        existing = decisions_path.read_text(encoding="utf-8")

    # Parse entries (split on ## [ pattern), keep last 14 + new one = 15
    parts = existing.split("\n## [")
    header = parts[0] if parts else "# Recent Decisions\n"
    entries = [f"\n## [{p}" for p in parts[1:]] if len(parts) > 1 else []
    entries.append(entry)
    entries = entries[-15:]  # Keep last 15

    decisions_path.write_text(header + "".join(entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# API Usage Tracking
# ---------------------------------------------------------------------------


@dataclass
class ApiCallRecord:
    """Record of a single API call.

    Attributes:
        timestamp: Unix timestamp of the call.
        provider: API provider name (e.g., "openrouter", "anthropic").
        model: Model name used (e.g., "claude-sonnet-4-20250514").
        agent_session_id: ID of the agent session that made the call.
        tokens_input: Number of input tokens.
        tokens_output: Number of output tokens.
        tokens_total: Total tokens used.
        cost_usd: Cost in USD for this call.
        latency_ms: Request latency in milliseconds.
        success: Whether the call succeeded.
        error: Error message if failed.
    """

    timestamp: float
    provider: str
    model: str
    agent_session_id: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    success: bool = True
    error: str | None = None


@dataclass
class ProviderUsageSummary:
    """Aggregated usage summary for a provider.

    Attributes:
        provider: Provider name.
        total_calls: Total number of API calls.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total cost in USD.
        successful_calls: Number of successful calls.
        failed_calls: Number of failed calls.
        avg_latency_ms: Average latency across calls.
        models_used: Set of model names used.
    """

    provider: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    successful_calls: int = 0
    failed_calls: int = 0
    avg_latency_ms: float = 0.0
    models_used: set[str] = field(default_factory=set[str])


@dataclass
class AgentSessionUsage:
    """Usage summary for an agent session.

    Attributes:
        agent_session_id: Agent session identifier.
        total_calls: Total API calls made by this session.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total cost in USD.
        providers_used: Set of providers used.
        start_time: First call timestamp.
        last_activity: Last call timestamp.
    """

    agent_session_id: str
    total_calls: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    providers_used: set[str] = field(default_factory=set[str])
    start_time: float | None = None
    last_activity: float | None = None


@dataclass
class TierConsumption:
    """Tier-based consumption tracking.

    Attributes:
        provider: Provider name.
        tier: API tier (free, plus, pro, etc.).
        tokens_used: Tokens consumed in this tier.
        tokens_limit: Tier token limit (if applicable).
        requests_used: Requests made in this tier.
        requests_limit: Tier request limit (if applicable).
        percentage_used: Percentage of tier quota used.
    """

    provider: str
    tier: ApiTier
    tokens_used: int = 0
    tokens_limit: int | None = None
    requests_used: int = 0
    requests_limit: int | None = None
    percentage_used: float = 0.0


class ApiUsageTracker:
    """Background service that tracks API calls, token usage, costs, and tier consumption.

    Tracks metrics per provider and per agent session, storing them in memory
    and optionally persisting to .sdd/metrics/ directory.

    Args:
        metrics_dir: Directory to store metrics files. Defaults to .sdd/metrics/.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        self._metrics_dir = metrics_dir or Path.cwd() / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)

        # In-memory tracking
        self._calls: list[ApiCallRecord] = []
        self._provider_summaries: dict[str, ProviderUsageSummary] = {}
        self._agent_summaries: dict[str, AgentSessionUsage] = {}
        self._tier_consumption: dict[str, TierConsumption] = {}

        # EMA for latency tracking
        self._provider_latency_ema: dict[str, float] = {}

    def record_call(
        self,
        provider: str,
        model: str,
        agent_session_id: str,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        success: bool = True,
        error: str | None = None,
    ) -> ApiCallRecord:
        """Record an API call.

        Args:
            provider: API provider name.
            model: Model name used.
            agent_session_id: ID of the agent session.
            tokens_input: Input tokens count.
            tokens_output: Output tokens count.
            cost_usd: Cost in USD.
            latency_ms: Request latency.
            success: Whether the call succeeded.
            error: Error message if failed.

        Returns:
            The recorded ApiCallRecord.
        """
        tokens_total = tokens_input + tokens_output
        timestamp = time.time()

        record = ApiCallRecord(
            timestamp=timestamp,
            provider=provider,
            model=model,
            agent_session_id=agent_session_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=tokens_total,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            success=success,
            error=error,
        )
        self._calls.append(record)
        self._update_aggregates(record)
        self._persist_record(record)

        return record

    def _update_aggregates(self, record: ApiCallRecord) -> None:
        """Update aggregated summaries with a new record.

        Args:
            record: New API call record.
        """
        # Update provider summary
        if record.provider not in self._provider_summaries:
            self._provider_summaries[record.provider] = ProviderUsageSummary(provider=record.provider)
        prov = self._provider_summaries[record.provider]
        prov.total_calls += 1
        prov.total_tokens += record.tokens_total
        prov.total_cost_usd += record.cost_usd
        if record.success:
            prov.successful_calls += 1
        else:
            prov.failed_calls += 1
        prov.models_used.add(record.model)

        # Update latency EMA
        alpha = 0.3
        if record.provider in self._provider_latency_ema:
            self._provider_latency_ema[record.provider] = (
                alpha * record.latency_ms + (1 - alpha) * self._provider_latency_ema[record.provider]
            )
        else:
            self._provider_latency_ema[record.provider] = record.latency_ms
        prov.avg_latency_ms = self._provider_latency_ema[record.provider]

        # Update agent session summary
        if record.agent_session_id not in self._agent_summaries:
            self._agent_summaries[record.agent_session_id] = AgentSessionUsage(agent_session_id=record.agent_session_id)
        agent = self._agent_summaries[record.agent_session_id]
        agent.total_calls += 1
        agent.total_tokens += record.tokens_total
        agent.total_cost_usd += record.cost_usd
        agent.providers_used.add(record.provider)
        if agent.start_time is None:
            agent.start_time = record.timestamp
        agent.last_activity = record.timestamp

    def _persist_record(self, record: ApiCallRecord) -> None:
        """Persist a record to the metrics directory.

        Args:
            record: API call record to persist.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"api_calls_{today}.jsonl"
        filepath = self._metrics_dir / filename

        data = {
            "timestamp": record.timestamp,
            "provider": record.provider,
            "model": record.model,
            "agent_session_id": record.agent_session_id,
            "tokens_input": record.tokens_input,
            "tokens_output": record.tokens_output,
            "tokens_total": record.tokens_total,
            "cost_usd": record.cost_usd,
            "latency_ms": record.latency_ms,
            "success": record.success,
            "error": record.error,
        }

        with filepath.open("a") as f:
            f.write(json.dumps(data) + "\n")

    def set_tier_consumption(
        self,
        provider: str,
        tier: ApiTier,
        tokens_used: int = 0,
        tokens_limit: int | None = None,
        requests_used: int = 0,
        requests_limit: int | None = None,
    ) -> None:
        """Set or update tier consumption for a provider.

        Args:
            provider: Provider name.
            tier: API tier.
            tokens_used: Tokens consumed.
            tokens_limit: Token limit for tier.
            requests_used: Requests made.
            requests_limit: Request limit for tier.
        """
        key = f"{provider}:{tier.value}"
        percentage = 0.0
        if tokens_limit:
            percentage = max(percentage, tokens_used / tokens_limit * 100)
        if requests_limit:
            percentage = max(percentage, requests_used / requests_limit * 100)

        self._tier_consumption[key] = TierConsumption(
            provider=provider,
            tier=tier,
            tokens_used=tokens_used,
            tokens_limit=tokens_limit,
            requests_used=requests_used,
            requests_limit=requests_limit,
            percentage_used=percentage,
        )

    def get_provider_summary(self, provider: str) -> ProviderUsageSummary | None:
        """Get usage summary for a specific provider.

        Args:
            provider: Provider name.

        Returns:
            ProviderUsageSummary or None if not found.
        """
        return self._provider_summaries.get(provider)

    def get_agent_summary(self, agent_session_id: str) -> AgentSessionUsage | None:
        """Get usage summary for a specific agent session.

        Args:
            agent_session_id: Agent session ID.

        Returns:
            AgentSessionUsage or None if not found.
        """
        return self._agent_summaries.get(agent_session_id)

    def get_all_provider_summaries(self) -> dict[str, ProviderUsageSummary]:
        """Get all provider usage summaries.

        Returns:
            Dict of provider name to ProviderUsageSummary.
        """
        return dict(self._provider_summaries)

    def get_all_agent_summaries(self) -> dict[str, AgentSessionUsage]:
        """Get all agent session usage summaries.

        Returns:
            Dict of agent session ID to AgentSessionUsage.
        """
        return dict(self._agent_summaries)

    def get_tier_consumption(self, provider: str) -> list[TierConsumption]:
        """Get tier consumption for a provider.

        Args:
            provider: Provider name.

        Returns:
            List of TierConsumption for all tiers.
        """
        return [tc for _, tc in self._tier_consumption.items() if tc.provider == provider]

    def get_global_summary(self) -> dict[str, str]:
        """Get a global summary of all API usage.

        Returns:
            Dict with aggregated metrics as string values for endpoint exposure.
        """
        total_calls = sum(p.total_calls for p in self._provider_summaries.values())
        total_tokens = sum(p.total_tokens for p in self._provider_summaries.values())
        total_cost = sum(p.total_cost_usd for p in self._provider_summaries.values())
        total_success = sum(p.successful_calls for p in self._provider_summaries.values())
        total_failed = sum(p.failed_calls for p in self._provider_summaries.values())

        return {
            "total_api_calls": str(total_calls),
            "total_tokens_consumed": str(total_tokens),
            "total_cost_usd": f"{total_cost:.4f}",
            "successful_calls": str(total_success),
            "failed_calls": str(total_failed),
            "success_rate": f"{total_success / total_calls:.2%}" if total_calls > 0 else "N/A",
            "providers_active": str(len(self._provider_summaries)),
            "agent_sessions_active": str(len(self._agent_summaries)),
        }

    def get_summary_for_agent(self, agent_session_id: str) -> dict[str, str]:
        """Get usage summary for a specific agent session.

        Args:
            agent_session_id: Agent session ID.

        Returns:
            Dict with metrics as string values.
        """
        agent = self._agent_summaries.get(agent_session_id)
        if not agent:
            return {"error": "Agent session not found"}

        return {
            "agent_session_id": agent.agent_session_id,
            "total_calls": str(agent.total_calls),
            "total_tokens": str(agent.total_tokens),
            "total_cost_usd": f"{agent.total_cost_usd:.4f}",
            "providers_used": ", ".join(sorted(agent.providers_used)),
            "start_time": datetime.fromtimestamp(agent.start_time).isoformat() if agent.start_time else "N/A",
            "last_activity": datetime.fromtimestamp(agent.last_activity).isoformat() if agent.last_activity else "N/A",
        }

    def export_summary(self, output_path: Path) -> None:
        """Export full usage summary to a JSON file.

        Args:
            output_path: Path to write the export.
        """
        data = {
            "exported_at": datetime.now().isoformat(),
            "global_summary": self.get_global_summary(),
            "provider_summaries": {
                name: {
                    "provider": s.provider,
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "total_cost_usd": round(s.total_cost_usd, 4),
                    "successful_calls": s.successful_calls,
                    "failed_calls": s.failed_calls,
                    "avg_latency_ms": round(s.avg_latency_ms, 2),
                    "models_used": sorted(s.models_used),
                }
                for name, s in self._provider_summaries.items()
            },
            "agent_summaries": {
                aid: {
                    "agent_session_id": s.agent_session_id,
                    "total_calls": s.total_calls,
                    "total_tokens": s.total_tokens,
                    "total_cost_usd": round(s.total_cost_usd, 4),
                    "providers_used": sorted(s.providers_used),
                }
                for aid, s in self._agent_summaries.items()
            },
            "tier_consumption": {
                key: {
                    "provider": tc.provider,
                    "tier": tc.tier.value,
                    "tokens_used": tc.tokens_used,
                    "tokens_limit": tc.tokens_limit,
                    "requests_used": tc.requests_used,
                    "requests_limit": tc.requests_limit,
                    "percentage_used": round(tc.percentage_used, 2),
                }
                for key, tc in self._tier_consumption.items()
            },
        }

        with output_path.open("w") as f:
            json.dump(data, f, indent=2)


# Global instance for easy access
_default_usage_tracker: ApiUsageTracker | None = None


def get_usage_tracker(metrics_dir: Path | None = None) -> ApiUsageTracker:
    """Get or create the default API usage tracker.

    Args:
        metrics_dir: Optional custom metrics directory.

    Returns:
        ApiUsageTracker instance.
    """
    global _default_usage_tracker
    if _default_usage_tracker is None:
        _default_usage_tracker = ApiUsageTracker(metrics_dir)
    return _default_usage_tracker
