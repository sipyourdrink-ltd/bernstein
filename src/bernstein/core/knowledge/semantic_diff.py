"""Semantic diff analysis — behavior preservation verification after refactoring.

Beyond syntactic diff review, this module verifies that refactoring preserves
behavior by:

1. **Signature extraction** — parse Python AST to extract function signatures
   (name, argument list with types, return type) from before and after states.
2. **Signature change detection** — classify each change as added, removed, or
   modified, and flag modifications that break call-site compatibility.
3. **Call-site scanning** — search all Python files in the worktree for
   callers of changed functions and verify their argument counts still match.
4. **Type compatibility** — detect widened or narrowed return type annotations
   that may violate caller assumptions.

Designed to catch the class of bugs where "the code looks right but the
behavior changed."

Typical usage::

    report = analyze_semantic_diff(
        worktree_path=Path("."),
        changed_files=["src/auth/login.py", "src/auth/token.py"],
    )
    if not report.behavior_preserved:
        for issue in report.call_site_mismatches + report.type_incompatibilities:
            print(issue)
"""

from __future__ import annotations

import ast
import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


def _empty_defaults() -> frozenset[str]:
    """Return a typed empty defaults set for signature records."""
    return frozenset()


@dataclass(frozen=True)
class FunctionSignature:
    """Extracted signature of a Python function or method.

    Attributes:
        name: Function name (unqualified).
        qualname: Qualified name, e.g. ``"MyClass.my_method"``.
        args: Positional argument names (excluding ``self``/``cls``).
        arg_annotations: Map of arg name → annotation string (or ``""``).
        return_annotation: Return type annotation string, or ``""`` if absent.
        has_varargs: True when ``*args`` is present.
        has_kwargs: True when ``**kwargs`` is present.
        file: Source file path (relative).
        lineno: Line number of the ``def`` statement.
    """

    name: str
    qualname: str
    args: list[str]
    arg_annotations: dict[str, str]
    return_annotation: str
    has_varargs: bool
    has_kwargs: bool
    file: str
    lineno: int
    defaults: frozenset[str] = field(default_factory=_empty_defaults)


@dataclass(frozen=True)
class SignatureChange:
    """A detected change between old and new versions of a function signature.

    Attributes:
        function_name: Unqualified function name.
        qualname: Qualified name.
        file: Source file path.
        change_type: ``"added"`` | ``"removed"`` | ``"modified"``.
        before: Signature before the change (``None`` for added functions).
        after: Signature after the change (``None`` for removed functions).
        compatibility_issues: Human-readable descriptions of breaking changes.
    """

    function_name: str
    qualname: str
    file: str
    change_type: str  # "added" | "removed" | "modified"
    before: FunctionSignature | None
    after: FunctionSignature | None
    compatibility_issues: list[str]


@dataclass
class CallSiteMismatch:
    """A call site that no longer matches a changed function signature.

    Attributes:
        caller_file: File containing the call.
        lineno: Line number of the call.
        function_name: Function being called.
        issue: Human-readable description of the mismatch.
    """

    caller_file: str
    lineno: int
    function_name: str
    issue: str


@dataclass
class SemanticDiffReport:
    """Full semantic diff analysis report.

    Attributes:
        changed_files: Files that were analysed.
        signature_changes: All detected signature changes.
        call_site_mismatches: Call sites that may be broken.
        type_incompatibilities: Type-annotation compatibility issues.
        behavior_preserved: ``True`` when no breaking changes were detected.
        errors: Parse or I/O errors encountered during analysis.
    """

    changed_files: list[str]
    signature_changes: list[SignatureChange] = field(default_factory=list[SignatureChange])
    call_site_mismatches: list[CallSiteMismatch] = field(default_factory=list[CallSiteMismatch])
    type_incompatibilities: list[str] = field(default_factory=list[str])
    behavior_preserved: bool = True
    errors: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _annotation_str(node: ast.expr | None) -> str:
    """Convert an AST annotation node to a string representation.

    Args:
        node: AST expression node (from ``arg.annotation`` or
            ``FunctionDef.returns``), or ``None``.

    Returns:
        String representation, or ``""`` when *node* is ``None``.
    """
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _is_self_or_cls(arg_name: str) -> bool:
    return arg_name in ("self", "cls")


