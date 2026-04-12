"""API backward-compatibility checker — detects breaking changes in Python function signatures.

Compares old and new source using the ``ast`` module to find:
  - Removed public functions / methods / classes
  - Removed parameters
  - Changed parameter type annotations (incompatible)
  - Positional parameter reordering
  - Renamed classes

Non-breaking changes (ignored):
  - Adding new optional parameters (with defaults)
  - Adding new functions / classes / methods
  - Changes to private names (``_``-prefixed)
"""

from __future__ import annotations

import ast
import logging
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ChangeType(Enum):
    """Category of a breaking API change."""

    REMOVED_FUNCTION = "removed_function"
    REMOVED_METHOD = "removed_method"
    REMOVED_PARAMETER = "removed_parameter"
    CHANGED_PARAM_TYPE = "changed_param_type"
    CHANGED_PARAM_POSITION = "changed_param_position"
    REMOVED_CLASS = "removed_class"


@dataclass(frozen=True)
class BreakingChange:
    """A single breaking change detected between two source versions.

    Attributes:
        file: Relative file path where the change was detected.
        name: Fully-qualified function/class name (e.g. ``MyClass.method``).
        change_type: Category of the breaking change.
        description: Human-readable description of what changed.
        line: Line number in the *old* source (0 when unavailable).
    """

    file: str
    name: str
    change_type: ChangeType
    description: str
    line: int = 0


@dataclass(frozen=True)
class Addition:
    """A non-breaking addition (new function, class, or method).

    Attributes:
        file: Relative file path.
        name: Name of the new symbol.
        kind: ``"function"``, ``"class"``, or ``"method"``.
    """

    file: str
    name: str
    kind: str


@dataclass
class CompatReport:
    """Result of an API compatibility check.

    Attributes:
        breaking_changes: List of detected breaking changes.
        additions: List of non-breaking additions.
        is_compatible: True when no breaking changes were found.
    """

    breaking_changes: list[BreakingChange] = field(default_factory=list[BreakingChange])
    additions: list[Addition] = field(default_factory=list[Addition])

    @property
    def is_compatible(self) -> bool:
        """True when no breaking changes were detected."""
        return len(self.breaking_changes) == 0


# ---------------------------------------------------------------------------
# AST extraction helpers
# ---------------------------------------------------------------------------


def _is_public(name: str) -> bool:
    """Return True if *name* is a public symbol (does not start with ``_``)."""
    return not name.startswith("_")


@dataclass(frozen=True)
class _ParamInfo:
    """Extracted info about a single function parameter."""

    name: str
    annotation: str  # empty string when no annotation
    has_default: bool
    position: int


@dataclass(frozen=True)
class _FuncInfo:
    """Extracted info about a function or method."""

    name: str
    qualified_name: str  # e.g. "MyClass.method" or just "func"
    params: list[_ParamInfo]
    line: int


@dataclass(frozen=True)
class _ClassInfo:
    """Extracted info about a class."""

    name: str
    methods: dict[str, _FuncInfo]
    line: int


def _annotation_str(node: ast.expr | None) -> str:
    """Convert an AST annotation node to a comparable string representation."""
    if node is None:
        return ""
    return ast.unparse(node)


def _extract_params(args: ast.arguments, qualified_prefix: str) -> list[_ParamInfo]:
    """Extract parameter info from an ``ast.arguments`` node.

    Skips ``self`` and ``cls`` for methods.
    """
    params: list[_ParamInfo] = []
    # Number of positional args without defaults
    n_args = len(args.args)
    n_defaults = len(args.defaults)
    first_default_idx = n_args - n_defaults

    position = 0
    for i, arg in enumerate(args.args):
        if arg.arg in ("self", "cls"):
            continue
        has_default = i >= first_default_idx
        params.append(
            _ParamInfo(
                name=arg.arg,
                annotation=_annotation_str(arg.annotation),
                has_default=has_default,
                position=position,
            )
        )
        position += 1

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        default = args.kw_defaults[i] if i < len(args.kw_defaults) else None
        params.append(
            _ParamInfo(
                name=arg.arg,
                annotation=_annotation_str(arg.annotation),
                has_default=default is not None,
                position=position,
            )
        )
        position += 1

    return params


def _extract_functions(tree: ast.Module) -> dict[str, _FuncInfo]:
    """Extract all top-level public function definitions from a module AST."""
    funcs: dict[str, _FuncInfo] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(node.name):
            params = _extract_params(node.args, node.name)
            funcs[node.name] = _FuncInfo(
                name=node.name,
                qualified_name=node.name,
                params=params,
                line=node.lineno,
            )
    return funcs


