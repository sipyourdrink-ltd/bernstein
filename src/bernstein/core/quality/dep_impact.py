"""Dependency impact analysis — detects call sites that break when a function signature changes.

When an agent modifies a function signature, this module scans the entire
codebase for files that import and call the changed function.  It flags
call sites that are incompatible with the new signature, blocking merge
when breaking callers are found.

This is distinct from :mod:`~bernstein.core.dep_validator`, which validates
*task dependency graphs*.  This module analyses *Python runtime code
dependencies*.

Typical use::

    report = analyze_dep_impact(Path("."), base_ref="HEAD~1")
    if report.blocks_merge:
        for impact in report.call_site_impacts:
            print(impact)
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.api_compat_checker import (
    BreakingChange,
    ChangeType,
    CompatReport,
    check_git_diff,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSiteImpact:
    """A call site in a dependent file that will break under the new signature.

    Attributes:
        caller_file: Relative path of the file containing the call.
        caller_line: Line number of the call expression.
        callee_qualified: Fully-qualified symbol name that was changed
            (e.g. ``"check_compatibility"`` or ``"Service.stop"``).
        reason: Human-readable explanation of why this call site breaks.
    """

    caller_file: str
    caller_line: int
    callee_qualified: str
    reason: str


def _empty_breaking_changes() -> list[BreakingChange]:
    """Return a typed empty list for dataclass default_factory."""
    return []


def _empty_call_site_impacts() -> list[CallSiteImpact]:
    """Return a typed empty list for dataclass default_factory."""
    return []


@dataclass
class DepImpactReport:
    """Full result of a dependency impact analysis run.

    Attributes:
        api_breaking: Breaking changes found in the changed files themselves.
        call_site_impacts: Downstream call sites that are incompatible with
            the new signatures.
        blocks_merge: True when the analysis found any incompatibility.
    """

    api_breaking: list[BreakingChange] = field(default_factory=_empty_breaking_changes)
    call_site_impacts: list[CallSiteImpact] = field(default_factory=_empty_call_site_impacts)

    @property
    def blocks_merge(self) -> bool:
        """True when the analysis found any breaking change or broken call site."""
        return bool(self.api_breaking or self.call_site_impacts)


# ---------------------------------------------------------------------------
# Module path helpers
# ---------------------------------------------------------------------------


def _rel_path_to_module(rel_path: str) -> str:
    """Convert a relative ``.py`` path to a dotted module name.

    Strips a leading ``src/`` directory if present (the Bernstein convention).

    Examples::

        "src/bernstein/core/foo.py"  →  "bernstein.core.foo"
        "bernstein/core/foo.py"       →  "bernstein.core.foo"
    """
    p = rel_path.replace("\\", "/").removesuffix(".py")
    if p.startswith("src/"):
        p = p[4:]
    return p.replace("/", ".")


# ---------------------------------------------------------------------------
# Import analysis
# ---------------------------------------------------------------------------


def _collect_imported_names(
    tree: ast.Module,
    target_module: str,
    broken_symbol_names: set[str],
) -> dict[str, str]:
    """Return a map of *local_name → original_name* for symbols imported from
    ``target_module`` that appear in ``broken_symbol_names``.

    Handles::
        from bernstein.core.foo import bar            # local "bar" → "bar"
        from bernstein.core.foo import bar as baz     # local "baz" → "bar"
    """
    result: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != target_module:
            continue
        for alias in node.names:
            if alias.name == "*":
                # Wildcard — cannot resolve statically; skip.
                continue
            if alias.name in broken_symbol_names:
                local = alias.asname or alias.name
                result[local] = alias.name

    return result


def _collect_module_aliases(
    tree: ast.Module,
    target_module: str,
) -> set[str]:
    """Return the set of local names bound to the whole ``target_module``.

    Handles::
        import bernstein.core.foo          # bound as "bernstein.core.foo"
        import bernstein.core.foo as foo   # bound as "foo"
    """
    aliases: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == target_module:
                local = alias.asname or alias.name.split(".")[-1]
                aliases.add(local)

    return aliases


# ---------------------------------------------------------------------------
# Call-site impact detection
# ---------------------------------------------------------------------------


def _extract_removed_param_name(description: str) -> str | None:
    """Parse a removed-parameter name from a BreakingChange description string."""
    m = re.search(r"Parameter '([^']+)' was removed", description)
    return m.group(1) if m else None


def _resolve_call_target(
    node: ast.Call,
    imported_names: dict[str, str],
    module_aliases: set[str],
) -> str | None:
    """Return the original symbol name for a call node, or None if unresolvable."""
    func_node = node.func
    if isinstance(func_node, ast.Name):
        return imported_names.get(func_node.id)
    if (
        isinstance(func_node, ast.Attribute)
        and isinstance(func_node.value, ast.Name)
        and func_node.value.id in module_aliases
    ):
        return func_node.attr
    return None


def _check_single_breaking_change(
    bc: BreakingChange,
    node: ast.Call,
    caller_file: str,
    call_line: int,
) -> CallSiteImpact | None:
    """Check whether a single breaking change impacts the given call node."""
    ct = bc.change_type

    if ct in (ChangeType.REMOVED_FUNCTION, ChangeType.REMOVED_CLASS):
        return CallSiteImpact(
            caller_file=caller_file,
            caller_line=call_line,
            callee_qualified=bc.name,
            reason=f"calls removed symbol '{bc.name}'",
        )

    if ct == ChangeType.REMOVED_METHOD:
        method_name = bc.name.split(".")[-1]
        if isinstance(node.func, ast.Attribute) and node.func.attr == method_name:
            return CallSiteImpact(
                caller_file=caller_file,
                caller_line=call_line,
                callee_qualified=bc.name,
                reason=f"calls potentially-removed method '{bc.name}'",
            )

    if ct == ChangeType.REMOVED_PARAMETER:
        removed_param = _extract_removed_param_name(bc.description)
        if removed_param:
            uses_removed = any(kw.arg == removed_param for kw in node.keywords)
            if uses_removed:
                return CallSiteImpact(
                    caller_file=caller_file,
                    caller_line=call_line,
                    callee_qualified=bc.name,
                    reason=f"passes removed keyword argument '{removed_param}'",
                )

    if ct == ChangeType.CHANGED_PARAM_POSITION:
        pos_args = [a for a in node.args if not isinstance(a, ast.Starred)]
        if len(pos_args) >= 2:
            return CallSiteImpact(
                caller_file=caller_file,
                caller_line=call_line,
                callee_qualified=bc.name,
                reason=f"uses positional args that may break due to reordered parameters in '{bc.name}'",
            )

    return None


def _find_call_impacts(
    tree: ast.Module,
    caller_file: str,
    imported_names: dict[str, str],
    module_aliases: set[str],
    breaking_changes: list[BreakingChange],
) -> list[CallSiteImpact]:
    """Walk *tree* and return :class:`CallSiteImpact` entries for each call
    site that is incompatible with one of the *breaking_changes*.

    Args:
        tree: AST of the caller file.
        caller_file: Relative path of the caller file (for reporting).
        imported_names: Map of ``{local_name → original_name}`` from
            :func:`_collect_imported_names`.
        module_aliases: Local names bound to the module as a whole (e.g.
            ``import bernstein.core.foo as foo`` → ``{"foo"}``).
        breaking_changes: The breaking changes to check against.

    Returns:
        List of impacted call sites in ``tree``.
    """
    # Build a lookup: original_name → list[BreakingChange]
    breaks_by_symbol: dict[str, list[BreakingChange]] = {}
    for bc in breaking_changes:
        top = bc.name.split(".")[0]
        breaks_by_symbol.setdefault(top, []).append(bc)

    # Map local_name → list[BreakingChange]
    local_to_breaks: dict[str, list[BreakingChange]] = {}
    for local_name, orig_name in imported_names.items():
        if orig_name in breaks_by_symbol:
            local_to_breaks[local_name] = breaks_by_symbol[orig_name]

    has_module_import = bool(module_aliases)

    if not local_to_breaks and not has_module_import:
        return []

    impacts: list[CallSiteImpact] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func_orig = _resolve_call_target(node, imported_names, module_aliases)
        if func_orig is None:
            continue

        bcs = breaks_by_symbol.get(func_orig)
        if not bcs:
            continue

        call_line: int = getattr(node, "lineno", 0)

        for bc in bcs:
            impact = _check_single_breaking_change(bc, node, caller_file, call_line)
            if impact is not None:
                impacts.append(impact)

    return impacts


# ---------------------------------------------------------------------------
# Repository scan
# ---------------------------------------------------------------------------


def _iter_python_files(workdir: Path, exclude_rel: set[str]) -> list[Path]:
    """Return all ``.py`` files under *workdir* not in *exclude_rel*."""
    result: list[Path] = []
    for p in workdir.rglob("*.py"):
        try:
            rel = str(p.relative_to(workdir)).replace("\\", "/")
        except ValueError:
            continue
        if rel not in exclude_rel:
            result.append(p)
    return result


def _parse_python_file(py_path: Path) -> ast.Module | None:
    """Parse a Python file, returning None on read/syntax errors."""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _scan_file_for_impacts(
    py_path: Path,
    workdir: Path,
    module_for_file: dict[str, str],
    broken_symbols_per_module: dict[str, set[str]],
    breaks_by_file: dict[str, list[BreakingChange]],
) -> list[CallSiteImpact]:
    """Scan a single file for breaking call-site impacts."""
    tree = _parse_python_file(py_path)
    if tree is None:
        return []

    try:
        caller_rel = str(py_path.relative_to(workdir)).replace("\\", "/")
    except ValueError:
        caller_rel = str(py_path)

    impacts: list[CallSiteImpact] = []
    for changed_rel, module_path in module_for_file.items():
        broken_syms = broken_symbols_per_module[module_path]
        bcs = breaks_by_file[changed_rel]

        imported_names = _collect_imported_names(tree, module_path, broken_syms)
        module_aliases = _collect_module_aliases(tree, module_path)

        if not imported_names and not module_aliases:
            continue

        impacts.extend(_find_call_impacts(tree, caller_rel, imported_names, module_aliases, bcs))
    return impacts


def find_call_site_impacts(
    workdir: Path,
    compat_report: CompatReport,
    changed_files: list[str],
) -> list[CallSiteImpact]:
    """Scan the repo for call sites that will break given the API changes in
    *compat_report*.

    Args:
        workdir: Repository root.
        compat_report: Result from :func:`~bernstein.core.api_compat_checker.check_git_diff`.
        changed_files: Relative paths of files that were modified (excluded
            from the scan since they are already analysed by the API checker).

    Returns:
        List of :class:`CallSiteImpact` entries for all broken call sites.
    """
    if not compat_report.breaking_changes:
        return []

    breaks_by_file: dict[str, list[BreakingChange]] = {}
    for bc in compat_report.breaking_changes:
        breaks_by_file.setdefault(bc.file, []).append(bc)

    module_for_file: dict[str, str] = {rel: _rel_path_to_module(rel) for rel in breaks_by_file}

    broken_symbols_per_module: dict[str, set[str]] = {}
    for rel, bcs in breaks_by_file.items():
        broken_symbols_per_module[module_for_file[rel]] = {bc.name.split(".")[0] for bc in bcs}

    all_py_files = _iter_python_files(workdir, set(changed_files))

    all_impacts: list[CallSiteImpact] = []
    for py_path in all_py_files:
        all_impacts.extend(
            _scan_file_for_impacts(py_path, workdir, module_for_file, broken_symbols_per_module, breaks_by_file)
        )
    return all_impacts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_dep_impact(
    workdir: Path,
    base_ref: str = "HEAD~1",
) -> DepImpactReport:
    """Run a full dependency impact analysis comparing current code against
    *base_ref*.

    1. Runs API compatibility check (signatures in changed files).
    2. Scans the entire repo for call sites that break.

    Args:
        workdir: Repository root directory.
        base_ref: Git ref to compare against (default ``HEAD~1``).

    Returns:
        :class:`DepImpactReport` containing all findings.
    """
    from bernstein.core.api_compat_checker import _git_diff_files  # pyright: ignore[reportPrivateUsage]

    compat = check_git_diff(workdir, base_ref=base_ref)

    changed_files = _git_diff_files(workdir, base_ref, "ACMR")
    changed_files += _git_diff_files(workdir, base_ref, "D")

    call_impacts = find_call_site_impacts(workdir, compat, changed_files)

    return DepImpactReport(
        api_breaking=list(compat.breaking_changes),
        call_site_impacts=call_impacts,
    )
