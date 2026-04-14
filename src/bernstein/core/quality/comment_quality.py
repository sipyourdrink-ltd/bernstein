"""Comment quality analyser for agent-produced documentation.

Verifies that docstrings in changed Python files are:
- **Accurate**: parameter names in docstrings match the function signature.
- **Non-redundant**: descriptions are not trivially derived from the name alone.
- **Complete**: public functions/methods document all parameters, return values,
  and raised exceptions.
- **Correctly styled**: Google, NumPy, or reST (auto-detected from config).
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

DocstyleKind = Literal["google", "numpy", "rest", "auto"]

# ---------------------------------------------------------------------------
# Regex patterns for each docstring style
# ---------------------------------------------------------------------------

# Google: "Args:", "Returns:", "Raises:", "Yields:", "Attributes:", "Example:"
_GOOGLE_SECTION_RE = re.compile(
    r"^\s*(Args|Returns?|Raises?|Yields?|Attributes?|Examples?|Note|Notes|Todo)\s*:",
    re.MULTILINE,
)
# Google param line: "    name: description"
_GOOGLE_PARAM_RE = re.compile(r"^[ \t]{4,20}(\w+)[ \t]{0,10}(?:\([^)]{0,100}\))?[ \t]{0,10}:", re.MULTILINE)

# NumPy: "Parameters\n----------"
_NUMPY_SECTION_NAMES = frozenset(
    {
        "Parameters",
        "Returns",
        "Return",
        "Raises",
        "Raise",
        "Yields",
        "Yield",
        "Attributes",
        "Attribute",
        "Examples",
        "Example",
        "Notes",
        "Note",
        "See Also",
    }
)
_NUMPY_SECTION_RE = re.compile(
    r"^\s*([A-Z][A-Za-z ]+)[ \t]*\n[ \t]*[-=]+",
    re.MULTILINE,
)
# NumPy param line: "name : type"
_NUMPY_PARAM_RE = re.compile(r"^\s*(\w+)\s*:", re.MULTILINE)

# reST: ":param name:", ":type name:", ":returns:", ":raises ExcType:"
_REST_PARAM_RE = re.compile(r":param\s+(?:\w+\s+)?(\w+)\s*:")
_REST_RETURNS_RE = re.compile(r":returns?:", re.IGNORECASE)
_REST_RAISES_RE = re.compile(r":raises?\s+\w+\s*:")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CommentIssue:
    """A single docstring quality finding.

    Attributes:
        kind: Category — ``inaccurate``, ``redundant``, ``incomplete``,
            ``wrong_style``.
        symbol: Qualified name of the function/class/method.
        file: Source file path relative to workdir.
        line: Line number of the docstring.
        detail: Human-readable description.
    """

    kind: str
    symbol: str
    file: str
    line: int
    detail: str


@dataclass
class CommentQualityReport:
    """Aggregated comment quality results.

    Attributes:
        issues: All detected issues.
        checked_functions: Number of public functions/methods examined.
        checked_files: Files that were analysed.
    """

    issues: list[CommentIssue] = field(default_factory=list)
    checked_functions: int = 0
    checked_files: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True when no blocking issues were found."""
        blocking = {"inaccurate", "incomplete"}
        return not any(i.kind in blocking for i in self.issues)

    def summary(self) -> str:
        """Return a one-line summary suitable for a gate detail field."""
        if not self.issues:
            return (
                f"All docstrings OK across {self.checked_functions} function(s) in {len(self.checked_files)} file(s)."
            )
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        return f"{len(self.issues)} docstring issue(s): {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Style detection
# ---------------------------------------------------------------------------


def _detect_style(docstring: str) -> DocstyleKind:
    """Auto-detect the docstring style from content."""
    if _GOOGLE_SECTION_RE.search(docstring):
        return "google"
    if _NUMPY_SECTION_RE.search(docstring):
        return "numpy"
    if _REST_PARAM_RE.search(docstring) or _REST_RETURNS_RE.search(docstring):
        return "rest"
    return "google"  # Default assumption


