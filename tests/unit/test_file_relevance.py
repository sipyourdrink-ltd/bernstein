"""Tests for file relevance scoring (knowledge/file_relevance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.knowledge.file_relevance import (
    FileRelevanceScore,
    RelevanceResult,
    extract_query_terms,
    get_project_files,
    rank_files,
    score_file_relevance,
)

# ---------------------------------------------------------------------------
# extract_query_terms
# ---------------------------------------------------------------------------


class TestExtractQueryTerms:
    """Tests for query tokenization and normalization."""

    def test_basic_tokenization(self) -> None:
        terms = extract_query_terms("fix the login bug")
        assert "fix" in terms
        assert "login" in terms
        assert "bug" in terms

    def test_stop_words_removed(self) -> None:
        terms = extract_query_terms("fix the bug in the auth module")
        assert "the" not in terms
        assert "in" not in terms

    def test_camel_case_split(self) -> None:
        terms = extract_query_terms("refactor FileRelevanceScore class")
        assert "file" in terms
        assert "relevance" in terms
        assert "score" in terms

    def test_snake_case_tokens(self) -> None:
        terms = extract_query_terms("update score_file_relevance function")
        assert "score_file_relevance" in terms

    def test_deduplication(self) -> None:
        terms = extract_query_terms("fix fix fix bug bug")
        assert terms.count("fix") == 1
        assert terms.count("bug") == 1

    def test_empty_input(self) -> None:
        assert extract_query_terms("") == []

    def test_only_stop_words(self) -> None:
        assert extract_query_terms("the is a an") == []

    def test_preserves_order(self) -> None:
        terms = extract_query_terms("alpha beta gamma")
        assert terms == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# FileRelevanceScore / RelevanceResult dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Tests for frozen dataclasses."""

    def test_file_relevance_score_frozen(self) -> None:
        score = FileRelevanceScore(file_path="src/foo.py", score=0.75, match_reason="keyword=0.50")
        assert score.file_path == "src/foo.py"
        assert score.score == pytest.approx(0.75)
        with pytest.raises(AttributeError):
            score.score = 0.5  # type: ignore[misc]

    def test_relevance_result_frozen(self) -> None:
        result = RelevanceResult(query="test", scored_files=(), top_k=10)
        assert result.query == "test"
        assert result.scored_files == ()
        assert result.top_k == 10
        with pytest.raises(AttributeError):
            result.top_k = 5  # type: ignore[misc]

    def test_relevance_result_tuple_of_scores(self) -> None:
        s1 = FileRelevanceScore(file_path="a.py", score=0.9, match_reason="kw")
        s2 = FileRelevanceScore(file_path="b.py", score=0.5, match_reason="path")
        result = RelevanceResult(query="q", scored_files=(s1, s2), top_k=2)
        assert len(result.scored_files) == 2
        assert result.scored_files[0].score > result.scored_files[1].score


# ---------------------------------------------------------------------------
# score_file_relevance
# ---------------------------------------------------------------------------


