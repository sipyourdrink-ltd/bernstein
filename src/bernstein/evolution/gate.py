"""ApprovalGate and EvalGate - risk-stratified routing for evolution proposals.

ApprovalGate routes proposals through L0/L1/L2/L3 risk levels:
  L0 (Config)     - auto-apply after schema check
  L1 (Templates)  - sandbox A/B test, auto-apply if metrics improve
  L2 (Logic)      - git worktree + tests + PR + human review
  L3 (Structural) - NEVER auto-apply, human only

Confidence thresholds for L0/L1:
  >=95% on reversible changes: auto-approve
  85-95%: auto-approve with async audit
  70-85%: human review within 4h
  <70%: immediate human review

EvalGate gates evolution on eval harness scores:
  L0 proposals: skip eval (tests only)
  L1 proposals: smoke eval (~5 tasks)
  L2 proposals: standard eval (~15 tasks)
  Accept if score >= baseline - 0.02
  Reject if score < baseline - 0.02
  Promote baseline if score > baseline + 0.05
"""

from __future__ import annotations

import json
import logging
import time
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.eval.gate_receipt import VerdictReceipt

from bernstein.evolution.oscillation_guard import (
    OscillationGuard,
    OscillationResult,
    OscillationVerdict,
)
from bernstein.evolution.predicted_delta import (
    PatchProposal,
    PatchVerdict,
    PredictedDeltaGate,
    PredictedDeltaResult,
)
from bernstein.evolution.types import RiskLevel, UpgradeProposal

logger = logging.getLogger(__name__)

# Confidence thresholds for routing decisions
CONFIDENCE_AUTO = 0.95  # >= this → auto-approve
CONFIDENCE_AUTO_AUDIT = 0.85  # >= this → auto-approve with async audit
CONFIDENCE_HUMAN_4H = 0.70  # >= this → human review within 4h
# <  0.70  → immediate human review