def extract_signatures_from_source(
    source: str,
    file: str = "",
) -> dict[str, FunctionSignature]:
    """Parse Python *source* and return all function signatures keyed by qualname.

    Traverses the AST depth-first, tracking class scopes to produce correct
    qualified names.  Methods named ``self`` / ``cls`` are stripped from the
    argument list to focus on the caller-visible interface.

    Args:
        source: Python source text.
        file: Filename to attach to each signature (for reporting).

    Returns:
        Dict mapping ``qualname`` → :class:`FunctionSignature`.  Returns an
        empty dict when *source* cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        logger.debug("semantic_diff: syntax error in %s: %s", file, exc)
        return {}

    signatures: dict[str, FunctionSignature] = {}
    _walk_scope(tree, qualname_prefix="", file=file, out=signatures)
    return signatures


def _walk_scope(
    node: ast.AST,
    qualname_prefix: str,
    file: str,
    out: dict[str, FunctionSignature],
) -> None:
    """Recursively walk AST nodes to collect function signatures."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            prefix = f"{qualname_prefix}{child.name}." if qualname_prefix else f"{child.name}."
            _walk_scope(child, qualname_prefix=prefix, file=file, out=out)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = f"{qualname_prefix}{child.name}"
            sig = _build_signature(child, qualname=qualname, file=file)
            out[qualname] = sig
            # Recurse into nested functions / methods
            _walk_scope(child, qualname_prefix=f"{qualname}.", file=file, out=out)


def _build_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualname: str,
    file: str,
) -> FunctionSignature:
    """Build a :class:`FunctionSignature` from an AST ``FunctionDef`` node."""
    args_obj = node.args

    # Collect all positional args, skipping self/cls
    all_args: list[ast.arg] = [a for a in args_obj.args if not _is_self_or_cls(a.arg)]
    # Include posonlyargs (Python 3.8+)
    all_args = [a for a in args_obj.posonlyargs if not _is_self_or_cls(a.arg)] + all_args

    arg_names = [a.arg for a in all_args]
    arg_annotations = {a.arg: _annotation_str(a.annotation) for a in all_args}

    # Determine which args have defaults.  In CPython the last N args in
    # `args_obj.args` share defaults with `args_obj.defaults` (right-aligned).
    non_self_args = [a for a in args_obj.args if not _is_self_or_cls(a.arg)]
    n_defaults = len(args_obj.defaults)
    defaults_set: set[str] = set()
    if n_defaults:
        for a in non_self_args[-n_defaults:]:
            defaults_set.add(a.arg)
    # kwonly args: kw_defaults has one entry per kwonlyarg (None = no default)
    for i, kwa in enumerate(args_obj.kwonlyargs):
        if i < len(args_obj.kw_defaults) and args_obj.kw_defaults[i] is not None:
            defaults_set.add(kwa.arg)

    return FunctionSignature(
        name=node.name,
        qualname=qualname,
        args=arg_names,
        arg_annotations=arg_annotations,
        return_annotation=_annotation_str(node.returns),
        has_varargs=args_obj.vararg is not None,
        has_kwargs=args_obj.kwarg is not None,
        file=file,
        lineno=node.lineno,
        defaults=frozenset(defaults_set),
    )


# ---------------------------------------------------------------------------
# Signature diff
# ---------------------------------------------------------------------------


def _check_arg_compat(
    before: FunctionSignature,
    after: FunctionSignature,
) -> list[str]:
    """Return a list of human-readable compatibility issues between two signatures.

    Checks for:
    - Added required arguments (breaks existing callers)
    - Removed arguments (breaks callers that pass them)
    - Reordered arguments
    - Changed return type annotation
    """
    issues: list[str] = []

    before_args = set(before.args)
    after_args = set(after.args)

    removed = before_args - after_args
    added = after_args - before_args

    for arg in sorted(removed):
        issues.append(f"argument '{arg}' removed — callers passing it will break")

    for arg in sorted(added):
        if arg in after.defaults:
            # Added with a default value — existing callers are fine
            continue
        issues.append(f"argument '{arg}' added without default — callers without it will break")

    # Reordering check
    if before.args != after.args:
        common = [a for a in before.args if a in after_args]
        reordered = [a for a in after.args if a in before_args]
        if common != reordered:
            issues.append(f"argument order changed: {before.args} → {after.args}")

    # Return type change
    if before.return_annotation != after.return_annotation:
        issues.append(f"return type changed: '{before.return_annotation}' → '{after.return_annotation}'")

    return issues


