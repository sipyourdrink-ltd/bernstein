"""Unit tests for review_rubric quality gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.review_rubric import (
    ReviewRubricConfig,
    RubricHistoryWriter,
    _compute_composite,
    _parse_rubric_response,
    score_diff,
)

# ---------------------------------------------------------------------------
# _compute_composite
# ---------------------------------------------------------------------------


def test_compute_composite_all_tens() -> None:
    scores = {
        "style_compliance": 10,
        "correctness": 10,
        "performance_impact": 10,
        "security": 10,
        "maintainability": 10,
    }
    assert _compute_composite(scores) == 10.0


def test_compute_composite_all_zeros() -> None:
    scores = {
        "style_compliance": 0,
        "correctness": 0,
        "performance_impact": 0,
        "security": 0,
        "maintainability": 0,
    }
    assert _compute_composite(scores) == 0.0


def test_compute_composite_mixed() -> None:
    # correctness has highest weight (0.35) + security (0.25)
    scores = {
        "style_compliance": 0,
        "correctness": 10,
        "performance_impact": 0,
        "security": 10,
        "maintainability": 0,
    }
    composite = _compute_composite(scores)
    # correctness=10*0.35 + security=10*0.25 = 6.0, rest = 0
    assert composite == pytest.approx(6.0, abs=0.1)


def test_compute_composite_empty_scores() -> None:
    assert _compute_composite({}) == 0.0


# ---------------------------------------------------------------------------
# _parse_rubric_response
# ---------------------------------------------------------------------------


def _valid_json(**overrides: object) -> str:
    payload = {
        "style_compliance": 8,
        "correctness": 9,
        "performance_impact": 7,
        "security": 10,
        "maintainability": 6,
        "feedback": {
            "style_compliance": "Clean.",
            "correctness": "Correct.",
            "performance_impact": "No regression.",
            "security": "No issues.",
            "maintainability": "Readable.",
        },
        "summary": "Overall good change.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_rubric_response_valid() -> None:
    scores, feedbacks, summary = _parse_rubric_response(_valid_json())
    assert scores["correctness"] == 9
    assert scores["security"] == 10
    assert feedbacks["correctness"] == "Correct."
    assert summary == "Overall good change."


def test_parse_rubric_response_strips_fences() -> None:
    raw = f"```json\n{_valid_json()}\n```"
    scores, _, _ = _parse_rubric_response(raw)
    assert scores["correctness"] == 9


def test_parse_rubric_response_clamps_out_of_range() -> None:
    scores, _, _ = _parse_rubric_response(_valid_json(correctness=15, security=-3))
    assert scores["correctness"] == 10  # clamped
    assert scores["security"] == 0  # clamped


def test_parse_rubric_response_invalid_json_returns_empty() -> None:
    scores, _feedbacks, _summary = _parse_rubric_response("this is not json at all")
    assert scores == {} or all(v >= 0 for v in scores.values())


def test_parse_rubric_response_partial_json_extraction() -> None:
    # LLM wraps JSON in extra text
    raw = f"Here is my review:\n{_valid_json()}\nThat's it!"
    scores, _, _ = _parse_rubric_response(raw)
    assert scores.get("correctness") == 9


# ---------------------------------------------------------------------------
# score_diff — no changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_diff_no_changes(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "t-rubric-1"
    task.title = "Fix typo"
    task.description = ""

    config = ReviewRubricConfig(enabled=True)

    result = await score_diff(task, tmp_path, config, diff="")

    assert result.passed is True
    assert "No Python changes" in result.detail


# ---------------------------------------------------------------------------
# score_diff — LLM failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_diff_llm_failure(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "t-rubric-2"
    task.title = "Add feature"
    task.description = ""

    config = ReviewRubricConfig(enabled=True, block_below_threshold=True)

    with patch("bernstein.core.llm.call_llm", side_effect=RuntimeError("timeout")):
        result = await score_diff(task, tmp_path, config, diff="--- a/x.py\n+++ b/x.py\n@@ def f(): pass")

    assert result.passed is False
    assert result.blocked is True
    assert "LLM error" in result.detail


# ---------------------------------------------------------------------------
# score_diff — passing score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_diff_passing(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "t-rubric-3"
    task.title = "Refactor"
    task.description = ""

    config = ReviewRubricConfig(enabled=True, composite_threshold=6.0)

    with patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value=_valid_json()):
        result = await score_diff(task, tmp_path, config, diff="--- a/x.py\n+++ b/x.py\n@@ def f(): pass")

    assert result.passed is True
    assert result.composite >= 6.0
    assert len(result.dimensions) == 5


# ---------------------------------------------------------------------------
# score_diff — failing composite triggers block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_diff_below_threshold_blocks(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "t-rubric-4"
    task.title = "Risky change"
    task.description = ""

    config = ReviewRubricConfig(
        enabled=True,
        composite_threshold=8.0,
        block_below_threshold=True,
        rework_threshold=4.0,
    )
    # All scores are 3 → composite ≈ 3.0
    low_json = _valid_json(
        style_compliance=3,
        correctness=3,
        performance_impact=3,
        security=3,
        maintainability=3,
    )

    with patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value=low_json):
        result = await score_diff(task, tmp_path, config, diff="--- a/x.py\n+++ b/x.py\n@@ code")

    assert result.passed is False
    assert result.blocked is True
    assert result.composite < 8.0


# ---------------------------------------------------------------------------
# RubricHistoryWriter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rubric_history_writer_records(tmp_path: Path) -> None:
    task = MagicMock()
    task.id = "t-history-1"
    task.title = "Test"
    task.description = ""

    config = ReviewRubricConfig(enabled=True)

    with patch("bernstein.core.llm.call_llm", new_callable=AsyncMock, return_value=_valid_json()):
        result = await score_diff(task, tmp_path, config, diff="--- a/x.py")

    writer = RubricHistoryWriter(tmp_path)
    writer.record("t-history-1", result)

    history_path = tmp_path / ".sdd" / "metrics" / "review_rubric.jsonl"
    assert history_path.exists()
    payload = json.loads(history_path.read_text(encoding="utf-8").strip())
    assert payload["task_id"] == "t-history-1"
    assert "composite" in payload
