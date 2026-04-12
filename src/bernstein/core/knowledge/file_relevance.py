"""Embedding-based file relevance scoring for smarter context injection.

Computes relevance scores between a query and project files using
pure-Python TF-IDF-like scoring. No external ML/embedding dependencies.

Scoring strategy (weighted combination):
  1. Keyword overlap (0.5): Jaccard similarity between tokenized query
     and file content.
  2. Path relevance (0.3): fraction of query terms found in the file path.
  3. Import relevance (0.2): whether the file imports or is imported by
     files mentioned in the query.

Public API:
  - ``score_file_relevance`` -- score a single file against a query.
  - ``rank_files`` -- score many files and return the top-K.
  - ``get_project_files`` -- enumerate project files filtered by extension.
  - ``extract_query_terms`` -- tokenize and normalize query text.
"""

from __future__ import annotations

import logging
import pathlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

_WEIGHT_KEYWORD: float = 0.5
_WEIGHT_PATH: float = 0.3
_WEIGHT_IMPORT: float = 0.2

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "because",
        "as",
        "until",
        "while",
        "of",
        "at",
        "by",
        "for",
        "with",
        "about",
        "against",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "to",
        "from",
        "up",
        "down",
        "in",
        "out",
        "on",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
    }
)

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".git",
        ".sdd",
        ".ruff_cache",
        ".tox",
        ".egg-info",
    }
)

_MAX_FILE_SIZE: int = 512 * 1024  # 512 KB

# Regex for Python import lines.
_IMPORT_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)


@dataclass(frozen=True)
class FileRelevanceScore:
    """A file scored for relevance to a query.

    Attributes:
        file_path: Relative path from project root.
        score: Relevance score in [0, 1].
        match_reason: Human-readable explanation of score components.
    """

    file_path: str
    score: float
    match_reason: str


@dataclass(frozen=True)
class RelevanceResult:
    """Result of ranking files against a query.

    Attributes:
        query: Original query text.
        scored_files: Scored files ordered by relevance (descending).
        top_k: Maximum number of results requested.
    """

    query: str
    scored_files: tuple[FileRelevanceScore, ...]
    top_k: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_]*")


