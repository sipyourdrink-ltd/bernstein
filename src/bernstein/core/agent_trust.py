"""Graduated access control for agents — trust expands with demonstrated reliability.

New agents start at UNTRUSTED with minimal read-only permissions.  As they
complete tasks without security violations their trust level increases and
permissions expand.  Trust scores persist across runs in
``.sdd/trust/<agent_id>.json``.

Trust levels form a linear progression:

- **UNTRUSTED** (new agent): read-only, no network, no ``.sdd/`` writes.
- **RESTRICTED** (3+ clean tasks): limited writes to ``src/`` and ``tests/``.
- **TRUSTED** (10+ tasks, ≥90 % success): standard role permissions.
- **ELEVATED** (25+ tasks, ≥95 % success): expanded permissions including
  infrastructure files.

A security violation (attempted path traversal, denied command, prompt
injection detected) immediately resets ``consecutive_successes`` to zero and
requires more evidence before the next promotion.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from bernstein.core.permissions import AgentPermissions

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared glob pattern constants (used across trust level permission profiles)
# ---------------------------------------------------------------------------

_CMD_CURL = "curl *"
_CMD_WGET = "wget *"
_CMD_SSH = "ssh *"
_CMD_NC = "nc *"
_CMD_NCAT = "ncat *"
_PATH_SRC = "src/*"
_PATH_TESTS = "tests/*"
_PATH_SDD = ".sdd/*"
_PATH_GITHUB = ".github/*"
_PATH_ROLES = "templates/roles/*"

# ---------------------------------------------------------------------------
# Trust level definitions
# ---------------------------------------------------------------------------


class TrustLevel(Enum):
    """Trust level assigned to an agent.

    Levels form a linear progression from UNTRUSTED through ELEVATED.
    Each level unlocks broader permissions as the agent demonstrates
    reliable, policy-compliant behaviour.
    """

    UNTRUSTED = "untrusted"
    RESTRICTED = "restricted"
    TRUSTED = "trusted"
    ELEVATED = "elevated"


_TRUST_ORDER: list[TrustLevel] = [
    TrustLevel.UNTRUSTED,
    TrustLevel.RESTRICTED,
    TrustLevel.TRUSTED,
    TrustLevel.ELEVATED,
]

# ---------------------------------------------------------------------------
# Promotion policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustPolicy:
    """Minimum conditions required to graduate *from* this trust level.

    Attributes:
        level: The trust level these thresholds apply to.
        min_tasks: Minimum successful tasks required before promotion.
        min_success_rate: Minimum success ratio (0.0-1.0).
        max_security_violations: Maximum cumulative violations allowed
            before promotion is permanently blocked at this level.
        min_consecutive_successes: Consecutive violation-free successes
            required immediately before promotion.
    """

    level: TrustLevel
    min_tasks: int
    min_success_rate: float
    max_security_violations: int
    min_consecutive_successes: int


_DEFAULT_POLICIES: dict[str, TrustPolicy] = {
    TrustLevel.UNTRUSTED.value: TrustPolicy(
        level=TrustLevel.UNTRUSTED,
        min_tasks=3,
        min_success_rate=0.0,
        max_security_violations=0,
        min_consecutive_successes=3,
    ),
    TrustLevel.RESTRICTED.value: TrustPolicy(
        level=TrustLevel.RESTRICTED,
        min_tasks=10,
        min_success_rate=0.90,
        max_security_violations=1,
        min_consecutive_successes=5,
    ),
    TrustLevel.TRUSTED.value: TrustPolicy(
        level=TrustLevel.TRUSTED,
        min_tasks=25,
        min_success_rate=0.95,
        max_security_violations=2,
        min_consecutive_successes=10,
    ),
    # ELEVATED is terminal — no outbound policy.
}

# ---------------------------------------------------------------------------
# Permission profiles per trust level
# ---------------------------------------------------------------------------


def get_permissions_for_trust_level(level: TrustLevel) -> AgentPermissions:
    """Return the permission set that corresponds to *level*.

    Args:
        level: Trust level to resolve.

    Returns:
        ``AgentPermissions`` appropriate for that trust level.
    """
    match level:
        case TrustLevel.UNTRUSTED:
            # Read-only access to source and tests; no writes, no network.
            return AgentPermissions(
                allowed_paths=(),  # deny all writes (checked separately)
                denied_paths=("*",),
                allowed_commands=(
                    "git log*",
                    "git status*",
                    "git diff*",
                    "cat *",
                    "ls *",
                    "echo *",
                    "uv run pytest*",
                    "python -m pytest*",
                ),
                denied_commands=(
                    "rm *",
                    "rm-rf *",
                    _CMD_CURL,
                    _CMD_WGET,
                    "git push*",
                    "git commit*",
                    "pip install*",
                    "uv add*",
                    _CMD_SSH,
                    _CMD_NC,
                    _CMD_NCAT,
                ),
            )
        case TrustLevel.RESTRICTED:
            # Limited writes to source and test directories.
            return AgentPermissions(
                allowed_paths=(_PATH_SRC, _PATH_TESTS),
                denied_paths=(_PATH_SDD, _PATH_GITHUB, _PATH_ROLES),
                allowed_commands=(
                    "git log*",
                    "git status*",
                    "git diff*",
                    "git add *",
                    "git commit*",
                    "cat *",
                    "ls *",
                    "echo *",
                    "uv run*",
                    "python *",
                ),
                denied_commands=(
                    "rm *",
                    _CMD_CURL,
                    _CMD_WGET,
                    "git push*",
                    _CMD_SSH,
                    _CMD_NC,
                    _CMD_NCAT,
                ),
            )
        case TrustLevel.TRUSTED:
            # Standard permissions — matches the "backend" role profile.
            return AgentPermissions(
                allowed_paths=(_PATH_SRC, _PATH_TESTS, "docs/*", "scripts/*", "pyproject.toml"),
                denied_paths=(_PATH_SDD, _PATH_GITHUB, _PATH_ROLES),
                allowed_commands=(),  # all commands allowed (subject to deny list)
                denied_commands=(
                    _CMD_CURL,
                    _CMD_WGET,
                    _CMD_SSH,
                    _CMD_NC,
                    _CMD_NCAT,
                ),
            )
        case TrustLevel.ELEVATED:
            # Expanded — infrastructure files and CI allowed.
            return AgentPermissions(
                allowed_paths=(_PATH_SRC, _PATH_TESTS, "docs/*", "scripts/*", _PATH_GITHUB, "Dockerfile*", "*.yml"),
                denied_paths=(_PATH_SDD, _PATH_ROLES),
                allowed_commands=(),
                denied_commands=(_CMD_NC, _CMD_NCAT),
            )


# ---------------------------------------------------------------------------
# Agent trust score
# ---------------------------------------------------------------------------


@dataclass
class AgentTrustScore:
    """Accumulated trust metrics for a single agent.

    Attributes:
        agent_id: Stable identifier for the agent (e.g. ``"worker-abc123"``).
        trust_level: Current trust level.
        tasks_completed: Successful task completions.
        tasks_failed: Failed task completions.
        security_violations: Cumulative policy violations recorded.
        consecutive_successes: Unbroken chain of violation-free successes;
            resets to zero on any violation.
        created_at: Unix timestamp when this record was first created.
        last_updated: Unix timestamp of the most recent task event.
        promotion_log: Ordered list of promotion events.
    """

    agent_id: str
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    tasks_completed: int = 0
    tasks_failed: int = 0
    security_violations: int = 0
    consecutive_successes: int = 0
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    promotion_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tasks_total(self) -> int:
        """Total tasks attempted."""
        return self.tasks_completed + self.tasks_failed

    @property
    def success_rate(self) -> float:
        """Ratio of successes to total; 0.0 when no tasks attempted."""
        return self.tasks_completed / self.tasks_total if self.tasks_total > 0 else 0.0

    @property
    def permissions(self) -> AgentPermissions:
        """Current ``AgentPermissions`` for this agent."""
        return get_permissions_for_trust_level(self.trust_level)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "trust_level": self.trust_level.value,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "security_violations": self.security_violations,
            "consecutive_successes": self.consecutive_successes,
            "tasks_total": self.tasks_total,
            "success_rate": round(self.success_rate, 4),
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "promotion_log": self.promotion_log,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentTrustScore:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        return cls(
            agent_id=d["agent_id"],
            trust_level=TrustLevel(d.get("trust_level", "untrusted")),
            tasks_completed=d.get("tasks_completed", 0),
            tasks_failed=d.get("tasks_failed", 0),
            security_violations=d.get("security_violations", 0),
            consecutive_successes=d.get("consecutive_successes", 0),
            created_at=d.get("created_at", time.time()),
            last_updated=d.get("last_updated", time.time()),
            promotion_log=d.get("promotion_log", []),
        )


# ---------------------------------------------------------------------------
# Trust evaluator
# ---------------------------------------------------------------------------


class TrustEvaluator:
    """Evaluates whether an agent is ready for promotion to the next level.

    Args:
        policies: Trust-level-keyed promotion policies.  Defaults to
            :data:`_DEFAULT_POLICIES`.
    """

    def __init__(self, policies: dict[str, TrustPolicy] | None = None) -> None:
        self._policies = policies if policies is not None else _DEFAULT_POLICIES

    def can_promote(self, score: AgentTrustScore) -> tuple[bool, str]:
        """Check whether *score* meets promotion criteria for its current level.

        Args:
            score: The agent's current trust score.

        Returns:
            ``(True, reason)`` when criteria are met; ``(False, reason)`` otherwise.
        """
        if score.trust_level == TrustLevel.ELEVATED:
            return False, "already at terminal trust level (elevated)"

        policy = self._policies.get(score.trust_level.value)
        if policy is None:
            return False, f"no policy configured for level {score.trust_level.value!r}"

        if score.tasks_completed < policy.min_tasks:
            return False, (f"need {policy.min_tasks} completed tasks, have {score.tasks_completed}")
        if score.success_rate < policy.min_success_rate:
            return False, (f"success rate {score.success_rate:.0%} below required {policy.min_success_rate:.0%}")
        if score.security_violations > policy.max_security_violations:
            return False, (
                f"{score.security_violations} security violations exceed max {policy.max_security_violations}"
            )
        if score.consecutive_successes < policy.min_consecutive_successes:
            return False, (
                f"need {policy.min_consecutive_successes} consecutive successes, have {score.consecutive_successes}"
            )

        next_level = self.next_level(score.trust_level)
        return True, f"ready to promote to {next_level.value}"

    @staticmethod
    def next_level(current: TrustLevel) -> TrustLevel:
        """Return the trust level that follows *current*.

        Args:
            current: Current trust level.

        Returns:
            The next level.

        Raises:
            ValueError: When *current* is already the terminal level.
        """
        idx = _TRUST_ORDER.index(current)
        if idx + 1 >= len(_TRUST_ORDER):
            raise ValueError(f"no level after {current.value!r}")
        return _TRUST_ORDER[idx + 1]

    def promote(
        self,
        score: AgentTrustScore,
        *,
        reason: str = "auto",
        promoted_by: str = "system",
    ) -> AgentTrustScore:
        """Advance *score* to the next trust level.

        Args:
            score: The agent trust score to mutate.
            reason: Human-readable reason for the promotion.
            promoted_by: Who triggered the promotion.

        Returns:
            The updated *score* instance.

        Raises:
            ValueError: When already at the terminal level.
        """
        from_level = score.trust_level
        to_level = self.next_level(from_level)
        now = time.time()

        score.promotion_log.append(
            {
                "from_level": from_level.value,
                "to_level": to_level.value,
                "timestamp": now,
                "reason": reason,
                "promoted_by": promoted_by,
                "tasks_completed": score.tasks_completed,
                "success_rate": round(score.success_rate, 4),
                "security_violations": score.security_violations,
            }
        )
        score.trust_level = to_level
        score.last_updated = now

        from bernstein.core.sanitize import sanitize_log

        logger.info(
            "agent %s promoted %s → %s (reason=%s, by=%s)",
            sanitize_log(score.agent_id),
            from_level.value,
            to_level.value,
            sanitize_log(reason),
            sanitize_log(promoted_by),
        )
        return score


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------


class AgentTrustStore:
    """File-based persistence for agent trust scores.

    State files: ``.sdd/trust/<agent_id>.json``

    Args:
        sdd_dir: Path to the ``.sdd/`` directory.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._trust_dir = sdd_dir / "trust"

    def _ensure_dir(self) -> None:
        self._trust_dir.mkdir(parents=True, exist_ok=True)

    def load(self, agent_id: str) -> AgentTrustScore | None:
        """Load a trust score by agent ID, or return ``None`` if absent."""
        path = self._trust_dir / f"{agent_id}.json"
        if not path.exists():
            return None
        try:
            return AgentTrustScore.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (KeyError, ValueError) as exc:
            logger.warning("failed to load trust score for %s: %s", agent_id.replace("\n", ""), exc)
            return None

    def save(self, score: AgentTrustScore) -> None:
        """Persist a trust score to disk."""
        self._ensure_dir()
        path = self._trust_dir / f"{score.agent_id}.json"
        path.write_text(json.dumps(score.to_dict(), indent=2), encoding="utf-8")

    def get_or_create(self, agent_id: str) -> AgentTrustScore:
        """Return an existing trust score or create a new UNTRUSTED one."""
        score = self.load(agent_id)
        if score is None:
            score = AgentTrustScore(agent_id=agent_id)
            self.save(score)
        return score

    def record_task_outcome(
        self,
        agent_id: str,
        *,
        success: bool,
        security_violation: bool = False,
        auto_promote: bool = True,
        evaluator: TrustEvaluator | None = None,
    ) -> AgentTrustScore:
        """Update trust metrics after a task completes or fails.

        If *auto_promote* is ``True`` (the default) and the agent now meets
        the promotion criteria, the level is automatically advanced.

        Args:
            agent_id: The agent that completed the task.
            success: Whether the task succeeded.
            security_violation: Whether a policy violation was detected.
            auto_promote: Automatically promote when criteria are met.
            evaluator: Optional custom evaluator; defaults to
                :class:`TrustEvaluator`.

        Returns:
            The updated trust score.
        """
        score = self.get_or_create(agent_id)
        now = time.time()

        if success and not security_violation:
            score.tasks_completed += 1
            score.consecutive_successes += 1
        elif not success:
            score.tasks_failed += 1
            score.consecutive_successes = 0
        # success=True but security_violation=True counts the task as done
        # but resets the consecutive streak
        if security_violation:
            score.security_violations += 1
            score.consecutive_successes = 0
            if success:
                score.tasks_completed += 1
            logger.warning(
                "Security violation recorded for agent %s (total: %d)",
                agent_id.replace("\n", ""),
                score.security_violations,
            )

        score.last_updated = now

        if auto_promote:
            ev = evaluator or TrustEvaluator()
            can, _reason = ev.can_promote(score)
            if can:
                ev.promote(score)

        self.save(score)
        return score

    def list_all(self) -> list[AgentTrustScore]:
        """Return all tracked agent trust scores."""
        if not self._trust_dir.exists():
            return []
        scores: list[AgentTrustScore] = []
        for path in sorted(self._trust_dir.glob("*.json")):
            try:
                scores.append(AgentTrustScore.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (KeyError, ValueError) as exc:
                logger.warning("skipping malformed trust file %s: %s", path.name, exc)
        return scores