def _extract_classes(tree: ast.Module) -> dict[str, _ClassInfo]:
    """Extract all top-level public class definitions and their public methods."""
    classes: dict[str, _ClassInfo] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and _is_public(node.name):
            methods: dict[str, _FuncInfo] = {}
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and _is_public(child.name):
                    qualified = f"{node.name}.{child.name}"
                    params = _extract_params(child.args, qualified)
                    methods[child.name] = _FuncInfo(
                        name=child.name,
                        qualified_name=qualified,
                        params=params,
                        line=child.lineno,
                    )
            classes[node.name] = _ClassInfo(
                name=node.name,
                methods=methods,
                line=node.lineno,
            )
    return classes


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------


def _compare_params(
    old_func: _FuncInfo,
    new_func: _FuncInfo,
    filename: str,
) -> list[BreakingChange]:
    """Compare parameters of two function versions and return breaking changes."""
    breaks: list[BreakingChange] = []

    old_params = {p.name: p for p in old_func.params}
    new_params = {p.name: p for p in new_func.params}

    # Removed parameters
    for param_name, old_param in old_params.items():
        if param_name not in new_params:
            breaks.append(
                BreakingChange(
                    file=filename,
                    name=old_func.qualified_name,
                    change_type=ChangeType.REMOVED_PARAMETER,
                    description=f"Parameter '{param_name}' was removed from '{old_func.qualified_name}'",
                    line=old_func.line,
                )
            )
            continue

        new_param = new_params[param_name]

        # Changed type annotation (only if both had annotations)
        if old_param.annotation and new_param.annotation and old_param.annotation != new_param.annotation:
            breaks.append(
                BreakingChange(
                    file=filename,
                    name=old_func.qualified_name,
                    change_type=ChangeType.CHANGED_PARAM_TYPE,
                    description=(
                        f"Parameter '{param_name}' in '{old_func.qualified_name}' "
                        f"changed type from '{old_param.annotation}' to '{new_param.annotation}'"
                    ),
                    line=old_func.line,
                )
            )

        # Changed position for required parameters
        if not old_param.has_default and not new_param.has_default and old_param.position != new_param.position:
            breaks.append(
                BreakingChange(
                    file=filename,
                    name=old_func.qualified_name,
                    change_type=ChangeType.CHANGED_PARAM_POSITION,
                    description=(
                        f"Required parameter '{param_name}' in '{old_func.qualified_name}' "
                        f"moved from position {old_param.position} to {new_param.position}"
                    ),
                    line=old_func.line,
                )
            )

    return breaks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _diff_functions(
    old_funcs: dict[str, Any],
    new_funcs: dict[str, Any],
    filename: str,
) -> tuple[list[BreakingChange], list[Addition]]:
    """Compare top-level functions between old and new trees."""
    breaking: list[BreakingChange] = []
    additions: list[Addition] = []

    for name, old_func in old_funcs.items():
        if name not in new_funcs:
            breaking.append(
                BreakingChange(
                    file=filename,
                    name=name,
                    change_type=ChangeType.REMOVED_FUNCTION,
                    description=f"Public function '{name}' was removed",
                    line=old_func.line,
                )
            )
        else:
            breaking.extend(_compare_params(old_func, new_funcs[name], filename))

    for name in new_funcs:
        if name not in old_funcs:
            additions.append(Addition(file=filename, name=name, kind="function"))

    return breaking, additions


def _diff_class_methods(
    cls_name: str,
    old_cls: Any,
    new_cls: Any,
    filename: str,
) -> tuple[list[BreakingChange], list[Addition]]:
    """Compare methods between old and new versions of a single class."""
    breaking: list[BreakingChange] = []
    additions: list[Addition] = []

    for method_name, old_method in old_cls.methods.items():
        if method_name not in new_cls.methods:
            breaking.append(
                BreakingChange(
                    file=filename,
                    name=f"{cls_name}.{method_name}",
                    change_type=ChangeType.REMOVED_METHOD,
                    description=f"Public method '{cls_name}.{method_name}' was removed",
                    line=old_method.line,
                )
            )
        else:
            breaking.extend(_compare_params(old_method, new_cls.methods[method_name], filename))

    for method_name in new_cls.methods:
        if method_name not in old_cls.methods:
            additions.append(Addition(file=filename, name=f"{cls_name}.{method_name}", kind="method"))

    return breaking, additions


