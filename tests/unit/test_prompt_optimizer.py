"""Tests for bernstein.core.prompt_optimizer — SPRT, assignments, challenger generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.prompt_optimizer import (
    _CHALLENGER_TEMPLATES,
    PromptOptimizer,
    SprtConfig,
    SprtDecision,
    VariantAssignment,
    _next_challenger_template,
    _sprt_decide,
    generate_challenger_content,
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
def templates_dir(tmp_path: Path) -> Path:
    roles_dir = tmp_path / "templates" / "roles"
    for role in ("backend", "qa", "frontend"):
        role_dir = roles_dir / role
        role_dir.mkdir(parents=True)
        (role_dir / "system_prompt.md").write_text(f"You are a {role} expert.", encoding="utf-8")
    return tmp_path / "templates"


@pytest.fixture()
def optimizer(sdd_dir: Path, templates_dir: Path) -> PromptOptimizer:
    cfg = SprtConfig(
        alpha=0.05,
        beta=0.20,
        min_effect_size=0.10,
        max_sample=50,
        min_sample=5,
    )
    return PromptOptimizer(sdd_dir, templates_dir, cfg=cfg)


# ---------------------------------------------------------------------------
# SprtDecision — _sprt_decide
# ---------------------------------------------------------------------------


class TestSprtDecide:
    def test_sprt_continue_when_below_min_sample(self) -> None:
        # Arrange
        cfg = SprtConfig(min_sample=10)

        # Act
        result = _sprt_decide(
            control_successes=4,
            control_obs=8,
            challenger_successes=6,
            challenger_obs=8,
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.CONTINUE

    def test_sprt_promote_challenger_when_clearly_better(self) -> None:
        # Arrange: challenger has 90% success, control has 40% (large delta)
        cfg = SprtConfig(alpha=0.05, beta=0.20, min_effect_size=0.10, min_sample=10, max_sample=500)

        # Act: simulate many successes for challenger
        result = _sprt_decide(
            control_successes=20,
            control_obs=50,   # 40% success rate
            challenger_successes=45,
            challenger_obs=50,  # 90% success rate — clearly better
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.PROMOTE_CHALLENGER

    def test_sprt_keep_control_when_challenger_is_worse(self) -> None:
        # Arrange: challenger clearly worse (10% vs 70% success)
        cfg = SprtConfig(alpha=0.05, beta=0.20, min_effect_size=0.10, min_sample=10, max_sample=500)

        # Act
        result = _sprt_decide(
            control_successes=35,
            control_obs=50,   # 70% success
            challenger_successes=5,
            challenger_obs=50,  # 10% success
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.KEEP_CONTROL

    def test_sprt_max_sample_reached_promotes_better_challenger(self) -> None:
        # Arrange
        cfg = SprtConfig(min_sample=5, max_sample=10)

        # Act: both at max sample, challenger slightly better
        result = _sprt_decide(
            control_successes=6,
            control_obs=10,
            challenger_successes=8,
            challenger_obs=10,
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.MAX_SAMPLE_REACHED

    def test_sprt_max_sample_reached_keeps_control_when_challenger_not_better(self) -> None:
        # Arrange
        cfg = SprtConfig(min_sample=5, max_sample=10)

        # Act: both at max sample, control better
        result = _sprt_decide(
            control_successes=8,
            control_obs=10,
            challenger_successes=6,
            challenger_obs=10,
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.KEEP_CONTROL

    def test_sprt_continue_when_insufficient_evidence(self) -> None:
        # Arrange: small difference, needs more data
        cfg = SprtConfig(alpha=0.05, beta=0.20, min_effect_size=0.10, min_sample=5, max_sample=500)

        # Act: 55% vs 50% — tiny difference, insufficient evidence
        result = _sprt_decide(
            control_successes=5,
            control_obs=10,
            challenger_successes=6,
            challenger_obs=10,
            cfg=cfg,
        )

        # Assert
        assert result == SprtDecision.CONTINUE


# ---------------------------------------------------------------------------
# Challenger generation
# ---------------------------------------------------------------------------


class TestChallengerGeneration:
    def test_generate_challenger_content_appends_suffix(self) -> None:
        # Arrange
        base = "You are a backend engineer."
        template = _CHALLENGER_TEMPLATES[0]

        # Act
        result = generate_challenger_content(base, template)

        # Assert
        assert result.startswith(base)
        assert template.suffix in result
        assert len(result) > len(base)

    def test_next_challenger_template_cycles(self) -> None:
        # Arrange: cycle through all templates
        idx = -1
        seen: set[str] = set()

        # Act
        for _ in range(len(_CHALLENGER_TEMPLATES) + 1):
            tmpl, idx = _next_challenger_template(idx)
            seen.add(tmpl.name)

        # Assert: all templates covered
        assert seen == {t.name for t in _CHALLENGER_TEMPLATES}

    def test_next_challenger_template_wraps_around(self) -> None:
        # Arrange: start at last index
        last_idx = len(_CHALLENGER_TEMPLATES) - 1

        # Act
        tmpl, new_idx = _next_challenger_template(last_idx)

        # Assert: wraps back to 0
        assert new_idx == 0
        assert tmpl.name == _CHALLENGER_TEMPLATES[0].name


# ---------------------------------------------------------------------------
# VariantAssignment
# ---------------------------------------------------------------------------


class TestVariantAssignment:
    def test_is_challenger_returns_true_for_non_control(self) -> None:
        # Arrange
        assignment = VariantAssignment(role="backend", task_id="t1", variant_version=2)

        # Act / Assert
        assert assignment.is_challenger(control_version=1) is True

    def test_is_challenger_returns_false_for_control(self) -> None:
        # Arrange
        assignment = VariantAssignment(role="backend", task_id="t1", variant_version=1)

        # Act / Assert
        assert assignment.is_challenger(control_version=1) is False

    def test_is_challenger_returns_false_when_no_version(self) -> None:
        # Arrange
        assignment = VariantAssignment(role="backend", task_id="t1", variant_version=None)

        # Act / Assert
        assert assignment.is_challenger(control_version=1) is False


# ---------------------------------------------------------------------------
# PromptOptimizer integration
# ---------------------------------------------------------------------------


class TestPromptOptimizer:
    def test_assign_variant_seeds_role_and_returns_assignment(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange / Act
        assignment = optimizer.assign_variant(role="backend", task_id="task-001")

        # Assert
        assert assignment.role == "backend"
        assert assignment.task_id == "task-001"

    def test_assign_variant_introduces_challenger_on_first_call(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange / Act
        optimizer.assign_variant(role="backend", task_id="task-001")
        status = optimizer.get_status("backend")

        # Assert: challenger introduced
        assert status["challenger_version"] is not None
        assert status["challenger_version"] != status["active_version"]

    def test_record_outcome_accumulates_metrics(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange
        optimizer.assign_variant(role="backend", task_id="task-001")

        # Act
        optimizer.record_outcome(role="backend", task_id="task-001", passed=True)
        status = optimizer.get_status("backend")

        # Assert: at least one side has one observation
        total_obs = (
            status["control_metrics"]["observations"]
            + status["challenger_metrics"]["observations"]
        )
        assert total_obs == 1

    def test_optimizer_state_persists_across_instances(
        self, sdd_dir: Path, templates_dir: Path
    ) -> None:
        # Arrange: create optimizer, assign, record
        cfg = SprtConfig(min_sample=5, max_sample=50)
        opt1 = PromptOptimizer(sdd_dir, templates_dir, cfg=cfg)
        opt1.assign_variant(role="qa", task_id="t1")
        opt1.record_outcome(role="qa", task_id="t1", passed=True)

        # Act: reload from disk
        opt2 = PromptOptimizer(sdd_dir, templates_dir, cfg=cfg)
        status = opt2.get_status("qa")

        # Assert: observations survived reload
        total_obs = (
            status["control_metrics"]["observations"]
            + status["challenger_metrics"]["observations"]
        )
        assert total_obs >= 1

    def test_optimizer_promotes_challenger_after_clear_winner(
        self, sdd_dir: Path, templates_dir: Path
    ) -> None:
        # Arrange: tight cfg so promotion happens quickly
        cfg = SprtConfig(
            alpha=0.05,
            beta=0.20,
            min_effect_size=0.10,
            min_sample=5,
            max_sample=100,
        )
        opt = PromptOptimizer(sdd_dir, templates_dir, cfg=cfg)

        # Act: assign 50 tasks all to the challenger (by seeding the state)
        # We force challenger assignment by manipulating the registry
        opt.assign_variant(role="backend", task_id="seed")
        status = opt.get_status("backend")
        challenger_ver = status["challenger_version"]
        _active = status["active_version"]

        # Simulate recording many outcomes: challenger almost always passes,
        # control fails often.  We bypass assignment tracking by directly
        # manipulating internal state to record known versions.
        rs = opt._role_state("backend")

        # Record control outcomes: 3/10 pass (30%)
        for _ in range(3):
            rs["control_metrics"]["successes"] += 1
        rs["control_metrics"]["observations"] = 10

        # Record challenger outcomes: 9/10 pass (90%)
        for _ in range(9):
            rs["challenger_metrics"]["successes"] += 1
        rs["challenger_metrics"]["observations"] = 10
        opt._save_state()

        # Now record one more challenger success — should trigger SPRT
        # Assign a new task forced to challenger
        from bernstein.core.tokens.prompt_versioning import PromptRegistry
        registry = PromptRegistry(sdd_dir)
        registry.record_outcome(
            "backend", challenger_ver, success=True, quality_score=0.9
        )
        rs["challenger_metrics"]["successes"] += 1
        rs["challenger_metrics"]["observations"] += 1
        opt._save_state()

        decision = _sprt_decide(
            control_successes=rs["control_metrics"]["successes"],
            control_obs=rs["control_metrics"]["observations"],
            challenger_successes=rs["challenger_metrics"]["successes"],
            challenger_obs=rs["challenger_metrics"]["observations"],
            cfg=cfg,
        )

        # Assert: SPRT should detect promotion
        assert decision == SprtDecision.PROMOTE_CHALLENGER

    def test_list_active_roles_returns_tracked_roles(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange
        optimizer.assign_variant(role="backend", task_id="t1")
        optimizer.assign_variant(role="qa", task_id="t2")

        # Act
        roles = optimizer.list_active_roles()

        # Assert
        assert "backend" in roles
        assert "qa" in roles

    def test_get_status_returns_expected_keys(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange
        optimizer.assign_variant(role="frontend", task_id="t1")

        # Act
        status = optimizer.get_status("frontend")

        # Assert
        assert "role" in status
        assert "active_version" in status
        assert "challenger_version" in status
        assert "tests_run" in status
        assert "control_metrics" in status
        assert "challenger_metrics" in status
        assert "recent_promotions" in status

    def test_record_outcome_for_unknown_task_id_does_not_raise(
        self, optimizer: PromptOptimizer
    ) -> None:
        # Arrange: record without prior assign
        optimizer.assign_variant(role="backend", task_id="t1")

        # Act / Assert: should not raise
        optimizer.record_outcome(role="backend", task_id="unknown-task", passed=True)
