"""Upgrade-category target mapping: declaration must equal execution (issue #3398).

Auto-spawned upgrade tasks declare ``owned_files`` derived from their
proposal's category. These tests pin the properties that make that
declaration trustworthy: the mapping resolves to real, path-shaped targets
for every category; the files the real ``FileUpgradeExecutor`` touches are
exactly the mapping's resolution; and both proposal-to-task construction
paths carry the derivation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.models import RiskAssessment, RollbackPlan

from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.detector import ImprovementOpportunity, UpgradeCategory
from bernstein.evolution.proposals import AnalysisTrigger, ProposalGenerator, UpgradeProposal
from bernstein.evolution.upgrade_targets import (
    UPGRADE_CATEGORY_TARGETS,
    upgrade_owned_files,
    upgrade_target_paths,
)

ALL_CATEGORIES = list(UpgradeCategory)


def _proposal(category: UpgradeCategory) -> UpgradeProposal:
    return UpgradeProposal(
        id="UPG-0001",
        title="Upgrade something",
        category=category,
        description="desc",
        current_state="state",
        proposed_change="change",
        benefits=["benefit"],
        risk_assessment=RiskAssessment(level="low"),
        rollback_plan=RollbackPlan(steps=["revert"]),
        cost_estimate_usd=0.0,
        expected_improvement="better",
        confidence=0.9,
    )


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_every_category_has_a_nonempty_path_shaped_mapping(category: UpgradeCategory) -> None:
    owned = upgrade_owned_files(category)
    assert owned, f"category {category} has no owned_files mapping"
    for entry in owned:
        assert "/" in entry, f"{entry!r} is not path-shaped"
        assert entry == entry.strip()
        assert not entry.startswith("/"), f"{entry!r} must be workdir-relative"


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_owned_files_match_what_the_applicator_actually_writes(category: UpgradeCategory, tmp_path: Path) -> None:
    """The scope a task declares and the files an upgrade touches are one table.

    Runs the real executor into a temp workdir and compares the files it
    creates - excluding its own bookkeeping under ``upgrades/`` (backups,
    history.jsonl) - against the mapping's resolution for the same category.
    """
    state_dir = tmp_path / ".sdd"
    executor = FileUpgradeExecutor(state_dir)
    bookkeeping_dir = executor.upgrades_dir

    def _files() -> set[Path]:
        return {p for p in tmp_path.rglob("*") if p.is_file() and bookkeeping_dir not in p.parents}

    before = _files()
    assert executor.execute_upgrade(_proposal(category)) is True
    touched = _files() - before

    expected = set(upgrade_target_paths(category, state_dir))
    assert touched == expected

    declared = {tmp_path / rel for rel in upgrade_owned_files(category)}
    assert declared == expected


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_to_task_declares_the_category_target_files(category: UpgradeCategory) -> None:
    task = _proposal(category).to_task()
    assert task.owned_files == upgrade_owned_files(category)
    assert task.owned_files


def test_create_proposal_forwards_opportunity_components() -> None:
    """The opportunity's components must reach the risk assessment, not be dropped."""
    opportunity = ImprovementOpportunity(
        category=UpgradeCategory.MODEL_ROUTING,
        title="Improve routing",
        description="desc",
        expected_improvement="better",
        confidence=0.8,
        risk_level="medium",
        affected_components=["model_routing", "task_verification"],
    )

    proposal = ProposalGenerator().create_proposal(opportunity, AnalysisTrigger.SCHEDULED)

    assert proposal.risk_assessment.level == "medium"
    assert proposal.risk_assessment.affected_components == ["model_routing", "task_verification"]


def test_create_proposal_forwards_components_on_unknown_risk_level() -> None:
    opportunity = ImprovementOpportunity(
        category=UpgradeCategory.POLICY_UPDATE,
        title="Tweak policy",
        description="desc",
        expected_improvement="better",
        confidence=0.8,
        risk_level="weird",  # type: ignore[arg-type]
        affected_components=["policy"],
    )

    proposal = ProposalGenerator().create_proposal(opportunity, AnalysisTrigger.SCHEDULED)

    assert proposal.risk_assessment.affected_components == ["policy"]


def test_component_labels_never_enter_owned_files() -> None:
    """Subsystem labels are risk metadata; owned_files carry only real targets."""
    label_values = {
        component
        for opportunity_components in (
            ["model_routing", "task_verification"],
            ["role_templates"],
            ["policy"],
        )
        for component in opportunity_components
    }
    for category in ALL_CATEGORIES:
        for entry in upgrade_owned_files(category):
            assert entry not in label_values


def test_mapping_covers_every_category_today() -> None:
    """A new UpgradeCategory member must get a target mapping (or its tasks
    will spawn scopeless-with-a-recorded-reason until it does)."""
    assert set(UPGRADE_CATEGORY_TARGETS) == set(UpgradeCategory)
