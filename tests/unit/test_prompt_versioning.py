"""Tests for bernstein.core.prompt_versioning — registry, A/B, metrics, auto-promote."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.prompt_versioning import (
    MIN_OBSERVATIONS_FOR_PROMOTION,
    PromptMeta,
    PromptRegistry,
    VersionMetrics,
    _hash_content,
    seed_prompts_from_templates,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def registry(sdd_dir: Path) -> PromptRegistry:
    return PromptRegistry(sdd_dir)


@pytest.fixture()
def templates_dir(tmp_path: Path) -> Path:
    prompts = tmp_path / "templates" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "plan.md").write_text("Plan prompt v1 content")
    (prompts / "review.md").write_text("Review prompt v1 content")
    return tmp_path / "templates"


# ---------------------------------------------------------------------------
# VersionMetrics
# ---------------------------------------------------------------------------


class TestVersionMetrics:
    def test_initial_state(self) -> None:
        m = VersionMetrics()
        assert m.observations == 0
        assert m.success_rate == pytest.approx(0.0)
        assert m.avg_quality == pytest.approx(0.0)
        assert m.avg_cost == pytest.approx(0.0)

    def test_record_success(self) -> None:
        m = VersionMetrics()
        m.record(success=True, quality_score=0.9, cost_usd=0.05, latency_s=10.0)
        assert m.observations == 1
        assert m.successes == 1
        assert m.success_rate == pytest.approx(1.0)
        assert m.avg_quality == pytest.approx(0.9)
        assert m.avg_cost == pytest.approx(0.05)

    def test_record_mixed(self) -> None:
        m = VersionMetrics()
        m.record(success=True, quality_score=0.8, cost_usd=0.04)
        m.record(success=False, quality_score=0.2, cost_usd=0.06)
        assert m.observations == 2
        assert m.successes == 1
        assert m.success_rate == pytest.approx(0.5)
        assert m.avg_quality == pytest.approx(0.5)
        assert m.avg_cost == pytest.approx(0.05)

    def test_roundtrip(self) -> None:
        m = VersionMetrics(observations=10, successes=8, total_quality_score=7.5, total_cost_usd=0.5)
        d = m.to_dict()
        m2 = VersionMetrics.from_dict(d)
        assert m2.observations == 10
        assert m2.successes == 8
        assert m2.total_quality_score == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# PromptRegistry basics
# ---------------------------------------------------------------------------


class TestPromptRegistry:
    def test_empty_registry(self, registry: PromptRegistry) -> None:
        assert registry.list_prompts() == []

    def test_add_version(self, registry: PromptRegistry) -> None:
        pv = registry.add_version("plan", "Hello {{GOAL}}", description="initial")
        assert pv.name == "plan"
        assert pv.version == 1
        assert pv.content == "Hello {{GOAL}}"
        assert pv.content_hash == _hash_content("Hello {{GOAL}}")

    def test_list_prompts(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "content A")
        registry.add_version("review", "content B")
        assert registry.list_prompts() == ["plan", "review"]

    def test_multiple_versions(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1 content", set_active=True)
        registry.add_version("plan", "v2 content")
        assert registry.list_versions("plan") == [1, 2]
        # v1 should still be active since v2 didn't set_active
        meta = registry.get_meta("plan")
        assert meta is not None
        assert meta.active_version == 1

    def test_get_active_content(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1 content", set_active=True)
        registry.add_version("plan", "v2 content")
        assert registry.get_active_content("plan") == "v1 content"

    def test_get_active_content_missing(self, registry: PromptRegistry) -> None:
        assert registry.get_active_content("nope") is None

    def test_promote_version(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1", set_active=True)
        registry.add_version("plan", "v2")
        assert registry.promote_version("plan", 2)
        assert registry.get_active_content("plan") == "v2"

    def test_promote_nonexistent(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        assert not registry.promote_version("plan", 99)

    def test_get_version(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "my content", author="test", description="desc")
        pv = registry.get_version("plan", 1)
        assert pv is not None
        assert pv.content == "my content"
        assert pv.author == "test"
        assert pv.description == "desc"

    def test_get_version_missing(self, registry: PromptRegistry) -> None:
        assert registry.get_version("plan", 1) is None


# ---------------------------------------------------------------------------
# A/B testing
# ---------------------------------------------------------------------------


class TestABTesting:
    def test_start_ab_test(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.add_version("plan", "v2")
        assert registry.start_ab_test("plan", 1, 2)
        meta = registry.get_meta("plan")
        assert meta is not None
        assert meta.ab_enabled
        assert meta.ab_versions == [1, 2]

    def test_start_ab_missing_version(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        assert not registry.start_ab_test("plan", 1, 99)

    def test_stop_ab_test(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2)
        assert registry.stop_ab_test("plan")
        meta = registry.get_meta("plan")
        assert meta is not None
        assert not meta.ab_enabled

    def test_stop_ab_no_test(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        assert not registry.stop_ab_test("plan")

    def test_select_version_no_ab(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1", set_active=True)
        registry.add_version("plan", "v2")
        assert registry.select_version("plan", task_id="task-1") == 1

    def test_select_version_with_ab_deterministic(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1", set_active=True)
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2, traffic_split=0.5)

        # Same task_id should always get the same version
        v_first = registry.select_version("plan", task_id="task-abc")
        for _ in range(10):
            assert registry.select_version("plan", task_id="task-abc") == v_first

    def test_select_version_with_ab_split(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1", set_active=True)
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2, traffic_split=0.5)

        # With many task IDs, both versions should appear
        versions = {registry.select_version("plan", task_id=f"task-{i}") for i in range(100)}
        assert versions == {1, 2}

    def test_select_version_missing_prompt(self, registry: PromptRegistry) -> None:
        assert registry.select_version("nonexistent") is None


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    def test_record_outcome(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.record_outcome("plan", 1, success=True, quality_score=0.9, cost_usd=0.05)
        registry.record_outcome("plan", 1, success=False, quality_score=0.3, cost_usd=0.04)

        meta = registry.get_meta("plan")
        assert meta is not None
        m = VersionMetrics.from_dict(meta.versions[1].get("metrics", {}))
        assert m.observations == 2
        assert m.successes == 1
        assert m.success_rate == pytest.approx(0.5)

    def test_record_outcome_missing(self, registry: PromptRegistry) -> None:
        # Should not raise
        registry.record_outcome("nope", 1, success=True)

    def test_compare_versions(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.add_version("plan", "v2")

        for _ in range(5):
            registry.record_outcome("plan", 1, success=True, quality_score=0.8)
        for _ in range(5):
            registry.record_outcome("plan", 2, success=True, quality_score=0.9)
        registry.record_outcome("plan", 2, success=False, quality_score=0.2)

        result = registry.compare_versions("plan", 1, 2)
        assert result is not None
        assert result["v1"]["observations"] == 5
        assert result["v2"]["observations"] == 6
        assert result["v1"]["success_rate"] == pytest.approx(1.0)

    def test_compare_versions_missing(self, registry: PromptRegistry) -> None:
        assert registry.compare_versions("nope", 1, 2) is None
        registry.add_version("plan", "v1")
        assert registry.compare_versions("plan", 1, 99) is None


# ---------------------------------------------------------------------------
# Auto-promotion
# ---------------------------------------------------------------------------


class TestAutoPromote:
    def test_no_promotion_without_enough_observations(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2)

        # Only a few observations
        for _ in range(5):
            registry.record_outcome("plan", 1, success=True)
            registry.record_outcome("plan", 2, success=True)

        assert registry.check_auto_promote("plan") is None

    def test_promotion_with_clear_winner(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1", set_active=True)
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2)

        # v2 is much better
        for _ in range(MIN_OBSERVATIONS_FOR_PROMOTION):
            registry.record_outcome("plan", 1, success=True)
        for _ in range(MIN_OBSERVATIONS_FOR_PROMOTION):
            registry.record_outcome("plan", 2, success=True)
        # Make v1 worse
        for _ in range(5):
            registry.record_outcome("plan", 1, success=False)

        winner = registry.check_auto_promote("plan")
        assert winner == 2
        # A/B test should be stopped
        meta = registry.get_meta("plan")
        assert meta is not None
        assert not meta.ab_enabled
        assert meta.active_version == 2

    def test_no_promotion_without_ab(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        assert registry.check_auto_promote("plan") is None

    def test_no_promotion_when_tied(self, registry: PromptRegistry) -> None:
        registry.add_version("plan", "v1")
        registry.add_version("plan", "v2")
        registry.start_ab_test("plan", 1, 2)

        # Both have identical metrics
        for _ in range(MIN_OBSERVATIONS_FOR_PROMOTION):
            registry.record_outcome("plan", 1, success=True)
            registry.record_outcome("plan", 2, success=True)

        assert registry.check_auto_promote("plan") is None


# ---------------------------------------------------------------------------
# Seed from templates
# ---------------------------------------------------------------------------


class TestSeedFromTemplates:
    def test_seed(self, sdd_dir: Path, templates_dir: Path) -> None:
        count = seed_prompts_from_templates(sdd_dir, templates_dir)
        assert count == 2  # plan.md and review.md

        registry = PromptRegistry(sdd_dir)
        assert "plan" in registry.list_prompts()
        assert "review" in registry.list_prompts()

        pv = registry.get_version("plan", 1)
        assert pv is not None
        assert pv.content == "Plan prompt v1 content"

    def test_seed_idempotent(self, sdd_dir: Path, templates_dir: Path) -> None:
        seed_prompts_from_templates(sdd_dir, templates_dir)
        count2 = seed_prompts_from_templates(sdd_dir, templates_dir)
        assert count2 == 0  # already seeded

    def test_seed_no_templates(self, sdd_dir: Path, tmp_path: Path) -> None:
        empty_templates = tmp_path / "empty_templates"
        empty_templates.mkdir()
        count = seed_prompts_from_templates(sdd_dir, empty_templates)
        assert count == 0


# ---------------------------------------------------------------------------
# PromptMeta serialization
# ---------------------------------------------------------------------------


class TestPromptMeta:
    def test_roundtrip(self) -> None:
        meta = PromptMeta(
            name="plan",
            active_version=2,
            ab_enabled=True,
            ab_versions=[1, 2],
            ab_traffic_split=0.3,
        )
        d = meta.to_dict()
        meta2 = PromptMeta.from_dict(d)
        assert meta2.name == "plan"
        assert meta2.active_version == 2
        assert meta2.ab_enabled
        assert meta2.ab_versions == [1, 2]
        assert meta2.ab_traffic_split == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Integration with manager_prompts
# ---------------------------------------------------------------------------


class TestManagerPromptsIntegration:
    def test_load_template_with_versioned_prompt(self, sdd_dir: Path, templates_dir: Path) -> None:
        """Versioned prompt should be preferred over static template."""
        from bernstein.core.manager_prompts import _load_template

        # Seed v1 from templates
        seed_prompts_from_templates(sdd_dir, templates_dir)

        # Add a v2 and promote it
        registry = PromptRegistry(sdd_dir)
        registry.add_version("plan", "Improved plan prompt v2", set_active=True)

        content = _load_template(templates_dir, "plan.md", sdd_dir=sdd_dir)
        assert content == "Improved plan prompt v2"

    def test_load_template_fallback(self, templates_dir: Path, tmp_path: Path) -> None:
        """Without versioned prompts, fall back to static template."""
        from bernstein.core.manager_prompts import _load_template

        empty_sdd = tmp_path / "empty_sdd"
        empty_sdd.mkdir()

        content = _load_template(templates_dir, "plan.md", sdd_dir=empty_sdd)
        assert content == "Plan prompt v1 content"

    def test_load_template_no_sdd(self, templates_dir: Path) -> None:
        """Without sdd_dir, load static template (backward compatible)."""
        from bernstein.core.manager_prompts import _load_template

        content = _load_template(templates_dir, "plan.md")
        assert content == "Plan prompt v1 content"