# Minimum confidence thresholds per risk level for auto-approval (legacy)
AUTO_APPROVE_THRESHOLDS: dict[RiskLevel, float] = {
    RiskLevel.L0_CONFIG: 0.7,
    RiskLevel.L1_TEMPLATE: 0.85,
    RiskLevel.L2_LOGIC: 1.1,  # Effectively never auto-approved
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

    def to_dict(self) -> dict[str, Any]:
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
        self.thresholds = thresholds or AUTO_APPROVE_THRESHOLDS.copy()
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
                logger.info("L1 proposal → auto-approve (sandbox passed, confidence %.2f)", confidence)
                return "auto_approve"
            logger.info(
                "L1 proposal → human review (sandbox=%s, confidence=%.2f)",
                sandbox_passed,
                confidence,
            )
            return "human_review"

        # L0 config
        if confidence >= threshold:
            logger.info("L0 proposal → auto-approve (confidence %.2f >= %.2f)", confidence, threshold)
            return "auto_approve"

        logger.info("L0 proposal → human review (confidence %.2f < %.2f)", confidence, threshold)
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
                    f"L2_LOGIC change (confidence={confidence:.2f}) - human review within 4h",
                    True,
                )
            return (
                ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
                f"L2_LOGIC change with low confidence ({confidence:.2f}) - immediate human review",
                True,
            )

        # L0 / L1: confidence-based routing
        if confidence >= CONFIDENCE_AUTO:
            return (
                ApprovalOutcome.AUTO_APPROVED,
                f"High confidence ({confidence:.2f}) reversible {risk_level.value} change - auto-approved",
                False,
            )

        if confidence >= CONFIDENCE_AUTO_AUDIT:
            return (
                ApprovalOutcome.AUTO_APPROVED_AUDIT,
                f"Confidence {confidence:.2f} - auto-approved with async audit",
                False,
            )

        if confidence >= CONFIDENCE_HUMAN_4H:
            return (
                ApprovalOutcome.HUMAN_REVIEW_4H,
                f"Moderate confidence ({confidence:.2f}) - human review within 4h",
                True,
            )

        return (
            ApprovalOutcome.HUMAN_REVIEW_IMMEDIATE,
            f"Low confidence ({confidence:.2f}) - immediate human review required",
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
                decisions.append(
                    ApprovalDecision(
                        proposal_id=data["proposal_id"],
                        risk_level=RiskLevel(data["risk_level"]),
                        confidence=data["confidence"],
                        outcome=ApprovalOutcome(data["outcome"]),
                        reason=data["reason"],
                        requires_human=data["requires_human"],
                        decided_at=data.get("decided_at", 0.0),
                        reviewer=data.get("reviewer"),
                    )
                )
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed decision record: %s", exc)

        return decisions


# ======================================================================
# EvalGate - eval-harness-based quality gate for evolution proposals
# ======================================================================

# Thresholds for eval gate decisions.
EVAL_REGRESSION_TOLERANCE = 0.02  # Accept if score >= baseline - this
EVAL_PROMOTION_THRESHOLD = 0.05  # Update baseline if score > baseline + this

# Eval tier per risk level.
_EVAL_TIER_FOR_RISK: dict[RiskLevel, str | None] = {
    RiskLevel.L0_CONFIG: None,  # Skip eval for config tweaks
    RiskLevel.L1_TEMPLATE: "smoke",  # Smoke eval for prompt changes
    RiskLevel.L2_LOGIC: "standard",  # Standard eval for routing logic
    RiskLevel.L3_STRUCTURAL: None,  # Blocked anyway - never reaches eval
}


@dataclass
class EvalGateResult:
    """Result of the eval gate evaluation.

    Attributes:
        accepted: Whether the proposal passed the eval gate.
        score: Eval score of the candidate (post-proposal).
        baseline_score: The baseline score it was compared against.
        delta: score - baseline_score.
        promoted: Whether the baseline was updated (score significantly improved).
        reason: Human-readable explanation of the decision.
        tier: Which eval tier was run (None if skipped).
        skipped: True if eval was skipped for this risk level.
    """

    accepted: bool
    score: float
    baseline_score: float
    delta: float
    promoted: bool
    reason: str
    tier: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONL logging."""
        return {
            "accepted": self.accepted,
            "score": self.score,
            "baseline_score": self.baseline_score,
            "delta": round(self.delta, 6),
            "promoted": self.promoted,
            "reason": self.reason,
            "tier": self.tier,
            "skipped": self.skipped,
        }


@dataclass
class EvalGateReceiptResult:
    """Result of the receipt-consuming (statistical) eval gate path (#2520).

    Unlike :class:`EvalGateResult` this decision is not a point-threshold
    compare: it carries the sealed verdict receipt and the deterministic
    promotion projection over the receipt chain. ``accepted`` is ``True`` only
    for a promoting verdict; ``promoted`` reflects whether the baseline ratchet
    advanced, which happens only on a ``significant_improvement`` receipt.

    Attributes:
        verdict: The statistical verdict string.
        accepted: Whether the verdict promotes (improvement or non-inferior).
        promoted: Whether the baseline ratchet advanced on this verdict.
        stage: The candidate's projected promotion stage after this verdict.
        receipt_hash: Content hash of the sealed verdict receipt.
        revocation_receipt_hash: Content hash of the revocation receipt when a
            rollback fired, else ``None``.
        reason: The receipt's machine-readable reason code.
    """

    verdict: str
    accepted: bool
    promoted: bool
    stage: str
    receipt_hash: str
    revocation_receipt_hash: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONL logging."""
        return {
            "verdict": self.verdict,
            "accepted": self.accepted,
            "promoted": self.promoted,
            "stage": self.stage,
            "receipt_hash": self.receipt_hash,
            "revocation_receipt_hash": self.revocation_receipt_hash,
            "reason": self.reason,
        }


class EvalGate:
    """Gates evolution proposals on eval harness scores.

    Two decision paths are available:

    * The receipt-consuming statistical path (:meth:`evaluate_paired`, #2520) is
      the recommended path: it compares two paired result sets, seals a signed
      verdict receipt carrying the significance evidence, and promotes a
      candidate as a deterministic projection of the receipt chain.
    * The legacy point-threshold path (:meth:`evaluate`) compares a single
      candidate score against the baseline with a fixed tolerance. It is kept
      behind the ``legacy_point_threshold`` compatibility flag and is
      **deprecated**: at suite sizes of a handful of tasks its accept/reject is
      frequently noise a re-run can flip, and it records no evidence a later
      regression can be traced to.

    Legacy decision logic:
    - L0 proposals: skip eval entirely (tests-only validation is sufficient)
    - L1 proposals: run smoke eval (~5 tasks, ~$0.50)
    - L2 proposals: run standard eval (~15 tasks, ~$2.00)
    - Accept: score >= baseline - REGRESSION_TOLERANCE (0.02)
    - Reject: score < baseline - REGRESSION_TOLERANCE
    - Promote baseline: score > baseline + PROMOTION_THRESHOLD (0.05)

    Args:
        eval_harness: An EvalHarness instance (or any object with a
            ``run(tier, sandbox_dir)`` method returning an EvalResult).
        state_dir: Path to the .sdd directory for baseline persistence.
        regression_tolerance: Override the default regression tolerance.
        promotion_threshold: Override the default promotion threshold.
        legacy_point_threshold: When ``True`` (the default, for backward
            compatibility) :meth:`evaluate` uses the deprecated point-threshold
            path. New callers should prefer :meth:`evaluate_paired`.
    """

    def __init__(
        self,
        eval_harness: Any,
        state_dir: Path,
        regression_tolerance: float = EVAL_REGRESSION_TOLERANCE,
        promotion_threshold: float = EVAL_PROMOTION_THRESHOLD,
        *,
        legacy_point_threshold: bool = True,
    ) -> None:
        self._harness = eval_harness
        self._state_dir = state_dir
        self._regression_tolerance = regression_tolerance
        self._promotion_threshold = promotion_threshold
        self._legacy_point_threshold = legacy_point_threshold
        self._trajectory_path = state_dir / "metrics" / "eval_trajectory.jsonl"
        self._trajectory_path.parent.mkdir(parents=True, exist_ok=True)

    def evaluate_paired(
        self,
        *,
        baseline_outcomes: Mapping[str, bool],
        candidate_outcomes: Mapping[str, bool],
        hmac_key: bytes,
        timestamp: int,
        candidate_config_id: str = "candidate",
        baseline_config_id: str = "baseline",
        prior_receipts: Sequence[VerdictReceipt] = (),
        audit_chain: AuditChainStore | None = None,
        ratchet_baseline: bool = True,
    ) -> EvalGateReceiptResult:
        """Gate on a signed verdict receipt over two paired result sets (#2520).

        Seals a verdict receipt carrying the significance evidence, projects the
        candidate's promotion stage as a deterministic fold over
        ``prior_receipts`` plus this verdict, ratchets the baseline only on a
        ``significant_improvement`` verdict, and (on a rollback) seals a
        revocation receipt. Strip the receipts and this degrades to the legacy
        threshold compare.

        Args:
            baseline_outcomes: Per-task pass/fail under the incumbent baseline.
            candidate_outcomes: Per-task pass/fail under the candidate; must
                cover the identical task set.
            hmac_key: HMAC key sealing the lineage spine and receipts.
            timestamp: Stable integer timestamp anchored into the receipts.
            candidate_config_id: Identifier of the candidate configuration.
            baseline_config_id: Identifier of the incumbent baseline.
            prior_receipts: The candidate's prior verdict receipts, in order.
            audit_chain: Optional audit chain the receipts are mirrored into.
            ratchet_baseline: Whether to advance the baseline on a
                ``significant_improvement`` verdict.

        Returns:
            An :class:`EvalGateReceiptResult` naming the sealed receipt, the
            projected stage, and any revocation receipt.
        """
        from bernstein.eval.baseline import promote_baseline_from_receipt
        from bernstein.eval.gate_receipt import build_verdict_receipt
        from bernstein.eval.promotion import (
            build_revocation_receipt,
            project,
            steps_from_receipts,
        )
        from bernstein.eval.significance import PROMOTING_VERDICTS

        lineage_root = self._state_dir / "lineage"
        receipt = build_verdict_receipt(
            baseline_outcomes=baseline_outcomes,
            candidate_outcomes=candidate_outcomes,
            candidate_config_id=candidate_config_id,
            baseline_config_id=baseline_config_id,
            workdir=self._state_dir.parent,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            timestamp=timestamp,
            chain=audit_chain,
        )

        steps = steps_from_receipts([*prior_receipts, receipt])
        projection = project(steps)

        revocation_receipt_hash: str | None = None
        for revocation in projection.revocations:
            if revocation.trigger_receipt_hash != receipt.receipt_hash:
                continue
            revocation_receipt = build_revocation_receipt(
                revocation=revocation,
                workdir=self._state_dir.parent,
                lineage_root=lineage_root,
                hmac_key=hmac_key,
                timestamp=timestamp,
                chain=audit_chain,
            )
            revocation_receipt_hash = revocation_receipt.receipt_hash

        promoted = False
        if ratchet_baseline:
            promoted = promote_baseline_from_receipt(self._state_dir, receipt)

        accepted = receipt.verdict in PROMOTING_VERDICTS
        return EvalGateReceiptResult(
            verdict=receipt.verdict.value,
            accepted=accepted,
            promoted=promoted,
            stage=projection.final_stage.value,
            receipt_hash=receipt.receipt_hash,
            revocation_receipt_hash=revocation_receipt_hash,
            reason=receipt.evidence.reason,
        )

    def evaluate(
        self,
        proposal: UpgradeProposal,
        risk_level: RiskLevel,
        sandbox_dir: Path | None = None,
    ) -> EvalGateResult:
        """Evaluate a proposal against the eval baseline (legacy, deprecated).

        This is the point-threshold path (#2520 deprecates it): a single
        candidate score is compared against the baseline with a fixed
        tolerance. At small suite sizes the verdict is frequently noise and
        records no evidence a later regression can be traced to. Prefer
        :meth:`evaluate_paired`, which seals a signed verdict receipt carrying
        the significance evidence.

        Args:
            proposal: The upgrade proposal being evaluated.
            risk_level: Risk classification of the proposal.
            sandbox_dir: If provided, run eval in this sandbox directory.

        Returns:
            EvalGateResult with accept/reject decision.
        """
        if self._legacy_point_threshold:
            warnings.warn(
                "EvalGate.evaluate uses the deprecated point-threshold path; "
                "prefer EvalGate.evaluate_paired for statistically gated promotion (#2520).",
                DeprecationWarning,
                stacklevel=2,
            )
        eval_tier = _EVAL_TIER_FOR_RISK.get(risk_level)

        # L0 and L3 skip eval.
        if eval_tier is None:
            logger.info(
                "Eval gate: skipping eval for %s proposal %s (risk=%s)",
                risk_level.value,
                proposal.id,
                risk_level.value,
            )
            return EvalGateResult(
                accepted=True,
                score=0.0,
                baseline_score=0.0,
                delta=0.0,
                promoted=False,
                reason=f"Eval skipped for {risk_level.value} proposals",
                tier=None,
                skipped=True,
            )

        # Load baseline.
        from bernstein.eval.baseline import load_baseline

        baseline = load_baseline(self._state_dir)
        baseline_score = baseline.score if baseline is not None else 0.0

        # Run eval harness.
        try:
            eval_result = self._harness.run(tier=eval_tier, sandbox_dir=sandbox_dir)
        except Exception as exc:
            logger.error("Eval gate: harness failed for %s: %s", proposal.id, exc)
            return EvalGateResult(
                accepted=False,
                score=0.0,
                baseline_score=baseline_score,
                delta=-baseline_score,
                promoted=False,
                reason=f"Eval harness failed: {exc}",
                tier=eval_tier,
            )

        candidate_score = eval_result.score
        delta = candidate_score - baseline_score
        threshold = baseline_score - self._regression_tolerance

        # Decision logic.
        if candidate_score >= threshold:
            accepted = True
            promoted = candidate_score > baseline_score + self._promotion_threshold

            if promoted:
                self._update_baseline(candidate_score, eval_result)
                reason = (
                    f"Eval passed and baseline promoted: "
                    f"{candidate_score:.4f} > {baseline_score:.4f} + {self._promotion_threshold} "
                    f"(delta={delta:+.4f})"
                )
            else:
                reason = (
                    f"Eval passed: {candidate_score:.4f} >= {threshold:.4f} "
                    f"(baseline={baseline_score:.4f}, delta={delta:+.4f})"
                )
        else:
            accepted = False
            promoted = False
            reason = (
                f"Eval REJECTED: {candidate_score:.4f} < {threshold:.4f} "
                f"(baseline={baseline_score:.4f}, delta={delta:+.4f})"
            )

        logger.info("Eval gate [%s]: %s - %s", proposal.id, "ACCEPT" if accepted else "REJECT", reason)

        # Log trajectory.
        self._log_trajectory(proposal, baseline_score, candidate_score, accepted)

        return EvalGateResult(
            accepted=accepted,
            score=candidate_score,
            baseline_score=baseline_score,
            delta=delta,
            promoted=promoted,
            reason=reason,
            tier=eval_tier,
        )

    def _update_baseline(self, score: float, eval_result: Any) -> None:
        """Promote the baseline to a new score."""
        from bernstein.eval.baseline import EvalBaseline, compute_config_hash, save_baseline

        new_baseline = EvalBaseline(
            score=score,
            components=getattr(eval_result, "components", {}),
            config_hash=compute_config_hash(self._state_dir),
        )
        save_baseline(self._state_dir, new_baseline)
        logger.info("Eval baseline promoted to %.4f", score)

    def _log_trajectory(
        self,
        proposal: UpgradeProposal,
        baseline_score: float,
        candidate_score: float,
        accepted: bool,
    ) -> None:
        """Append to eval_trajectory.jsonl for tracking score over time."""
        record = {
            "proposal_id": proposal.id,
            "title": proposal.title,
            "baseline": round(baseline_score, 6),
            "proposed": round(candidate_score, 6),
            "delta": round(candidate_score - baseline_score, 6),
            "accepted": accepted,
            "timestamp": time.time(),
        }
        try:
            with self._trajectory_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            logger.warning("Failed to write eval trajectory record")


def eval_gate(
    proposal: UpgradeProposal,
    risk_level: RiskLevel,
    eval_harness: Any,
    state_dir: Path,
    sandbox_dir: Path | None = None,
    regression_tolerance: float = EVAL_REGRESSION_TOLERANCE,
    promotion_threshold: float = EVAL_PROMOTION_THRESHOLD,
) -> EvalGateResult:
    """Convenience function - run the eval gate for a single proposal.

    Creates a temporary EvalGate and evaluates the proposal. Useful for
    one-shot evaluation without managing an EvalGate instance.

    Args:
        proposal: The upgrade proposal being evaluated.
        risk_level: Risk classification.
        eval_harness: EvalHarness instance.
        state_dir: Path to .sdd directory.
        sandbox_dir: Optional sandbox directory for isolated eval.
        regression_tolerance: Override default tolerance.
        promotion_threshold: Override default promotion threshold.

    Returns:
        EvalGateResult with accept/reject decision.
    """
    return (
        EvalGate(
            eval_harness=eval_harness,
            state_dir=state_dir,
            regression_tolerance=regression_tolerance,
            promotion_threshold=promotion_threshold,
        )
    ).evaluate(proposal, risk_level, sandbox_dir=sandbox_dir)


# ======================================================================
# PromptPatchGate - predicted-delta + oscillation guard wiring
# ======================================================================

# Veto message format. Pinned so the snapshot test can detect regressions.
_VETO_MESSAGE_TEMPLATE: str = (
    "[prompt-patch-gate] prompt={prompt} proposal_id={proposal_id} verdict={verdict} reason={reason}"
)


class PromptPatchOutcome(Enum):
    """Top-level outcome of a :class:`PromptPatchGate` evaluation."""

    APPLIED = "applied"
    """Both sub-gates approved and the patch is queued for application."""

    PENDING = "pending_confirmation"
    """Oscillation guard wants one more consecutive cycle."""

    REJECTED_DELTA = "below_threshold"
    """Predicted delta did not clear the threshold."""

    REJECTED_OSCILLATION = "flip_back"
    """Patch would oscillate against a recent applied change."""

    REJECTED_SESSION_CAP = "session_cap"
    """Per-session accepted-patch cap reached."""

    REJECTED_INVALID_DELTA = "invalid_delta"
    """``predicted_delta`` was not finite and the predictor could not fix it."""


@dataclass
class PromptPatchDecision:
    """Composite verdict of :class:`PromptPatchGate`.

    Attributes:
        proposal_id: ID of the evaluated patch proposal.
        outcome: Top-level outcome.
        applied: True iff this decision results in the patch being applied.
        delta_result: Underlying predicted-delta verdict.
        oscillation_result: Underlying oscillation verdict
            (``None`` if the delta gate vetoed first).
        veto_message: Standardised veto message (empty if accepted).
        decided_at: Unix timestamp of the decision.
    """

    proposal_id: str
    outcome: PromptPatchOutcome
    applied: bool
    delta_result: PredictedDeltaResult
    oscillation_result: OscillationResult | None
    veto_message: str
    decided_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the per-cycle audit row."""
        return {
            "proposal_id": self.proposal_id,
            "outcome": self.outcome.value,
            "applied": self.applied,
            "delta": {
                "verdict": self.delta_result.verdict.value,
                "predicted_delta": self.delta_result.predicted_delta,
                "threshold": self.delta_result.threshold,
                "reason": self.delta_result.reason,
            },
            "oscillation": (
                {
                    "verdict": self.oscillation_result.verdict.value,
                    "confirmations": self.oscillation_result.confirmations,
                    "applied_count": self.oscillation_result.applied_count,
                    "reason": self.oscillation_result.reason,
                }
                if self.oscillation_result is not None
                else None
            ),
            "veto_message": self.veto_message,
            "decided_at": self.decided_at,
        }


class PromptPatchGate:
    """Combined predicted-delta + oscillation gate for prompt patches.

    This is the wiring point requested by issue #1348. It owns two
    sub-gates and an optional JSONL audit log under
    ``<audit_dir>/<session>.jsonl``. Decision flow:

    1. Run :class:`PredictedDeltaGate`. If the patch fails, write an
       audit row and short-circuit with
       :attr:`PromptPatchOutcome.REJECTED_DELTA` or
       :attr:`PromptPatchOutcome.REJECTED_INVALID_DELTA`.
    2. Run :class:`OscillationGuard`. Translate its verdict into a
       :class:`PromptPatchOutcome`. Audit-log the result.
    3. On :attr:`PromptPatchOutcome.APPLIED`, the caller is expected
       to invoke :meth:`mark_applied` once the patch has actually
       been written.

    Args:
        delta_gate: Override the predicted-delta gate.
        oscillation_guard: Override the oscillation guard.
        audit_dir: Optional directory for the per-cycle audit JSONL.
            When ``None``, no audit file is written.
        session_id: Stable id used for the audit filename. Defaults
            to ``"default"``.
    """

    def __init__(
        self,
        delta_gate: PredictedDeltaGate | None = None,
        oscillation_guard: OscillationGuard | None = None,
        audit_dir: Path | None = None,
        session_id: str = "default",
    ) -> None:
        self._delta_gate = delta_gate or PredictedDeltaGate()
        self._oscillation = oscillation_guard or OscillationGuard()
        self._audit_dir = audit_dir
        self._session_id = session_id
        if self._audit_dir is not None:
            self._audit_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def delta_gate(self) -> PredictedDeltaGate:
        return self._delta_gate

    @property
    def oscillation_guard(self) -> OscillationGuard:
        return self._oscillation

    @property
    def audit_path(self) -> Path | None:
        if self._audit_dir is None:
            return None
        return self._audit_dir / f"{self._session_id}.jsonl"

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, proposal: PatchProposal) -> PromptPatchDecision:
        """Evaluate ``proposal`` through both sub-gates.

        Returns:
            :class:`PromptPatchDecision` describing the combined verdict.
            Audit-logged exactly once.
        """
        delta_result = self._delta_gate.evaluate(proposal)
        if delta_result.verdict == PatchVerdict.REJECTED_BELOW_THRESHOLD:
            decision = PromptPatchDecision(
                proposal_id=proposal.proposal_id,
                outcome=PromptPatchOutcome.REJECTED_DELTA,
                applied=False,
                delta_result=delta_result,
                oscillation_result=None,
                veto_message=self._format_veto(
                    prompt=proposal.prompt_name,
                    proposal_id=proposal.proposal_id,
                    outcome=PromptPatchOutcome.REJECTED_DELTA,
                    reason=delta_result.reason,
                ),
            )
            self._log_decision(decision)
            return decision

        if delta_result.verdict == PatchVerdict.REJECTED_INVALID_DELTA:
            decision = PromptPatchDecision(
                proposal_id=proposal.proposal_id,
                outcome=PromptPatchOutcome.REJECTED_INVALID_DELTA,
                applied=False,
                delta_result=delta_result,
                oscillation_result=None,
                veto_message=self._format_veto(
                    prompt=proposal.prompt_name,
                    proposal_id=proposal.proposal_id,
                    outcome=PromptPatchOutcome.REJECTED_INVALID_DELTA,
                    reason=delta_result.reason,
                ),
            )
            self._log_decision(decision)
            return decision

        # Delta passed - run the oscillation guard.
        osc_result = self._oscillation.evaluate(proposal)
        outcome = _oscillation_to_outcome(osc_result.verdict)
        applied = outcome == PromptPatchOutcome.APPLIED
        veto = (
            ""
            if applied
            else self._format_veto(
                prompt=proposal.prompt_name,
                proposal_id=proposal.proposal_id,
                outcome=outcome,
                reason=osc_result.reason,
            )
        )
        decision = PromptPatchDecision(
            proposal_id=proposal.proposal_id,
            outcome=outcome,
            applied=applied,
            delta_result=delta_result,
            oscillation_result=osc_result,
            veto_message=veto,
        )
        self._log_decision(decision)
        return decision

    def mark_applied(self, proposal: PatchProposal) -> None:
        """Mark ``proposal`` as applied. Forwards to the oscillation guard."""
        self._oscillation.record_applied(proposal)

    def reset_session(self) -> None:
        """Reset the per-session counters on the oscillation guard."""
        self._oscillation.reset_session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_veto(
        *,
        prompt: str,
        proposal_id: str,
        outcome: PromptPatchOutcome,
        reason: str,
    ) -> str:
        return _VETO_MESSAGE_TEMPLATE.format(
            prompt=prompt,
            proposal_id=proposal_id,
            verdict=outcome.value,
            reason=reason,
        )

    def _log_decision(self, decision: PromptPatchDecision) -> None:
        path = self.audit_path
        if path is None:
            return
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision.to_dict()) + "\n")


def _oscillation_to_outcome(verdict: OscillationVerdict) -> PromptPatchOutcome:
    """Translate an :class:`OscillationVerdict` to a :class:`PromptPatchOutcome`."""
    match verdict:
        case OscillationVerdict.ACCEPTED:
            return PromptPatchOutcome.APPLIED
        case OscillationVerdict.PENDING_CONFIRMATION:
            return PromptPatchOutcome.PENDING
        case OscillationVerdict.REJECTED_FLIP_BACK:
            return PromptPatchOutcome.REJECTED_OSCILLATION
        case OscillationVerdict.REJECTED_SESSION_CAP:
            return PromptPatchOutcome.REJECTED_SESSION_CAP