# ---------------------------------------------------------------------------
# Parameter extraction from docstring
# ---------------------------------------------------------------------------


def _extract_documented_params_google(docstring: str) -> set[str]:
    """Extract parameter names from a Google-style docstring."""
    # Find the Args section.  It is terminated by either the end of the
    # docstring or a sibling section header (an unindented or
    # minimally-indented capitalised word followed by ``:``, e.g.
    # ``Returns:`` / ``Raises:``).  We must NOT terminate on the param
    # lines themselves which are typically indented by 4+ spaces.
    args_match = re.search(
        r"Args:\s*\n((?:(?!\n[ \t]{0,3}[A-Z]\w*:).)*)",
        docstring,
        re.DOTALL,
    )
    if not args_match:
        return set()
    args_block = args_match.group(1)
    params: set[str] = set()
    for m in _GOOGLE_PARAM_RE.finditer(args_block):
        params.add(m.group(1))
    return params


_NUMPY_HEADER_RE = re.compile(r"Parameters[ \t]*\n[ \t]*[-=]+[ \t]*\n")
_NUMPY_SECTION_END_RE = re.compile(r"\n[ \t]*\w[^\n]*\n[ \t]*[-=]+")


def _extract_documented_params_numpy(docstring: str) -> set[str]:
    """Extract parameter names from a NumPy-style docstring."""
    header_match = _NUMPY_HEADER_RE.search(docstring)
    if not header_match:
        return set()
    rest = docstring[header_match.end():]
    end_match = _NUMPY_SECTION_END_RE.search(rest)
    block = rest[:end_match.start()] if end_match else rest
    params: set[str] = set()
    for m in _NUMPY_PARAM_RE.finditer(block):
        name = m.group(1)
        if name not in ("type", "optional", "default"):
            params.add(name)
    return params


def _extract_documented_params_rest(docstring: str) -> set[str]:
    """Extract parameter names from a reST-style docstring."""
    return {m.group(1) for m in _REST_PARAM_RE.finditer(docstring)}


def _extract_documented_params(docstring: str, style: DocstyleKind) -> set[str]:
    """Extract documented parameter names for the given style."""
    if style == "google":
        return _extract_documented_params_google(docstring)
    if style == "numpy":
        return _extract_documented_params_numpy(docstring)
    return _extract_documented_params_rest(docstring)


# ---------------------------------------------------------------------------
# Return/raise documentation checks
# ---------------------------------------------------------------------------


def _has_return_doc(docstring: str, style: DocstyleKind) -> bool:
    """Return True if the docstring documents a return value."""
    if style == "google":
        return bool(re.search(r"^\s*Returns?:", docstring, re.MULTILINE))
    if style == "numpy":
        return bool(re.search(r"Returns?[ \t]*\n[ \t]*[-=]+", docstring))
    return bool(_REST_RETURNS_RE.search(docstring))


def _has_raises_doc(docstring: str, style: DocstyleKind) -> bool:
    """Return True if the docstring documents raised exceptions."""
    if style == "google":
        return bool(re.search(r"^\s*Raises?:", docstring, re.MULTILINE))
    if style == "numpy":
        return bool(re.search(r"Raises?[ \t]*\n[ \t]*[-=]+", docstring))
    return bool(_REST_RAISES_RE.search(docstring))


# ---------------------------------------------------------------------------
# Redundancy detection
# ---------------------------------------------------------------------------

_TRIVIAL_VERBS = re.compile(
    r"^(get|set|init|initialise|initialize|create|make|build|return|returns?|"
    r"return\s+the|gets?\s+the|sets?\s+the)\b",
    re.IGNORECASE,
)