def detect_signature_changes(
    before: dict[str, FunctionSignature],
    after: dict[str, FunctionSignature],
) -> list[SignatureChange]:
    """Compare two signature dicts and return all detected changes.

    Args:
        before: Signatures from the previous version.
        after: Signatures from the new version.

    Returns:
        List of :class:`SignatureChange` (added, removed, or modified).
    """
    changes: list[SignatureChange] = []
    all_names = set(before) | set(after)

    for qualname in sorted(all_names):
        b = before.get(qualname)
        a = after.get(qualname)

        if b is None and a is not None:
            changes.append(
                SignatureChange(
                    function_name=a.name,
                    qualname=qualname,
                    file=a.file,
                    change_type="added",
                    before=None,
                    after=a,
                    compatibility_issues=[],
                )
            )
        elif b is not None and a is None:
            changes.append(
                SignatureChange(
                    function_name=b.name,
                    qualname=qualname,
                    file=b.file,
                    change_type="removed",
                    before=b,
                    after=None,
                    compatibility_issues=[f"function '{qualname}' removed — all callers will break"],
                )
            )
        elif b is not None and a is not None:
            issues = _check_arg_compat(b, a)
            if issues:
                changes.append(
                    SignatureChange(
                        function_name=a.name,
                        qualname=qualname,
                        file=a.file,
                        change_type="modified",
                        before=b,
                        after=a,
                        compatibility_issues=issues,
                    )
                )

    return changes


# ---------------------------------------------------------------------------
# Call-site scanner
# ---------------------------------------------------------------------------


def find_call_sites(
    source: str,
    func_names: set[str],
    _file: str = "",
) -> list[tuple[str, int, str]]:
    """Find call sites for *func_names* in *source*.

    Returns a list of ``(function_name, lineno, call_text)`` tuples.

    Args:
        source: Python source text to search.
        func_names: Set of unqualified function names to look for.
        _file: Filename (for reporting only).

    Returns:
        List of ``(function_name, lineno, call_text)`` tuples.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name and name in func_names:
            # Count positional args
            n_args = len(node.args)
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            call_text = f"{name}({n_args} positional, kwargs={sorted(kw_names)})"
            results.append((name, node.lineno, call_text))

    return results


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_file_at_revision(
    worktree_path: Path,
    file_rel: str,
    revision: str = "HEAD~1",
) -> str | None:
    """Retrieve *file_rel* contents at *revision* via ``git show``.

    Args:
        worktree_path: Root of the git worktree.
        file_rel: File path relative to *worktree_path*.
        revision: Git revision spec (default ``"HEAD~1"``).

    Returns:
        File contents as a string, or ``None`` if not available.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{file_rel}"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("semantic_diff: git show failed for %s@%s: %s", file_rel, revision, exc)
        return None


