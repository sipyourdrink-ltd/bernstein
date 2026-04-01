"""Unit tests for risk-based approval workflow logic."""

from __future__ import annotations

from typing import Any

from bernstein.core.models import OrchestratorConfig, ApprovalWorkflowConfig


def test_orchestrator_config_accepts_workflow_dict() -> None:
    config = OrchestratorConfig(
        approval={
            "low_risk": "auto",
            "medium_risk": "review",
            "high_risk": "pr",
            "critical_risk": "pr",
            "timeout_hours": 12,
            "notify_channels": ["slack"],
        }
    )

    assert config.approval == "workflow"
    assert config.approval_workflow is not None
    assert config.approval_workflow.low_risk == "auto"
    assert config.approval_workflow.high_risk == "pr"
    assert config.approval_workflow.timeout_hours == 12
    assert config.approval_workflow.notify_channels == ["slack"]

def test_orchestrator_config_accepts_string_mode() -> None:
    config = OrchestratorConfig(approval="review")
    assert config.approval == "review"