def _is_redundant(func_name: str, docstring: str) -> bool:
    """Return True when the first sentence trivially restates the function name.

    For example, a function ``get_user`` with a docstring ``Get user.`` or
    ``Returns the user.`` is considered redundant.
    """
    if not docstring.strip():
        return False
    # Normalise function name to words
    name_words = set(re.sub(r"[_\s]+", " ", func_name.lower()).split())
    first_sentence = re.split(r"[.\n]", docstring.strip())[0].lower()

    # Remove common trivial openers
    first_sentence = _TRIVIAL_VERBS.sub("", first_sentence).strip()

    # Compare significant words
    doc_words = set(re.findall(r"\w+", first_sentence))
    if not doc_words:
        return False
    # If all words in the first sentence appear in the function name, it's trivial
    meaningful = doc_words - {"the", "a", "an", "and", "or", "of", "for", "is", "to"}
    return bool(meaningful) and meaningful.issubset(name_words)


# ---------------------------------------------------------------------------
# Accuracy checks
# ---------------------------------------------------------------------------


def _get_func_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the public parameter names of a function, excluding ``self``/``cls``."""
    params: list[str] = []
    args = node.args
    all_args = list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    for arg in all_args:
        if arg.arg not in ("self", "cls"):
            params.append(arg.arg)
    return params


def _check_param_accuracy(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    docstring: str,
    style: DocstyleKind,
    rel_path: str,
    qualified_name: str,
) -> list[CommentIssue]:
    """Check that documented parameter names match the actual signature."""
    issues: list[CommentIssue] = []
    actual_params = set(_get_func_params(node))
    if not actual_params:
        return issues

    documented = _extract_documented_params(docstring, style)
    if not documented:
        return issues  # Nothing documented — caught by completeness check

    # Params in docstring that don't exist in signature
    phantom = documented - actual_params
    for name in sorted(phantom):
        issues.append(
            CommentIssue(
                kind="inaccurate",
                symbol=qualified_name,
                file=rel_path,
                line=node.lineno,
                detail=(
                    f"Docstring documents parameter {name!r} but it is not in "
                    f"the function signature. Actual params: {sorted(actual_params)}"
                ),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Completeness checks
# ---------------------------------------------------------------------------


def _func_has_return_value(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when *node* contains at least one non-bare ``return`` statement."""
    return any(isinstance(child, ast.Return) and child.value is not None for child in ast.walk(node))


