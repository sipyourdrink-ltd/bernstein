"""Contextual bandit routing — learns (model, effort) selection from task outcomes.

``BanditRouter`` wraps the existing static cascade routing with a LinUCB
contextual bandit.  During cold-start (fewer than ``warmup_min`` completions),
it delegates to the same static heuristics used by ``CascadeRouter``.  After
warm-up, the LinUCB policy takes over, using the task's feature vector to
select the model that maximises the composite quality-cost reward.

Feature vector (``TaskContext.to_vector()``):
    [complexity_norm, scope_norm, priority_norm, log_repo_size,
     log_est_tokens, bias_term, task_type_one_hot..., language_one_hot...,
     role_embedding_0, ..., role_embedding_7]

Reward signal:
    ``quality_score * (1 - normalized_cost)``
    where ``quality_score ∈ [0, 1]`` (1.0 = janitor passed, 0.0 = failed)
    and ``normalized_cost = min(cost_usd / budget_ceiling, 1.0)``

Policy persistence:
    ``.sdd/routing/policy.json`` and ``.sdd/routing/bandit_state.json``
    survive orchestrator restarts so learning accumulates across runs.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, TypeGuard, cast

from bernstein.core.models import Complexity, Scope, Task, TaskType

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ROLE_HASH_DIM: int = 8
# 6 numeric features + task type one-hot + language one-hot + role embedding
_TASK_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in TaskType)
_LANGUAGE_VALUES: tuple[str, ...] = (
    "python",
    "javascript",
    "typescript",
    "markdown",
    "yaml",
    "json",
    "shell",
    "go",
    "rust",
    "other",
)
FEATURE_SCHEMA_VERSION: int = 2
FEATURE_DIM: int = 6 + len(_TASK_TYPE_VALUES) + len(_LANGUAGE_VALUES) + _ROLE_HASH_DIM

_DEFAULT_ARMS: list[str] = ["haiku", "sonnet", "opus"]
_DEFAULT_ALPHA: float = 0.3
_DEFAULT_WARMUP_MIN: int = 50
_EXPLORATION_HISTORY_LIMIT: int = 100
_POLICY_FORMAT_VERSION: int = 2

# Adapters whose model names match the default bandit arms (haiku/sonnet/opus).
# The bandit router only produces meaningful selections for these adapters;
# for anything else the operator's explicit model config should be used directly.
_CLAUDE_COMPATIBLE_ADAPTERS: frozenset[str] = frozenset({"claude", "claude code", "claude_code", "claude-code"})

# High-stakes roles never start at haiku (mirrors cascade_router logic)
_HIGH_STAKES_ROLES: frozenset[str] = frozenset({"manager", "architect", "security"})
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
    ".mdx": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonl": "json",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".go": "go",
    ".rs": "rust",
}


# ---------------------------------------------------------------------------
# TaskContext
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_OBJ = "dict[str, object]"
_CAST_LIST_OBJ = "list[object]"


@dataclass
class TaskContext:
    """Feature vector extracted from a Task for bandit routing.

    Attributes:
        role: Task role string (e.g. ``"backend"``).
        task_type: Fixed task taxonomy value.
        complexity_tier: Integer encoding of complexity (0=LOW, 1=MEDIUM, 2=HIGH).
        scope_tier: Integer encoding of scope (0=SMALL, 1=MEDIUM, 2=LARGE).
        priority_norm: Normalised priority in [0, 1] (0 = critical, 1 = nice-to-have).
        language: Primary language inferred from owned files.
        repo_size: Repository size signal from metadata, falling back to owned file count.
        estimated_tokens: Rough token estimate (``estimated_minutes * 1000``).
    """

    role: str
    task_type: str
    complexity_tier: int
    scope_tier: int
    priority_norm: float
    language: str
    repo_size: int
    estimated_tokens: float

    @classmethod
    def from_task(cls, task: Task) -> TaskContext:
        """Extract a feature context from a Task.

        Args:
            task: Task to extract features from.

        Returns:
            Populated ``TaskContext``.
        """
        complexity_map = {Complexity.LOW: 0, Complexity.MEDIUM: 1, Complexity.HIGH: 2}
        scope_map = {Scope.SMALL: 0, Scope.MEDIUM: 1, Scope.LARGE: 2}
        priority_norm = max(0.0, min(1.0, (task.priority - 1) / 2.0))
        return cls(
            role=task.role,
            task_type=task.task_type.value,
            complexity_tier=complexity_map.get(task.complexity, 1),
            scope_tier=scope_map.get(task.scope, 1),
            priority_norm=priority_norm,
            language=_infer_primary_language(task.owned_files),
            repo_size=_repo_size_signal(task),
            estimated_tokens=float(task.estimated_minutes * 1_000),
        )

    def to_vector(self) -> list[float]:
        """Convert to a normalised float feature vector of length ``FEATURE_DIM``.

        Returns:
            List of floats suitable for LinUCB matrix operations.
        """
        numeric: list[float] = [
            self.complexity_tier / 2.0,  # [0] complexity
            self.scope_tier / 2.0,  # [1] scope
            self.priority_norm,  # [2] priority
            math.log10(self.repo_size + 1.0) / 6.0,  # [3] repo size (log-scaled)
            math.log1p(self.estimated_tokens) / 15.0,  # [4] token estimate (log-scaled)
            1.0,  # [5] bias term
        ]
        task_type = _one_hot(self.task_type, _TASK_TYPE_VALUES, fallback=TaskType.STANDARD.value)
        language = _one_hot(self.language, _LANGUAGE_VALUES, fallback="other")
        role_embed = _hash_role(self.role, _ROLE_HASH_DIM)
        return numeric + task_type + language + role_embed


def _one_hot(value: str, choices: tuple[str, ...], fallback: str) -> list[float]:
    """Encode ``value`` as one-hot over ``choices`` with a deterministic fallback."""
    selected = value if value in choices else fallback
    return [1.0 if choice == selected else 0.0 for choice in choices]


def _infer_primary_language(owned_files: list[str]) -> str:
    """Infer the dominant language from owned file extensions."""
    counts: Counter[str] = Counter()
    for raw_path in owned_files:
        lower_path = raw_path.lower()
        language = "other"
        for suffix, candidate in _LANGUAGE_BY_SUFFIX.items():
            if lower_path.endswith(suffix):
                language = candidate
                break
        counts[language] += 1

    if not counts:
        return "other"
    return max(_LANGUAGE_VALUES, key=lambda language: (counts.get(language, 0), -_LANGUAGE_VALUES.index(language)))


def _repo_size_signal(task: Task) -> int:
    """Return a non-negative repo-size signal from task metadata or owned files."""
    for key in ("repo_size", "repo_file_count", "repository_file_count"):
        raw_value = task.metadata.get(key)
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, int | float):
            return max(0, int(raw_value))
        if isinstance(raw_value, str) and raw_value.isdecimal():
            return int(raw_value)
    return len(task.owned_files)


def _hash_role(role: str, dim: int) -> list[float]:
    """Hash a role string to a fixed-dimensional float vector in ``[-1, 1]``.

    Uses a seeded PRNG so the same role always produces the same embedding.

    Args:
        role: Role name string.
        dim: Output dimensionality.

    Returns:
        List of ``dim`` floats in ``[-1, 1]``.
    """
    seed = int.from_bytes(sha256(role.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


# ---------------------------------------------------------------------------
# Linear algebra helpers (pure Python — no numpy dependency)
# ---------------------------------------------------------------------------


def _identity(d: int) -> list[list[float]]:
    """Return a d x d identity matrix."""
    return [[1.0 if i == j else 0.0 for j in range(d)] for i in range(d)]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two equal-length vectors."""
    return sum(x * y for x, y in zip(a, b, strict=False))


