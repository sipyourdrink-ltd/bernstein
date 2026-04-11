"""Tests for embedding-based file relevance scoring."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Create a minimal project for embedding scorer tests."""
    src = tmp_path / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "auth.py").write_text(
        "class AuthService:\n    def login(self, username: str, password: str) -> bool:\n        return True\n"
    )
    (src / "models.py").write_text("class User:\n    name: str\n    email: str\n\nclass Order:\n    total: float\n")
    (src / "api.py").write_text("from myapp.auth import AuthService\n\ndef handle_login():\n    auth = AuthService()\n")
    (src / "utils.py").write_text("def format_date(d):\n    return str(d)\n\ndef parse_csv(data):\n    pass\n")
    return tmp_path


@pytest.fixture()
def task() -> MagicMock:
    t = MagicMock()
    t.title = "Fix authentication login flow"
    t.description = "The login endpoint returns 500 when password is empty"
    t.owned_files = ["src/myapp/auth.py"]
    return t


class TestTfIdfBackend:
    def test_encode_returns_vectors(self) -> None:
        from bernstein.core.embedding_scorer import TfIdfBackend

        backend = TfIdfBackend()
        docs = ["hello world", "testing python code"]
        backend.fit(docs)
        vecs = backend.encode(docs)
        assert len(vecs) == 2
        assert len(vecs[0]) == len(vecs[1])

    def test_similarity_self_is_one(self) -> None:
        from bernstein.core.embedding_scorer import TfIdfBackend

        backend = TfIdfBackend()
        docs = ["authentication login user"]
        backend.fit(docs)
        vec = backend.encode(docs)[0]
        sim = backend.similarity(vec, vec)
        assert sim == pytest.approx(1.0, abs=0.01)

    def test_similarity_different_docs(self) -> None:
        from bernstein.core.embedding_scorer import TfIdfBackend

        backend = TfIdfBackend()
        docs = ["authentication login user", "database migration sql"]
        backend.fit(docs)
        vecs = backend.encode(docs)
        sim = backend.similarity(vecs[0], vecs[1])
        assert sim < 0.5

    def test_empty_text_returns_zero_vector(self) -> None:
        from bernstein.core.embedding_scorer import TfIdfBackend

        backend = TfIdfBackend()
        backend.fit(["some text here"])
        vecs = backend.encode([""])
        assert all(v == pytest.approx(0.0) for v in vecs[0])

    def test_similarity_zero_norm(self) -> None:
        from bernstein.core.embedding_scorer import TfIdfBackend

        backend = TfIdfBackend()
        assert backend.similarity([0.0, 0.0], [1.0, 2.0]) == pytest.approx(0.0)


class TestEmbeddingScorer:
    def test_index_counts_files(self, project: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=project)
        count = scorer.index()
        assert count >= 4  # auth.py, models.py, api.py, utils.py

    def test_score_returns_ranked_results(self, project: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=project)
        scorer.index()
        results = scorer.score("authentication login", top_k=3)
        assert len(results) > 0
        assert results[0].score >= results[-1].score

    def test_auth_query_ranks_auth_file_high(self, project: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=project)
        scorer.index()
        results = scorer.score("authentication login password", top_k=5)
        paths = [r.path for r in results]
        assert "src/myapp/auth.py" in paths

    def test_score_for_tasks_boosts_owned(self, project: Path, task: MagicMock) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=project)
        scorer.index()
        results = scorer.score_for_tasks([task], top_k=5)
        owned_result = next((r for r in results if r.path == "src/myapp/auth.py"), None)
        assert owned_result is not None
        assert owned_result.score > 0.0

    def test_top_k_limits_results(self, project: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=project)
        scorer.index()
        results = scorer.score("anything", top_k=2)
        assert len(results) <= 2

    def test_empty_project(self, tmp_path: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        scorer = EmbeddingScorer(workdir=tmp_path)
        count = scorer.index()
        assert count == 0
        results = scorer.score("test query")
        assert results == []

    def test_skips_large_files(self, tmp_path: Path) -> None:
        from bernstein.core.embedding_scorer import _MAX_FILE_SIZE, EmbeddingScorer

        big = tmp_path / "huge.py"
        big.write_text("x = 1\n" * (_MAX_FILE_SIZE // 5))
        scorer = EmbeddingScorer(workdir=tmp_path)
        scorer.index()
        paths = [sf.path for sf in scorer.score("anything", top_k=100)]
        assert "huge.py" not in paths

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        from bernstein.core.embedding_scorer import EmbeddingScorer

        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("password = '123'")
        (tmp_path / "visible.py").write_text("def hello(): pass")
        scorer = EmbeddingScorer(workdir=tmp_path)
        scorer.index()
        paths = [sf.path for sf in scorer.score("anything", top_k=100)]
        assert ".hidden/secret.py" not in paths


class TestShouldSkipEmbedding:
    def test_git_dir_skipped(self) -> None:
        from bernstein.core.embedding_scorer import _should_skip

        assert _should_skip((".git", "config")) is True

    def test_sdd_skipped(self) -> None:
        from bernstein.core.embedding_scorer import _should_skip

        assert _should_skip((".sdd", "metrics", "data.json")) is True

    def test_normal_path_passes(self) -> None:
        from bernstein.core.embedding_scorer import _should_skip

        assert _should_skip(("src", "myapp", "auth.py")) is False
