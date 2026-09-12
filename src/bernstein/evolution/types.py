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


class EffectDirection(Enum):
    """Direction of a predicted metric effect."""

    INCREASING = "increasing"
    DECREASING = "decreasing"


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


class ReplayVerdict(Enum):
    """Verdict from replaying a proposal's effects against recorded invariants."""

    ACCEPT = "accept"  # Replay matched expected outcome
    INVARIANT_VIOLATED = "invariant_violated"  # Replay broke a recorded invariant
    CHANGED_UNEXPECTEDLY = "changed_unexpectedly"  # Replay produced unexpected diff


@dataclass
class PredictedEffect:
    """Structured metric + direction for a predicted change effect."""

    metric: str
    direction: EffectDirection

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "direction": self.direction.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PredictedEffect:
        return cls(
            metric=d["metric"],
            direction=EffectDirection(d["direction"]),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.metric, str):
            raise TypeError(f"PredictedEffect.metric must be str, got {type(self.metric).__name__}")
        if not isinstance(self.direction, EffectDirection):
            raise TypeError(f"PredictedEffect.direction must be EffectDirection, got {type(self.direction).__name__}")


@dataclass
class ChangeFalsifier:
    """Describes which recorded history a prediction should be checked against."""

    history_ref: str
    expected_verdicts: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_ref": self.history_ref,
            "expected_verdicts": self.expected_verdicts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChangeFalsifier:
        return cls(
            history_ref=d["history_ref"],
            expected_verdicts=d["expected_verdicts"],
        )

    def __post_init__(self) -> None:
        if not isinstance(self.history_ref, str):
            raise TypeError(f"ChangeFalsifier.history_ref must be str, got {type(self.history_ref).__name__}")
        if not isinstance(self.expected_verdicts, list):
            raise TypeError(
                f"ChangeFalsifier.expected_verdicts must be list, got {type(self.expected_verdicts).__name__}"
            )


@dataclass
class ChangeRollback:
    """Describes the inverse change for rollback purposes."""

    files_to_restore: list[str]
    change_description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_to_restore": self.files_to_restore,
            "change_description": self.change_description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChangeRollback:
        return cls(
            files_to_restore=d["files_to_restore"],
            change_description=d["change_description"],
        )

    def __post_init__(self) -> None:
        if not isinstance(self.files_to_restore, list):
            raise TypeError(f"ChangeRollback.files_to_restore must be list, got {type(self.files_to_restore).__name__}")
        if not isinstance(self.change_description, str):
            raise TypeError(
                f"ChangeRollback.change_description must be str, got {type(self.change_description).__name__}"
            )