def _matmul_vec(mat: list[list[float]], v: list[float]) -> list[float]:
    """Multiply matrix ``mat`` (n x d) by column vector ``v`` (d,) -> result (n,)."""
    return [_dot(row, v) for row in mat]


def _inv(mat: list[list[float]]) -> list[list[float]]:
    """Invert square matrix ``mat`` via Gauss-Jordan elimination.

    Falls back to the identity matrix if ``mat`` is singular (shouldn't happen
    in practice once the diagonal stays >= 1 due to the identity init).

    Args:
        mat: Square n x n matrix.

    Returns:
        Inverse of ``mat``, or identity on failure.
    """
    n = len(mat)
    # Augment [mat | I]
    aug: list[list[float]] = [
        [mat[i][j] for j in range(n)] + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)
    ]
    for col in range(n):
        # Partial pivoting
        pivot_row: int | None = None
        for row in range(col, n):
            if abs(aug[row][col]) > 1e-12:
                pivot_row = row
                break
        if pivot_row is None:
            logger.warning("BanditPolicy: singular matrix encountered, returning identity")
            return _identity(n)
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        scale = aug[col][col]
        aug[col] = [v / scale for v in aug[col]]
        for row in range(n):
            if row != col:
                factor = aug[row][col]
                aug[row] = [aug[row][k] - factor * aug[col][k] for k in range(2 * n)]
    return [[aug[i][n + j] for j in range(n)] for i in range(n)]


def _sherman_morrison_update(mat_inv: list[list[float]], x: list[float]) -> list[list[float]]:
    """Rank-1 inverse update for ``A + x x^T``.

    Args:
        mat_inv: Current inverse matrix.
        x: Feature vector.

    Returns:
        Updated inverse matrix.
    """
    mat_x = _matmul_vec(mat_inv, x)
    denom = 1.0 + _dot(x, mat_x)
    if abs(denom) <= 1e-12:
        logger.warning("BanditPolicy: Sherman-Morrison denominator too small, recomputing inverse")
        d = len(mat_inv)
        recovered = _inv(mat_inv)
        for i in range(d):
            for j in range(d):
                recovered[i][j] += x[i] * x[j]
        return _inv(recovered)

    updated: list[list[float]] = []
    for i, row in enumerate(mat_inv):
        updated.append([value - (mat_x[i] * mat_x[j]) / denom for j, value in enumerate(row)])
    return updated


def _load_arms(value: object, fallback: list[str]) -> list[str]:
    """Load model arms from JSON, falling back when the payload is invalid."""
    if not isinstance(value, list):
        return list(fallback)
    raw_items = cast(_CAST_LIST_OBJ, value)
    arms = [item for item in raw_items if isinstance(item, str) and item]
    return arms or list(fallback)


