"""Tests for side_query — btw / side-question protocol for agents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.side_query import (
    SideQuery,
    answer_side_query,
    get_open_queries,
    get_side_answer,
    post_side_query,
    skip_side_query,
)

# --- Fixtures ---


@pytest.fixture()
def store_dir(tmp_path: Path) -> Path:
    """Fresh side query store directory."""
    return tmp_path / "sq"


# --- TestSideQuery ---


class TestSideQuery:
    def test_to_dict_roundtrip(self) -> None:
        q = SideQuery(
            id="abc123",
            agent_id="agent1",
            task_id="task1",
            question="What is the db migration strategy?",
            context="src/db.py line 42",
        )
        d = q.to_dict()
        q2 = SideQuery.from_dict(d)
        assert q2.question == q.question
        assert q2.context == "src/db.py line 42"
        assert q2.status == "open"

    def test_defaults(self) -> None:
        q = SideQuery()
        assert q.id  # auto-generated
        assert q.status == "open"
        assert q.answer == ""
        assert q.answered_at == pytest.approx(0.0)
        assert q.created_at > 0

    def test_id_is_12_hex(self) -> None:
        q = SideQuery()
        assert len(q.id) == 12
        int(q.id, 16)  # must be valid hex


# --- TestPostSideQuery ---


class TestPostSideQuery:
    def test_post_creates_file(self, store_dir: Path) -> None:
        post_side_query(store_dir, "a1", "t1", "Q1")
        f = store_dir / "side_queries.jsonl"
        assert f.exists()
        line = f.read_text().strip()
        data = json.loads(line)
        assert data["question"] == "Q1"

    def test_deduplicates_by_agent_task_question(self, store_dir: Path) -> None:
        q1 = post_side_query(store_dir, "a1", "t1", "Same question")
        q2 = post_side_query(store_dir, "a1", "t1", "Same question")
        assert q1.id == q2.id  # same query returned

    def test_different_questions_not_deduped(self, store_dir: Path) -> None:
        q1 = post_side_query(store_dir, "a1", "t1", "Q-A")
        q2 = post_side_query(store_dir, "a1", "t1", "Q-B")
        assert q1.id != q2.id


# --- TestGetSideAnswer ---


class TestGetSideAnswer:
    def test_returns_none_when_no_store(self, store_dir: Path) -> None:
        assert get_side_answer(store_dir, "missing") is None

    def test_returns_query(self, store_dir: Path) -> None:
        q = post_side_query(store_dir, "a1", "t1", "Hello?")
        result = get_side_answer(store_dir, q.id)
        assert result is not None
        assert result.id == q.id


# --- TestAnswerSideQuery ---


class TestAnswerSideQuery:
    def test_answers_open_query(self, store_dir: Path) -> None:
        q = post_side_query(store_dir, "a1", "t1", "How does caching work?")
        ok = answer_side_query(store_dir, q.id, "It avoids recomputation")
        assert ok is True
        resolved = get_side_answer(store_dir, q.id)
        assert resolved is not None
        assert resolved.status == "answered"
        assert resolved.answered_at > 0

    def test_false_for_missing_query(self, store_dir: Path) -> None:
        assert answer_side_query(store_dir, "nonexistent", "answer") is False

    def test_false_for_already_answered(self, store_dir: Path) -> None:
        q = post_side_query(store_dir, "a1", "t1", "Already done")
        answer_side_query(store_dir, q.id, "done")
        ok = answer_side_query(store_dir, q.id, "again?")
        assert ok is False


# --- TestGetOpenQueries ---


class TestGetOpenQueries:
    def test_returns_only_open(self, store_dir: Path) -> None:
        q1 = post_side_query(store_dir, "a1", "t1", "Q-open")
        q2 = post_side_query(store_dir, "a2", "t2", "Q-answered")
        answer_side_query(store_dir, q2.id, "answer")
        open_qs = get_open_queries(store_dir)
        ids = {q.id for q in open_qs}
        assert q1.id in ids
        assert q2.id not in ids

    def test_empty_when_no_store(self, store_dir: Path) -> None:
        assert get_open_queries(store_dir) == []


# --- TestSkipSideQuery ---


class TestSkipSideQuery:
    def test_skips_open_query(self, store_dir: Path) -> None:
        q = post_side_query(store_dir, "a1", "t1", "Skip this")
        ok = skip_side_query(store_dir, q.id)
        assert ok is True
        resolved = get_side_answer(store_dir, q.id)
        assert resolved is not None
        assert resolved.status == "skipped"
        assert resolved.answer == ""
        assert resolved.answered_at == pytest.approx(0.0)

    def test_false_for_missing_query(self, store_dir: Path) -> None:
        assert skip_side_query(store_dir, "nonexistent") is False

    def test_cannot_skip_already_skipped(self, store_dir: Path) -> None:
        q = post_side_query(store_dir, "a1", "t1", "Skip me")
        skip_side_query(store_dir, q.id)
        ok = skip_side_query(store_dir, q.id)
        assert ok is False
