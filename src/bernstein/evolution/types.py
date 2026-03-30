"""Shared types for the evolution system."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(Enum):
    """Risk classification for evolution proposals."""

    L0_CONFIG = "config"  # YAML configs, routing rules, batch sizes
    L1_TEMPLATE = "template"  # Prompts, role definitions, markdown
    L2_LOGIC = "logic"  # Task routing, orchestrator params
    L3_STRUCTURAL = "structural"  # Python code, data models, core logic


class ProposalStatus(Enum):
    """Lifecycle state of an upgrade proposal."""

    PENDING = "pending"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class CircuitState(Enum):
    """Tri-state circuit breaker."""

    CLOSED = "closed"  # Normal operation, evolution allowed
    OPEN = "open"  # Evolution halted, cooling off
    HALF_OPEN = "half_open"  # Testing single low-risk change


@dataclass
class MetricsRecord:
    """14-field standardized metrics per agent run.

    Every agent invocation MUST produce one of these records,
    appended to .sdd/metrics/YYYY-MM-DD.jsonl.
    """

    timestamp: str  # ISO 8601
    task_id: str
    agent_id: str
    role: str
    model_used: str
    duration_seconds: float
    token_count: int  # prompt + completion
    cost_usd: float  # estimated
    success: bool  # janitor pass
    error_type: str | None  # null if success
    files_modified: int
    test_pass_rate: float  # 0.0 - 1.0
    retry_count: int
    step_count: int  # tool invocations
    schema_version: int = 1
    config_id: str = "default"  # tracks which config was active

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSONL output."""
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "model_used": self.model_used,
            "duration_seconds": self.duration_seconds,
            "token_count": self.token_count,
            "cost_usd": self.cost_usd,
            "success": self.success,
            "error_type": self.error_type,
            "files_modified": self.files_modified,
            "test_pass_rate": self.test_pass_rate,
            "retry_count": self.retry_count,
            "step_count": self.step_count,
            "config_id": self.config_id,
        }


@dataclass
class UpgradeProposal:
    """A proposed self-modification with risk assessment."""

    id: str
    title: str
    description: str
    risk_level: RiskLevel
    target_files: list[str]
    diff: str  # unified diff
    rationale: str  # why this change
    expected_impact: str  # predicted improvement
    confidence: float  # 0.0 - 1.0
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    evaluated_at: float | None = None
    applied_at: float | None = None
    sandbox_result: dict[str, Any] | None = None  # metrics from sandbox run
    reviewer: str | None = None  # human reviewer if applicable


@dataclass
class SandboxResult:
    """Result of running a proposal in an isolated sandbox."""

    proposal_id: str
    passed: bool
    tests_passed: int
    tests_failed: int
    tests_total: int
    baseline_score: float
    candidate_score: float
    delta: float  # candidate - baseline
    duration_seconds: float
    log_path: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Evolution error taxonomy
# ---------------------------------------------------------------------------


class EvolutionError(Exception):
    """Base class for evolution loop errors.

    Carries structured context for audit logging.

    Args:
        msg: Human-readable error description.
        proposal_id: ID of the proposal being processed when the error occurred.
        focus_area: Cycle focus area (e.g. "code_quality", "test_coverage").
        risk_level: Risk level string of the proposal (e.g. "config", "template").
    """

    def __init__(
        self,
        msg: str,
        *,
        proposal_id: str | None = None,
        focus_area: str = "",
        risk_level: str = "",
    ) -> None:
        super().__init__(msg)
        self.proposal_id = proposal_id
        self.focus_area = focus_area
        self.risk_level = risk_level


class ProposalGenerationError(EvolutionError):
    """Raised when proposal generation fails in the evolution loop."""


class SandboxValidationError(EvolutionError):
    """Raised when sandbox validation encounters an unexpected error."""


class ApplyError(EvolutionError):
    """Raised when applying a proposal fails unexpectedly."""


class RollbackError(EvolutionError):
    """Raised when rolling back a failed proposal fails."""