def _is_vector(value: object, dim: int) -> TypeGuard[list[float]]:
    """Return whether ``value`` is a numeric vector of length ``dim``."""
    if not isinstance(value, list):
        return False
    raw_items = cast(_CAST_LIST_OBJ, value)
    return len(raw_items) == dim and all(isinstance(item, int | float) for item in raw_items)


def _is_matrix(value: object, dim: int) -> TypeGuard[list[list[float]]]:
    """Return whether ``value`` is a numeric ``dim`` x ``dim`` matrix."""
    if not isinstance(value, list):
        return False
    raw_rows = cast(_CAST_LIST_OBJ, value)
    return len(raw_rows) == dim and all(_is_vector(row, dim) for row in raw_rows)


def _coerce_int_mapping(value: object) -> dict[str, int]:
    """Coerce a JSON mapping into ``dict[str, int]``."""
    if not isinstance(value, dict):
        return {}
    raw_mapping = cast(_CAST_DICT_STR_OBJ, value)
    coerced: dict[str, int] = {}
    for key, raw_item in raw_mapping.items():
        if isinstance(raw_item, bool):
            continue
        if isinstance(raw_item, int | float | str):
            with_value = int(float(raw_item))
            coerced[str(key)] = with_value
    return coerced


def _load_exploration_history(value: object, arms: list[str]) -> dict[str, list[float]]:
    """Load persisted exploration history with per-arm bounded windows."""
    history: dict[str, list[float]] = {arm: [] for arm in arms}
    if not isinstance(value, dict):
        return history
    raw_mapping = cast(_CAST_DICT_STR_OBJ, value)
    for arm in arms:
        raw_samples: object = raw_mapping.get(arm, [])
        if not isinstance(raw_samples, list):
            continue
        samples: list[float] = [float(v) for v in cast(_CAST_LIST_OBJ, raw_samples) if isinstance(v, (int, float))]
        history[arm] = samples[-_EXPLORATION_HISTORY_LIMIT:]
    return history


def _load_shadow_pending(value: object) -> dict[str, dict[str, Any]]:
    """Load pending shadow decisions keyed by task ID."""
    if not isinstance(value, dict):
        return {}
    raw_mapping = cast(_CAST_DICT_STR_OBJ, value)
    pending: dict[str, dict[str, Any]] = {}
    for task_id, raw_payload in raw_mapping.items():
        if isinstance(raw_payload, dict):
            pending[str(task_id)] = cast("dict[str, Any]", raw_payload)
    return pending


def _load_shadow_counters(value: object) -> dict[str, float]:
    """Load shadow analytics counters from persisted state."""
    defaults = {
        "total_decisions": 0.0,
        "matched_outcomes": 0.0,
        "agreement_count": 0.0,
        "disagreement_count": 0.0,
        "agree_reward_sum": 0.0,
        "agree_reward_count": 0.0,
        "disagree_reward_sum": 0.0,
        "disagree_reward_count": 0.0,
    }
    if not isinstance(value, dict):
        return defaults
    raw_mapping = cast(_CAST_DICT_STR_OBJ, value)
    for key in tuple(defaults):
        raw_item = raw_mapping.get(key)
        if isinstance(raw_item, bool):
            continue
        if isinstance(raw_item, int | float | str):
            defaults[key] = float(raw_item)
    return defaults


def _arm_score_payload(score: ArmScore | None) -> dict[str, float] | None:
    """Serialize an arm score for JSON output."""
    if score is None:
        return None
    return {
        "exploit": round(score.exploit, 6),
        "explore": round(score.explore, 6),
        "total": round(score.total, 6),
    }


# ---------------------------------------------------------------------------
# BanditPolicy (LinUCB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmScore:
    """LinUCB score components for a candidate model arm."""

    arm: str
    exploit: float
    explore: float
    total: float


def _schema_version_matches(raw_data: dict[str, object], path: Path) -> bool:
    """Check if stored schema version and feature dim match current values."""
    stored_schema_version = raw_data.get("feature_schema_version")
    stored_feature_dim = raw_data.get("feature_dim")
    if stored_schema_version != FEATURE_SCHEMA_VERSION or stored_feature_dim != FEATURE_DIM:
        logger.info(
            "BanditPolicy: resetting %s because feature schema changed (stored=%s/%s current=%s/%s)",
            path,
            stored_schema_version,
            stored_feature_dim,
            FEATURE_SCHEMA_VERSION,
            FEATURE_DIM,
        )
        return False
    return True


def _validate_raw_matrices(
    raw_data: dict[str, object],
    path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]] | None:
    """Validate and extract raw matrix dicts from policy data.

    Returns (raw_inv_by_arm, raw_mat_by_arm, raw_vec_by_arm) or None on invalid data.
    """
    raw_inv = raw_data.get("A_inv")
    raw_mat = raw_data.get("A")
    raw_vec = raw_data.get("b", {})

    if raw_inv is None and raw_mat is None:
        logger.info("BanditPolicy: resetting %s because policy matrices are missing", path)
        return None
    if raw_inv is not None and not isinstance(raw_inv, dict):
        logger.info("BanditPolicy: resetting %s because inverse matrices are invalid", path)
        return None
    if raw_mat is not None and not isinstance(raw_mat, dict):
        logger.info("BanditPolicy: resetting %s because legacy matrices are invalid", path)
        return None
    if not isinstance(raw_vec, dict):
        logger.info("BanditPolicy: resetting %s because policy matrices are invalid", path)
        return None

    return (
        cast(_CAST_DICT_STR_OBJ, raw_inv or {}),
        cast(_CAST_DICT_STR_OBJ, raw_mat or {}),
        cast(_CAST_DICT_STR_OBJ, raw_vec),
    )