def _func_raises_exceptions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when *node* contains at least one ``raise`` statement."""
    return any(isinstance(child, ast.Raise) and child.exc is not None for child in ast.walk(node))


def _check_completeness(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    docstring: str,
    style: DocstyleKind,
    rel_path: str,
    qualified_name: str,
) -> list[CommentIssue]:
    """Check that the docstring covers all params, returns, and raises."""
    issues: list[CommentIssue] = []
    actual_params = _get_func_params(node)

    if actual_params:
        documented = _extract_documented_params(docstring, style)
        missing = set(actual_params) - documented
        for name in sorted(missing):
            issues.append(
                CommentIssue(
                    kind="incomplete",
                    symbol=qualified_name,
                    file=rel_path,
                    line=node.lineno,
                    detail=f"Parameter {name!r} is not documented in the docstring.",
                )
            )

    if _func_has_return_value(node) and not _has_return_doc(docstring, style):
        issues.append(
            CommentIssue(
                kind="incomplete",
                symbol=qualified_name,
                file=rel_path,
                line=node.lineno,
                detail="Function returns a value but the docstring has no return section.",
            )
        )

    if _func_raises_exceptions(node) and not _has_raises_doc(docstring, style):
        issues.append(
            CommentIssue(
                kind="incomplete",
                symbol=qualified_name,
                file=rel_path,
                line=node.lineno,
                detail="Function raises exceptions but the docstring has no raises section.",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Style compliance checks
# ---------------------------------------------------------------------------


def _check_style(
    docstring: str,
    detected_style: DocstyleKind,
    expected_style: DocstyleKind,
    rel_path: str,
    qualified_name: str,
    lineno: int,
) -> list[CommentIssue]:
    """Warn when detected style does not match *expected_style*."""
    if expected_style == "auto":
        return []
    if detected_style != expected_style:
        return [
            CommentIssue(
                kind="wrong_style",
                symbol=qualified_name,
                file=rel_path,
                line=lineno,
                detail=(f"Expected {expected_style!r} docstring style but found {detected_style!r} style."),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _is_public(name: str) -> bool:
    """Return True for public names (not prefixed with ``_``)."""
    return not name.startswith("_")


def _qualified(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def _analyse_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified_name: str,
    rel_path: str,
    docstyle: DocstyleKind,
    report: CommentQualityReport,
) -> None:
    """Analyse one function node and append issues to *report*."""
    report.checked_functions += 1
    raw_doc = ast.get_docstring(node)
    if not raw_doc:
        # Public functions must have docstrings
        if _is_public(node.name):
            report.issues.append(
                CommentIssue(
                    kind="incomplete",
                    symbol=qualified_name,
                    file=rel_path,
                    line=node.lineno,
                    detail="Public function has no docstring.",
                )
            )
        return

    style = _detect_style(raw_doc) if docstyle == "auto" else docstyle

    # Accuracy
    report.issues.extend(_check_param_accuracy(node, raw_doc, style, rel_path, qualified_name))

    # Completeness (only for public functions/methods)
    if _is_public(node.name):
        report.issues.extend(_check_completeness(node, raw_doc, style, rel_path, qualified_name))

    # Redundancy (warn only)
    if _is_redundant(node.name, raw_doc):
        report.issues.append(
            CommentIssue(
                kind="redundant",
                symbol=qualified_name,
                file=rel_path,
                line=node.lineno,
                detail=(
                    f"Docstring for {node.name!r} appears to trivially restate "
                    "the function name without adding meaning."
                ),
            )
        )

    # Style compliance
    detected = _detect_style(raw_doc)
    report.issues.extend(_check_style(raw_doc, detected, docstyle, rel_path, qualified_name, node.lineno))


def analyse_file(
    source: str,
    rel_path: str,
    docstyle: DocstyleKind = "auto",
) -> list[CommentIssue]:
    """Analyse docstrings in a single Python *source* string.

    Args:
        source: Full source text of the Python file.
        rel_path: Repository-relative path (used in issue messages).
        docstyle: Expected docstring style. ``"auto"`` detects per-docstring.

    Returns:
        List of :class:`CommentIssue` found in the file.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        logger.debug("Syntax error in %s: %s", rel_path, exc)
        return []

    report = CommentQualityReport()
    report.checked_files = [rel_path]

    # Walk top-level and class-member functions
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in ast.walk(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = _qualified(node.name, item.name)
                    _analyse_node(item, qualified, rel_path, docstyle, report)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Skip if it's inside a class (handled above)
            _analyse_node(node, node.name, rel_path, docstyle, report)

    return report.issues


def analyse(
    changed_files: list[str],
    workdir: Path,
    *,
    docstyle: DocstyleKind = "auto",
) -> CommentQualityReport:
    """Run comment quality analysis on *changed_files*.

    Args:
        changed_files: Repository-relative paths of changed Python files.
        workdir: Project root directory.
        docstyle: Expected docstring style. ``"auto"`` detects per-docstring.

    Returns:
        A :class:`CommentQualityReport` with all detected issues.
    """
    report = CommentQualityReport()
    report.checked_files = list(changed_files)

    for rel_path in changed_files:
        abs_path = workdir / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not read %s: %s", rel_path, exc)
            continue

        issues = analyse_file(source, rel_path, docstyle=docstyle)
        report.issues.extend(issues)
        report.checked_functions += sum(
            1
            for node in ast.walk(ast.parse(source, type_comments=False))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    return report
