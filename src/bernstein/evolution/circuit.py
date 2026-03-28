"""CircuitBreaker — halt evolution when safety conditions are violated.

Implements tri-state circuit breaker (Closed/Open/Half-Open) with
rate limiting per modification type.

Rate limits (token bucket):
  L0 config changes: max 5/day
  L1 template changes: max 3/day
  L2 code proposals: max 1/week
  Evolution API budget: configurable cap
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.evolution.types import CircuitState, RiskLevel

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default rate limits per risk level (changes per 24h period)
DEFAULT_RATE_LIMITS: dict[RiskLevel, int] = {
    RiskLevel.L0_CONFIG: 5,
    RiskLevel.L1_TEMPLATE: 3,
    RiskLevel.L2_LOGIC: 1,   # 1 per week, enforced as 1/day + cooldown
    RiskLevel.L3_STRUCTURAL: 0,  # Never auto-apply
}

# Cooldown period before transitioning from Open → Half-Open (seconds)
COOLDOWN_SECONDS = 3600  # 1 hour


@dataclass
class CircuitBreaker:
    """Tri-state circuit breaker for evolution safety.

    Tracks rate limits, halt conditions, and state transitions.
    State persisted to .sdd/evolution/circuit_state.json.

    Args:
        state_dir: Path to .sdd/evolution/ directory.
        rate_limits: Override default rate limits per risk level.
    """
    state_dir: Path
    rate_limits: dict[RiskLevel, int] = field(
        default_factory=lambda: dict(DEFAULT_RATE_LIMITS)
    )
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0
    recent_changes: list[dict] = field(default_factory=list)
    recent_rollbacks: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

    def _state_path(self) -> Path:
        return self.state_dir / "circuit_state.json"

    def _load_state(self) -> None:
        """Load persisted state from disk."""
        path = self._state_path()
        if path.exists():
            data = json.loads(path.read_text())
            self.state = CircuitState(data.get("state", "closed"))
            self.opened_at = data.get("opened_at", 0.0)
            self.recent_changes = data.get("recent_changes", [])
            self.recent_rollbacks = data.get("recent_rollbacks", [])

    def _save_state(self) -> None:
        """Persist state to disk."""
        data = {
            "state": self.state.value,
            "opened_at": self.opened_at,
            "recent_changes": self.recent_changes,
            "recent_rollbacks": self.recent_rollbacks,
        }
        self._state_path().write_text(json.dumps(data, indent=2) + "\n")

    def can_evolve(self, risk_level: RiskLevel) -> tuple[bool, str]:
        """Check if evolution is allowed for the given risk level.

        Args:
            risk_level: Risk classification of the proposal.

        Returns:
            Tuple of (allowed, reason). If not allowed, reason explains why.
        """
        # L3 is never auto-allowed
        if risk_level == RiskLevel.L3_STRUCTURAL:
            return False, "L3_STRUCTURAL changes require human-only approval"

        # Check circuit state
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.opened_at
            if elapsed < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - elapsed)
                return False, f"Circuit OPEN — cooling off ({remaining}s remaining)"
            # Transition to half-open
            self.state = CircuitState.HALF_OPEN
            self._save_state()
            logger.info("Circuit transitioning to HALF_OPEN")

        if self.state == CircuitState.HALF_OPEN and risk_level != RiskLevel.L0_CONFIG:
            # Only allow L0 in half-open
            return False, "Circuit HALF_OPEN — only L0 config changes allowed"

        # Check rate limits
        now = time.time()
        cutoff = now - 86400  # 24h window
        recent_for_level = [
            c for c in self.recent_changes
            if c.get("risk_level") == risk_level.value and c.get("ts", 0) > cutoff
        ]
        limit = self.rate_limits.get(risk_level, 0)
        if len(recent_for_level) >= limit:
            return False, (
                f"Rate limit reached for {risk_level.value}: "
                f"{len(recent_for_level)}/{limit} in last 24h"
            )

        return True, "ok"

    def record_change(self, risk_level: RiskLevel, proposal_id: str) -> None:
        """Record a successfully applied change."""
        self.recent_changes.append({
            "risk_level": risk_level.value,
            "proposal_id": proposal_id,
            "ts": time.time(),
        })
        # If in half-open and change succeeded, transition to closed
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info("Circuit transitioning back to CLOSED")
        self._save_state()

    def record_rollback(self, proposal_id: str) -> None:
        """Record a rollback. May trip the circuit breaker."""
        now = time.time()
        self.recent_rollbacks.append(now)
        # Prune old rollbacks (keep last 7 days)
        week_ago = now - 7 * 86400
        self.recent_rollbacks = [t for t in self.recent_rollbacks if t > week_ago]

        # Check halt conditions
        recent_48h = [t for t in self.recent_rollbacks if t > now - 48 * 3600]
        if len(recent_48h) >= 1:
            self._trip(f"Rollback detected (proposal {proposal_id})")
        elif len(self.recent_rollbacks) > 2:
            self._trip(">2 rollbacks in 7 days")
        self._save_state()

    def record_sandbox_failure(self, proposal_id: str) -> None:
        """Record a sandbox test failure. 3 consecutive failures trips breaker."""
        # Count recent consecutive failures (simplified)
        self.recent_changes.append({
            "risk_level": "sandbox_failure",
            "proposal_id": proposal_id,
            "ts": time.time(),
        })
        recent_failures = [
            c for c in self.recent_changes[-5:]
            if c.get("risk_level") == "sandbox_failure"
        ]
        if len(recent_failures) >= 3:
            self._trip("3 consecutive sandbox failures")
        self._save_state()

    def check_metrics_regression(
        self,
        janitor_pass_rate_delta: float,
        cost_per_task_delta: float,
    ) -> None:
        """Check if metrics have regressed enough to trip the breaker.

        Args:
            janitor_pass_rate_delta: Week-over-week change (negative = worse).
            cost_per_task_delta: Week-over-week change (positive = more expensive).
        """
        if janitor_pass_rate_delta < -0.15:
            self._trip(
                f"Janitor pass rate dropped {janitor_pass_rate_delta:.1%} WoW"
            )
        if cost_per_task_delta > 0.25:
            self._trip(
                f"Cost per task increased {cost_per_task_delta:.1%} WoW"
            )

    def _trip(self, reason: str) -> None:
        """Trip the circuit breaker to OPEN state."""
        self.state = CircuitState.OPEN
        self.opened_at = time.time()
        logger.error("CIRCUIT BREAKER TRIPPED: %s", reason)
        self._save_state()

    def reset(self) -> None:
        """Manually reset the circuit breaker (human override)."""
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0
        logger.info("Circuit breaker manually reset")
        self._save_state()