def _diff_classes(
    old_classes: dict[str, Any],
    new_classes: dict[str, Any],
    filename: str,
) -> tuple[list[BreakingChange], list[Addition]]:
    """Compare classes (and their methods) between old and new trees."""
    breaking: list[BreakingChange] = []
    additions: list[Addition] = []

    for cls_name, old_cls in old_classes.items():
        if cls_name not in new_classes:
            breaking.append(
                BreakingChange(
                    file=filename,
                    name=cls_name,
                    change_type=ChangeType.REMOVED_CLASS,
                    description=f"Public class '{cls_name}' was removed",
                    line=old_cls.line,
                )
            )
            continue

        b, a = _diff_class_methods(cls_name, old_cls, new_classes[cls_name], filename)
        breaking.extend(b)
        additions.extend(a)

    for cls_name in new_classes:
        if cls_name not in old_classes:
            additions.append(Addition(file=filename, name=cls_name, kind="class"))

    return breaking, additions


def check_compatibility(old_source: str, new_source: str, filename: str) -> CompatReport:
    """Compare two Python source strings and return an API compatibility report.

    Args:
        old_source: Previous version of the source file.
        new_source: Current version of the source file.
        filename: File path used in report entries.

    Returns:
        CompatReport with breaking changes and additions.
    """
    try:
        old_tree = ast.parse(old_source)
    except SyntaxError:
        logger.warning("api_compat: cannot parse old source for %s", filename)
        return CompatReport()

    try:
        new_tree = ast.parse(new_source)
    except SyntaxError:
        logger.warning("api_compat: cannot parse new source for %s", filename)
        return CompatReport()

    breaking: list[BreakingChange] = []
    additions: list[Addition] = []

    b, a = _diff_functions(_extract_functions(old_tree), _extract_functions(new_tree), filename)
    breaking.extend(b)
    additions.extend(a)

    b, a = _diff_classes(_extract_classes(old_tree), _extract_classes(new_tree), filename)
    breaking.extend(b)
    additions.extend(a)

    return CompatReport(breaking_changes=breaking, additions=additions)


def _git_diff_files(workdir: Path, base_ref: str, diff_filter: str) -> list[str]:
    """Return list of changed ``.py`` files for the given ``--diff-filter``."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"--diff-filter={diff_filter}", base_ref, "--", "*.py"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip().endswith(".py")]
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("api_compat: git diff (filter=%s) failed: %s", diff_filter, exc)
        return []


def _breaking_from_deleted_file(rel_path: str, old_source: str) -> list[BreakingChange]:
    """Produce breaking-change entries for every public symbol in a deleted file."""
    try:
        old_tree = ast.parse(old_source)
    except SyntaxError:
        return []

    breaks: list[BreakingChange] = []
    for func_info in _extract_functions(old_tree).values():
        breaks.append(
            BreakingChange(
                file=rel_path,
                name=func_info.name,
                change_type=ChangeType.REMOVED_FUNCTION,
                description=f"Public function '{func_info.name}' removed (file deleted)",
                line=func_info.line,
            )
        )
    for cls_info in _extract_classes(old_tree).values():
        breaks.append(
            BreakingChange(
                file=rel_path,
                name=cls_info.name,
                change_type=ChangeType.REMOVED_CLASS,
                description=f"Public class '{cls_info.name}' removed (file deleted)",
                line=cls_info.line,
            )
        )
    return breaks


def check_git_diff(workdir: Path, base_ref: str = "HEAD~1") -> CompatReport:
    """Check API compatibility for all Python files changed since *base_ref*.

    Uses ``git diff --name-only`` to find changed ``.py`` files, then fetches
    the old version via ``git show`` and reads the new version from disk.

    Args:
        workdir: Repository root directory.
        base_ref: Git ref to compare against (default ``HEAD~1``).

    Returns:
        Merged CompatReport across all changed files.
    """
    changed_files = _git_diff_files(workdir, base_ref, "ACMR")
    deleted_files = _git_diff_files(workdir, base_ref, "D")

    all_breaking: list[BreakingChange] = []
    all_additions: list[Addition] = []

    for rel_path in changed_files:
        old_source = _git_show(workdir, base_ref, rel_path)
        new_path = workdir / rel_path
        if not new_path.exists():
            continue
        try:
            new_source = new_path.read_text(encoding="utf-8")
        except OSError:
            continue

        report = check_compatibility(old_source, new_source, rel_path)
        all_breaking.extend(report.breaking_changes)
        all_additions.extend(report.additions)

    for rel_path in deleted_files:
        old_source = _git_show(workdir, base_ref, rel_path)
        if old_source:
            all_breaking.extend(_breaking_from_deleted_file(rel_path, old_source))

    return CompatReport(breaking_changes=all_breaking, additions=all_additions)


def _git_show(workdir: Path, ref: str, rel_path: str) -> str:
    """Retrieve file contents at a given git ref. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""
