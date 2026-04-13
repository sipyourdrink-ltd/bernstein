"""Dead code detector for post-agent-modification analysis.

Extends vulture-style detection with cross-codebase caller analysis:
checks if functions/classes removed or renamed in a diff still have
callers elsewhere in the project, and detects unreachable branches
and unused imports in changed files via AST.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Regex to extract function/class names from diff context lines
_FUNC_DEF_RE = re.compile(r"^-\s*(?:async\s+)?def\s+(\w+)\s*\(")
_CLASS_DEF_RE = re.compile(r"^-\s*class\s+(\w+)\s*[:(]")
_ADDED_FUNC_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+(\w+)\s*\(")
_ADDED_CLASS_RE = re.compile(r"^\+\s*class\s+(\w+)\s*[:(]")

# Ignore common dunder methods that should not be checked for callers
_IGNORE_NAMES = frozenset(
    {
        "__init__",
        "__new__",
        "__repr__",
        "__str__",
        "__eq__",
        "__hash__",
        "__len__",
        "__iter__",
        "__next__",
        "__enter__",
        "__exit__",
        "__del__",
        "__call__",
        "__getitem__",
        "__setitem__",
        "__delitem__",
        "__contains__",
        "__bool__",
        "__int__",
        "__float__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__ne__",
        "__add__",
        "__sub__",
        "__mul__",
        "__truediv__",
    }
)


@dataclass
class DeadCodeIssue:
    """A single dead-code finding.

    Attributes:
        kind: Category of issue (``lost_caller``, ``unused_import``,
            ``unreachable_branch``, ``no_callers``).
        name: Symbol name (function, class, or import).
        file: Source file path relative to workdir.
        detail: Human-readable explanation of the finding.
    """

    kind: str
    name: str
    file: str
    detail: str


@dataclass
class DeadCodeReport:
    """Aggregated dead-code analysis results.

    Attributes:
        issues: All detected issues.
        checked_files: Python source files that were analysed.
        searched_files: Files searched for caller references.
    """

    issues: list[DeadCodeIssue] = field(default_factory=list)
    checked_files: list[str] = field(default_factory=list)
    searched_files: int = 0

    @property
    def passed(self) -> bool:
        """Return True when no blocking issues were found."""
        return len(self.issues) == 0

    def summary(self) -> str:
        """Return a one-line summary suitable for a gate detail field."""
        if self.passed:
            return (
                f"No dead code found in {len(self.checked_files)} file(s), "
                f"searched {self.searched_files} file(s) for callers."
            )
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        return f"{len(self.issues)} issue(s): {', '.join(parts)}"


def _get_diff(workdir: Path, changed_files: list[str]) -> str:
    """Return the git diff for *changed_files* relative to HEAD~1."""
    try:
        cmd = ["git", "diff", "HEAD~1", "--"]
        cmd.extend(changed_files)
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        diff = result.stdout.strip()
        if not diff:
            # Fall back to staged diff
            result = subprocess.run(
                ["git", "diff", "--cached", "--"],
                cwd=workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            diff = result.stdout.strip()
        return diff
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to get git diff: %s", exc)
        return ""


def _extract_removed_names(diff: str) -> set[str]:
    """Parse *diff* and return names of functions/classes removed from the diff.

    Only lines prefixed with ``-`` (removed) are considered. Lines prefixed
    with ``+`` (added) represent new names and are excluded — if they were
    renamed, the old name is still removed and will be checked for callers.
    """
    names: set[str] = set()
    for line in diff.splitlines():
        m = _FUNC_DEF_RE.match(line)
        if m:
            names.add(m.group(1))
            continue
        m = _CLASS_DEF_RE.match(line)
        if m:
            names.add(m.group(1))
    return names - _IGNORE_NAMES


def _extract_added_names(diff: str) -> set[str]:
    """Parse *diff* and return names of new functions/classes added in the diff."""
    names: set[str] = set()
    for line in diff.splitlines():
        m = _ADDED_FUNC_RE.match(line)
        if m:
            names.add(m.group(1))
            continue
        m = _ADDED_CLASS_RE.match(line)
        if m:
            names.add(m.group(1))
    return names - _IGNORE_NAMES


def _find_callers_in_codebase(name: str, workdir: Path) -> list[str]:
    """Search for references to *name* across the Python codebase.

    Uses ``grep`` for speed, returning a list of ``file:line`` strings.
    Excludes lines that are definition lines (``def name`` or ``class name``).
    """
    try:
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "--include=*.py",
                r"\b" + re.escape(name) + r"\b",
                ".",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode not in (0, 1):
            return []
        callers: list[str] = []
        for line in result.stdout.splitlines():
            # Exclude definition lines themselves
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            content = parts[2]
            if re.match(r"\s*(?:async\s+)?def\s+" + re.escape(name) + r"\s*\(", content):
                continue
            if re.match(r"\s*class\s+" + re.escape(name) + r"\s*[:(]", content):
                continue
            callers.append(f"{parts[0]}:{parts[1]}")
        return callers
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to search for callers of %r: %s", name, exc)
        return []


def _count_py_files(workdir: Path) -> int:
    """Count Python source files under *workdir* (fast glob)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return len(result.stdout.strip().splitlines())
    except (subprocess.TimeoutExpired, OSError):
        return 0


def _collect_imported_names(tree: ast.Module) -> dict[str, int]:
    """Collect all imported names and their line numbers from an AST."""
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                imported[bound] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    return imported


