"""Repo orchestration-readiness analysis.

Implementation of [#768](https://github.com/sipyourdrink-ltd/bernstein/issues/768):
`bernstein analyze` scans a repo and reports a readiness score for
multi-agent orchestration, plus actionable opportunities.

The module is pure-Python and offline - no network calls, no LLM calls.
This keeps `bernstein analyze` cheap to run in CI and on first-clone
before the user has configured an LLM provider.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Reuse the file-discovery exclusion rules so `analyze` and `run` see the
# same project surface.
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
        ".next",
        ".turbo",
        ".cache",
        "target",  # rust
        "vendor",  # go
    }
)
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info", ".lock"})

# Language map keyed on file extension. Extensions chosen to cover the
# stacks bernstein already supports first-class (py / ts) plus the most
# common 'agent stack' siblings (rust, go, sol). Anything else lands in
# "Other".
_LANGUAGE_BY_EXT = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".rs": "Rust",
    ".go": "Go",
    ".sol": "Solidity",
    ".java": "Java",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".c": "C",
    ".h": "C",
    ".hpp": "C++",
}

# Heuristic: file is a test if its name starts with `test_` or ends with
# `.test.<ext>` / `.spec.<ext>`, OR it lives inside a `tests/` / `__tests__`
# directory. Mirrors how most ecosystems mark test files.
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})


def _is_test_file(path: Path) -> bool:
    if any(p in _TEST_DIR_NAMES for p in path.parts):
        return True
    name = path.name
    stem = path.stem  # filename without extension
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return stem.endswith((".test", ".spec"))


def _should_skip_dir(name: str) -> bool:
    return name in _IGNORED_DIRS or (name.startswith(".") and name != ".github")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageBreakdown:
    """Files-by-language summary."""

    name: str
    files: int
    pct: float  # of total source files in the repo


def _language_breakdowns() -> list[LanguageBreakdown]:
    """Return a typed empty language-breakdown list."""
    return []


def _paths() -> list[Path]:
    """Return a typed empty path list."""
    return []


def _file_line_pairs() -> list[tuple[Path, int]]:
    """Return a typed empty file-line list."""
    return []


def _strings() -> list[str]:
    """Return a typed empty string list."""
    return []


@dataclass
class RepoAnalysis:
    """Result of analyzing a repo for orchestration readiness."""

    root: Path
    total_files: int = 0
    total_source_files: int = 0
    total_lines: int = 0
    largest_file_lines: int = 0
    largest_file_path: Path | None = None

    languages: list[LanguageBreakdown] = field(default_factory=_language_breakdowns)

    test_files: int = 0
    source_files_without_tests_estimate: int = 0  # source files that have no matching test
    test_coverage_estimate_pct: float = 0.0  # estimated, not measured

    has_ci: bool = False
    ci_kind: str = ""  # "github", "gitlab", "jenkins", or ""

    modules_without_tests: list[Path] = field(default_factory=_paths)
    files_over_300_lines: list[tuple[Path, int]] = field(default_factory=_file_line_pairs)
    python_files_without_type_hints: int = 0  # files where no `: ` annotation found

    readiness_score: float = 0.0  # 0-10 scale
    strengths: list[str] = field(default_factory=_strings)
    opportunities: list[str] = field(default_factory=_strings)
    recommended_first_run: str = ""


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


def analyze_repo(root: Path) -> RepoAnalysis:
    """Run the full analysis on *root* and return the result.

    Performs a single os.walk pass (cheap on a Macbook even for repos with
    10k+ files) and computes every metric in-line. Returns a `RepoAnalysis`
    with structured fields that callers can render however they want.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    analysis = RepoAnalysis(root=root)

    files_by_language: dict[str, int] = {}
    # Single-pass walk.
    for path in _walk_files(root):
        analysis.total_files += 1
        ext = path.suffix.lower()
        lang = _LANGUAGE_BY_EXT.get(ext)
        if lang is None:
            continue

        analysis.total_source_files += 1
        files_by_language[lang] = files_by_language.get(lang, 0) + 1

        lines = _line_count(path)
        analysis.total_lines += lines
        if lines > analysis.largest_file_lines:
            analysis.largest_file_lines = lines
            analysis.largest_file_path = path.relative_to(root)

        if lines > 300:
            analysis.files_over_300_lines.append((path.relative_to(root), lines))

        # Relative to the analyzed root, as the module scan below does: an absolute path counted a
        # `tests` folder ABOVE the checkout, so every file of a repo under it was a test (#8056).
        if _is_test_file(path.relative_to(root)):
            analysis.test_files += 1
        elif lang == "Python" and not _has_type_hints(path):
            analysis.python_files_without_type_hints += 1

    # Language ranking.
    total_for_pct = max(analysis.total_source_files, 1)
    ranked = sorted(files_by_language.items(), key=operator.itemgetter(1), reverse=True)
    analysis.languages = [
        LanguageBreakdown(name=name, files=count, pct=round(count / total_for_pct * 100, 1)) for name, count in ranked
    ]

    # Test coverage estimate (count-based, not line-based).
    non_test_source = analysis.total_source_files - analysis.test_files
    if non_test_source > 0:
        ratio = min(analysis.test_files / non_test_source, 1.0)
        analysis.test_coverage_estimate_pct = round(ratio * 100, 1)

    # Modules without tests - group source files by top-level package and
    # flag packages with no test files at all.
    analysis.modules_without_tests = _modules_without_tests(root)

    # CI detection.
    analysis.has_ci, analysis.ci_kind = _detect_ci(root)

    # Compute readiness score + narrative.
    _compute_score_and_narrative(analysis)

    return analysis