def _load_arm_matrices(
    raw_data: dict[str, object],
    arm_list: list[str],
    path: Path,
) -> tuple[dict[str, list[list[float]]], dict[str, list[float]], bool] | None:
    """Load per-arm matrices from raw policy data.

    Returns (loaded_A_inv, loaded_b, legacy_loaded) or None on validation failure.
    """
    validated = _validate_raw_matrices(raw_data, path)
    if validated is None:
        return None

    raw_inv_by_arm, raw_mat_by_arm, raw_vec_by_arm = validated
    loaded_inv: dict[str, list[list[float]]] = {}
    loaded_vec: dict[str, list[float]] = {}
    legacy_loaded = False

    for arm in arm_list:
        raw_matrix = raw_inv_by_arm.get(arm)
        raw_vector = raw_vec_by_arm.get(arm)
        if raw_matrix is None:
            raw_matrix = raw_mat_by_arm.get(arm)
            if raw_matrix is not None:
                legacy_loaded = True
        if not _is_matrix(raw_matrix, FEATURE_DIM) or not _is_vector(raw_vector, FEATURE_DIM):
            logger.info("BanditPolicy: resetting %s because arm %s has incompatible dimensions", path, arm)
            return None
        matrix = [[float(value) for value in row] for row in raw_matrix]
        loaded_inv[arm] = matrix if arm in raw_inv_by_arm else _inv(matrix)
        loaded_vec[arm] = [float(value) for value in raw_vector]

    return loaded_inv, loaded_vec, legacy_loaded