def extract_query_terms(text: str) -> list[str]:
    """Tokenize and normalize query text.

    Splits on non-alphanumeric boundaries (including camelCase and
    snake_case), lowercases, removes stop-words, and deduplicates
    while preserving order.

    Args:
        text: Raw query or description text.

    Returns:
        Deduplicated list of normalized, non-stop-word tokens.
    """
    # Split camelCase / PascalCase before lowering.
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    expanded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", expanded)
    lowered = expanded.lower()

    tokens = _TOKEN_RE.findall(lowered)
    seen: set[str] = set()
    result: list[str] = []
    for tok in tokens:
        if tok not in _STOP_WORDS and tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def _read_file_safe(path: pathlib.Path) -> str | None:
    """Read file content, returning None on any error."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_FILE_SIZE:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _should_skip_dir(part: str) -> bool:
    return part.startswith(".") or part in _SKIP_DIRS


def _extract_imports(content: str) -> set[str]:
    """Extract imported module names from Python source.

    Args:
        content: Python file content.

    Returns:
        Set of top-level module names referenced by import statements.
    """
    modules: set[str] = set()
    for match in _IMPORT_RE.finditer(content):
        mod = match.group(1) or match.group(2)
        if mod:
            # Keep only the first dotted component for fuzzy matching.
            modules.add(mod.split(".")[0])
            # Also add the full dotted path for precision.
            modules.add(mod)
    return modules


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def _keyword_score(query_terms: set[str], file_content: str) -> float:
    """Jaccard similarity between query terms and file content tokens.

    Args:
        query_terms: Set of normalized query tokens.
        file_content: Raw file text.

    Returns:
        Jaccard similarity in [0, 1].
    """
    if not query_terms:
        return 0.0
    file_tokens = set(_TOKEN_RE.findall(file_content.lower()))
    if not file_tokens:
        return 0.0
    intersection = query_terms & file_tokens
    union = query_terms | file_tokens
    return len(intersection) / len(union) if union else 0.0


def _path_score(query_terms: set[str], file_path: str) -> float:
    """Fraction of query terms found in the file path.

    Args:
        query_terms: Set of normalized query tokens.
        file_path: Relative path string (e.g. ``src/foo/bar.py``).

    Returns:
        Score in [0, 1].
    """
    if not query_terms:
        return 0.0
    path_lower = file_path.lower()
    path_parts = set(_TOKEN_RE.findall(path_lower))
    hits = sum(1 for term in query_terms if term in path_parts)
    return hits / len(query_terms)


def _import_score(
    query_terms: set[str],
    file_imports: set[str],
) -> float:
    """Score based on whether imports overlap with query terms.

    Args:
        query_terms: Set of normalized query tokens.
        file_imports: Module names imported by the file.

    Returns:
        Score in [0, 1].
    """
    if not query_terms or not file_imports:
        return 0.0
    import_tokens: set[str] = set()
    for mod in file_imports:
        import_tokens.update(_TOKEN_RE.findall(mod.lower()))
    if not import_tokens:
        return 0.0
    overlap = query_terms & import_tokens
    return len(overlap) / len(query_terms)


def _build_match_reason(
    kw: float,
    path: float,
    imp: float,
) -> str:
    """Build a human-readable explanation of score components.

    Args:
        kw: Keyword score.
        path: Path score.
        imp: Import score.

    Returns:
        Explanation string.
    """
    parts: list[str] = []
    if kw > 0:
        parts.append(f"keyword={kw:.2f}")
    if path > 0:
        parts.append(f"path={path:.2f}")
    if imp > 0:
        parts.append(f"import={imp:.2f}")
    return ", ".join(parts) if parts else "no match"


def score_file_relevance(
    query: str,
    file_path: str,
    project_root: str | pathlib.Path,
) -> FileRelevanceScore:
    """Compute relevance of a single file to a query.

    Uses a weighted combination of keyword overlap, path relevance,
    and import relevance.

    Args:
        query: Task description or search text.
        file_path: Relative path from *project_root*.
        project_root: Absolute path to the project root directory.

    Returns:
        A ``FileRelevanceScore`` with the combined score and explanation.
    """
    root = pathlib.Path(project_root)
    query_terms = set(extract_query_terms(query))

    abs_path = root / file_path
    content = _read_file_safe(abs_path)
    if content is None:
        return FileRelevanceScore(
            file_path=file_path,
            score=0.0,
            match_reason="unreadable",
        )

    kw = _keyword_score(query_terms, content)
    path = _path_score(query_terms, file_path)

    imports = _extract_imports(content) if abs_path.suffix == ".py" else set[str]()
    imp = _import_score(query_terms, imports)

    combined = _WEIGHT_KEYWORD * kw + _WEIGHT_PATH * path + _WEIGHT_IMPORT * imp
    # Clamp to [0, 1]
    combined = max(0.0, min(1.0, combined))

    reason = _build_match_reason(kw, path, imp)
    return FileRelevanceScore(
        file_path=file_path,
        score=round(combined, 4),
        match_reason=reason,
    )


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------


def get_project_files(
    project_root: str | pathlib.Path,
    extensions: Sequence[str] = (".py",),
) -> list[str]:
    """List project files filtered by extension.

    Skips hidden directories, common build/cache directories, and files
    exceeding ``_MAX_FILE_SIZE``.

    Args:
        project_root: Absolute path to the project root.
        extensions: File extensions to include (e.g. ``(".py", ".ts")``).

    Returns:
        List of relative POSIX paths from *project_root*.
    """
    root = pathlib.Path(project_root)
    ext_set = frozenset(extensions)
    result: list[str] = []

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.suffix not in ext_set:
            continue
        try:
            rel = fpath.relative_to(root)
        except ValueError:
            continue
        if any(_should_skip_dir(part) for part in rel.parts[:-1]):
            continue
        try:
            if fpath.stat().st_size > _MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        result.append(rel.as_posix())

    return result


def rank_files(
    query: str,
    file_paths: Sequence[str],
    project_root: str | pathlib.Path,
    top_k: int = 10,
) -> RelevanceResult:
    """Score all given files against a query and return the top-K.

    Args:
        query: Task description or search text.
        file_paths: Iterable of relative file paths from *project_root*.
        project_root: Absolute path to the project root directory.
        top_k: Number of top results to return.

    Returns:
        A ``RelevanceResult`` containing ranked ``FileRelevanceScore`` items.
    """
    scored: list[FileRelevanceScore] = []
    for fp in file_paths:
        scored.append(score_file_relevance(query, fp, project_root))

    scored.sort(key=lambda s: s.score, reverse=True)
    top = scored[:top_k]
    return RelevanceResult(
        query=query,
        scored_files=tuple(top),
        top_k=top_k,
    )