class TestScoreFileRelevance:
    """Tests for single-file scoring."""

    def test_relevant_file_scores_above_zero(self, tmp_path: Path) -> None:
        (tmp_path / "auth.py").write_text("def login(user, password):\n    return authenticate(user, password)\n")
        score = score_file_relevance("fix the login authentication", "auth.py", tmp_path)
        assert score.score > 0.0
        assert score.file_path == "auth.py"

    def test_unrelated_file_scores_low(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("This is a readme with general info.\n")
        score = score_file_relevance("fix database migration", "readme.txt", tmp_path)
        assert score.score < 0.1

    def test_path_match_boosts_score(self, tmp_path: Path) -> None:
        """A file whose path matches query terms should score higher."""
        sub = tmp_path / "auth"
        sub.mkdir()
        (sub / "login.py").write_text("x = 1\n")
        (tmp_path / "utils.py").write_text("x = 1\n")

        score_auth = score_file_relevance("fix login auth", "auth/login.py", tmp_path)
        score_utils = score_file_relevance("fix login auth", "utils.py", tmp_path)
        assert score_auth.score > score_utils.score

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        score = score_file_relevance("anything", "nonexistent.py", tmp_path)
        assert score.score == pytest.approx(0.0)
        assert score.match_reason == "unreadable"

    def test_score_clamped_0_to_1(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("import os\nimport sys\n")
        score = score_file_relevance("test query", "f.py", tmp_path)
        assert 0.0 <= score.score <= 1.0

    def test_import_relevance(self, tmp_path: Path) -> None:
        """A file importing a module mentioned in the query should score on import."""
        (tmp_path / "client.py").write_text("from auth import login\n\ndef run():\n    login()\n")
        score = score_file_relevance("fix auth module", "client.py", tmp_path)
        assert "import" in score.match_reason or score.score > 0.0

    def test_match_reason_non_empty(self, tmp_path: Path) -> None:
        (tmp_path / "hello.py").write_text("print('hello world')\n")
        score = score_file_relevance("hello world", "hello.py", tmp_path)
        assert score.match_reason != ""

    def test_accepts_path_as_string_or_pathlib(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("code = True\n")
        s1 = score_file_relevance("code", "f.py", str(tmp_path))
        s2 = score_file_relevance("code", "f.py", tmp_path)
        assert s1.score == s2.score


# ---------------------------------------------------------------------------
# get_project_files
# ---------------------------------------------------------------------------


class TestGetProjectFiles:
    """Tests for file discovery."""

    def test_finds_python_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / "c.txt").write_text("text\n")
        files = get_project_files(tmp_path, extensions=(".py",))
        assert "a.py" in files
        assert "b.py" in files
        assert "c.txt" not in files

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("x = 1\n")
        files = get_project_files(tmp_path, extensions=(".py",))
        assert all(".hidden" not in f for f in files)

    def test_skips_pycache(self, tmp_path: Path) -> None:
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-312.pyc").write_text("")
        (tmp_path / "real.py").write_text("x = 1\n")
        files = get_project_files(tmp_path, extensions=(".py", ".pyc"))
        assert all("__pycache__" not in f for f in files)

    def test_multiple_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x\n")
        (tmp_path / "b.ts").write_text("y\n")
        (tmp_path / "c.rs").write_text("z\n")
        files = get_project_files(tmp_path, extensions=(".py", ".ts"))
        assert "a.py" in files
        assert "b.ts" in files
        assert "c.rs" not in files

    def test_nested_directories(self, tmp_path: Path) -> None:
        sub = tmp_path / "src" / "core"
        sub.mkdir(parents=True)
        (sub / "main.py").write_text("x = 1\n")
        files = get_project_files(tmp_path, extensions=(".py",))
        assert "src/core/main.py" in files

    def test_empty_dir(self, tmp_path: Path) -> None:
        files = get_project_files(tmp_path, extensions=(".py",))
        assert files == []


# ---------------------------------------------------------------------------
# rank_files
# ---------------------------------------------------------------------------


class TestRankFiles:
    """Tests for batch ranking."""

    def test_returns_relevance_result(self, tmp_path: Path) -> None:
        (tmp_path / "auth.py").write_text("def login(): pass\n")
        (tmp_path / "db.py").write_text("def connect(): pass\n")
        result = rank_files("fix login", ["auth.py", "db.py"], tmp_path, top_k=10)
        assert isinstance(result, RelevanceResult)
        assert result.query == "fix login"
        assert result.top_k == 10

    def test_top_k_limits_output(self, tmp_path: Path) -> None:
        for i in range(20):
            (tmp_path / f"file_{i}.py").write_text(f"value = {i}\n")
        paths = [f"file_{i}.py" for i in range(20)]
        result = rank_files("value", paths, tmp_path, top_k=5)
        assert len(result.scored_files) == 5

    def test_ordering_by_relevance(self, tmp_path: Path) -> None:
        (tmp_path / "auth_login.py").write_text("def login(user):\n    authenticate(user)\n")
        (tmp_path / "unrelated.py").write_text("x = 42\n")
        result = rank_files(
            "fix login authentication",
            ["auth_login.py", "unrelated.py"],
            tmp_path,
        )
        assert len(result.scored_files) >= 1
        # The auth file should rank first.
        assert result.scored_files[0].file_path == "auth_login.py"

    def test_empty_file_list(self, tmp_path: Path) -> None:
        result = rank_files("query", [], tmp_path)
        assert result.scored_files == ()

    def test_scored_files_is_tuple(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        result = rank_files("test", ["a.py"], tmp_path)
        assert isinstance(result.scored_files, tuple)

    def test_scores_descending(self, tmp_path: Path) -> None:
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (tmp_path / name).write_text(f"# {name}\ncode = True\n")
        result = rank_files("code", ["alpha.py", "beta.py", "gamma.py"], tmp_path)
        scores = [s.score for s in result.scored_files]
        assert scores == sorted(scores, reverse=True)