class BanditPolicy:
    """LinUCB contextual bandit for model selection.

    Each arm corresponds to a model name (e.g. ``"haiku"``, ``"sonnet"``).
    After each task completion, the arm that was used is updated with the
    observed reward.  The UCB score balances exploitation (high mean reward)
    with exploration (uncertainty in the estimate).

    LinUCB update rules per arm ``a``::

        A_a = A_a + x_t * x_t.T
        b_a = b_a + r_t * x_t
        theta_a = inv(A_a) * b_a
        score_a = theta_a.T * x + alpha * sqrt(x.T * inv(A_a) * x)

    Args:
        arms: List of model names to consider.
        alpha: Exploration parameter.  Higher means more exploration.
    """

    def __init__(self, arms: list[str], alpha: float = _DEFAULT_ALPHA) -> None:
        d = FEATURE_DIM
        self.arms = list(arms)
        self.alpha = alpha
        self.total_updates: int = 0
        # Per-arm matrices: A_a^-1 (d x d) and b_a (d,)
        self._A_inv: dict[str, list[list[float]]] = {arm: _identity(d) for arm in self.arms}
        self._b: dict[str, list[float]] = {arm: [0.0] * d for arm in self.arms}

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def select(self, context: TaskContext) -> str:
        """Select the arm with the highest UCB score for this context.

        Args:
            context: Feature vector for the current task.

        Returns:
            Model name of the selected arm.
        """
        return self.score(context)[0].arm

    def score(self, context: TaskContext) -> list[ArmScore]:
        """Return LinUCB score components for each arm, best first.

        Args:
            context: Feature vector for the current task.

        Returns:
            Candidate arm scores sorted by total score descending.
        """
        x = context.to_vector()
        scores: list[ArmScore] = []

        for arm in self.arms:
            arm_inv = self._A_inv[arm]
            theta = _matmul_vec(arm_inv, self._b[arm])
            exploit = _dot(theta, x)
            variance = _dot(x, _matmul_vec(arm_inv, x))
            explore = self.alpha * math.sqrt(max(0.0, variance))
            scores.append(ArmScore(arm=arm, exploit=exploit, explore=explore, total=exploit + explore))

        return sorted(scores, key=lambda score: score.total, reverse=True)

    def update(self, arm: str, context: TaskContext, reward: float) -> None:
        """Update the arm's matrices with an observed reward.

        Args:
            arm: Model name that was used.
            context: Feature vector for the task.
            reward: Observed reward in ``[0, 1]``.
        """
        x = context.to_vector()
        # Lazily initialise arms not present at construction time
        if arm not in self._A_inv:
            d = FEATURE_DIM
            self.arms.append(arm)
            self._A_inv[arm] = _identity(d)
            self._b[arm] = [0.0] * d

        current_inv = self._A_inv[arm]
        b = self._b[arm]
        self._A_inv[arm] = _sherman_morrison_update(current_inv, x)
        for i in range(len(x)):
            b[i] += reward * x[i]

        self.total_updates += 1

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist policy matrices to JSON.

        Args:
            path: Destination file path (parent directories are created).
        """
        data: dict[str, Any] = {
            "arms": self.arms,
            "alpha": self.alpha,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_dim": FEATURE_DIM,
            "policy_format_version": _POLICY_FORMAT_VERSION,
            "matrix_storage": "A_inv",
            "total_updates": self.total_updates,
            "A_inv": self._A_inv,
            "b": self._b,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        logger.debug("BanditPolicy: saved to %s (total_updates=%d)", path, self.total_updates)

    @classmethod
    def load(cls, path: Path, arms: list[str] | None = None) -> BanditPolicy:
        """Load a policy from JSON, or return a fresh instance on error.

        Args:
            path: JSON file to load.
            arms: Fallback arm list if file is missing or corrupt.

        Returns:
            Loaded (or fresh) ``BanditPolicy``.
        """
        default_arms = arms or list(_DEFAULT_ARMS)
        if not path.exists():
            return cls(arms=default_arms)
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return cls(arms=default_arms)
            raw_data = cast(_CAST_DICT_STR_OBJ, data)
            if not _schema_version_matches(raw_data, path):
                return cls(arms=default_arms)
            raw_alpha = raw_data.get("alpha", _DEFAULT_ALPHA)
            raw_total_updates = raw_data.get("total_updates", 0)
            policy = cls(
                arms=_load_arms(raw_data.get("arms"), default_arms),
                alpha=float(raw_alpha) if isinstance(raw_alpha, int | float | str) else _DEFAULT_ALPHA,
            )
            policy.total_updates = int(raw_total_updates) if isinstance(raw_total_updates, int | float | str) else 0
            result = _load_arm_matrices(raw_data, policy.arms, path)
            if result is None:
                return cls(arms=default_arms)
            loaded_inv, loaded_vec, legacy_loaded = result
            policy._A_inv.clear()
            policy._A_inv.update(loaded_inv)
            policy._b.clear()
            policy._b.update(loaded_vec)
            if legacy_loaded:
                try:
                    policy.save(path)
                except OSError as exc:
                    logger.warning("BanditPolicy: could not rewrite legacy policy format at %s: %s", path, exc)
            return policy
        except Exception as exc:
            logger.warning("BanditPolicy: could not load from %s: %s — starting fresh", path, exc)
            return cls(arms=default_arms)


# ---------------------------------------------------------------------------
# BanditRoutingDecision
# ---------------------------------------------------------------------------


@dataclass
class BanditRoutingDecision:
    """Result of ``BanditRouter.select()``.

    Attributes:
        model: Model name (e.g. ``"haiku"``).
        effort: Effort level (e.g. ``"low"``, ``"high"``, ``"max"``).
        from_bandit: ``True`` when the LinUCB policy made the selection;
            ``False`` during cold-start (static routing).
        reason: Human-readable explanation of the routing decision.
        estimated_cost_usd: Rough per-task cost estimate.
    """

    model: str
    effort: str
    from_bandit: bool
    reason: str
    estimated_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# BanditRouter
# ---------------------------------------------------------------------------


class BanditRouter:
    """Contextual bandit model router with cold-start static fallback.

    During cold-start (fewer than ``warmup_min`` completions), delegates to
    the same static heuristics used by the cascade router: high-stakes roles
    start at ``sonnet``, everything else starts at ``haiku``.

    After warm-up, uses a ``BanditPolicy`` (LinUCB) to select the model that
    historically maximises ``quality_score * (1 - normalized_cost)`` for the
    given task context.

    Policy state is persisted to ``policy_dir/policy.json`` (LinUCB matrices)
    and ``policy_dir/bandit_state.json`` (completion counts) so learning
    accumulates across orchestrator restarts.

    Usage::

        router = BanditRouter(
            warmup_min=50,
            policy_dir=workdir / ".sdd" / "routing",
        )

        # Before spawning:
        decision = router.select(task)
        # spawn with decision.model / decision.effort …

        # After task completes:
        router.record_outcome(
            task=task,
            model=decision.model,
            effort=decision.effort,
            cost_usd=actual_cost,
            quality_score=1.0 if janitor_passed else 0.0,
        )
        router.save()
    """

    POLICY_FILE = "policy.json"
    STATE_FILE = "bandit_state.json"
    SHADOW_DECISIONS_FILE = "shadow_decisions.jsonl"
    SHADOW_OUTCOMES_FILE = "shadow_outcomes.jsonl"

    def __init__(
        self,
        warmup_min: int = _DEFAULT_WARMUP_MIN,
        arms: list[str] | None = None,
        policy_dir: Path | None = None,
        alpha: float = _DEFAULT_ALPHA,
    ) -> None:
        self._warmup_min = warmup_min
        self._arms = arms or list(_DEFAULT_ARMS)
        self._policy_dir = policy_dir
        self._alpha = alpha
        self._policy: BanditPolicy | None = None
        self._total_completions: int = 0
        self._selection_counts: dict[str, int] = {}
        self._exploration_history: dict[str, list[float]] = {}
        self._shadow_pending: dict[str, dict[str, Any]] = {}
        self._shadow_counters: dict[str, float] = {
            "total_decisions": 0.0,
            "matched_outcomes": 0.0,
            "agreement_count": 0.0,
            "disagreement_count": 0.0,
            "agree_reward_sum": 0.0,
            "agree_reward_count": 0.0,
            "disagree_reward_sum": 0.0,
            "disagree_reward_count": 0.0,
        }
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @staticmethod
    def router_applicable(adapter_name: str) -> bool:
        """Return whether this router's arms are valid for the given adapter.

        The bandit router's arms (haiku/sonnet/opus) are Claude-specific model
        names.  For non-Claude adapters (qwen, gemini, codex, etc.) the router
        cannot produce a meaningful model selection and should be skipped.

        Args:
            adapter_name: Name returned by ``adapter.name()`` or the ``cli``
                value from ``role_model_policy``.

        Returns:
            ``True`` when the router can route for this adapter.
        """
        return adapter_name.lower().strip() in _CLAUDE_COMPATIBLE_ADAPTERS

    @property
    def total_completions(self) -> int:
        """Number of reward observations recorded (across restarts if persisted)."""
        self._ensure_loaded()
        return self._total_completions

    @property
    def is_warmed_up(self) -> bool:
        """``True`` once the policy has seen enough completions to take over routing."""
        return self.total_completions >= self._warmup_min

    @property
    def exploration_rate(self) -> float:
        """Effective exploration rate.

        Zero during cold-start (static routing has no notion of exploration).
        Decays as ``alpha / sqrt(completions)`` after warm-up.
        """
        if not self.is_warmed_up:
            return 0.0
        return self._alpha / math.sqrt(max(1, self._total_completions))

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def select(self, task: Task) -> BanditRoutingDecision:
        """Select ``(model, effort)`` for a task.

        Falls back to static cascade routing during cold-start; uses the
        LinUCB bandit policy after warm-up.

        Args:
            task: Task to route.

        Returns:
            ``BanditRoutingDecision`` with model, effort, and provenance.
        """
        self._ensure_loaded()
        ctx = TaskContext.from_task(task)

        if not self.is_warmed_up or _is_high_stakes(task):
            model, static_reason = _static_select(task)
            effort = _effort_for_task(model, task)
            mode = "guardrail" if self.is_warmed_up else "cold-start"
            decision = BanditRoutingDecision(
                model=model,
                effort=effort,
                from_bandit=False,
                reason=(f"{mode} ({self._total_completions}/{self._warmup_min} completions): {static_reason}"),
            )
        else:
            assert self._policy is not None
            scores = self._policy.score(ctx)
            self._record_exploration_scores(scores)
            best_score = scores[0]
            runner_up = scores[1] if len(scores) > 1 else None
            model = best_score.arm
            effort = _effort_for_task(model, task)
            runner_reason = f"; runner_up={runner_up.arm} total={runner_up.total:.3f}" if runner_up is not None else ""
            decision = BanditRoutingDecision(
                model=model,
                effort=effort,
                from_bandit=True,
                reason=(
                    f"bandit: LinUCB selected {model!r} "
                    f"(exploit={best_score.exploit:.3f}, explore={best_score.explore:.3f}, "
                    f"total={best_score.total:.3f}{runner_reason}, "
                    f"completions={self._total_completions})"
                ),
            )

        self._selection_counts[model] = self._selection_counts.get(model, 0) + 1
        logger.debug("BanditRouter.select: task=%s → %s (%s)", task.id, model, decision.reason)
        return decision

    def record_outcome(
        self,
        task: Task,
        model: str,
        effort: str,
        cost_usd: float,
        quality_score: float,
        budget_ceiling: float = 1.0,
    ) -> None:
        """Record a task completion and feed the reward back to the bandit policy.

        Args:
            task: The completed task.
            model: Model name that was used (arm to update).
            effort: Effort level that was used.
            cost_usd: Actual USD cost incurred.
            quality_score: Quality of output in ``[0, 1]`` (1.0 = janitor passed).
            budget_ceiling: Per-task budget ceiling for cost normalisation.
        """
        self._ensure_loaded()
        reward = compute_reward(
            quality_score=quality_score,
            cost_usd=cost_usd,
            budget_ceiling=budget_ceiling,
        )
        ctx = TaskContext.from_task(task)
        assert self._policy is not None
        self._policy.update(arm=model, context=ctx, reward=reward)
        self._total_completions += 1
        self._record_shadow_outcome(
            task_id=task.id,
            reward=reward,
            quality_score=quality_score,
            cost_usd=cost_usd,
            model=model,
            effort=effort,
        )
        logger.debug(
            "BanditRouter.record_outcome: task=%s model=%s reward=%.3f quality=%.2f cost=%.5f total=%d",
            task.id,
            model,
            reward,
            quality_score,
            cost_usd,
            self._total_completions,
        )

    def selection_frequency(self) -> dict[str, int]:
        """Return a snapshot of how many times each model has been selected.

        Returns:
            Dict mapping model name → selection count.
        """
        self._ensure_loaded()
        return dict(self._selection_counts)

    def save(self) -> None:
        """Persist policy matrices and state to disk.

        No-op if no ``policy_dir`` was provided.
        """
        self._ensure_loaded()
        if self._policy_dir is None:
            return
        assert self._policy is not None
        self._policy.save(self._policy_dir / self.POLICY_FILE)
        state_path = self._policy_dir / self.STATE_FILE
        try:
            self._policy_dir.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "total_completions": self._total_completions,
                        "selection_counts": self._selection_counts,
                        "mode": "bandit" if self.is_warmed_up else "cold-start",
                        "warmup_min": self._warmup_min,
                        "exploration_rate": round(self.exploration_rate, 6),
                        "exploration_history": self._exploration_history,
                        "exploration_stats": self._exploration_stats(),
                        "shadow_pending": self._shadow_pending,
                        "shadow_counters": self._shadow_counters,
                        "shadow_stats": self._shadow_stats(),
                        "saved_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("BanditRouter: could not save state to %s: %s", state_path, exc)

    def record_shadow_decision(
        self,
        task: Task,
        decision: BanditRoutingDecision,
        executed_model: str,
        executed_effort: str,
    ) -> None:
        """Append a shadow-routing decision without changing live routing.

        Args:
            task: Task evaluated by the bandit.
            decision: Bandit decision that would have been used.
            executed_model: Model that the static live route actually used.
            executed_effort: Effort that the static live route actually used.
        """
        self._ensure_loaded()
        if self._policy_dir is None:
            return
        score_map = self._score_map(task)
        selected_score = score_map.get(decision.model)
        executed_score = score_map.get(executed_model)
        agreement = decision.model == executed_model and decision.effort == executed_effort
        payload = {
            "timestamp": time.time(),
            "task_id": task.id,
            "role": task.role,
            "task_type": task.task_type.value,
            "selected_model": decision.model,
            "selected_effort": decision.effort,
            "executed_model": executed_model,
            "executed_effort": executed_effort,
            "from_bandit": decision.from_bandit,
            "reason": decision.reason,
            "agreement": agreement,
            "selected_score": _arm_score_payload(selected_score),
            "executed_score": _arm_score_payload(executed_score),
        }
        self._shadow_pending[task.id] = dict(payload)
        self._shadow_counters["total_decisions"] += 1.0
        shadow_path = self._policy_dir / self.SHADOW_DECISIONS_FILE
        try:
            self._policy_dir.mkdir(parents=True, exist_ok=True)
            with shadow_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except OSError as exc:
            logger.warning("BanditRouter: could not record shadow decision to %s: %s", shadow_path, exc)
        self.save()

    def summary(self) -> dict[str, Any]:
        """Return a dict suitable for dashboard display.

        Returns:
            Dict with ``mode``, ``total_completions``, ``warmup_min``,
            ``exploration_rate``, and ``selection_frequency``.
        """
        self._ensure_loaded()
        return {
            "mode": "bandit (LinUCB)" if self.is_warmed_up else "cold-start (static)",
            "total_completions": self._total_completions,
            "warmup_min": self._warmup_min,
            "exploration_rate": round(self.exploration_rate, 4),
            "selection_frequency": dict(self._selection_counts),
            "exploration_stats": self._exploration_stats(),
            "shadow_stats": self._shadow_stats(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Lazy-load policy and state from disk on first access."""
        if self._loaded:
            return
        self._loaded = True

        if self._policy_dir is not None:
            self._policy = BanditPolicy.load(
                self._policy_dir / self.POLICY_FILE,
                arms=self._arms,
            )
            state_path = self._policy_dir / self.STATE_FILE
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self._total_completions = int(state.get("total_completions", 0))
                    self._selection_counts = _coerce_int_mapping(state.get("selection_counts", {}))
                    self._exploration_history = _load_exploration_history(
                        state.get("exploration_history", {}),
                        self._policy.arms,
                    )
                    self._shadow_pending = _load_shadow_pending(state.get("shadow_pending", {}))
                    self._shadow_counters = _load_shadow_counters(state.get("shadow_counters", {}))
                except Exception as exc:
                    logger.warning("BanditRouter: could not load state from %s: %s", state_path, exc)
            else:
                # Infer completions from policy total_updates (backward compat)
                self._total_completions = self._policy.total_updates
            for arm in self._policy.arms:
                self._selection_counts.setdefault(arm, 0)
                self._exploration_history.setdefault(arm, [])
        else:
            self._policy = BanditPolicy(arms=self._arms, alpha=self._alpha)
            for arm in self._policy.arms:
                self._selection_counts.setdefault(arm, 0)
                self._exploration_history.setdefault(arm, [])

    def _score_map(self, task: Task) -> dict[str, ArmScore]:
        """Return current arm scores keyed by model for a task context."""
        if self._policy is None:
            return {}
        return {score.arm: score for score in self._policy.score(TaskContext.from_task(task))}

    def _record_exploration_scores(self, scores: list[ArmScore]) -> None:
        """Persist a bounded exploration-bonus history per arm."""
        for score in scores:
            history = self._exploration_history.setdefault(score.arm, [])
            history.append(score.explore)
            if len(history) > _EXPLORATION_HISTORY_LIMIT:
                del history[:-_EXPLORATION_HISTORY_LIMIT]

    def _exploration_stats(self) -> dict[str, dict[str, float | int]]:
        """Return per-arm exploration aggregates for observability."""
        stats: dict[str, dict[str, float | int]] = {}
        arms = self._policy.arms if self._policy is not None else self._arms
        for arm in arms:
            history = self._exploration_history.get(arm, [])
            if not history:
                stats[arm] = {"samples": 0, "last": 0.0, "mean": 0.0, "variance": 0.0}
                continue
            mean = sum(history) / len(history)
            variance = sum((value - mean) ** 2 for value in history) / len(history)
            stats[arm] = {
                "samples": len(history),
                "last": round(history[-1], 6),
                "mean": round(mean, 6),
                "variance": round(variance, 6),
            }
        return stats

    def _shadow_stats(self) -> dict[str, float | int]:
        """Return aggregated observed shadow-routing statistics."""
        total_decisions = int(self._shadow_counters["total_decisions"])
        matched_outcomes = int(self._shadow_counters["matched_outcomes"])
        agreement_count = int(self._shadow_counters["agreement_count"])
        disagreement_count = int(self._shadow_counters["disagreement_count"])
        agree_reward_count = int(self._shadow_counters["agree_reward_count"])
        disagree_reward_count = int(self._shadow_counters["disagree_reward_count"])
        return {
            "total_decisions": total_decisions,
            "matched_outcomes": matched_outcomes,
            "pending_outcomes": len(self._shadow_pending),
            "agreement_rate": round(agreement_count / matched_outcomes, 6) if matched_outcomes else 0.0,
            "disagreement_count": disagreement_count,
            "avg_executed_reward_when_agree": round(
                self._shadow_counters["agree_reward_sum"] / agree_reward_count,
                6,
            )
            if agree_reward_count
            else 0.0,
            "avg_executed_reward_when_disagree": round(
                self._shadow_counters["disagree_reward_sum"] / disagree_reward_count,
                6,
            )
            if disagree_reward_count
            else 0.0,
        }

    def _record_shadow_outcome(
        self,
        *,
        task_id: str,
        reward: float,
        quality_score: float,
        cost_usd: float,
        model: str,
        effort: str,
    ) -> None:
        """Match a completion with its shadow decision and persist observed outcome."""
        if self._policy_dir is None:
            return
        shadow = self._shadow_pending.pop(task_id, None)
        if shadow is None:
            return
        agreement = bool(shadow.get("agreement", False))
        self._shadow_counters["matched_outcomes"] += 1.0
        if agreement:
            self._shadow_counters["agreement_count"] += 1.0
            self._shadow_counters["agree_reward_sum"] += reward
            self._shadow_counters["agree_reward_count"] += 1.0
        else:
            self._shadow_counters["disagreement_count"] += 1.0
            self._shadow_counters["disagree_reward_sum"] += reward
            self._shadow_counters["disagree_reward_count"] += 1.0

        payload = {
            **shadow,
            "completed_at": time.time(),
            "observed_reward": round(reward, 6),
            "observed_quality_score": round(quality_score, 6),
            "observed_cost_usd": round(cost_usd, 6),
            "executed_model": model,
            "executed_effort": effort,
        }
        outcome_path = self._policy_dir / self.SHADOW_OUTCOMES_FILE
        try:
            self._policy_dir.mkdir(parents=True, exist_ok=True)
            with outcome_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except OSError as exc:
            logger.warning("BanditRouter: could not record shadow outcome to %s: %s", outcome_path, exc)