@dataclass
class ChangeContract:
    """Typed change contract attached to an UpgradeProposal."""

    component: str
    target_fingerprint: str
    predicted_effect: PredictedEffect
    invariants: list[str]
    falsifier: ChangeFalsifier
    rollback: ChangeRollback

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "target_fingerprint": self.target_fingerprint,
            "predicted_effect": self.predicted_effect.to_dict(),
            "invariants": self.invariants,
            "falsifier": self.falsifier.to_dict(),
            "rollback": self.rollback.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChangeContract:
        return cls(
            component=d["component"],
            target_fingerprint=d["target_fingerprint"],
            predicted_effect=PredictedEffect.from_dict(d["predicted_effect"]),
            invariants=d["invariants"],
            falsifier=ChangeFalsifier.from_dict(d["falsifier"]),
            rollback=ChangeRollback.from_dict(d["rollback"]),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.component, str):
            raise TypeError(f"ChangeContract.component must be str, got {type(self.component).__name__}")
        if not isinstance(self.target_fingerprint, str):
            raise TypeError(
                f"ChangeContract.target_fingerprint must be str, got {type(self.target_fingerprint).__name__}"
            )
        if not isinstance(self.predicted_effect, PredictedEffect):
            raise TypeError(
                f"ChangeContract.predicted_effect must be PredictedEffect, got {type(self.predicted_effect).__name__}"
            )
        if not isinstance(self.invariants, list):
            raise TypeError(f"ChangeContract.invariants must be list, got {type(self.invariants).__name__}")
        if not isinstance(self.falsifier, ChangeFalsifier):
            raise TypeError(f"ChangeContract.falsifier must be ChangeFalsifier, got {type(self.falsifier).__name__}")
        if not isinstance(self.rollback, ChangeRollback):
            raise TypeError(f"ChangeContract.rollback must be ChangeRollback, got {type(self.rollback).__name__}")


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
    contract: ChangeContract | None = None  # typed change contract (optional)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "target_files": self.target_files,
            "diff": self.diff,
            "rationale": self.rationale,
            "expected_impact": self.expected_impact,
            "confidence": self.confidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "evaluated_at": self.evaluated_at,
            "applied_at": self.applied_at,
            "sandbox_result": self.sandbox_result,
            "reviewer": self.reviewer,
            "contract": self.contract.to_dict() if self.contract is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UpgradeProposal:
        contract_raw = d.get("contract")
        return cls(
            id=d["id"],
            title=d["title"],
            description=d["description"],
            risk_level=RiskLevel(d["risk_level"]),
            target_files=d["target_files"],
            diff=d["diff"],
            rationale=d["rationale"],
            expected_impact=d["expected_impact"],
            confidence=d["confidence"],
            status=ProposalStatus(d.get("status", "pending")),
            created_at=d.get("created_at", time.time()),
            evaluated_at=d.get("evaluated_at"),
            applied_at=d.get("applied_at"),
            sandbox_result=d.get("sandbox_result"),
            reviewer=d.get("reviewer"),
            contract=ChangeContract.from_dict(contract_raw) if contract_raw is not None else None,
        )


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
    """Base class for evolution loop errors."""

    error_type: str = "evolution_error"


class ProposalGenerationError(EvolutionError):
    """Failure during proposal generation from detected opportunities."""

    error_type: str = "proposal_generation"


class SandboxValidationError(EvolutionError):
    """Failure during sandbox validation of a proposal."""

    error_type: str = "sandbox_validation"


class ApplyError(EvolutionError):
    """Failure when applying an approved proposal to the codebase."""

    error_type: str = "apply"


class RollbackError(EvolutionError):
    """Failure when rolling back a failed proposal application."""

    error_type: str = "rollback"


# ---------------------------------------------------------------------------
# Replay service types — ReplayContract and verdict enums
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictedDecisionChange:
    """A single predicted change within a ReplayContract.

    The contract claims that the subject will undergo the specified action,
    producing the expected_verdict.
    """

    subject: str
    action: str
    expected_verdict: str


@dataclass(frozen=True)
class ContractInvariant:
    """A named property that must hold across replayed governance decisions.

    The predicate_hash is the stable identity of the invariant predicate;
    the name is a human-readable label.  The actual check logic lives in
    a separate registry keyed by predicate_hash.
    """

    name: str
    predicate_hash: str


@dataclass
class ReplayContract:
    """A self-contained specification for the replay service.

    The contract encodes what a proposer claims will happen: which subjects
    will change, how, and what invariants must hold throughout the replay.
    """

    target_fingerprint: str
    predicted_changes: tuple[PredictedDecisionChange, ...]
    invariants: tuple[ContractInvariant, ...]
    min_corpus_size: int = 5


class ReplayVerdict(Enum):
    """Outcome classification for a single replayed run."""

    UNCHANGED = "unchanged"
    CHANGED_AS_PREDICTED = "changed_as_predicted"
    CHANGED_UNEXPECTEDLY = "changed_unexpectedly"
    INVARIANT_VIOLATED = "invariant_violated"
    THIN_CORPUS = "thin_corpus"
    INCONCLUSIVE = "inconclusive"


@dataclass
class RunVerdict:
    """Verdict for one replayed run."""

    run_id: str
    verdict: ReplayVerdict
    changed_subjects: list[str]
    violated_invariants: list[str]
    details: str


@dataclass
class ReplayServiceResult:
    """Top-level result returned by the replay service."""

    verdict: ReplayVerdict
    contract_fingerprint: str
    selected_run_ids: list[str]
    run_verdicts: list[RunVerdict]
    thin_corpus: bool