def _get_all_python_files(worktree_path: Path) -> list[Path]:
    """Return all Python files tracked in *worktree_path*."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return list(worktree_path.rglob("*.py"))
        return [worktree_path / p for p in result.stdout.splitlines() if p.endswith(".py")]
    except (subprocess.TimeoutExpired, OSError):
        return list(worktree_path.rglob("*.py"))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _collect_before_after_signatures(
    worktree_path: Path,
    py_files: list[str],
    revision: str,
    report: SemanticDiffReport,
) -> tuple[dict[str, FunctionSignature], dict[str, FunctionSignature]]:
    """Collect before/after function signatures for each changed file.

    Args:
        worktree_path: Root of the git worktree.
        py_files: Changed Python files.
        revision: Git revision for the "before" state.
        report: Report to accumulate errors into.

    Returns:
        (all_before, all_after) signature dicts keyed by qualname.
    """
    all_before: dict[str, FunctionSignature] = {}
    all_after: dict[str, FunctionSignature] = {}

    for rel_path in py_files:
        before_source = _get_file_at_revision(worktree_path, rel_path, revision)
        if before_source is not None:
            all_before.update(extract_signatures_from_source(before_source, file=rel_path))

        try:
            after_source = (worktree_path / rel_path).read_text(encoding="utf-8", errors="replace")
            all_after.update(extract_signatures_from_source(after_source, file=rel_path))
        except OSError as exc:
            report.errors.append(f"Could not read {rel_path}: {exc}")

    return all_before, all_after


def _check_arg_count_mismatch(change: SignatureChange, n_pos: int) -> str | None:
    """Check if a call site has an arg-count incompatibility.

    Args:
        change: The signature change with before/after info.
        n_pos: Number of positional args at the call site.

    Returns:
        Description of the issue, or None if compatible.
    """
    if change.before is None or change.after is None:
        return None
    after_argc = len(change.after.args)
    if n_pos < 0 or change.after.has_varargs:
        return None
    if n_pos > after_argc:
        return f"call passes {n_pos} positional args but '{change.qualname}' now accepts {after_argc}"
    if n_pos < after_argc and not _has_defaults(change.after):
        return f"call passes {n_pos} positional args but '{change.qualname}' now requires {after_argc}"
    return None


def _parse_positional_count(call_text: str) -> int:
    """Extract the positional arg count from a call_text string.

    Args:
        call_text: Call description like ``"func(2 positional, kwargs=['x'])"``.

    Returns:
        Number of positional args, or -1 if unparseable.
    """
    try:
        return int(call_text.split("(")[1].split(" ")[0])
    except (IndexError, ValueError):
        return -1


def _scan_call_sites(
    worktree_path: Path,
    breaking: list[SignatureChange],
    report: SemanticDiffReport,
) -> None:
    """Scan all Python files for call sites broken by signature changes.

    Args:
        worktree_path: Root of the git worktree.
        breaking: Breaking signature changes to check.
        report: Report to accumulate mismatches into.
    """
    changed_func_names: set[str] = {c.function_name for c in breaking}
    all_py = _get_all_python_files(worktree_path)

    for py_file in all_py:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(py_file.relative_to(worktree_path))
        sites = find_call_sites(source, changed_func_names, _file=rel)

        for func_name, lineno, call_text in sites:
            matching = [c for c in breaking if c.function_name == func_name]
            for change in matching:
                n_pos = _parse_positional_count(call_text)
                issue = _check_arg_count_mismatch(change, n_pos)
                if issue:
                    report.call_site_mismatches.append(
                        CallSiteMismatch(caller_file=rel, lineno=lineno, function_name=func_name, issue=issue)
                    )


def analyze_semantic_diff(
    worktree_path: Path,
    changed_files: list[str],
    *,
    revision: str = "HEAD~1",
    scan_call_sites: bool = True,
) -> SemanticDiffReport:
    """Analyse changed Python files for behavior-breaking signature changes.

    For each changed file:
    1. Extract signatures from the previous revision (via ``git show``).
    2. Extract signatures from the current version on disk.
    3. Detect and classify all signature changes.
    4. Optionally scan the full worktree for call sites that may be broken.

    Args:
        worktree_path: Root of the git worktree.
        changed_files: List of changed file paths (relative to *worktree_path*).
        revision: Git revision for the "before" state (default ``"HEAD~1"``).
        scan_call_sites: When ``True``, scan all Python files for outdated
            call sites.  Can be disabled for speed when only signature data
            is needed.

    Returns:
        :class:`SemanticDiffReport` with all findings.
    """
    report = SemanticDiffReport(changed_files=list(changed_files))

    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return report

    all_before, all_after = _collect_before_after_signatures(worktree_path, py_files, revision, report)

    changes = detect_signature_changes(all_before, all_after)
    report.signature_changes = changes

    breaking = [c for c in changes if c.change_type in ("removed", "modified") and c.compatibility_issues]

    if breaking:
        report.behavior_preserved = False
        for change in breaking:
            report.type_incompatibilities.extend(
                [f"{change.qualname}: {issue}" for issue in change.compatibility_issues]
            )

    if scan_call_sites and breaking:
        _scan_call_sites(worktree_path, breaking, report)

    if report.call_site_mismatches:
        report.behavior_preserved = False

    logger.info(
        "semantic_diff: files=%d signature_changes=%d call_site_mismatches=%d behavior_preserved=%s",
        len(py_files),
        len(changes),
        len(report.call_site_mismatches),
        report.behavior_preserved,
    )
    return report


def _has_defaults(sig: FunctionSignature) -> bool:
    """Return True when the signature has default values or variadic params."""
    return sig.has_varargs or sig.has_kwargs or bool(sig.defaults)


# ---------------------------------------------------------------------------
# Convenience: build a human-readable summary
# ---------------------------------------------------------------------------


def format_report(report: SemanticDiffReport) -> str:
    """Format a :class:`SemanticDiffReport` as a human-readable string.

    Args:
        report: Report produced by :func:`analyze_semantic_diff`.

    Returns:
        Multi-line string suitable for logging or display.
    """
    lines: list[str] = ["## Semantic Diff Report"]
    lines.append(f"Files analysed: {', '.join(report.changed_files) or '(none)'}")
    lines.append(f"Behavior preserved: {'YES' if report.behavior_preserved else 'NO'}")

    if report.signature_changes:
        lines.append(f"\nSignature changes ({len(report.signature_changes)}):")
        for change in report.signature_changes:
            marker = {"added": "+", "removed": "-", "modified": "~"}.get(change.change_type, "?")
            lines.append(f"  [{marker}] {change.qualname} ({change.file})")
            for issue in change.compatibility_issues:
                lines.append(f"      ⚠ {issue}")

    if report.call_site_mismatches:
        lines.append(f"\nCall-site mismatches ({len(report.call_site_mismatches)}):")
        for m in report.call_site_mismatches:
            lines.append(f"  {m.caller_file}:{m.lineno}  {m.function_name}: {m.issue}")

    if report.type_incompatibilities:
        lines.append(f"\nType incompatibilities ({len(report.type_incompatibilities)}):")
        for issue in report.type_incompatibilities:
            lines.append(f"  ⚠ {issue}")

    if report.errors:
        lines.append(f"\nErrors ({len(report.errors)}):")
        for err in report.errors:
            lines.append(f"  ! {err}")

    return "\n".join(lines)