# ---------------------------------------------------------------------------
# Reward function
# ---------------------------------------------------------------------------


def compute_reward(quality_score: float, cost_usd: float, budget_ceiling: float) -> float:
    """Composite reward for a task completion: ``quality * (1 - normalized_cost)``.

    Args:
        quality_score: Quality of task output in ``[0, 1]``.  Typically 1.0
            when the janitor passes, 0.0 on failure, or a judge score.
        cost_usd: Actual USD cost incurred for the task.
        budget_ceiling: Per-task budget ceiling for cost normalisation.
            Pass ``0.0`` or negative to skip cost normalisation (reward = quality).

    Returns:
        Composite reward in ``[0, 1]``.
    """
    quality = max(0.0, min(1.0, quality_score))
    if budget_ceiling <= 0.0:
        return quality
    norm_cost = max(0.0, min(1.0, cost_usd / budget_ceiling))
    return quality * (1.0 - norm_cost)


# ---------------------------------------------------------------------------
# Static routing helpers (cold-start fallback)
# ---------------------------------------------------------------------------


def _is_high_stakes(task: Task) -> bool:
    """Return whether static guardrails should override learned routing."""
    return (
        task.role in _HIGH_STAKES_ROLES
        or task.complexity == Complexity.HIGH
        or task.scope == Scope.LARGE
        or task.priority == 1
    )


def _static_select(task: Task) -> tuple[str, str]:
    """Static initial model selection (mirrors ``CascadeRouter._select_initial_model``).

    Args:
        task: Task to route.

    Returns:
        Tuple of ``(model_name, reason_string)``.
    """
    # High-stakes: start at sonnet (never haiku)
    if _is_high_stakes(task):
        return "sonnet", f"high-stakes (role={task.role!r})"

    # Manager-specified model override
    if task.model and task.model.lower() in _DEFAULT_ARMS:
        return task.model.lower(), "manager override"

    return "haiku", "cheapest viable model"


def _effort_for_task(model: str, task: Task) -> str:
    """Select an effort level appropriate for the model and task.

    Args:
        model: Model name (e.g. ``"haiku"``, ``"sonnet"``, ``"opus"``).
        task: Task (checked for a manager-specified effort override).

    Returns:
        Effort string (e.g. ``"low"``, ``"high"``, ``"max"``).
    """
    if task.effort:
        return task.effort
    model_lower = model.lower()
    if "opus" in model_lower:
        return "max"
    if "haiku" in model_lower:
        return "low"
    return "high"
