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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.models import Task


from bernstein.core.git_context import (
    cochange_files as _git_cochanged_files,
)
from bernstein.core.git_context import (
    ls_files_pattern as _gc_ls_files_pattern,
)
from bernstein.core.git_context import (
    recent_changes_multi as _recent_git_changes,
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

    return FileSummary(
        path="",  # Filled by caller
        docstring=docstring,
        classes=classes,
        functions=functions,
        imports=sorted(list(set(imports))),
    )


def _subsystem_context(rel_path: str, workdir: Path) -> str:
    """Extract minimal subsystem context (docstring) for a file.

    Args:
        rel_path: Relative path to the file.
        workdir: Project root directory.

    Returns:
        The first line of the module docstring, or empty string.
    """
    abspath = workdir / rel_path
    if not abspath.exists():
        return ""
    summary = _parse_python_file(abspath)
    return summary.docstring if summary else ""


def _find_importers(rel_path: str, workdir: Path) -> list[str]:
    """Find files that import the given file.

    Args:
        rel_path: Relative path to the file.
        workdir: Project root directory.

    Returns:
        List of relative paths to importing files.
    """
    # Simplified implementation for now
    return []


class TaskContextBuilder:
    """Builds rich context strings for agent tasks."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self._summaries: dict[str, FileSummary] = {}

    def file_summary(self, rel_path: str) -> FileSummary | None:
        """Get structural summary for a file (cached)."""
        if rel_path in self._summaries:
            return self._summaries[rel_path]

        abspath = self.workdir / rel_path
        if not abspath.exists():
            return None

        summary = _parse_python_file(abspath)
        if summary:
            summary.path = rel_path
            self._summaries[rel_path] = summary
        return summary

    def file_context(self, rel_path: str, max_chars: int = 1000) -> str:
        """Build context string for a single file.

        Args:
            rel_path: Relative path to file.
            max_chars: Maximum characters for the context string.

        Returns:
            Formatted context string.
        """
        summary = self.file_summary(rel_path)
        if not summary:
            return f"### {rel_path}\n(file summary unavailable)\n"

        sections = [f"### {rel_path}"]
        if summary.docstring:
            sections.append(f"**Docstring**: {summary.docstring}")

        if summary.classes:
            cls_info = []
            for name, methods in summary.classes[:10]:
                methods_str = ", ".join(methods[:8])
                if len(methods) > 8:
                    methods_str += ", ..."
                cls_info.append(f"- `class {name}`: {methods_str}")
            sections.append("**Classes**:\n" + "\n".join(cls_info))

        if summary.functions:
            funcs = ", ".join(summary.functions[:15])
            if len(summary.functions) > 15:
                funcs += ", ..."
            sections.append(f"**Functions**: {funcs}")

        # Add importers and cochanges for richer context
        importers = _find_importers(rel_path, self.workdir)
        if importers:
            sections.append(f"**Imported by**: {', '.join(importers[:5])}")

        cochanges = _git_cochanged_files(rel_path, self.workdir, max_results=3)
        if cochanges:
            sections.append(f"**Often changes with**: {', '.join(cochanges)}")

        recent = _recent_git_changes(self.workdir, [rel_path], max_entries=2)
        if recent:
            sections.append(f"**Recent changes**: {', '.join(recent)}")

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

    def build_context(self, tasks: list[Task], store: Any | None = None) -> str:
        """Build compressed context for a task batch.

        Uses ContextCompressor to select only task-relevant files,
        then generates rich context for those files.  Falls back to
        task-owned file context if compression is unavailable.

        Includes owned_files from parent tasks to ensure context continuity.

        Args:
            tasks: Batch of tasks to build context for.
            store: Optional TaskStore to look up parent tasks.

        Returns:
            Formatted context string with compressed file summaries.
        """
        from bernstein.core.context_compression import ContextCompressor

        sections: list[str] = []

        # 1. Expand tasks with parent owned_files if store is available
        if store is not None:
            for task in tasks:
                if task.parent_task_id:
                    try:
                        # We use sync wrapper or just check if it's in memory if possible
                        # For this implementation, we assume store has a way to get task by id
                        parent = getattr(store, "get_task", lambda tid: None)(task.parent_task_id)
                        if parent:
                            # Inherit owned_files from parent
                            task.owned_files = list(set(task.owned_files) | set(parent.owned_files))
                    except Exception:
                        continue

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
        path: Relative path from project root.
        last_modified: Timestamp of last modification.
        summary: AST structural summary.
    """

    path: str
    last_modified: datetime
    summary: FileSummary

    @property
    def docstring(self) -> str:
        """First line of module docstring."""
        return self.summary.docstring

    @property
    def classes(self) -> list[tuple[str, list[str]]]:
        """Class names and methods."""
        return self.summary.classes


def build_file_index(workdir: Path) -> dict[str, FileIndexEntry]:
    """Crawl project root and index all Python files."""
    index: dict[str, FileIndexEntry] = {}
    files = _gc_ls_files_pattern(workdir, "*.py")

    for fpath in files:
        abspath = workdir / fpath
        if not abspath.exists():
            continue

        mtime = datetime.fromtimestamp(abspath.stat().st_mtime)
        summary = _parse_python_file(abspath)
        if summary:
            summary.path = fpath
            index[fpath] = FileIndexEntry(path=fpath, last_modified=mtime, summary=summary)

    return index


def build_architecture_md(index: dict[str, FileIndexEntry]) -> str:
    """Generate architecture documentation from file index."""
    lines = ["# Architecture Overview", ""]

    # Group by directory
    by_dir: dict[str, list[FileIndexEntry]] = {}
    for entry in index.values():
        parent = str(Path(entry.path).parent)
        if parent not in by_dir:
            by_dir[parent] = []
        by_dir[parent].append(entry)

    for parent in sorted(by_dir.keys()):
        lines.append(f"## {parent}")
        for entry in sorted(by_dir[parent], key=lambda e: e.path):
            filename = Path(entry.path).name
            doc = entry.summary.docstring or "(no docstring)"
            lines.append(f"**{filename}**: {doc}")
        lines.append("")

    return "\n".join(lines)


def refresh_knowledge_base(workdir: Path) -> None:
    """Force refresh of all cached structural info and persist to disk."""
    kb_dir = workdir / ".sdd" / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    index = build_file_index(workdir)

    # Persist raw index as JSON for other tools
    from dataclasses import asdict

    def _json_serial(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    # Flatten data for JSON index to match test expectations
    index_data = {}
    for path, entry in index.items():
        entry_dict = asdict(entry)
        # Pull up summary fields
        summary = entry_dict.pop("summary")
        entry_dict.update(summary)
        index_data[path] = entry_dict

    (kb_dir / "file_index.json").write_text(json.dumps(index_data, default=_json_serial, indent=2), encoding="utf-8")

    arch_md = build_architecture_md(index)
    (kb_dir / "architecture.md").write_text(arch_md, encoding="utf-8")

    logger.info("Knowledge base refreshed: %d files indexed", len(index))


def append_decision(workdir: Path, task_id: str, title: str, decision: str) -> None:
    """Append a key architecture decision to the project knowledge base."""
    kb_dir = workdir / ".sdd" / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)

    # 1. Append to JSONL for machine reading
    jsonl_path = kb_dir / "decisions.jsonl"
    now_dt = datetime.now()
    record = {
        "timestamp": now_dt.isoformat(),
        "task_id": task_id,
        "title": title,
        "decision": decision,
    }
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    # 2. Append to Markdown for human reading
    md_path = kb_dir / "recent_decisions.md"
    ts_str = now_dt.strftime("%Y-%m-%d %H:%M")
    md_entry = f"\n## [{ts_str}] {title} ({task_id})\n{decision}\n"

    content = ""
    if md_path.exists():
        content = md_path.read_text(encoding="utf-8")

    # Keep header
    header = "# Recent Decisions\n"
    if content.startswith("#"):
        parts = content.split("\n## [", 1)
        header = parts[0]
        if not header.endswith("\n"):
            header += "\n"
        body = "## [" + parts[1] if len(parts) > 1 else ""
    else:
        body = content

    # Split into entries
    entries = ["## [" + e for e in body.split("## [") if e.strip()]
    entries.append(md_entry.strip())

    # Cap at 15
    if len(entries) > 15:
        entries = entries[-15:]

    md_path.write_text(header + "\n" + "\n\n".join(entries) + "\n", encoding="utf-8")