def _collect_used_names(tree: ast.Module) -> set[str]:
    """Collect all referenced names from an AST."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    return used


def _check_unused_imports(source: str, rel_path: str) -> list[DeadCodeIssue]:
    """Detect unused imports in *source* via AST analysis.

    Returns issues for imports whose bound names never appear in any
    ``ast.Name`` or ``ast.Attribute`` node in the module body.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imported_names = _collect_imported_names(tree)
    used_names = _collect_used_names(tree)

    return [
        DeadCodeIssue(
            kind="unused_import",
            name=name,
            file=rel_path,
            detail=f"Imported name {name!r} at line {lineno} is never used.",
        )
        for name, lineno in imported_names.items()
        if name not in used_names and not name.startswith("_")
    ]


def _check_unreachable_branches(source: str, rel_path: str) -> list[DeadCodeIssue]:
    """Detect obvious unreachable branches via AST pattern matching.

    Checks for:
    - ``if False:`` / ``if True: ... else:`` patterns
    - Code after ``return``/``raise`` at the top of a function body
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    issues: list[DeadCodeIssue] = []

    for node in ast.walk(tree):
        # if False: <body>  — body is always skipped
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Constant) and test.value is False:
                issues.append(
                    DeadCodeIssue(
                        kind="unreachable_branch",
                        name="if False",
                        file=rel_path,
                        detail=f"Line {node.lineno}: unreachable branch — condition is always False.",
                    )
                )
            elif isinstance(test, ast.Constant) and test.value is True and node.orelse:
                issues.append(
                    DeadCodeIssue(
                        kind="unreachable_branch",
                        name="if True ... else",
                        file=rel_path,
                        detail=(f"Line {node.lineno}: unreachable else-branch — condition is always True."),
                    )
                )

        # Statements after return/raise/break/continue in a block
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_unreachable_after_jump(node.body, rel_path, issues)

    return issues


def _check_unreachable_after_jump(
    stmts: list[ast.stmt],
    rel_path: str,
    issues: list[DeadCodeIssue],
) -> None:
    """Append issues for statements that appear after an unconditional jump."""
    jump_types = (ast.Return, ast.Raise, ast.Break, ast.Continue)
    for i, stmt in enumerate(stmts):
        if isinstance(stmt, jump_types) and i < len(stmts) - 1:
            next_stmt = stmts[i + 1]
            # Allow a lone docstring immediately after (common pattern)
            if (
                isinstance(next_stmt, ast.Expr)
                and isinstance(next_stmt.value, ast.Constant)
                and isinstance(next_stmt.value.value, str)
            ):
                continue
            issues.append(
                DeadCodeIssue(
                    kind="unreachable_branch",
                    name=type(stmt).__name__.lower(),
                    file=rel_path,
                    detail=(
                        f"Line {next_stmt.lineno}: unreachable — "
                        f"statement follows {type(stmt).__name__.lower()} at line {stmt.lineno}."
                    ),
                )
            )
            break  # Only report the first unreachable in each block


def _check_lost_callers(
    diff: str,
    workdir: Path,
    changed_files: list[str],
) -> list[DeadCodeIssue]:
    """Find removed function/class names that still have callers in the codebase.

    When an agent removes a name from a file but does not update all call
    sites, the callers reference a symbol that no longer exists.  This is
    the inverse of dead code — it's *broken* code — but it arises from the
    same agent pattern of adding new implementations without removing old
    ones.

    Also checks whether newly added names lack any callers (warnings only).
    """
    issues: list[DeadCodeIssue] = []

    removed = _extract_removed_names(diff)
    added = _extract_added_names(diff)

    # Names that appear in both removed and added are likely renames.
    # The old name is still in ``removed`` and we still check it for stale callers.

    for name in sorted(removed):
        callers = _find_callers_in_codebase(name, workdir)
        if callers:
            sample = callers[:3]
            extra = len(callers) - 3
            detail = f"Removed/renamed {name!r} still referenced at: {', '.join(sample)}"
            if extra > 0:
                detail += f" (+{extra} more)"
            issues.append(
                DeadCodeIssue(
                    kind="lost_caller",
                    name=name,
                    file=changed_files[0] if changed_files else "",
                    detail=detail,
                )
            )

    # Warn when newly added private helpers have no callers
    for name in sorted(added):
        if not name.startswith("_"):
            continue  # Only warn about private symbols
        callers = _find_callers_in_codebase(name, workdir)
        if not callers:
            issues.append(
                DeadCodeIssue(
                    kind="no_callers",
                    name=name,
                    file=changed_files[0] if changed_files else "",
                    detail=f"Newly added {name!r} has no callers — may be dead code.",
                )
            )

    return issues


def analyse(
    changed_files: list[str],
    workdir: Path,
    *,
    check_unused_imports: bool = True,
    check_unreachable: bool = True,
    check_lost_callers: bool = True,
) -> DeadCodeReport:
    """Run dead-code analysis on *changed_files* in *workdir*.

    Args:
        changed_files: Repository-relative paths of changed Python files.
        workdir: Project root directory.
        check_unused_imports: Whether to check for unused imports.
        check_unreachable: Whether to check for unreachable branches.
        check_lost_callers: Whether to check for lost callers cross-codebase.

    Returns:
        A :class:`DeadCodeReport` with all detected issues.
    """
    report = DeadCodeReport()
    report.checked_files = list(changed_files)
    report.searched_files = _count_py_files(workdir)

    # Per-file AST checks
    for rel_path in changed_files:
        abs_path = workdir / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not read %s: %s", rel_path, exc)
            continue

        if check_unused_imports:
            report.issues.extend(_check_unused_imports(source, rel_path))

        if check_unreachable:
            report.issues.extend(_check_unreachable_branches(source, rel_path))

    # Cross-codebase caller analysis
    if check_lost_callers:
        diff = _get_diff(workdir, changed_files)
        if diff:
            report.issues.extend(_check_lost_callers(diff, workdir, changed_files))

    return report
