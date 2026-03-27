"""Knowledge base, file indexing, and task context enrichment.

Provides:
- FileSummary: AST-based Python file structure
- TaskContextBuilder: Rich context for agent tasks
- File indexing and architecture documentation
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.models import Task

from bernstein.core.git_context import (
    cochange_files as _gc_cochange_files,
)
from bernstein.core.git_context import (
    ls_files_pattern as _gc_ls_files_pattern,
)
from bernstein.core.git_context import (
    recent_changes_multi as _gc_recent_changes_multi,
)

logger = logging.getLogger(__name__)


@dataclass
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
            importers = [p for p in result.stdout.strip().split("\n") if p]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return importers


def _git_cochanged_files(target_rel: str, workdir: Path, max_results: int = 5) -> list[str]:
    """Find files that often change together with the target file.

    Uses git's cochange data. Returns file paths that have been committed
    together with *target_rel* in recent history.

    Args:
        target_rel: Relative path of target file.
        workdir: Project root.
        max_results: Maximum files to return.

    Returns:
        List of related file paths.
    """
    try:
        return _gc_cochange_files(workdir, target_rel, max_results)
    except Exception as e:
        logger.debug(f"Cochange lookup failed for {target_rel}: {e}")
        return []


def _recent_git_changes(files: list[str], workdir: Path, max_entries: int = 5) -> list[str]:
    """Find recent git log entries for a set of files.

    Args:
        files: List of file paths to track.
        workdir: Project root.
        max_entries: Maximum log entries to return per file.

    Returns:
        List of formatted log entries.
    """
    if not files:
        return []
    try:
        return _gc_recent_changes_multi(workdir, files, max_entries)
    except Exception as e:
        logger.debug(f"Git log lookup failed: {e}")
        return []


def _subsystem_context(filepath: str, workdir: Path) -> str:
    """Build subsystem context for a file: imports, importers, cochanges.

    Args:
        filepath: Relative path of file (e.g., ``src/bernstein/core/spawner.py``).
        workdir: Project root.

    Returns:
        Formatted context string with dependency information.
    """
    sections: list[str] = []

    # Parse the file itself
    abs_path = workdir / filepath
    summary = _parse_python_file(abs_path)
    if summary:
        if summary.docstring:
            sections.append(f"**Module**: {summary.docstring}")
        if summary.classes:
            class_str = ", ".join(c[0] for c in summary.classes)
            sections.append(f"**Classes**: {class_str}")
        if summary.functions:
            func_str = ", ".join(summary.functions[:5])
            sections.append(f"**Functions**: {func_str}")

    # Importers
    importers = _find_importers(filepath, workdir)
    if importers:
        sections.append(f"**Imported by**: {', '.join(importers[:3])}")

    # Cochanged files
    cochanges = _git_cochanged_files(filepath, workdir, max_results=3)
    if cochanges:
        sections.append(f"**Often changes with**: {', '.join(cochanges)}")

    # Recent changes
    recent = _recent_git_changes([filepath], workdir, max_entries=2)
    if recent:
        sections.append(f"**Recent changes**: {recent[0]}")

    return "\n".join(sections)


@dataclass
class TaskContextBuilder:
    """Builder for enriching task prompts with codebase context.

    Provides file summaries, import graphs, related code samples,
    and subsystem context to help agents understand the scope quickly.

    Attributes:
        workdir: Project root directory.
        cache: Memoized file summaries.
    """

    workdir: Path
    cache: dict[str, FileSummary] = field(default_factory=dict)

    def file_summary(self, rel_path: str) -> FileSummary | None:
        """Get or parse a file summary for *rel_path*.

        Args:
            rel_path: Relative path from *workdir*.

        Returns:
            FileSummary or None if parsing fails.
        """
        if rel_path in self.cache:
            return self.cache[rel_path]

        abs_path = self.workdir / rel_path
        summary = _parse_python_file(abs_path)
        if summary:
            self.cache[rel_path] = summary
        return summary

    def file_context(self, rel_path: str, max_chars: int = 1500) -> str:
        """Build rich context for a single file.

        Includes:
        - File docstring
        - Class and function names
        - Imports (what it needs)
        - Importers (who depends on it)
        - Cochanged files (frequently modified together)
        - Recent git log

        Args:
            rel_path: Relative file path.
            max_chars: Approximate character limit for result.

        Returns:
            Formatted context string.
        """
        sections: list[str] = []
        sections.append(f"### {rel_path}")

        # File summary
        summary = self.file_summary(rel_path)
        if summary:
            if summary.docstring:
                sections.append(f"{summary.docstring}")
            if summary.classes or summary.functions:
                items = [c[0] for c in summary.classes] + summary.functions
                sections.append(f"**Exports**: {', '.join(items[:10])}")

        # Subsystem info
        subsystem = _subsystem_context(rel_path, self.workdir)
        if subsystem:
            sections.append(subsystem)

        # Join and truncate
        context = "\n".join(sections)
        if len(context) > max_chars:
            context = context[:max_chars] + "..."
        return context

    def task_context(self, files: list[str]) -> str:
        """Build task-level context for a set of files.

        Includes file context for each file plus cross-file dependency info.

        Args:
            files: List of relative file paths.

        Returns:
            Formatted task context string.
        """
        sections: list[str] = []

        # Per-file context
        for fpath in files[:5]:  # Limit to first 5 files
            sections.append(self.file_context(fpath, max_chars=800))

        # Cross-file info
        all_imports: set[str] = set()
        for fpath in files:
            summary = self.file_summary(fpath)
            if summary:
                all_imports.update(summary.imports)

        if all_imports:
            sections.append(f"\n**Imports used**: {', '.join(sorted(all_imports)[:15])}")

        # Cochanges across all files
        all_cochanges: list[str] = []
        for fpath in files[:2]:  # Check first 2 files only
            cochanges = _git_cochanged_files(fpath, self.workdir, max_results=2)
            all_cochanges.extend(cochanges)

        if all_cochanges:
            unique = sorted(set(all_cochanges))
            sections.append(f"\n**Related files**: {', '.join(unique[:5])}")

        return "\n".join(sections)

    def build_context(self, tasks: list[Task]) -> str:
        """Build compressed context for a task batch.

        Uses ContextCompressor to select only task-relevant files,
        then generates rich context for those files.  Falls back to
        task-owned file context if compression is unavailable.

        Args:
            tasks: Batch of tasks to build context for.

        Returns:
            Formatted context string with compressed file summaries.
        """
        from bernstein.core.context_compression import ContextCompressor

        sections: list[str] = []

        try:
            compressor = ContextCompressor(self.workdir)
            result = compressor.compress(tasks, max_files=15)

            reduction_pct = (1.0 - result.compression_ratio) * 100
            sections.append(
                f"## Context (auto-generated)\n"
                f"~{result.original_tokens} → ~{result.compressed_tokens} tokens "
                f"(**{reduction_pct:.0f}% reduction**, {len(result.selected_files)} files)\n"
            )

            for fpath in result.selected_files[:10]:
                file_ctx = self.file_context(fpath, max_chars=600)
                sections.append(file_ctx)

        except Exception as exc:
            logger.warning("ContextCompressor failed, falling back to uncompressed context: %s", exc)
            all_owned: list[str] = []
            for task in tasks:
                all_owned.extend(getattr(task, "owned_files", []))
            if all_owned:
                sections.append(self.task_context(all_owned))

        return "\n".join(sections) if sections else ""

    # Cached externally to avoid lru_cache on instance method
    def import_graph(self, filename: str) -> dict[str, list[str]]:
        """Build a reverse dependency map: who imports *filename*?

        Args:
            filename: Basename to search (e.g., ``spawner``).

        Returns:
            Dict mapping {importer_path: [import_statements]}.
        """
        result: dict[str, list[str]] = {}
        try:
            # Use git to find .py files, then grep each for imports
            files = _gc_ls_files_pattern(self.workdir, "*.py")
            for fpath in files[:50]:  # Limit search
                try:
                    (self.workdir / fpath).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                # Simple grep for filename
                try:
                    grep_result = subprocess.run(
                        ["grep", "-n", f"import.*{filename}", fpath],
                        cwd=self.workdir,
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if grep_result.returncode == 0:
                        lines = grep_result.stdout.strip().split("\n")
                        result[fpath] = [ln for ln in lines if ln]
                except subprocess.TimeoutExpired:
                    pass
        except Exception as e:
            logger.debug(f"import_graph failed for {filename}: {e}")

        return result


@dataclass
class FileIndexEntry:
    """Entry in the file index.

    Attributes:
        path: Relative file path.
        summary: AST summary of the file.
        language: Programming language (e.g., 'python', 'typescript').
    """

    path: str
    summary: dict[str, Any]  # From FileSummary
    language: str = "python"


def build_file_index(workdir: Path) -> dict[str, dict[str, object]]:
    """Build a comprehensive file index for the entire project.

    Indexes all .py files (and optionally .ts/.js/.go files) for quick lookup.

    Args:
        workdir: Project root.

    Returns:
        Dict mapping {file_path: {name, docstring, classes, functions}}.
    """
    index: dict[str, dict[str, object]] = {}

    try:
        py_files = _gc_ls_files_pattern(workdir, "*.py")
    except Exception:
        py_files = []

    for fpath in py_files:
        try:
            summary = _parse_python_file(workdir / fpath)
            if summary:
                index[fpath] = {
                    "docstring": summary.docstring,
                    "classes": summary.classes,
                    "functions": summary.functions,
                    "imports": summary.imports,
                }
        except Exception:
            pass

    return index


def build_architecture_md(workdir: Path) -> str:
    """Generate a Markdown overview of project architecture from file index.

    Args:
        workdir: Project root.

    Returns:
        Formatted Markdown string.
    """
    index = build_file_index(workdir)
    if not index:
        return ""

    # Group by directory
    by_dir: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for fpath, entry in index.items():
        dir_name = str(Path(fpath).parent)
        if dir_name not in by_dir:
            by_dir[dir_name] = []
        by_dir[dir_name].append((fpath, entry))

    lines = ["# Project Architecture\n"]
    for dir_name in sorted(by_dir.keys()):
        lines.append(f"\n## {dir_name}\n")
        for fpath, entry in by_dir[dir_name]:
            lines.append(f"- **{Path(fpath).name}**: {entry.get('docstring', 'N/A')}")

    return "\n".join(lines)


def refresh_knowledge_base(workdir: Path) -> None:
    """Refresh the knowledge base: re-index files and update architecture docs.

    Args:
        workdir: Project root.
    """
    kb_dir = workdir / ".sdd" / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    # Rebuild file index
    index = build_file_index(workdir)
    index_path = kb_dir / "file_index.json"
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    # Rebuild architecture.md
    arch = build_architecture_md(workdir)
    arch_path = kb_dir / "architecture.md"
    arch_path.write_text(arch, encoding="utf-8")

    logger.info(f"Knowledge base refreshed: {len(index)} files indexed")


def append_decision(workdir: Path, task_id: str, title: str, summary: str) -> None:
    """Append a decision/lesson to the knowledge base.

    Args:
        workdir: Project root.
        task_id: Task ID that produced the decision.
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
