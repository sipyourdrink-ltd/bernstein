"""Context compression engine for reducing agent spawn context by 40%+.

Provides:
- DependencyGraph: Builds file-level dependency graph via AST analysis
- BM25Ranker: Ranks files by keyword relevance to task description
- ContextCompressor: Orchestrates compression using dependencies + BM25
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.compression_models import CompressionMetrics, CompressionResult

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_SKIP_DIRS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build",
})


def _should_skip(rel_parts: tuple[str, ...]) -> bool:
    """Return True if any path component is a hidden dir or in _SKIP_DIRS."""
    return any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts)


def _iter_python_files(workdir: Path) -> list[Path]:
    """Collect all .py files in workdir, skipping hidden/vendored dirs."""
    result: list[Path] = []
    for fpath in workdir.rglob("*.py"):
        if not _should_skip(fpath.relative_to(workdir).parts):
            result.append(fpath)
    return result


class DependencyGraph:
    """Builds and queries file-level dependency graph.

    Uses AST analysis to extract imports from Python files and build
    a reverse dependency map: for each file, which other files does it import?

    Attributes:
        workdir: Project root directory.
        graph: Dict mapping {filename: [dependencies]}.
    """

    def __init__(self, workdir: Path) -> None:
        """Initialize DependencyGraph.

        Args:
            workdir: Project root directory.
        """
        self.workdir = workdir
        self.graph: dict[str, list[str]] = {}

    def build(self) -> None:
        """Build dependency graph by scanning all .py files in workdir.

        For each Python file, extract import statements via AST parsing
        and record which files it depends on (by relative path).

        The graph maps {file: [dependencies]}.
        """
        py_files = _iter_python_files(self.workdir)

        # Build module → path index first (needed for resolution)
        self._module_index: dict[str, str] = {}
        for fpath in py_files:
            try:
                rel = fpath.relative_to(self.workdir).as_posix()
                mod = self._path_to_module(rel)
                self._module_index[mod] = rel
                # Also index by final component for relative-import resolution
                parts = mod.rsplit(".", 1)
                if len(parts) == 2:
                    self._module_index.setdefault(parts[1], rel)
            except Exception:
                pass

        for fpath in py_files:
            try:
                rel_path = fpath.relative_to(self.workdir).as_posix()
                deps = self._extract_imports_from_file(fpath)
                file_deps = self._resolve_module_paths(deps, fpath)
                self.graph[rel_path] = file_deps
            except Exception as e:
                logger.debug("Failed to analyze %s: %s", fpath, e)

    @staticmethod
    def _path_to_module(rel: str) -> str:
        """Convert relative file path to dotted module name.

        Args:
            rel: Relative path like ``src/bernstein/core/spawner.py``.

        Returns:
            Module name like ``bernstein.core.spawner``.
        """
        p = rel
        if p.startswith("src/"):
            p = p[4:]
        if p.endswith(".py"):
            p = p[:-3]
        if p.endswith("/__init__"):
            p = p[:-9]
        return p.replace("/", ".")

    def _extract_imports_from_file(self, fpath: Path) -> set[str]:
        """Extract imported module names from a Python file via AST.

        Args:
            fpath: Absolute path to Python file.

        Returns:
            Set of module names (e.g., {"bernstein.core.spawner", "pathlib"}).
        """
        try:
            source = fpath.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            return set()

        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        return imports

    def _resolve_module_paths(self, modules: set[str], source_file: Path) -> list[str]:
        """Convert module names to relative file paths within workdir.

        Args:
            modules: Set of module names from import statements.
            source_file: The file doing the importing (for context).

        Returns:
            List of relative file paths (only those that exist in workdir).
        """
        file_deps: list[str] = []

        for module in modules:
            # Try index lookup first (fastest)
            resolved = self._module_index.get(module)
            if resolved:
                file_deps.append(resolved)
                continue

            # Try filesystem patterns
            parts = module.split(".")

            # Pattern 1: direct module path
            candidate = self.workdir / Path(*parts).with_suffix(".py")
            if candidate.is_file():
                try:
                    file_deps.append(candidate.relative_to(self.workdir).as_posix())
                    continue
                except ValueError:
                    pass

            # Pattern 2: package __init__.py
            candidate = self.workdir / Path(*parts) / "__init__.py"
            if candidate.is_file():
                try:
                    file_deps.append(candidate.relative_to(self.workdir).as_posix())
                    continue
                except ValueError:
                    pass

            # Pattern 3: src/ subdirectory
            candidate = self.workdir / "src" / Path(*parts).with_suffix(".py")
            if candidate.is_file():
                try:
                    file_deps.append(candidate.relative_to(self.workdir).as_posix())
                    continue
                except ValueError:
                    pass

        return file_deps

    def dependents_of(self, filename: str) -> list[str]:
        """Return all files that import the given filename.

        Args:
            filename: Relative file path (e.g., "src/foo.py").

        Returns:
            List of relative file paths that import filename.
        """
        return [fpath for fpath, deps in self.graph.items() if filename in deps]

    def reachable_from(self, filename: str, max_depth: int = 2) -> set[str]:
        """Return all files reachable from the given file via imports.

        Performs BFS to find all files reachable by following import chains
        up to max_depth levels deep.

        Args:
            filename: Starting file (relative path).
            max_depth: Maximum traversal depth.

        Returns:
            Set of reachable file paths (including the starting file).
        """
        reachable: set[str] = {filename}
        queue: list[tuple[str, int]] = [(filename, 0)]

        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for dep in self.graph.get(current, []):
                if dep not in reachable:
                    reachable.add(dep)
                    queue.append((dep, depth + 1))

        return reachable


_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "will",
        "with",
    }
)


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens, removing stop words.

    Args:
        text: Input text.

    Returns:
        List of tokens.
    """
    tokens = re.findall(r"\b\w+\b", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


class BM25Ranker:
    """Ranks files by TF-IDF / keyword relevance to a query.

    Uses scikit-learn TF-IDF when available; falls back to simple term
    overlap scoring so there are no hard dependencies on ML libraries.

    Attributes:
        documents: Dict mapping {filename: document_content}.
        filenames: Ordered list of filenames for index alignment.
    """

    def __init__(self, documents: dict[str, str]) -> None:
        """Initialize BM25Ranker with a set of documents.

        Args:
            documents: Dict mapping {filename: document_text}.
        """
        self.documents = documents
        self.filenames: list[str] = list(documents.keys())

        # Tokenized corpus (for fallback scorer)
        self._corpus: list[list[str]] = [_tokenize(documents[fname]) for fname in self.filenames]

        # Try to use sklearn TF-IDF for better scoring
        self._use_sklearn = False
        self._tfidf: object = None
        self._tfidf_matrix: object = None
        if self.filenames:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-untyped]

                self._tfidf = TfidfVectorizer(lowercase=True, stop_words="english")  # type: ignore[assignment]
                self._tfidf_matrix = self._tfidf.fit_transform(  # type: ignore[union-attr]
                    [documents[f] for f in self.filenames]
                )
                self._use_sklearn = True
            except (ImportError, Exception):
                logger.debug("sklearn not available, using fallback TF ranking")

    def rank(
        self,
        query: str,
        threshold: float = 0.0,
        top_k: int | None = None,
    ) -> list[tuple[str, float]]:
        """Rank documents by relevance score against query.

        Args:
            query: Query text.
            threshold: Minimum score to include in results.
            top_k: Return only top K results (if None, return all above threshold).

        Returns:
            List of (filename, score) tuples, sorted by score descending.
        """
        if not self.filenames:
            return []

        if self._use_sklearn:
            query_vec = self._tfidf.transform([query])  # type: ignore[union-attr]
            scores = (query_vec @ self._tfidf_matrix.T).toarray()[0]  # type: ignore[union-attr,operator]
            results: list[tuple[str, float]] = [
                (self.filenames[i], float(scores[i]))  # type: ignore[reportUnknownArgumentType]
                for i in range(len(self.filenames))
            ]
        else:
            query_terms = set(_tokenize(query))
            results = []
            for i, fname in enumerate(self.filenames):
                doc_terms = set(self._corpus[i])
                overlap = len(query_terms & doc_terms)
                results.append((fname, float(overlap)))

        results = [(fname, score) for fname, score in results if score >= threshold]
        results.sort(key=lambda x: x[1], reverse=True)

        if top_k is not None:
            results = results[:top_k]

        return results