# ---------------------------------------------------------------------------
# Pass helpers
# ---------------------------------------------------------------------------


def _walk_files(root: Path) -> Iterable[Path]:
    """Yield every non-ignored file under *root*."""
    # Use os.walk-ish recursion via pathlib so we can skip ignored dirs
    # cheaply without scanning their children.
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if _should_skip_dir(entry.name):
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    if entry.suffix in _IGNORED_SUFFIXES:
                        continue
                    yield entry
            except (PermissionError, OSError):
                continue


def _line_count(path: Path) -> int:
    """Cheap line count. Returns 0 on any read error."""
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except (OSError, PermissionError):
        return 0


def _has_type_hints(path: Path) -> bool:
    """Heuristic: does this Python file have any function-level type hints?

    Cheap-ish: looks for `def foo(...: T)` or `-> T:` patterns. Misses
    decorator-stacked or `from typing import` cases where annotations
    live elsewhere; that's acceptable as a heuristic. The goal is to
    catch genuinely-untyped files, not to score 100% accuracy.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return True  # don't penalize unreadable files
    # Simple substring checks - `: ` after an open-paren or `-> ` before `:`
    # is enough to indicate at least one annotation.
    return ") -> " in text or (": " in text and "def " in text)


def _modules_without_tests(root: Path) -> list[Path]:
    """Return top-level Python packages or src/ subdirs with zero test files."""
    candidates: dict[Path, dict[str, bool]] = {}
    for entry in _walk_files(root):
        rel = entry.relative_to(root)
        if rel.suffix not in (".py", ".ts", ".tsx", ".js", ".rs"):
            continue
        # Top-level "module" is the first directory component under src/
        # or under the repo root.
        parts = rel.parts
        if len(parts) < 2:
            continue
        top = Path(parts[0]) if parts[0] != "src" else Path(*parts[:2])
        info = candidates.setdefault(top, {"source": False, "test": False})
        if _is_test_file(rel):
            info["test"] = True
        else:
            info["source"] = True

    return sorted([top for top, info in candidates.items() if info["source"] and not info["test"]])


def _detect_ci(root: Path) -> tuple[bool, str]:
    """Return (has_ci, ci_kind) by checking for known CI config files."""
    if (root / ".github" / "workflows").is_dir() and any((root / ".github" / "workflows").iterdir()):
        return True, "github"
    if (root / ".gitlab-ci.yml").is_file():
        return True, "gitlab"
    if (root / "Jenkinsfile").is_file():
        return True, "jenkins"
    if (root / ".circleci" / "config.yml").is_file():
        return True, "circle"
    if (root / "azure-pipelines.yml").is_file():
        return True, "azure"
    return False, ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _compute_score_and_narrative(a: RepoAnalysis) -> None:
    """Fill in `readiness_score`, `strengths`, `opportunities`, and `recommended_first_run`.

    The score is the average of four equally-weighted components on a 0-10 scale:

      1. **Test coverage** - proxied by ratio of test files to source files.
      2. **Modularity** - proxied by absence of files over 300 lines + presence
         of test files in every top-level module.
      3. **CI presence** - 10 if a CI config exists, else 0.
      4. **Typed (Python only)** - 10 if 90%+ of Python files have at least
         one annotation, falls linearly to 0 at 0% typed.

    Each component contributes a maximum of 2.5 to the final out-of-10 score.
    Repos with no Python source skip component 4 and rebalance to 1/3 each.
    """
    has_python = any(lang.name == "Python" for lang in a.languages)

    # 1. Test coverage component (0-10).
    cov_pct = a.test_coverage_estimate_pct
    if cov_pct >= 80:
        c_tests = 10.0
    elif cov_pct >= 60:
        c_tests = 8.0
    elif cov_pct >= 40:
        c_tests = 6.0
    elif cov_pct >= 20:
        c_tests = 4.0
    elif cov_pct > 0:
        c_tests = 2.0
    else:
        c_tests = 0.0

    # 2. Modularity component (0-10).
    over_300_count = len(a.files_over_300_lines)
    modules_missing_tests = len(a.modules_without_tests)
    c_mod = 10.0
    if a.largest_file_lines > 1000:
        c_mod -= 5
    elif a.largest_file_lines > 500:
        c_mod -= 2
    if over_300_count > 10:
        c_mod -= 3
    elif over_300_count > 5:
        c_mod -= 1.5
    if modules_missing_tests > 0:
        c_mod -= min(modules_missing_tests * 0.5, 3)
    c_mod = max(c_mod, 0.0)

    # 3. CI component.
    c_ci = 10.0 if a.has_ci else 0.0

    # 4. Type-annotation component (Python-only). `python_files_without_type_hints`
    # is already counted excluding test files inside analyze_repo, so we divide
    # by the count of non-test Python source files.
    if has_python:
        py_files = next((lang.files for lang in a.languages if lang.name == "Python"), 1)
        # py_files counts ALL Python files including tests; subtract test files
        # (approximate: assume tests are evenly distributed across languages
        # which is fine because test_files is small relative to total).
        py_non_test = max(py_files - a.test_files, 1)
        typed_ratio = 1.0 - (a.python_files_without_type_hints / py_non_test)
        typed_ratio = max(min(typed_ratio, 1.0), 0.0)
        c_typed = round(typed_ratio * 10, 1)
    else:
        c_typed = None

    if c_typed is None:
        a.readiness_score = round((c_tests + c_mod + c_ci) / 3, 1)
    else:
        a.readiness_score = round((c_tests + c_mod + c_ci + c_typed) / 4, 1)

    # Narrative.
    strengths: list[str] = []
    opps: list[str] = []

    if c_tests >= 6:
        strengths.append("Good test coverage - agents can verify their work")
    elif c_tests > 0:
        n = len(a.modules_without_tests)
        if n > 0:
            opps.append(f'{n} module{"s" if n != 1 else ""} have no tests; consider `bernstein -g "add tests"`')
    else:
        opps.append('No test files detected; consider `bernstein -g "add tests for the public API"` first')

    if c_mod >= 8:
        strengths.append(f"Modular structure - no large monolith files (max: {a.largest_file_lines} lines)")
    elif a.files_over_300_lines:
        opps.append(f"{len(a.files_over_300_lines)} files over 300 lines could benefit from decomposition")

    if c_ci == 10:
        strengths.append("CI configured - quality gates will work out of the box")
    else:
        opps.append("No CI config detected; add .github/workflows/ci.yml before delegating to agents")

    if c_typed is not None and c_typed < 7:
        opps.append(
            f"~{a.python_files_without_type_hints} Python files lack type annotations; "
            'consider `bernstein -g "add type hints"`'
        )
    elif c_typed is not None and c_typed >= 9:
        strengths.append("Type annotations broadly present - agents can use them as machine-readable specs")

    a.strengths = strengths
    a.opportunities = opps

    # Recommended first run - pick the highest-leverage opportunity.
    if a.python_files_without_type_hints > 5:
        a.recommended_first_run = 'bernstein -g "Add type annotations to all public functions in src/"'
    elif a.modules_without_tests:
        a.recommended_first_run = 'bernstein -g "Add unit tests for the modules that have none"'
    elif a.files_over_300_lines:
        a.recommended_first_run = 'bernstein -g "Decompose the largest files into smaller modules"'
    else:
        a.recommended_first_run = 'bernstein -g "Add a docs/ARCHITECTURE.md summarising the codebase"'
