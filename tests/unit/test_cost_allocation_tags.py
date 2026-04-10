"""Tests for cost allocation tags (cost-013).

Validates that cost_tags flow through TokenUsage serialisation and
the /costs/by-tag aggregation logic.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

from bernstein.core.cost_tracker import CostTracker, TokenUsage

# ---------------------------------------------------------------------------
# TokenUsage.to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


class TestTokenUsageCostTags:
    def test_default_empty_tags(self) -> None:
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            model="sonnet",
            cost_usd=0.01,
            agent_id="a1",
            task_id="t1",
        )
        assert usage.cost_tags == {}

    def test_tags_in_to_dict(self) -> None:
        tags = {"department": "engineering", "project": "auth-rewrite"}
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            model="sonnet",
            cost_usd=0.01,
            agent_id="a1",
            task_id="t1",
            cost_tags=tags,
        )
        d = usage.to_dict()
        assert d["cost_tags"] == tags

    def test_roundtrip(self) -> None:
        tags = {"team": "platform", "env": "staging"}
        usage = TokenUsage(
            input_tokens=200,
            output_tokens=100,
            model="opus",
            cost_usd=0.05,
            agent_id="a2",
            task_id="t2",
            cost_tags=tags,
        )
        restored = TokenUsage.from_dict(usage.to_dict())
        assert restored.cost_tags == tags

    def test_from_dict_missing_tags(self) -> None:
        """Legacy dicts without cost_tags should default to empty."""
        d: dict[str, Any] = {
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "sonnet",
            "cost_usd": 0.01,
            "agent_id": "a1",
            "task_id": "t1",
        }
        usage = TokenUsage.from_dict(d)
        assert usage.cost_tags == {}

    def test_json_serialisation(self) -> None:
        tags = {"cost_center": "CC-42"}
        usage = TokenUsage(
            input_tokens=10,
            output_tokens=5,
            model="haiku",
            cost_usd=0.001,
            agent_id="a3",
            task_id="t3",
            cost_tags=tags,
        )
        serialised = json.dumps(usage.to_dict())
        restored = TokenUsage.from_dict(json.loads(serialised))
        assert restored.cost_tags == tags


# ---------------------------------------------------------------------------
# CostTracker persistence with cost_tags
# ---------------------------------------------------------------------------


class TestCostTrackerTagPersistence:
    def test_save_and_load_preserves_tags(self, tmp_path: Any) -> None:
        tracker = CostTracker(run_id="run-tags", budget_usd=100.0)
        usage = TokenUsage(
            input_tokens=500,
            output_tokens=200,
            model="sonnet",
            cost_usd=0.02,
            agent_id="a1",
            task_id="t1",
            cost_tags={"department": "engineering"},
        )
        tracker._usages.append(usage)
        tracker._spent_usd += usage.cost_usd

        tracker.save(tmp_path)
        loaded = CostTracker.load(tmp_path, "run-tags")
        assert loaded is not None
        assert len(loaded.usages) == 1
        assert loaded.usages[0].cost_tags == {"department": "engineering"}


# ---------------------------------------------------------------------------
# By-tag aggregation logic (mirrors /costs/by-tag endpoint logic)
# ---------------------------------------------------------------------------


def _aggregate_by_tag(
    usages: list[TokenUsage],
    tag_key: str | None = None,
) -> dict[str, dict[str, float]]:
    """Reusable aggregation matching the endpoint logic."""
    by_tag: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for u in usages:
        for k, v in u.cost_tags.items():
            if tag_key is None or k == tag_key:
                by_tag[k][v] += u.cost_usd
    return {k: dict(vals) for k, vals in by_tag.items()}


class TestByTagAggregation:
    def test_single_tag(self) -> None:
        usages = [
            TokenUsage(
                input_tokens=100,
                output_tokens=50,
                model="sonnet",
                cost_usd=0.10,
                agent_id="a1",
                task_id="t1",
                cost_tags={"department": "engineering"},
            ),
            TokenUsage(
                input_tokens=200,
                output_tokens=100,
                model="opus",
                cost_usd=0.20,
                agent_id="a2",
                task_id="t2",
                cost_tags={"department": "engineering"},
            ),
        ]
        result = _aggregate_by_tag(usages)
        assert result == {"department": {"engineering": pytest.approx(0.30)}}

    def test_multiple_tag_values(self) -> None:
        usages = [
            TokenUsage(
                input_tokens=100,
                output_tokens=50,
                model="sonnet",
                cost_usd=0.10,
                agent_id="a1",
                task_id="t1",
                cost_tags={"department": "engineering"},
            ),
            TokenUsage(
                input_tokens=200,
                output_tokens=100,
                model="opus",
                cost_usd=0.20,
                agent_id="a2",
                task_id="t2",
                cost_tags={"department": "marketing"},
            ),
        ]
        result = _aggregate_by_tag(usages)
        assert result["department"]["engineering"] == pytest.approx(0.10)
        assert result["department"]["marketing"] == pytest.approx(0.20)

    def test_filter_by_tag_key(self) -> None:
        usages = [
            TokenUsage(
                input_tokens=100,
                output_tokens=50,
                model="sonnet",
                cost_usd=0.10,
                agent_id="a1",
                task_id="t1",
                cost_tags={"department": "eng", "project": "auth"},
            ),
        ]
        result = _aggregate_by_tag(usages, tag_key="project")
        assert "project" in result
        assert "department" not in result

    def test_no_tags_produces_empty(self) -> None:
        usages = [
            TokenUsage(
                input_tokens=100,
                output_tokens=50,
                model="sonnet",
                cost_usd=0.10,
                agent_id="a1",
                task_id="t1",
            ),
        ]
        result = _aggregate_by_tag(usages)
        assert result == {}

    def test_multiple_tags_per_usage(self) -> None:
        usages = [
            TokenUsage(
                input_tokens=100,
                output_tokens=50,
                model="sonnet",
                cost_usd=0.10,
                agent_id="a1",
                task_id="t1",
                cost_tags={"department": "eng", "project": "auth"},
            ),
        ]
        result = _aggregate_by_tag(usages)
        assert result["department"] == {"eng": pytest.approx(0.10)}
        assert result["project"] == {"auth": pytest.approx(0.10)}