class ContextCompressor:
    """Orchestrates context compression using dependency graph and BM25 ranking.

    Selects a minimal set of files relevant to a set of tasks by:
    1. Using BM25/TF-IDF to match task keywords to file content
    2. Following dependency chains to include transitively-required files
    3. Limiting total selected files to avoid exceeding token budget

    Attributes:
        workdir: Project root directory.
        graph: DependencyGraph instance.
        ranker: BM25Ranker instance (or None if no Python files found).
    """

    def __init__(self, workdir: Path) -> None:
        """Initialize ContextCompressor.

        Args:
            workdir: Project root directory.
        """
        self.workdir = workdir
        self.graph = DependencyGraph(workdir)
        self.graph.build()

        file_contents: dict[str, str] = {}
        try:
            for fpath in _iter_python_files(workdir):
                try:
                    rel_path = fpath.relative_to(workdir).as_posix()
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                    file_contents[rel_path] = content[:500] + " " + rel_path
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to build BM25 index: %s", e)

        self.ranker: BM25Ranker | None = BM25Ranker(file_contents) if file_contents else None

    def select_relevant_files(
        self,
        tasks: list[Task],
        max_files: int = 20,
        max_depth: int = 2,
    ) -> tuple[list[str], int, int]:
        """Select minimal set of files relevant to a task batch.

        Combines BM25 keyword matching and dependency graph traversal to
        identify and include only the most relevant files.

        Args:
            tasks: List of tasks to find context for.
            max_files: Maximum number of files to select.
            max_depth: Maximum dependency traversal depth.

        Returns:
            Tuple of (selected_files, bm25_match_count, dependency_match_count).
        """
        selected: set[str] = set()
        bm25_matches: set[str] = set()
        dependency_matches: set[str] = set()

        # Always include files explicitly owned by tasks
        for task in tasks:
            for f in getattr(task, "owned_files", []):
                if f:
                    selected.add(f)
                    bm25_matches.add(f)

        if self.ranker is None:
            # No files to rank; return owned files + first max_files from workdir
            try:
                all_py = sorted(f.relative_to(self.workdir).as_posix() for f in self.workdir.rglob("*.py"))
                for f in all_py:
                    if len(selected) >= max_files:
                        break
                    selected.add(f)
            except Exception:
                pass
            return sorted(selected)[:max_files], len(bm25_matches), len(dependency_matches)

        for task in tasks:
            query = f"{task.title} {task.description}"

            # BM25 ranking: take top candidates
            ranked = self.ranker.rank(query, threshold=0.0, top_k=max_files * 2)
            for fname, _score in ranked:
                if len(selected) >= max_files:
                    break
                selected.add(fname)
                bm25_matches.add(fname)
                # Follow dependencies up to max_depth
                for dep in self.graph.reachable_from(fname, max_depth=max_depth):
                    if len(selected) >= max_files:
                        break
                    if dep not in selected:
                        dependency_matches.add(dep)
                    selected.add(dep)

        return sorted(selected)[:max_files], len(bm25_matches), len(dependency_matches)

    def estimate_tokens(self, files: list[str]) -> int:
        """Estimate token count for a list of files.

        Uses rough approximation: 1 token ≈ 4 characters.

        Args:
            files: List of relative file paths.

        Returns:
            Estimated token count (minimum 1).
        """
        total_chars = 0
        for fpath in files:
            try:
                full_path = self.workdir / fpath
                if full_path.is_file():
                    content = full_path.read_text(encoding="utf-8", errors="ignore")
                    total_chars += len(content)
            except Exception:
                pass
        return max(1, total_chars // 4)

    def compress(
        self,
        tasks: list[Task],
        max_files: int = 20,
    ) -> CompressionResult:
        """Run full context compression on a task batch.

        Args:
            tasks: Tasks to compress context for.
            max_files: Maximum files to select.

        Returns:
            CompressionResult with selected files, metrics, and token estimates.
        """
        all_files: list[str] = sorted(
            fpath.relative_to(self.workdir).as_posix()
            for fpath in _iter_python_files(self.workdir)
        )

        original_tokens = self.estimate_tokens(all_files)

        selected_files, bm25_count, dep_count = self.select_relevant_files(tasks, max_files=max_files)
        compressed_tokens = self.estimate_tokens(selected_files)

        dropped_files = [f for f in all_files if f not in set(selected_files)]
        compression_ratio = max(0.0, min(1.0, compressed_tokens / max(1, original_tokens)))

        metrics = CompressionMetrics(
            bm25_matches=bm25_count,
            dependency_matches=dep_count,
            semantic_matches=0,
            total_files_analyzed=len(all_files),
        )

        return CompressionResult(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            selected_files=selected_files,
            dropped_files=dropped_files,
            metrics=metrics,
        )
