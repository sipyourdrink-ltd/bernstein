"""ApprovalGate — risk-stratified routing for evolution proposals.

Routes proposals through L0/L1/L2/L3 risk levels:
  L0 (Config)     — auto-apply after schema check
  L1 (Templates)  — sandbox A/B test, auto-apply if metrics improve
  L2 (Logic)      — git worktree + tests + PR + human review
  L3 (Structural) — NEVER auto-apply, human only

Confidence thresholds for L0/L1:
  >=95% on reversible changes: auto-approve
  85-95%: auto-approve with async audit
  70-85%: human review within 4h
  <70%: immediate human review
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from bernstein.evolution.types import RiskLevel, UpgradeProposal

logger = logging.getLogger(__name__)

# Confidence thresholds for routing decisions
CONFIDENCE_AUTO = 0.95        # >= this → auto-approve
CONFIDENCE_AUTO_AUDIT = 0.85  # >= this → auto-approve with async audit
CONFIDENCE_HUMAN_4H = 0.70    # >= this → human review within 4h
                               # <  0.70  → immediate human review

# Minimum confidence thresholds per risk level for auto-approval (legacy)
AUTO_APPROVE_THRESHOLDS: dict[RiskLevel, float] = {
    RiskLevel.L0_CONFIG: 0.7,
    RiskLevel.L1_TEMPLATE: 0.85,
    RiskLevel.L2_LOGIC: 1.1,       # Effectively never auto-approved
    RiskLevel.L3_STRUCTURAL: 1.1,  # Never auto-approved
}

# File pattern sets for RiskClassifier
_L3_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyc", ".pyd"})
_L3_ROOT_FILES: frozenset[str] = frozenset({"pyproject.toml", "setup.cfg", "setup.py"})
_CONFIG_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml", ".json", ".toml"})
_LOGIC_KEYWORDS: frozenset[str] = frozenset({"routing", "orchestrator", "logic", "policy"})

# Numeric ordering so we can find the highest risk level
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.L0_CONFIG: 0,
    RiskLevel.L1_TEMPLATE: 1,
    RiskLevel.L2_LOGIC: 2,
    RiskLevel.L3_STRUCTURAL: 3,
}


class ApprovalOutcome(Enum):
    """Possible outcomes from the ApprovalGate."""

    AUTO_APPROVED = "auto_approved"
    AUTO_APPROVED_AUDIT = "auto_approved_audit"
    HUMAN_REVIEW_4H = "human_review_4h"
    HUMAN_REVIEW_IMMEDIATE = "human_review_immediate"
    BLOCKED = "blocked"


@dataclass
class ApprovalDecision:
    """Result of routing a proposal through the ApprovalGate.

    Attributes:
        proposal_id: ID of the evaluated proposal.
        risk_level: Classified risk level.
        confidence: Proposal confidence score (0-1).
        outcome: Routing decision.
        reason: Human-readable explanation.
        requires_human: True when a human must act before the change is applied.
        decided_at: Unix timestamp of this decision.
        reviewer: Who approved it (set on manual approval).
    """

    proposal_id: str
    risk_level: RiskLevel
    confidence: float
    outcome: ApprovalOutcome
    reason: str
    requires_human: bool
    decided_at: float = field(default_factory=time.time)
    reviewer: str | None = None

    def to_dict(self) -> dict:
        """Serialise for JSONL logging."""
        return {
            "proposal_id": self.proposal_id,
            "risk_level": self.risk_level.value,
            "confidence": self.confidence,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "requires_human": self.requires_human,
            "decided_at": self.decided_at,
            "reviewer": self.reviewer,
        }


class RiskClassifier:
    """Classify the risk level of a proposal based on its target files.

    Classification rules (applied per-file; the highest wins):
    - ``.py`` / ``pyproject.toml`` / build configs → L3_STRUCTURAL
    - Files under ``templates/`` → L1_TEMPLATE
    - Config files under ``.sdd/`` with routing/logic keywords → L2_LOGIC
    - Config files under ``.sdd/`` (other YAML/JSON) → L0_CONFIG
    - Everything else → L2_LOGIC (conservative default)
    """

    def classify(self, target_files: list[str]) -> RiskLevel:
        """Return the highest risk level among all target files.

        Args:
            target_files: File paths targeted by the proposal.

        Returns:
            The highest RiskLevel for the given files.
        """
        if not target_files:
            return RiskLevel.L2_LOGIC  # Unknown targets → conservative

        highest = RiskLevel.L0_CONFIG
        for file_path in target_files:
            level = self._classify_file(file_path)
            if _RISK_ORDER[level] > _RISK_ORDER[highest]:
                highest = level
                if highest == RiskLevel.L3_STRUCTURAL:
                    break  # Maximum reached
        return highest

    def _classify_file(self, file_path: str) -> RiskLevel:
        p = Path(file_path)
        parts = set(p.parts)

        # L3: Python source or known build-system files
        if p.suffix in _L3_EXTENSIONS or p.name in _L3_ROOT_FILES:
            return RiskLevel.L3_STRUCTURAL

        # L1: Anything inside the templates/ tree
        if "templates" in parts:
            return RiskLevel.L1_TEMPLATE

        # L0 / L2: Config files inside .sdd/
        if ".sdd" in parts and p.suffix in _CONFIG_EXTENSIONS:
            if any(kw in p.name for kw in _LOGIC_KEYWORDS):
                return RiskLevel.L2_LOGIC
            return RiskLevel.L0_CONFIG

        # Default: treat as logic-level (conservative)
        return RiskLevel.L2_LOGIC


class ApprovalGate:
    """Routes proposals to the appropriate approval path based on risk level.

    Decision matrix
    ---------------
    Risk \\ Confidence  | >=95%                | 85-95%               | 70-85%          | <70%
    L0_CONFIG          | AUTO_APPROVED        | AUTO_APPROVED_AUDIT  | HUMAN_REVIEW_4H | HUMAN_REVIEW_IMMEDIATE
    L1_TEMPLATE        | AUTO_APPROVED        | AUTO_APPROVED_AUDIT  | HUMAN_REVIEW_4H | HUMAN_REVIEW_IMMEDIATE
    L2_LOGIC           | HUMAN_REVIEW_4H      | HUMAN_REVIEW_4H      | HUMAN_REVIEW_4H | HUMAN_REVIEW_IMMEDIATE
    L3_STRUCTURAL      | BLOCKED              | BLOCKED              | BLOCKED         | BLOCKED

    Target: 10-15% escalation rate to humans.

    All decisions are logged to ``<decisions_dir>/decisions.jsonl``.

    Args:
        thresholds: Override default auto-approval confidence thresholds.
        decisions_dir: Path to directory for decision log (e.g. .sdd/evolution/).
    """

    def __init__(
        self,
        thresholds: dict[RiskLevel, float] | None = None,
        decisions_dir: Path | None = None,
    ) -> None:
        self.thresholds = thresholds or dict(AUTO_APPROVE_THRESHOLDS)
        self._decisions_dir = decisions_dir
        self._classifier = RiskClassifier()
        if self._decisions_dir is not None:
            self._decisions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _decisions_path(self) -> Path | None:
        if self._decisions_dir is None:
            return None
        return self._decisions_dir / "decisions.jsonl"

    # ------------------------------------------------------------------
    # Core evaluation (legacy simple interface)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        risk_level: RiskLevel,
        confidence: float,
        sandbox_passed: bool | None = None,
    ) -> Literal["auto_approve", "sandbox_required", "human_review"]:
        """Determine the approval path for a proposal.

        Args:
            risk_level: Risk classification of the proposal.
            confidence: Confidence score (0.0-1.0) from the proposal generator.
            sandbox_passed: Whether sandbox testing passed (None if not run).

        Returns:
            The approval path to follow.
        """
        if risk_level == RiskLevel.L3_STRUCTURAL:
            logger.info("L3 proposal → human review (always)")
            return "human_review"

        if risk_level == RiskLevel.L2_LOGIC:
            logger.info("L2 proposal → human review (requires PR)")
            return "human_review"

        threshold = self.thresholds.get(risk_level, 1.0)

        if risk_level == RiskLevel.L1_TEMPLATE:
            if sandbox_passed is None:
                logger.info("L1 proposal → sandbox required")
                return "sandbox_required"
            if sandbox_passed and confidence >= threshold:
                logger.info(
                    "L1 proposal → auto-approve (sandbox passed, confidence %.2f)", confidence
                )
                return "auto_approve"
            logger.info(
                "L1 proposal → human review (sandbox=%s, confidence=%.2f)",
                sandbox_passed,
                confidence,
            )
            return "human_review"

        # L0 config
        if confidence >= threshold:
            logger.info(
                "L0 proposal → auto-approve (confidence %.2f >= %.2f)", confidence, threshold
            )
            return "auto_approve"

        logger.info(
            "L0 proposal → human review (confidence %.2f < %.2f)", confidence, threshold
        )
        return "human_review"

    # ------------------------------------------------------------------
    # Full risk-stratified routing with decision logging
    # ------------------------------------------------------------------

    def route(self, proposal: UpgradeProposal) -> ApprovalDecision:
        """Evaluate a full proposal and return a logged routing decision.

        Uses RiskClassifier to infer the risk level from target_files, then
        applies the confidence-threshold matrix. Decision is appended to
        decisions.jsonl if a decisions_dir was configured.

        Args:
            proposal: The upgrade proposal to evaluate.

        Returns:
            ApprovalDecision describing the routing outcome.
        """
        risk_level = self._classifier.classify(proposal.target_files)
        outcome, reason, requires_human = self._decide(risk_level, proposal.confidence)

        decision = ApprovalDecision(
            proposal_id=proposal.id,
            risk_level=risk_level,
            confidence=proposal.confidence,
            outcome=outcome,
            reason=reason,
            requires_human=requires_human,
        )
        self._log_decision(decision)
        logger.info(
            "Proposal %s: risk=%s confidence=%.2f outcome=%s",
            proposal.id,
            risk_level.value,
            proposal.confidence,
            outcome.value,
        )
        return decision

    def approve(self, proposal_id: str, reviewer: str = "human") -> ApprovalDecision | None:
        """Manually approve a pending proposal.

        Finds the latest decision for *proposal_id* that still requires
        human action and records an approval.

        Args:
            proposal_id: ID of the proposal to approve.
            reviewer: Name/identifier of the approver.

        Returns:
            Updated ApprovalDecision, or None if no matching decision found.
        """
        decisions = self._load_decisions()
        pending = [d for d in decisions if d.proposal_id == proposal_id and d.requires_human]
        if not pending:
            return None

        latest = max(pending, key=lambda d: d.decided_at)
        latest.outcome = ApprovalOutcome.AUTO_APPROVED
        latest.reviewer = reviewer
        latest.requires_human = False
        latest.decided_at = time.time()
        latest.reason = f"Manually approved by {reviewer}"

        self._log_decision(latest)
        return latest

    def get_pending_decisions(self) -> list[ApprovalDecision]:
        """Return decisions that still require human action.

        Deduplicates by proposal_id, keeping only the most recent decision.
        A subsequent non-requiring decision (e.g. manual approval) clears
        pending status for that proposal.

        Returns:
            List of pending ApprovalDecision objects.
        """
        all_decisions = self._load_decisions()
        seen: dict[str, ApprovalDecision] = {}
        for d in all_decisions:
            if not d.requires_human:
                # Approval clears pending status
                seen.pop(d.proposal_id, None)
                continue
            if d.outcome in (
                ApprovalOutcome.HUMAN_REVIEW_4H,
                ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
                ApprovalOutcome.BLOCKED,
            ):
                prev = seen.get(d.proposal_id)
                if prev is None or d.decided_at > prev.decided_at:
                    seen[d.proposal_id] = d
        return list(seen.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _decide(
        self,
        risk_level: RiskLevel,
        confidence: float,
    ) -> tuple[ApprovalOutcome, str, bool]:
        """Return (outcome, reason, requires_human) for the given inputs."""
        # L3 is always blocked
        if risk_level == RiskLevel.L3_STRUCTURAL:
            return (
                ApprovalOutcome.BLOCKED,
                "L3_STRUCTURAL changes require human-only review and cannot be auto-applied",
                True,
            )

        # L2 always escalates to human review
        if risk_level == RiskLevel.L2_LOGIC:
            if confidence >= CONFIDENCE_HUMAN_4H:
                return (
                    ApprovalOutcome.HUMAN_REVIEW_4H,
                    f"L2_LOGIC change (confidence={confidence:.2f}) — human review within 4h",
                    True,
                )
            return (
                ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
                f"L2_LOGIC change with low confidence ({confidence:.2f}) — immediate human review",
                True,
            )

        # L0 / L1: confidence-based routing
        if confidence >= CONFIDENCE_AUTO:
            return (
                ApprovalOutcome.AUTO_APPROVED,
                f"High confidence ({confidence:.2f}) reversible {risk_level.value} change — auto-approved",
                False,
            )

        if confidence >= CONFIDENCE_AUTO_AUDIT:
            return (
                ApprovalOutcome.AUTO_APPROVED_AUDIT,
                f"Confidence {confidence:.2f} — auto-approved with async audit",
                False,
            )

        if confidence >= CONFIDENCE_HUMAN_4H:
            return (
                ApprovalOutcome.HUMAN_REVIEW_4H,
                f"Moderate confidence ({confidence:.2f}) — human review within 4h",
                True,
            )

        return (
            ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
            f"Low confidence ({confidence:.2f}) — immediate human review required",
            True,
        )

    def _log_decision(self, decision: ApprovalDecision) -> None:
        """Append a decision record to decisions.jsonl."""
        if self._decisions_path is None:
            return
        with self._decisions_path.open("a") as f:
            f.write(json.dumps(decision.to_dict()) + "\n")

    def _load_decisions(self) -> list[ApprovalDecision]:
        """Load all decision records from the log file."""
        if self._decisions_path is None or not self._decisions_path.exists():
            return []

        decisions: list[ApprovalDecision] = []
        for line in self._decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                decisions.append(ApprovalDecision(
                    proposal_id=data["proposal_id"],
                    risk_level=RiskLevel(data["risk_level"]),
                    confidence=data["confidence"],
                    outcome=ApprovalOutcome(data["outcome"]),
                    reason=data["reason"],
                    requires_human=data["requires_human"],
                    decided_at=data.get("decided_at", 0.0),
                    reviewer=data.get("reviewer"),
                ))
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed decision record: %s", exc)

        return decisions
