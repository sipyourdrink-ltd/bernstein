"""Embedding-based file relevance scoring for smarter context injection.

Computes relevance scores between task descriptions and project files
using local embeddings. Supports pluggable backends:
- "tfidf" (default): scikit-learn TF-IDF vectors, zero external deps
- "gte-small": sentence-transformers gte-small model for semantic matching

The scorer ranks ALL project files against a task query and returns the
top-K most relevant, regardless of what the task explicitly lists. This
helps agents discover relevant code they would otherwise miss.
"""

from __future__ import annotations

import logging
import math
import pathlib
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

Path = pathlib.Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = frozenset(
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
    }
)

_MAX_FILE_SIZE = 512 * 1024  # 512 KB


def _should_skip(rel_parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts)


@dataclass(frozen=True)
class ScoredFile:
    """A file with its relevance score to a query."""

    path: str
    score: float
    method: str  # "tfidf", "embedding", "owned"


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Protocol for pluggable embedding backends."""

    def encode(self, texts: list[str]) -> list[list[float]]: ...

    def similarity(self, query_vec: list[float], doc_vec: list[float]) -> float: ...


class TfIdfBackend:
    """TF-IDF based pseudo-embedding backend. Zero external dependencies beyond stdlib."""

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._vocab: list[str] = []
        self._fitted = False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\b[a-z_][a-z0-9_]{1,}\b", text.lower())

    def fit(self, documents: list[str]) -> None:
        doc_count = len(documents)
        if doc_count == 0:
            return
        df: dict[str, int] = {}
        for doc in documents:
            seen: set[str] = set()
            for token in self._tokenize(doc):
                if token not in seen:
                    df[token] = df.get(token, 0) + 1
                    seen.add(token)
        self._idf = {term: math.log(doc_count / (1 + freq)) for term, freq in df.items()}
        self._vocab = sorted(self._idf.keys())
        self._fitted = True

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not self._fitted:
            self.fit(texts)
        vocab_index = {term: i for i, term in enumerate(self._vocab)}
        dim = len(self._vocab)
        result: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dim
            tokens = self._tokenize(text)
            if not tokens:
                result.append(vec)
                continue
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            for term, count in tf.items():
                idx = vocab_index.get(term)
                if idx is not None:
                    vec[idx] = (count / len(tokens)) * self._idf.get(term, 0.0)
            result.append(vec)
        return result

    def similarity(self, query_vec: list[float], doc_vec: list[float]) -> float:
        dot = sum(a * b for a, b in zip(query_vec, doc_vec, strict=False))
        norm_q = math.sqrt(sum(a * a for a in query_vec))
        norm_d = math.sqrt(sum(b * b for b in doc_vec))
        if norm_q == 0.0 or norm_d == 0.0:
            return 0.0
        return dot / (norm_q * norm_d)


def _try_load_gte_backend() -> EmbeddingBackend | None:
    """Attempt to load sentence-transformers gte-small backend."""
    try:
        import numpy as np  # type: ignore[import-untyped]
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
    except ImportError:
        return None

    class GteBackend:
        def __init__(self) -> None:
            self._model = SentenceTransformer("thenlper/gte-small")  # type: ignore[no-untyped-call]

        def encode(self, texts: list[str]) -> list[list[float]]:
            embeddings = self._model.encode(texts, show_progress_bar=False)  # type: ignore[no-untyped-call]
            return [e.tolist() for e in embeddings]  # type: ignore[union-attr]

        def similarity(self, query_vec: list[float], doc_vec: list[float]) -> float:
            q = np.array(query_vec)
            d = np.array(doc_vec)
            dot_val: float = float(np.dot(q, d))
            nq: float = float(np.linalg.norm(q))
            nd: float = float(np.linalg.norm(d))
            if nq == 0.0 or nd == 0.0:
                return 0.0
            return dot_val / (nq * nd)

    try:
        return GteBackend()
    except Exception:
        logger.debug("Failed to initialize gte-small backend", exc_info=True)
        return None


def _collect_files(workdir: Path) -> dict[str, str]:
    """Collect file paths and their content summaries for scoring."""
    files: dict[str, str] = {}
    suffixes = {".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".md", ".json", ".sh"}
    for fpath in workdir.rglob("*"):
        if not fpath.is_file():
            continue
        try:
            rel = fpath.relative_to(workdir)
        except ValueError:
            continue
        if _should_skip(rel.parts):
            continue
        if fpath.suffix not in suffixes:
            continue
        if fpath.stat().st_size > _MAX_FILE_SIZE:
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            # Use first 500 chars + path as the document representation
            files[rel.as_posix()] = content[:500] + " " + rel.as_posix()
        except OSError:
            pass
    return files


@dataclass
class EmbeddingScorer:
    """Scores project files by semantic relevance to a task description.

    Attributes:
        workdir: Project root directory.
        backend_name: Which embedding backend is active ("tfidf" or "gte-small").
    """

    workdir: Path
    backend_name: str = "tfidf"
    _backend: EmbeddingBackend | None = field(default=None, repr=False)
    _file_contents: dict[str, str] = field(default_factory=dict, repr=False)
    _file_vectors: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _indexed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._backend is not None:
            return
        # Try gte-small first, fall back to tfidf
        gte = _try_load_gte_backend()
        if gte is not None:
            self._backend = gte
            self.backend_name = "gte-small"
        else:
            self._backend = TfIdfBackend()
            self.backend_name = "tfidf"

    def index(self) -> int:
        """Index all project files. Returns count of files indexed."""
        self._file_contents = _collect_files(self.workdir)
        if not self._file_contents:
            self._indexed = True
            return 0

        paths = list(self._file_contents.keys())
        contents = [self._file_contents[p] for p in paths]

        if isinstance(self._backend, TfIdfBackend):
            self._backend.fit(contents)

        vectors = self._backend.encode(contents)  # type: ignore[union-attr]
        self._file_vectors = dict(zip(paths, vectors, strict=False))
        self._indexed = True
        return len(paths)

    def score(self, query: str, top_k: int = 15) -> list[ScoredFile]:
        """Score all indexed files against a query, return top-K.

        Args:
            query: Task description or title to match against.
            top_k: Number of top results to return.

        Returns:
            List of ScoredFile sorted by score descending.
        """
        if not self._indexed:
            self.index()
        if not self._file_vectors or self._backend is None:
            return []

        query_vec = self._backend.encode([query])[0]

        scored: list[tuple[str, float]] = []
        for path, doc_vec in self._file_vectors.items():
            sim = self._backend.similarity(query_vec, doc_vec)
            if sim > 0.0:
                scored.append((path, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [ScoredFile(path=path, score=score, method=self.backend_name) for path, score in scored[:top_k]]

    def score_for_tasks(
        self,
        tasks: list[object],
        top_k: int = 15,
        owned_boost: float = 0.3,
    ) -> list[ScoredFile]:
        """Score files for a batch of tasks, boosting owned files.

        Args:
            tasks: Task objects with .title, .description, .owned_files attributes.
            top_k: Number of top results to return.
            owned_boost: Extra score added to explicitly owned files.

        Returns:
            Deduplicated list of ScoredFile sorted by score descending.
        """
        if not self._indexed:
            self.index()

        # Build combined query from all tasks
        query_parts: list[str] = []
        owned: set[str] = set()
        for task in tasks:
            title = getattr(task, "title", "")
            desc = getattr(task, "description", "")
            query_parts.append(f"{title} {desc}")
            for f in getattr(task, "owned_files", []):
                if f:
                    owned.add(f)

        query = " ".join(query_parts)
        results = self.score(query, top_k=top_k * 2)

        # Merge scores and boost owned files
        score_map: dict[str, float] = {}
        method_map: dict[str, str] = {}
        for sf in results:
            score_map[sf.path] = sf.score
            method_map[sf.path] = sf.method

        for f in owned:
            if f in score_map:
                score_map[f] += owned_boost
            else:
                score_map[f] = 1.0 + owned_boost
            method_map[f] = "owned"

        final = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
        return [
            ScoredFile(path=path, score=score, method=method_map.get(path, self.backend_name))
            for path, score in final[:top_k]
        ]
