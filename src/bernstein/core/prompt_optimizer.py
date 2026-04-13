"""Automatic prompt optimization using A/B testing with sequential testing.

Maintains multiple prompt variants per role, assigns variants to tasks, tracks
quality gate pass rate per variant, and uses Sequential Probability Ratio Test
(SPRT) to determine winners with minimal sample size.  After a winner is
promoted, a new challenger is automatically introduced to continue improvement
without human intervention.

Usage::

    optimizer = PromptOptimizer(sdd_dir, templates_dir)

    # At spawn time — get variant assignment for a task
    assignment = optimizer.assign_variant(role="backend", task_id="abc123")
    if assignment.content_override:
        role_prompt_content = assignment.content_override

    # After task completes — record quality gate outcome
    optimizer.record_outcome(
        role="backend",
        task_id="abc123",
        passed=True,
        quality_score=0.85,
        cost_usd=0.04,
        latency_s=42.0,
    )
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: False positive rate (Type I error) — probability of promoting a worse variant.
_DEFAULT_ALPHA: float = 0.05
#: False negative rate (Type II error) — probability of missing a real improvement.
_DEFAULT_BETA: float = 0.20
#: Minimum detectable improvement in success rate before promotion.
_DEFAULT_MIN_EFFECT: float = 0.08
#: Hard cap on observations per variant to bound experiment duration.
_DEFAULT_MAX_SAMPLE: int = 200
#: Minimum observations before SPRT can make a decision.
_DEFAULT_MIN_SAMPLE: int = 10


# ---------------------------------------------------------------------------
# SPRT implementation
# ---------------------------------------------------------------------------


class SprtDecision(Enum):
    """Outcome of a Sequential Probability Ratio Test."""

    CONTINUE = "continue"       # Insufficient evidence — keep collecting
    PROMOTE_CHALLENGER = "promote_challenger"  # Challenger is significantly better
    KEEP_CONTROL = "keep_control"   # Challenger is not better — revert to control
    MAX_SAMPLE_REACHED = "max_sample_reached"  # Forced decision from sample cap


@dataclass(frozen=True)
class SprtConfig:
    """Configuration for the Sequential Probability Ratio Test.

    Attributes:
        alpha: False positive rate (Type I error).  Lower = fewer bad promotions.
        beta: False negative rate (Type II error).  Lower = fewer missed improvements.
        min_effect_size: Minimum difference in success rate deemed worth detecting.
        max_sample: Hard cap on observations per variant before forced decision.
        min_sample: Minimum observations on each side before any decision.
    """

    alpha: float = _DEFAULT_ALPHA
    beta: float = _DEFAULT_BETA
    min_effect_size: float = _DEFAULT_MIN_EFFECT
    max_sample: int = _DEFAULT_MAX_SAMPLE
    min_sample: int = _DEFAULT_MIN_SAMPLE


def _sprt_decide(
    control_successes: int,
    control_obs: int,
    challenger_successes: int,
    challenger_obs: int,
    *,
    cfg: SprtConfig,
) -> SprtDecision:
    """Apply Wald's SPRT to decide the A/B test outcome.

    Tests H0 (no difference) vs H1 (challenger is better by min_effect_size).
    Uses log-likelihood ratio for numerical stability.

    Args:
        control_successes: Number of successes for control variant.
        control_obs: Total observations for control variant.
        challenger_successes: Number of successes for challenger variant.
        challenger_obs: Total observations for challenger variant.
        cfg: SPRT configuration.

    Returns:
        Decision based on current evidence.
    """
    if control_obs < cfg.min_sample or challenger_obs < cfg.min_sample:
        return SprtDecision.CONTINUE

    # Force a decision when the max sample cap is hit on either arm.
    if control_obs >= cfg.max_sample or challenger_obs >= cfg.max_sample:
        if challenger_successes / max(challenger_obs, 1) > control_successes / max(control_obs, 1):
            return SprtDecision.MAX_SAMPLE_REACHED
        return SprtDecision.KEEP_CONTROL

    # Estimated success rates with Laplace smoothing to avoid log(0).
    p0 = (control_successes + 1) / (control_obs + 2)      # control rate
    p1_hyp = min(p0 + cfg.min_effect_size, 1.0 - 1e-9)    # H1: challenger beats by delta

    # SPRT decision boundaries (log scale).
    log_A = math.log((1.0 - cfg.beta) / cfg.alpha)    # upper boundary → promote challenger
    log_B = math.log(cfg.beta / (1.0 - cfg.alpha))    # lower boundary → keep control

    # Compute log-likelihood ratio for the challenger arm.
    llr = 0.0
    ch_rate = (challenger_successes + 1) / (challenger_obs + 2)

    # We approximate LLR using aggregate sufficient statistics.
    # successes contribute log(p1/p0), failures contribute log((1-p1)/(1-p0)).
    s = challenger_successes
    f = challenger_obs - challenger_successes
    if p0 > 0 and p1_hyp > 0 and (1.0 - p0) > 0 and (1.0 - p1_hyp) > 0:
        llr = s * math.log(p1_hyp / p0) + f * math.log((1.0 - p1_hyp) / (1.0 - p0))
    else:
        # Degenerate case — fall back to raw comparison.
        if ch_rate > p0 + cfg.min_effect_size:
            return SprtDecision.PROMOTE_CHALLENGER
        return SprtDecision.CONTINUE

    if llr >= log_A:
        return SprtDecision.PROMOTE_CHALLENGER
    if llr <= log_B:
        return SprtDecision.KEEP_CONTROL
    return SprtDecision.CONTINUE


# ---------------------------------------------------------------------------
# Challenger generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChallengerTemplate:
    """A named strategy for generating challenger prompt content.

    Attributes:
        name: Short identifier for the strategy.
        suffix: Text appended to the base prompt to create the challenger.
    """

    name: str
    suffix: str


#: Ordered list of challenger strategies cycled through per role.
_CHALLENGER_TEMPLATES: list[ChallengerTemplate] = [
    ChallengerTemplate(
        name="step_by_step",
        suffix=(
            "\n\n## Reasoning approach\n"
            "Before writing any code, briefly outline the steps you'll take. "
            "This helps catch edge cases early and produce more correct solutions."
        ),
    ),
    ChallengerTemplate(
        name="examples_first",
        suffix=(
            "\n\n## Existing patterns\n"
            "Always search for existing code that solves a similar problem before "
            "writing new code. Prefer consistency with existing patterns over novelty."
        ),
    ),
    ChallengerTemplate(
        name="test_driven",
        suffix=(
            "\n\n## Test-first mindset\n"
            "When implementing a feature, write or verify the test first. "
            "Let the failing test guide the implementation."
        ),
    ),
    ChallengerTemplate(
        name="minimal_diff",
        suffix=(
            "\n\n## Minimal changes\n"
            "Make the smallest diff that satisfies the requirement. "
            "Avoid touching code unrelated to the task."
        ),
    ),
    ChallengerTemplate(
        name="verify_before_commit",
        suffix=(
            "\n\n## Verify before committing\n"
            "Run linting and tests locally before every commit. "
            "A passing CI pipeline is part of a complete task."
        ),
    ),
    ChallengerTemplate(
        name="explicit_contracts",
        suffix=(
            "\n\n## Document contracts\n"
            "For every public function or class you add, write a one-line docstring "
            "that states what it does, its main parameter, and its return value."
        ),
    ),
    ChallengerTemplate(
        name="defensive_review",
        suffix=(
            "\n\n## Self-review\n"
            "After finishing the implementation, re-read the diff once as a code "
            "reviewer would. Fix any issue you spot before marking the task done."
        ),
    ),
    ChallengerTemplate(
        name="context_scan",
        suffix=(
            "\n\n## Context awareness\n"
            "Before starting, scan relevant files to understand data structures, "
            "naming conventions, and API contracts used by neighboring code."
        ),
    ),
]


def _next_challenger_template(current_index: int) -> tuple[ChallengerTemplate, int]:
    """Return the next challenger template and its index.

    Args:
        current_index: Index of the last-used template (or -1 for first run).

    Returns:
        Tuple of (template, new_index).
    """
    next_idx = (current_index + 1) % len(_CHALLENGER_TEMPLATES)
    return _CHALLENGER_TEMPLATES[next_idx], next_idx


def generate_challenger_content(base_content: str, template: ChallengerTemplate) -> str:
    """Generate challenger prompt content from a base and a strategy template.

    Args:
        base_content: The current winning prompt content.
        template: The challenger strategy to apply.

    Returns:
        Modified prompt content for the challenger variant.
    """
    return base_content + template.suffix


# ---------------------------------------------------------------------------
# Variant assignment
# ---------------------------------------------------------------------------


@dataclass
class VariantAssignment:
    """Records which prompt variant was assigned to a task.

    Attributes:
        role: Agent role (e.g. "backend").
        task_id: Task being assigned.
        variant_version: Version number from PromptRegistry, or None if default.
        content_override: Non-None when the variant differs from the template file.
            Callers should use this string as the role system prompt.
        assigned_at: Unix timestamp of assignment.
    """

    role: str
    task_id: str
    variant_version: int | None = None
    content_override: str | None = None
    assigned_at: float = field(default_factory=time.time)

    def is_challenger(self, control_version: int | None) -> bool:
        """Return True if this assignment is the challenger (not control).

        Args:
            control_version: The control (active) version number.

        Returns:
            True when this task was assigned the non-control variant.
        """
        return self.variant_version is not None and self.variant_version != control_version


# ---------------------------------------------------------------------------
# PromptOptimizer
# ---------------------------------------------------------------------------


class PromptOptimizer:
    """Continuous prompt optimizer — manages A/B tests per role with SPRT.

    For each role:
    - Maintains an active version (control) and optionally a challenger.
    - Randomly assigns tasks to control or challenger.
    - Records quality gate outcomes per variant.
    - Uses SPRT to determine when to promote the challenger.
    - After promotion, automatically introduces a new challenger.

    State is persisted in ``.sdd/prompt_optimizer/`` so it survives restarts.

    Args:
        sdd_dir: Path to ``.sdd/`` directory.
        templates_dir: Path to ``templates/roles/`` directory (for seeding prompts).
        cfg: SPRT configuration (defaults used if not provided).
    """

    _STATE_FILE = "state.json"

    def __init__(
        self,
        sdd_dir: "Path",
        templates_dir: "Path | None" = None,
        cfg: SprtConfig | None = None,
    ) -> None:
        from pathlib import Path as _Path

        self._sdd_dir = _Path(sdd_dir)
        self._templates_dir = _Path(templates_dir) if templates_dir else None
        self._cfg = cfg or SprtConfig()
        self._state_dir = self._sdd_dir / "prompt_optimizer"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = self._load_state()

        # In-memory index: task_id → VariantAssignment (cleared on reload)
        self._assignments: dict[str, VariantAssignment] = {}

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        """Load optimizer state from disk."""
        path = self._state_dir / self._STATE_FILE
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load prompt_optimizer state: %s", exc)
        return {}

    def _save_state(self) -> None:
        """Persist optimizer state to disk."""
        path = self._state_dir / self._STATE_FILE
        try:
            path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save prompt_optimizer state: %s", exc)

    def _role_state(self, role: str) -> dict[str, Any]:
        """Return mutable state dict for a role, initialising if absent."""
        if role not in self._state:
            self._state[role] = {
                "active_version": None,
                "challenger_version": None,
                "challenger_template_idx": -1,
                "control_metrics": {"observations": 0, "successes": 0},
                "challenger_metrics": {"observations": 0, "successes": 0},
                "tests_run": 0,
                "promotions": [],
            }
        return self._state[role]

    # ------------------------------------------------------------------
    # Registry access
    # ------------------------------------------------------------------

    def _get_registry(self) -> "PromptRegistry":
        """Return a PromptRegistry for sdd_dir."""
        from bernstein.core.tokens.prompt_versioning import PromptRegistry

        return PromptRegistry(self._sdd_dir)

    def _ensure_role_seeded(self, role: str) -> bool:
        """Seed v1 for a role from templates if not yet tracked.

        Args:
            role: Agent role name.

        Returns:
            True if seeding succeeded or prompt already existed.
        """
        registry = self._get_registry()
        if registry.get_meta(role) is not None:
            return True
        if self._templates_dir is None:
            return False
        template_path = self._templates_dir / "roles" / role / "system_prompt.md"
        if not template_path.exists():
            return False
        content = template_path.read_text(encoding="utf-8")
        pv = registry.add_version(
            role,
            content,
            author="system",
            description="Initial version seeded from templates",
            set_active=True,
        )
        logger.info("Seeded prompt for role %r as v%d", role, pv.version)
        return True

    # ------------------------------------------------------------------
    # Variant assignment
    # ------------------------------------------------------------------

    def assign_variant(self, role: str, task_id: str) -> VariantAssignment:
        """Select and record a prompt variant for a task.

        If no challenger exists yet, returns the active (control) version.
        During an active A/B test, deterministically assigns based on task_id.

        Args:
            role: Agent role (e.g. "backend").
            task_id: Unique task identifier for deterministic assignment.

        Returns:
            VariantAssignment with optional content_override.
        """
        self._ensure_role_seeded(role)
        registry = self._get_registry()
        rs = self._role_state(role)

        meta = registry.get_meta(role)
        if meta is None:
            # No prompt registered — return default (no override)
            return VariantAssignment(role=role, task_id=task_id)

        # Sync active version from registry if not yet tracked locally
        if rs["active_version"] is None:
            rs["active_version"] = meta.active_version
            self._save_state()

        control_version: int = rs["active_version"]
        challenger_version: int | None = rs["challenger_version"]

        # Ensure challenger exists; introduce one if missing
        if challenger_version is None:
            challenger_version = self._introduce_challenger(role, registry, rs)

        # Assign using the registry's deterministic split
        if challenger_version is not None and meta.ab_enabled:
            selected = registry.select_version(role, task_id)
        else:
            selected = control_version

        # Resolve content
        content_override: str | None = None
        if selected != control_version or meta.active_version != control_version:
            # Use the versioned content from registry when it differs from v1
            pv = registry.get_version(role, selected)
            if pv and pv.content:
                content_override = pv.content

        assignment = VariantAssignment(
            role=role,
            task_id=task_id,
            variant_version=selected,
            content_override=content_override,
        )
        self._assignments[task_id] = assignment
        return assignment

    def _introduce_challenger(
        self,
        role: str,
        registry: "PromptRegistry",
        rs: dict[str, Any],
    ) -> int | None:
        """Add a new challenger version and start an A/B test.

        Args:
            role: Agent role.
            registry: Prompt registry.
            rs: Role state dict (mutated in place).

        Returns:
            Version number of the new challenger, or None on failure.
        """
        control_version: int = rs["active_version"]
        control_pv = registry.get_version(role, control_version)
        if control_pv is None or not control_pv.content:
            logger.debug("Cannot introduce challenger for %r — control has no content", role)
            return None

        # Pick the next challenger template
        tmpl, next_idx = _next_challenger_template(rs["challenger_template_idx"])
        challenger_content = generate_challenger_content(control_pv.content, tmpl)

        # Add new version to registry
        new_pv = registry.add_version(
            role,
            challenger_content,
            author="prompt_optimizer",
            description=f"Challenger: {tmpl.name}",
        )
        challenger_version = new_pv.version

        # Start A/B test
        registry.start_ab_test(role, control_version, challenger_version)

        rs["challenger_version"] = challenger_version
        rs["challenger_template_idx"] = next_idx
        rs["control_metrics"] = {"observations": 0, "successes": 0}
        rs["challenger_metrics"] = {"observations": 0, "successes": 0}
        self._save_state()

        logger.info(
            "Introduced challenger v%d for role %r using template %r",
            challenger_version,
            role,
            tmpl.name,
        )
        return challenger_version

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        role: str,
        task_id: str,
        *,
        passed: bool,
        quality_score: float = 0.0,
        cost_usd: float = 0.0,
        latency_s: float = 0.0,
    ) -> SprtDecision | None:
        """Record a task's quality gate outcome and run SPRT check.

        Args:
            role: Agent role.
            task_id: Task identifier (must match a previous assign_variant call,
                but falls back to recording for the active version if not found).
            passed: Whether the task passed quality gates.
            quality_score: Quality score from review (0–1).
            cost_usd: Cost incurred by this task.
            latency_s: Task duration in seconds.

        Returns:
            SprtDecision if a decision was reached this call, else None.
        """
        rs = self._role_state(role)
        registry = self._get_registry()

        # Look up which variant this task was assigned
        assignment = self._assignments.get(task_id)
        control_version: int | None = rs.get("active_version")
        challenger_version: int | None = rs.get("challenger_version")

        if assignment is not None:
            used_version = assignment.variant_version
        else:
            used_version = control_version

        # Record in registry
        if used_version is not None:
            registry.record_outcome(
                role,
                used_version,
                success=passed,
                quality_score=quality_score,
                cost_usd=cost_usd,
                latency_s=latency_s,
            )

        # Update local metrics (for SPRT without reloading all registry data)
        if used_version == challenger_version:
            m = rs["challenger_metrics"]
        else:
            m = rs["control_metrics"]
        m["observations"] += 1
        if passed:
            m["successes"] += 1

        self._save_state()
        self._assignments.pop(task_id, None)  # clean up

        # Run SPRT only when both arms have enough data
        if challenger_version is None:
            return None

        decision = _sprt_decide(
            control_successes=rs["control_metrics"]["successes"],
            control_obs=rs["control_metrics"]["observations"],
            challenger_successes=rs["challenger_metrics"]["successes"],
            challenger_obs=rs["challenger_metrics"]["observations"],
            cfg=self._cfg,
        )

        if decision != SprtDecision.CONTINUE:
            self._conclude_test(role, decision, registry, rs)

        return decision if decision != SprtDecision.CONTINUE else None

    def _conclude_test(
        self,
        role: str,
        decision: SprtDecision,
        registry: "PromptRegistry",
        rs: dict[str, Any],
    ) -> None:
        """Finalize an A/B test: promote winner and introduce new challenger.

        Args:
            role: Agent role.
            decision: SPRT decision that triggered conclusion.
            registry: Prompt registry.
            rs: Role state dict (mutated in place).
        """
        control_version: int = rs["active_version"]
        challenger_version: int = rs["challenger_version"]

        if decision in (SprtDecision.PROMOTE_CHALLENGER, SprtDecision.MAX_SAMPLE_REACHED):
            # Check raw rates when max sample is reached
            if decision == SprtDecision.MAX_SAMPLE_REACHED:
                c_rate = rs["challenger_metrics"]["successes"] / max(rs["challenger_metrics"]["observations"], 1)
                ctrl_rate = rs["control_metrics"]["successes"] / max(rs["control_metrics"]["observations"], 1)
                if c_rate <= ctrl_rate:
                    decision = SprtDecision.KEEP_CONTROL

        if decision in (SprtDecision.PROMOTE_CHALLENGER, SprtDecision.MAX_SAMPLE_REACHED):
            winner = challenger_version
            registry.promote_version(role, challenger_version)
            rs["active_version"] = challenger_version
            outcome_label = "promoted"
            logger.info(
                "PromptOptimizer: promoted challenger v%d for role %r "
                "(control obs=%d sr=%.2f, challenger obs=%d sr=%.2f)",
                challenger_version,
                role,
                rs["control_metrics"]["observations"],
                rs["control_metrics"]["successes"] / max(rs["control_metrics"]["observations"], 1),
                rs["challenger_metrics"]["observations"],
                rs["challenger_metrics"]["successes"] / max(rs["challenger_metrics"]["observations"], 1),
            )
        else:
            winner = control_version
            registry.stop_ab_test(role)
            outcome_label = "retained"
            logger.info(
                "PromptOptimizer: control v%d retained for role %r "
                "(challenger v%d not better: obs=%d sr=%.2f vs control sr=%.2f)",
                control_version,
                role,
                challenger_version,
                rs["challenger_metrics"]["observations"],
                rs["challenger_metrics"]["successes"] / max(rs["challenger_metrics"]["observations"], 1),
                rs["control_metrics"]["successes"] / max(rs["control_metrics"]["observations"], 1),
            )

        rs["tests_run"] = rs.get("tests_run", 0) + 1
        rs["promotions"].append(
            {
                "concluded_at": time.time(),
                "decision": decision.value,
                "outcome": outcome_label,
                "winner": winner,
                "control_version": control_version,
                "challenger_version": challenger_version,
                "control_obs": rs["control_metrics"]["observations"],
                "control_sr": round(rs["control_metrics"]["successes"] / max(rs["control_metrics"]["observations"], 1), 4),
                "challenger_obs": rs["challenger_metrics"]["observations"],
                "challenger_sr": round(rs["challenger_metrics"]["successes"] / max(rs["challenger_metrics"]["observations"], 1), 4),
            }
        )

        # Reset challenger to trigger new introduction on next spawn
        rs["challenger_version"] = None
        rs["control_metrics"] = {"observations": 0, "successes": 0}
        rs["challenger_metrics"] = {"observations": 0, "successes": 0}
        self._save_state()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_status(self, role: str) -> dict[str, Any]:
        """Return current optimizer status for a role.

        Args:
            role: Agent role to query.

        Returns:
            Dict with active version, challenger version, metrics, and test history.
        """
        rs = self._role_state(role)
        return {
            "role": role,
            "active_version": rs.get("active_version"),
            "challenger_version": rs.get("challenger_version"),
            "tests_run": rs.get("tests_run", 0),
            "control_metrics": rs.get("control_metrics", {}),
            "challenger_metrics": rs.get("challenger_metrics", {}),
            "recent_promotions": rs.get("promotions", [])[-5:],
        }

    def list_active_roles(self) -> list[str]:
        """Return list of roles that have optimizer state.

        Returns:
            Sorted list of role names.
        """
        return sorted(self._state.keys())
