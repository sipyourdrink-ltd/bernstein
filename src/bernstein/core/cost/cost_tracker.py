"""Per-run cost budget tracker.

Tracks cumulative token usage and cost per orchestrator run.  Emits
warnings at configurable thresholds (80% / 95% / 100%) and tells the
orchestrator when to stop spawning agents.

Cost data is persisted to ``.sdd/runtime/costs/{run_id}.json`` so that
the ``GET /costs/{run_id}`` endpoint and the CLI can report budget
status for any run, even after restart.

This module is about *budget enforcement*.  Model selection / ROI
optimization lives in ``cost.py`` - do not conflate the two.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.cost.cost import _MODEL_COST_USD_PER_1K  # pyright: ignore[reportPrivateUsage]
from bernstein.core.models import (
    AgentCostSummary,
    ModelCostBreakdown,
    RunCostProjection,
    RunCostReport,
)
from bernstein.core.tenanting import normalize_tenant_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Run-level budget cap (cost autopilot)
# ---------------------------------------------------------------------------

# Env var honoured by the orchestrator startup path so a CLI flag
# (``bernstein run --max-cost-usd N``) can override seed/run-config
# defaults without a YAML edit. Off-by-default: when unset, behaviour
# is identical to prior releases.
ENV_MAX_COST_USD: str = "BERNSTEIN_MAX_COST_USD"


def resolve_run_budget_usd(
    *,
    run_config_value: float | None = None,
    seed_value: float | None = None,
    env: dict[str, str] | None = None,
    default: float = 0.0,
) -> float:
    """Resolve the per-run USD budget cap from layered sources.

    Precedence (highest first):
      1. ``BERNSTEIN_MAX_COST_USD`` env var (CLI flag propagation).
      2. ``run_config_value`` (``.sdd/runtime/run_config.json``).
      3. ``seed_value`` (``bernstein.yaml`` ``budget`` field).
      4. ``default`` (``0.0`` = unlimited).

    Invalid / non-numeric env values are ignored with a warning so that a
    typo never silently disables the budget guard or crashes startup.
    Non-positive values mean "unlimited" and are normalised to ``0.0``.

    Args:
        run_config_value: Budget read from ``run_config.json``.
        seed_value: Budget read from ``bernstein.yaml`` (``seed.budget_usd``).
        env: Optional environment mapping (defaults to :data:`os.environ`).
            Exposed for tests so they don't have to mutate process state.
        default: Fallback when no source provides a value.

    Returns:
        Non-negative budget cap in USD (``0.0`` = unlimited).
    """
    env_map = env if env is not None else os.environ
    raw_env = env_map.get(ENV_MAX_COST_USD)
    if raw_env is not None and raw_env.strip():
        try:
            env_value = float(raw_env)
            if env_value < 0.0:
                env_value = 0.0
            return env_value
        except ValueError:
            logger.warning(
                "Invalid %s=%r; falling back to run_config/seed/default budget.",
                ENV_MAX_COST_USD,
                raw_env,
            )
    if run_config_value is not None and run_config_value > 0.0:
        return float(run_config_value)
    if seed_value is not None and seed_value > 0.0:
        return float(seed_value)
    return max(0.0, default)


# ---------------------------------------------------------------------------
# Threshold defaults
# ---------------------------------------------------------------------------

DEFAULT_WARN_THRESHOLD: float = 0.80
DEFAULT_CRITICAL_THRESHOLD: float = 0.95
DEFAULT_HARD_STOP_THRESHOLD: float = 1.00

# default grace window between SHUTDOWN signals and SIGKILL
# once ``should_stop`` flips.  Kept here (rather than on OrchestratorConfig)
# because the kill-switch semantics are owned by the cost tracker.
DEFAULT_KILL_GRACE_PERIOD_S: int = 30

# ---------------------------------------------------------------------------
# Usage history buffer
# ---------------------------------------------------------------------------

# Default number of recent ``TokenUsage`` records kept in memory per tracker.
# Older rows are evicted (and optionally rotated to JSONL) so that a long-
# running orchestrator does not grow its RSS without bound. Analytics
# (totals, per-agent, per-model, cache savings) are maintained via
# accumulators and remain correct after eviction.
DEFAULT_USAGE_BUFFER: int = 10_000


def _resolve_usage_buffer_size() -> int:
    """Read ``BERNSTEIN_COST_USAGE_BUFFER`` with a safe default/fallback.

    Returns:
        A positive integer buffer size. Invalid or non-positive values fall
        back to :data:`DEFAULT_USAGE_BUFFER`.
    """
    raw = os.environ.get("BERNSTEIN_COST_USAGE_BUFFER")
    if raw is None or raw == "":
        return DEFAULT_USAGE_BUFFER
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid BERNSTEIN_COST_USAGE_BUFFER=%r; using default %d",
            raw,
            DEFAULT_USAGE_BUFFER,
        )
        return DEFAULT_USAGE_BUFFER
    if value <= 0:
        return DEFAULT_USAGE_BUFFER
    return value


# ---------------------------------------------------------------------------
# Quota envelopes (issue #1405)
# ---------------------------------------------------------------------------

# Default envelope when an adapter does not classify a call. Existing
# behaviour is preserved: all spend rolls up under a single bucket named
# ``"subscription"``.
DEFAULT_QUOTA_ENVELOPE: str = "subscription"

# Threshold (fraction of envelope cap) at which the
# ``envelope_threshold_reached`` budget hook fires. Operators can override
# per-envelope via ``EnvelopeConfig.threshold_pct``.
DEFAULT_ENVELOPE_THRESHOLD: float = 0.80


@dataclass(frozen=True)
class EnvelopeConfig:
    """Per-envelope budget configuration loaded from ``bernstein.yaml``.

    Attributes:
        name: Envelope identifier (e.g. ``"subscription"``).
        budget_usd: Soft cap for this envelope in USD (``0`` = unlimited).
        hard_budget_usd: Hard cap for this envelope. Reaching this cap
            refuses further spawns / records for the envelope.
        model_allowlist: Optional whitelist of model substrings permitted
            on this envelope. Empty tuple means any model is allowed.
        threshold_pct: Fraction of ``budget_usd`` at which the
            ``envelope_threshold_reached`` hook fires.
    """

    name: str
    budget_usd: float = 0.0
    hard_budget_usd: float = 0.0
    model_allowlist: tuple[str, ...] = ()
    threshold_pct: float = DEFAULT_ENVELOPE_THRESHOLD

    def __post_init__(self) -> None:
        # Frozen dataclass with validation. Use object.__setattr__ to
        # normalise non-positive caps to zero (no cap) so callers can pass
        # negative defaults without surprises.
        if self.budget_usd < 0:
            object.__setattr__(self, "budget_usd", 0.0)
        if self.hard_budget_usd < 0:
            object.__setattr__(self, "hard_budget_usd", 0.0)
        if not (0.0 < self.threshold_pct <= 1.0):
            object.__setattr__(self, "threshold_pct", DEFAULT_ENVELOPE_THRESHOLD)

    def model_allowed(self, model: str) -> bool:
        """Return True when ``model`` is permitted on this envelope.

        Empty ``model_allowlist`` means "no restriction". Otherwise the
        check is substring-based and case-insensitive so concrete model
        names (``"claude-sonnet-4"``) match the configured family
        prefixes (``"sonnet"``).
        """
        if not self.model_allowlist:
            return True
        m = model.lower()
        return any(allowed.lower() in m for allowed in self.model_allowlist)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "name": self.name,
            "budget_usd": self.budget_usd,
            "hard_budget_usd": self.hard_budget_usd,
            "model_allowlist": list(self.model_allowlist),
            "threshold_pct": self.threshold_pct,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> EnvelopeConfig:
        """Build from a parsed ``bernstein.yaml`` mapping."""
        raw_allow = d.get("model_allowlist") or ()
        allow_iter: list[str] = []
        if isinstance(raw_allow, list | tuple):
            allow_iter = [str(x) for x in cast("list[Any]", raw_allow) if str(x).strip()]
        return cls(
            name=name,
            budget_usd=float(d.get("budget_usd", 0.0) or 0.0),
            hard_budget_usd=float(d.get("hard_budget_usd", 0.0) or 0.0),
            model_allowlist=tuple(allow_iter),
            threshold_pct=float(d.get("threshold_pct", DEFAULT_ENVELOPE_THRESHOLD) or DEFAULT_ENVELOPE_THRESHOLD),
        )


class EnvelopeBudgetError(RuntimeError):
    """Raised when a record/spawn would breach a per-envelope hard cap.

    The orchestrator catches this and refuses to admit the call without
    crashing the run. The exception carries the offending envelope name
    and the policy reason so callers can log a structured event.
    """

    def __init__(self, envelope: str, reason: str, *, spent_usd: float, cap_usd: float) -> None:
        super().__init__(f"envelope {envelope!r} refused: {reason} (spent=${spent_usd:.4f} cap=${cap_usd:.4f})")
        self.envelope = envelope
        self.reason = reason
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


@dataclass(frozen=True)
class EnvelopeReport:
    """Snapshot of one envelope's spend, cap, and burn state.

    Attributes:
        name: Envelope identifier.
        spent_usd: Cumulative spend attributed to this envelope.
        cap_usd: Soft cap from :class:`EnvelopeConfig.budget_usd`.
        hard_cap_usd: Hard cap from :class:`EnvelopeConfig.hard_budget_usd`.
        pct_used: ``spent_usd / cap_usd`` (``0.0`` when cap is unset).
        threshold_pct: Configured fire-the-hook fraction.
        calls: Number of LLM calls recorded against this envelope.
        remaining_usd: Soft-cap remaining (``inf`` when uncapped).
        hard_remaining_usd: Hard-cap remaining (``inf`` when uncapped).
        threshold_reached: ``True`` when ``pct_used >= threshold_pct``.
        hard_breached: ``True`` when the hard cap has been hit.
    """

    name: str
    spent_usd: float
    cap_usd: float
    hard_cap_usd: float
    pct_used: float
    threshold_pct: float
    calls: int
    remaining_usd: float
    hard_remaining_usd: float
    threshold_reached: bool
    hard_breached: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (``inf`` rendered as ``None``)."""
        import math

        def _safe(v: float) -> float | None:
            return v if math.isfinite(v) else None

        return {
            "name": self.name,
            "spent_usd": round(self.spent_usd, 6),
            "cap_usd": self.cap_usd,
            "hard_cap_usd": self.hard_cap_usd,
            "pct_used": round(self.pct_used, 4),
            "threshold_pct": self.threshold_pct,
            "calls": self.calls,
            "remaining_usd": _safe(self.remaining_usd),
            "hard_remaining_usd": _safe(self.hard_remaining_usd),
            "threshold_reached": self.threshold_reached,
            "hard_breached": self.hard_breached,
        }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """A single token-usage record for one agent invocation.

    Attributes:
        input_tokens: Number of input (prompt) tokens consumed.
        output_tokens: Number of output (completion) tokens consumed.
        model: Model name (e.g. ``"sonnet"``, ``"opus"``).
        cost_usd: Computed cost in USD for this usage record.
        agent_id: The agent session that incurred the cost.
        task_id: The task the agent was working on.
        timestamp: Unix timestamp of when the usage was recorded.
        cache_read_tokens: Tokens served from prompt cache (read).
        cache_write_tokens: Tokens written to prompt cache (creation).
    """

    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float
    agent_id: str
    task_id: str
    tenant_id: str = "default"
    timestamp: float = field(default_factory=time.time)
    cache_hit: bool = False  # Prompt cache hit tracking (legacy)
    cached_tokens: int = 0  # Tokens served from cache (legacy)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_tags: dict[str, str] = field(default_factory=dict)
    # Per-quota-envelope attribution (issue #1405). Defaults to
    # ``"subscription"`` so existing single-envelope rollups remain
    # backwards compatible.
    quota_envelope: str = DEFAULT_QUOTA_ENVELOPE

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp,
            "cache_hit": self.cache_hit,
            "cached_tokens": self.cached_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_tags": self.cost_tags,
            "quota_envelope": self.quota_envelope,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenUsage:
        """Deserialise from a dict."""
        raw_tags: object = d.get("cost_tags", {})
        tags: dict[str, str]
        if isinstance(raw_tags, dict):  # noqa: SIM108 - clearer than long ternary
            tags = {k: str(v) for k, v in cast("dict[str, Any]", raw_tags).items()}
        else:
            tags = {}
        env_raw: object = d.get("quota_envelope", DEFAULT_QUOTA_ENVELOPE)
        envelope = str(env_raw) if env_raw else DEFAULT_QUOTA_ENVELOPE
        return cls(
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            model=str(d["model"]),
            cost_usd=float(d["cost_usd"]),
            agent_id=str(d["agent_id"]),
            task_id=str(d["task_id"]),
            tenant_id=str(d.get("tenant_id", "default") or "default"),
            timestamp=float(d.get("timestamp", 0.0)),
            cache_hit=bool(d.get("cache_hit", False)),
            cached_tokens=int(d.get("cached_tokens", 0)),
            cache_read_tokens=int(d.get("cache_read_tokens", 0)),
            cache_write_tokens=int(d.get("cache_write_tokens", 0)),
            cost_tags=tags,
            quota_envelope=envelope,
        )


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of the current budget state for a run.

    Attributes:
        run_id: Unique identifier for the orchestrator run.
        budget_usd: Total budget cap in USD (0 = unlimited).
        spent_usd: Cumulative spend so far.
        remaining_usd: Budget minus spend (clamped to >= 0).
        percentage_used: Spend as a fraction of budget (0.0-1.0+).
        should_warn: True when spend >= warning threshold.
        should_stop: True when spend >= hard-stop threshold.
    """

    run_id: str
    budget_usd: float
    spent_usd: float
    remaining_usd: float
    percentage_used: float
    should_warn: bool
    should_stop: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        import math

        remaining = self.remaining_usd if math.isfinite(self.remaining_usd) else 0.0
        return {
            "run_id": self.run_id,
            "budget_usd": self.budget_usd,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(remaining, 6),
            "percentage_used": round(self.percentage_used, 4),
            "should_warn": self.should_warn,
            "should_stop": self.should_stop,
        }


# ---------------------------------------------------------------------------
# Cost estimation helper
# ---------------------------------------------------------------------------


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate cost in USD for a given model and token counts.

    Uses detailed pricing from ``MODEL_COSTS_PER_1M_TOKENS`` if available,
    otherwise falls back to the blended ``_MODEL_COST_USD_PER_1K`` table.

    Args:
        model: Model name (e.g. ``"sonnet"``, ``"opus"``).
        input_tokens: Number of input tokens (excluding cache read/write).
        output_tokens: Number of output tokens.
        cache_read_tokens: Tokens served from prompt cache.
        cache_write_tokens: Tokens written to prompt cache.

    Returns:
        Estimated cost in USD.
    """
    from bernstein.core.cost.cost import MODEL_COSTS_PER_1M_TOKENS

    model_lower = model.lower()
    pricing = None
    for key, costs in MODEL_COSTS_PER_1M_TOKENS.items():
        if key in model_lower:
            pricing = costs
            break

    if pricing:
        cost: float = 0.0
        # pricing is in $/1M tokens
        cost += (input_tokens / 1_000_000.0) * pricing.get("input", 0.0)
        cost += (output_tokens / 1_000_000.0) * pricing.get("output", 0.0)
        cost += (cache_read_tokens / 1_000_000.0) * cast("float", pricing.get("cache_read", pricing.get("input", 0.0)))
        cost += (cache_write_tokens / 1_000_000.0) * cast(
            "float", pricing.get("cache_write", pricing.get("input", 0.0))
        )
        return cost

    # Fallback to blended rate
    rate: float = 0.005  # safe fallback
    for key, blended_cost in _MODEL_COST_USD_PER_1K.items():
        if key in model_lower:
            rate = blended_cost
            break
    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
    return (total_tokens / 1000.0) * rate


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


@dataclass
class CostTracker:
    """Per-run cost tracker with budget enforcement.

    Tracks cumulative spend, records per-agent token usage, emits log
    warnings at configurable thresholds, and persists state to disk.

    Args:
        run_id: Unique identifier for the orchestrator run.
        budget_usd: Dollar cap for this run (0 = unlimited).
        warn_threshold: Fraction (0-1) at which a warning is logged.
        critical_threshold: Fraction (0-1) at which a critical warning is logged.
        hard_stop_threshold: Fraction (0-1) at which ``should_stop`` becomes True.
    """

    run_id: str
    budget_usd: float = 0.0
    warn_threshold: float = DEFAULT_WARN_THRESHOLD
    critical_threshold: float = DEFAULT_CRITICAL_THRESHOLD
    hard_stop_threshold: float = DEFAULT_HARD_STOP_THRESHOLD
    # once ``should_stop`` transitions True the orchestrator
    # sends SHUTDOWN to every live agent, waits ``kill_grace_period_s``
    # for them to commit WIP, and SIGKILLs any still alive afterwards.
    # 0 disables the grace window (immediate SIGKILL - not recommended
    # outside tests).
    kill_grace_period_s: int = DEFAULT_KILL_GRACE_PERIOD_S
    # bound in-memory usage history. ``None`` → resolve from
    # ``BERNSTEIN_COST_USAGE_BUFFER`` env var (default 10_000). 0 disables
    # the cap (unbounded) for legacy/test use only; not recommended.
    usage_buffer_size: int | None = None
    # Optional: directory to rotate evicted rows into as JSONL. When None,
    # evicted rows are dropped (accumulators still carry their stats).
    rotation_dir: Path | None = None

    # Optional hard-cap kill switch (issue #1320). When > 0 and reached,
    # ``status().should_stop`` flips True regardless of soft budget %.
    # Independent of ``budget_usd`` so the operator can run
    # ``--budget 5usd --hard-budget 10usd`` and trip the kill switch only
    # at the higher band.
    hard_budget_usd: float = 0.0
    # Optional rolling JSONL ledger. When set, every ``record()`` writes
    # one row tagged with ``task_id|agent_id|role|feature_label`` so
    # ``bernstein cost --by ...`` can attribute spend post-hoc.
    spend_ledger: Any | None = None  # SpendLedger; typed as Any to avoid import cycle

    # Per-envelope budgets (issue #1405). Maps envelope name to its
    # :class:`EnvelopeConfig`. When the dict is empty the legacy
    # single-envelope behaviour is preserved. Hard caps are enforced
    # inside ``record()``; the budget hook fires when an envelope crosses
    # its configured threshold.
    envelopes: dict[str, EnvelopeConfig] = field(default_factory=dict[str, EnvelopeConfig])

    # Mutable tracking state (not constructor args)
    _spent_usd: float = field(default=0.0, init=False, repr=False)
    _usages: deque[TokenUsage] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_USAGE_BUFFER),
        init=False,
        repr=False,
    )
    _total_usages_recorded: int = field(default=0, init=False, repr=False)
    _warned: bool = field(default=False, init=False, repr=False)
    _critical_warned: bool = field(default=False, init=False, repr=False)
    _spent_by_agent: dict[str, float] = field(default_factory=dict[str, float], init=False, repr=False)
    _spent_by_model: dict[str, float] = field(default_factory=dict[str, float], init=False, repr=False)
    # Per-agent analytics accumulator: total cost, invocation count, and
    # per-model breakdown. Populated incrementally so that analytics stay
    # correct after older usages are evicted from ``_usages``.
    _agent_accum: dict[str, dict[str, Any]] = field(
        default_factory=dict[str, dict[str, Any]],
        init=False,
        repr=False,
    )
    # Per-model analytics accumulator: cost + token buckets (input/output/
    # cache_read/cache_write) and invocation count.
    _model_accum: dict[str, dict[str, float]] = field(
        default_factory=dict[str, dict[str, float]],
        init=False,
        repr=False,
    )
    # Running total of savings from prompt-cache reads (USD) and savings vs
    # an all-Opus baseline - kept so reports survive usage eviction.
    _cache_savings_usd: float = field(default=0.0, init=False, repr=False)
    _opus_baseline_savings_usd: float = field(default=0.0, init=False, repr=False)
    _cumulative_tokens: dict[tuple[str, str, str], tuple[int, ...]] = field(
        default_factory=dict[tuple[str, str, str], tuple[int, ...]],
        init=False,
        repr=False,
    )
    # Per-envelope spend totals + invocation counts (issue #1405). Updated
    # under ``_lock`` so envelope rollups stay consistent with ``record()``.
    _spent_by_envelope: dict[str, float] = field(default_factory=dict[str, float], init=False, repr=False)
    _calls_by_envelope: dict[str, int] = field(default_factory=dict[str, int], init=False, repr=False)
    # Envelopes that have already fired the threshold hook so we don't
    # spam observers each call once they're over the watermark.
    _envelope_warned: set[str] = field(default_factory=set[str], init=False, repr=False)
    # Optional hook fired when an envelope crosses its threshold. The hook
    # receives the offending :class:`EnvelopeReport` so observers can route
    # the event to logs / dashboards / Slack without re-querying state.
    _envelope_hook: object | None = field(default=None, init=False, repr=False)
    # Thread-safe lock for atomic budget check-and-record (COST-001).
    # Prevents race where two concurrent agents both pass the budget check
    # before either's cost is recorded, causing budget overshoot.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        """Resolve the usage buffer size and rebuild the deque accordingly."""
        resolved = self.usage_buffer_size
        if resolved is None:
            resolved = _resolve_usage_buffer_size()
        # A non-positive value means "unbounded" - store as ``None`` maxlen.
        maxlen: int | None = resolved if resolved > 0 else None
        # Preserve any usages that may have been pre-seeded (load path).
        seed = list(self._usages)
        self._usages = deque(seed, maxlen=maxlen)
        self.usage_buffer_size = resolved

    # ---- recording --------------------------------------------------------

    def record(
        self,
        agent_id: str,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None = None,
        *,
        tenant_id: str = "default",
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        role: str = "",
        feature_label: str = "",
        cost_tags: dict[str, str] | None = None,
        quota_envelope: str = DEFAULT_QUOTA_ENVELOPE,
    ) -> BudgetStatus:
        """Record token usage for an agent and return updated budget status.

        If *cost_usd* is ``None``, the cost is estimated from the model
        pricing table.

        Args:
            agent_id: Agent session ID.
            task_id: Task ID the agent was working on.
            model: Model name used.
            input_tokens: Input tokens consumed.
            output_tokens: Output tokens consumed.
            cost_usd: Explicit cost override; estimated if omitted.
            cache_read_tokens: Tokens read from prompt cache.
            cache_write_tokens: Tokens written to prompt cache.
            quota_envelope: Quota envelope tag attributed to this call
                (issue #1405). Defaults to ``"subscription"`` so legacy
                callers keep working unchanged.

        Returns:
            Current ``BudgetStatus`` after recording.

        Raises:
            EnvelopeBudgetError: When the envelope has a configured
                ``hard_budget_usd`` and admitting this call would breach
                it, or when ``model`` is not in the envelope's allowlist.
        """
        normalized_tenant = normalize_tenant_id(tenant_id)
        if cost_usd is None:
            cost_usd = estimate_cost(
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )

        envelope = quota_envelope or DEFAULT_QUOTA_ENVELOPE
        env_cfg = self.envelopes.get(envelope)

        # Hard-cap + allowlist gates fire before we mutate any state so a
        # rejected record never partially updates the totals.
        if env_cfg is not None:
            if not env_cfg.model_allowed(model):
                raise EnvelopeBudgetError(
                    envelope,
                    f"model {model!r} not in allowlist {list(env_cfg.model_allowlist)!r}",
                    spent_usd=self._spent_by_envelope.get(envelope, 0.0),
                    cap_usd=env_cfg.hard_budget_usd,
                )
            if env_cfg.hard_budget_usd > 0:
                projected = self._spent_by_envelope.get(envelope, 0.0) + cost_usd
                if projected > env_cfg.hard_budget_usd:
                    raise EnvelopeBudgetError(
                        envelope,
                        "hard budget exhausted",
                        spent_usd=projected,
                        cap_usd=env_cfg.hard_budget_usd,
                    )

        merged_tags: dict[str, str] = dict(cost_tags or {})
        if role and "role" not in merged_tags:
            merged_tags["role"] = role
        if feature_label and "feature_label" not in merged_tags:
            merged_tags["feature_label"] = feature_label
        merged_tags.setdefault("quota_envelope", envelope)

        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            cost_usd=cost_usd,
            agent_id=agent_id,
            task_id=task_id,
            tenant_id=normalized_tenant,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_tags=merged_tags,
            quota_envelope=envelope,
        )
        evicted: TokenUsage | None = None
        envelope_fired: EnvelopeReport | None = None
        with self._lock:
            # Ring-buffer append: capture the row that would be evicted so we
            # can rotate it to JSONL before it falls off the end.
            if self._usages.maxlen is not None and len(self._usages) >= self._usages.maxlen:
                evicted = self._usages[0]
            self._usages.append(usage)
            self._total_usages_recorded += 1
            self._spent_usd += cost_usd
            self._spent_by_agent[agent_id] = self._spent_by_agent.get(agent_id, 0.0) + cost_usd
            self._spent_by_model[model] = self._spent_by_model.get(model, 0.0) + cost_usd
            self._spent_by_envelope[envelope] = self._spent_by_envelope.get(envelope, 0.0) + cost_usd
            self._calls_by_envelope[envelope] = self._calls_by_envelope.get(envelope, 0) + 1
            self._update_accumulators(usage)
            status = self.status()
            envelope_fired = self._maybe_fire_envelope_threshold_locked(envelope)

        # Bug item-7 (2026-07-02): the run ledger's spent_usd sat at 0.0 for
        # whole runs because nothing fed record() - every ledger mutation now
        # logs itself so a silent-zero ledger is diagnosable from logs alone.
        # item 31 (2026-07-02): also tag each mutation with the ORIGIN of its
        # token counts (via the existing cost_tags channel - no signature
        # change) so an alive-exit /complete sidecar ingestion is
        # distinguishable in the logs from an orphan/dead-exit recovery.
        logger.info(
            "ledger_update: run_spent_usd=%.6f delta=%.6f source=%s/%s model=%s "
            "input_tokens=%d output_tokens=%d tokens_sidecar_source=%s",
            status.spent_usd,
            cost_usd,
            task_id,
            agent_id,
            model,
            input_tokens,
            output_tokens,
            merged_tags.get("tokens_sidecar_source", ""),
        )

        if evicted is not None:
            self._rotate_evicted(evicted)
        self._emit_threshold_warnings(status)
        if envelope_fired is not None:
            self._invoke_envelope_hook(envelope_fired)

        # Push to the rolling spend ledger (issue #1320). Best-effort:
        # ledger IO must never block the record() hot path. Import locally
        # so the cost_tracker module stays import-cycle-free.
        if self.spend_ledger is not None:
            from bernstein.core.cost.spend_ledger import CallTags  # local import

            tags = CallTags(
                task_id=task_id,
                agent_id=agent_id,
                role=str(merged_tags.get("role", "")),
                feature_label=str(merged_tags.get("feature_label", "")),
                quota_envelope=envelope,
                extra={k: v for k, v in merged_tags.items() if k not in {"role", "feature_label", "quota_envelope"}},
            )
            try:
                self.spend_ledger.record(
                    tags=tags,
                    model=model,
                    cost_usd=cost_usd,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    ts=usage.timestamp,
                )
            except Exception as exc:  # pragma: no cover - belt-and-braces
                logger.debug("SpendLedger write failed: %s", exc)
        return status

    def record_cumulative(
        self,
        agent_id: str,
        task_id: str,
        model: str,
        total_input_tokens: int,
        total_output_tokens: int,
        total_cost_usd: float | None = None,
        *,
        tenant_id: str = "default",
        total_cache_read_tokens: int = 0,
        total_cache_write_tokens: int = 0,
        role: str = "",
        feature_label: str = "",
        cost_tags: dict[str, str] | None = None,
        quota_envelope: str = DEFAULT_QUOTA_ENVELOPE,
    ) -> float:
        """Record cumulative token usage for one agent/task pair.

        This helper is delta-safe: callers provide running token totals and the
        tracker records only the previously unseen token delta.

        Args:
            agent_id: Agent session ID.
            task_id: Task ID associated with the cumulative counters.
            model: Model name.
            total_input_tokens: Total prompt tokens seen so far.
            total_output_tokens: Total completion tokens seen so far.
            total_cost_usd: Optional cumulative cost observed so far.
            total_cache_read_tokens: Total prompt cache read tokens.
            total_cache_write_tokens: Total prompt cache write tokens.

        Returns:
            Newly-recorded delta cost in USD (0.0 when no new tokens).
        """
        key = (agent_id, task_id, model)
        # We need to track more than just input/output now.
        # Format in _cumulative_tokens: (input, output, cache_read, cache_write)
        # Backward compat: handle 2-tuple from old persisted state.
        prev = self._cumulative_tokens.get(key, (0, 0, 0, 0))
        if len(prev) == 2:
            prev_input, prev_output = prev
            prev_cache_read, prev_cache_write = 0, 0
        else:
            prev_input, prev_output, prev_cache_read, prev_cache_write = prev

        cur_input = max(0, total_input_tokens)
        cur_output = max(0, total_output_tokens)
        cur_cache_read = max(0, total_cache_read_tokens)
        cur_cache_write = max(0, total_cache_write_tokens)

        delta_input = max(0, cur_input - prev_input)
        delta_output = max(0, cur_output - prev_output)
        delta_cache_read = max(0, cur_cache_read - prev_cache_read)
        delta_cache_write = max(0, cur_cache_write - prev_cache_write)

        if all(d == 0 for d in (delta_input, delta_output, delta_cache_read, delta_cache_write)):
            return 0.0

        delta_cost: float | None = None
        if total_cost_usd is not None:
            total_tokens = cur_input + cur_output + cur_cache_read + cur_cache_write
            prev_tokens = prev_input + prev_output + prev_cache_read + prev_cache_write
            delta_tokens = total_tokens - prev_tokens
            if total_tokens > 0 and delta_tokens > 0:
                delta_cost = float(total_cost_usd) * (delta_tokens / total_tokens)
            elif prev_tokens == 0:
                delta_cost = float(total_cost_usd)

        before = self._spent_usd
        self.record(
            agent_id=agent_id,
            task_id=task_id,
            model=model,
            input_tokens=delta_input,
            output_tokens=delta_output,
            cost_usd=delta_cost,
            tenant_id=normalize_tenant_id(tenant_id),
            cache_read_tokens=delta_cache_read,
            cache_write_tokens=delta_cache_write,
            role=role,
            feature_label=feature_label,
            cost_tags=cost_tags,
            quota_envelope=quota_envelope,
        )
        self._cumulative_tokens[key] = (cur_input, cur_output, cur_cache_read, cur_cache_write)
        return self._spent_usd - before

    def can_spawn(self, *, quota_envelope: str | None = None) -> bool:
        """Atomically check whether the budget permits spawning another agent.

        This method acquires the internal lock so that no concurrent
        ``record()`` can change the spend between the caller's check and
        their subsequent spawn decision.

        Args:
            quota_envelope: Optional envelope to check against in addition
                to the run-wide caps. When set and the envelope has
                exhausted its per-envelope hard cap, returns ``False``.

        Returns:
            ``True`` if budget is unlimited or spend is below the hard-stop
            threshold.
        """
        with self._lock:
            # Hard cap (issue #1320) trips independently of the soft cap.
            if self.hard_budget_usd > 0 and self._spent_usd >= self.hard_budget_usd:
                return False
            if quota_envelope is not None:
                env_cfg = self.envelopes.get(quota_envelope)
                if env_cfg is not None and env_cfg.hard_budget_usd > 0:
                    spent_env = self._spent_by_envelope.get(quota_envelope, 0.0)
                    if spent_env >= env_cfg.hard_budget_usd:
                        return False
            if self.budget_usd <= 0:
                return True
            pct = self._spent_usd / self.budget_usd
            return pct < self.hard_stop_threshold

    # ---- envelopes (issue #1405) ----------------------------------------

    def configure_envelopes(self, envelopes: dict[str, EnvelopeConfig]) -> None:
        """Replace the envelope configuration map.

        Convenience setter so callers building a tracker from
        ``bernstein.yaml`` need not assign ``tracker.envelopes`` directly.
        Existing envelope spend totals are preserved - the new
        configuration only affects future records.
        """
        self.envelopes = envelopes.copy()

    def set_envelope_threshold_hook(self, hook: object) -> None:
        """Register a callable fired when an envelope crosses its threshold.

        The hook receives the :class:`EnvelopeReport` for the offending
        envelope. Callers typically wire this into the
        :mod:`bernstein.core.cost.budget_actions` envelope-threshold hook.
        Failures inside the hook are logged and swallowed so observers
        never break the record hot path.
        """
        self._envelope_hook = hook

    def spent_by_envelope(self) -> dict[str, float]:
        """Return a shallow copy of the per-envelope spend map."""
        with self._lock:
            return self._spent_by_envelope.copy()

    def calls_by_envelope(self) -> dict[str, int]:
        """Return a shallow copy of the per-envelope invocation map."""
        with self._lock:
            return self._calls_by_envelope.copy()

    def envelope_report(self, name: str) -> EnvelopeReport:
        """Return the live :class:`EnvelopeReport` for ``name``.

        Reads spend and the configured cap under the tracker lock so the
        returned report is consistent with a single record() snapshot.
        """
        with self._lock:
            return self._envelope_report_locked(name)

    def envelope_reports(self) -> dict[str, EnvelopeReport]:
        """Return a per-envelope report for every observed envelope.

        Both spent-only envelopes (no config) and configured-but-unspent
        envelopes show up in the map so dashboards can render the full
        picture without merging two sources.
        """
        with self._lock:
            seen = set(self._spent_by_envelope) | set(self.envelopes)
            return {name: self._envelope_report_locked(name) for name in sorted(seen)}

    def _envelope_report_locked(self, name: str) -> EnvelopeReport:
        cfg = self.envelopes.get(name)
        spent = self._spent_by_envelope.get(name, 0.0)
        calls = self._calls_by_envelope.get(name, 0)
        cap = cfg.budget_usd if cfg is not None else 0.0
        hard_cap = cfg.hard_budget_usd if cfg is not None else 0.0
        pct = (spent / cap) if cap > 0 else 0.0
        threshold = cfg.threshold_pct if cfg is not None else DEFAULT_ENVELOPE_THRESHOLD
        remaining = max(cap - spent, 0.0) if cap > 0 else float("inf")
        hard_remaining = max(hard_cap - spent, 0.0) if hard_cap > 0 else float("inf")
        return EnvelopeReport(
            name=name,
            spent_usd=spent,
            cap_usd=cap,
            hard_cap_usd=hard_cap,
            pct_used=pct,
            threshold_pct=threshold,
            calls=calls,
            remaining_usd=remaining,
            hard_remaining_usd=hard_remaining,
            threshold_reached=cap > 0 and pct >= threshold,
            hard_breached=hard_cap > 0 and spent >= hard_cap,
        )

    def _maybe_fire_envelope_threshold_locked(self, envelope: str) -> EnvelopeReport | None:
        """Return the EnvelopeReport when the threshold has just been crossed.

        Called under ``self._lock``. Does not invoke the hook - the
        caller fires the hook *after* releasing the lock so a slow
        observer never blocks other writers.
        """
        cfg = self.envelopes.get(envelope)
        if cfg is None or cfg.budget_usd <= 0:
            return None
        spent = self._spent_by_envelope.get(envelope, 0.0)
        pct = spent / cfg.budget_usd
        if pct < cfg.threshold_pct:
            return None
        if envelope in self._envelope_warned:
            return None
        self._envelope_warned.add(envelope)
        return self._envelope_report_locked(envelope)

    def _invoke_envelope_hook(self, report: EnvelopeReport) -> None:
        """Best-effort fire the registered envelope-threshold hook.

        Default behaviour (no hook attached) logs a structured warning so
        operators can see threshold crossings without bespoke wiring.
        """
        hook = self._envelope_hook
        if hook is None:
            logger.warning(
                "envelope_threshold_reached envelope=%s spent=$%.4f cap=$%.4f pct=%.2f",
                report.name,
                report.spent_usd,
                report.cap_usd,
                report.pct_used,
            )
            return
        try:
            cast("Any", hook)(report)
        except Exception as exc:  # pragma: no cover - best-effort hook
            logger.debug("envelope threshold hook raised: %s", exc)

    # ---- status -----------------------------------------------------------

    def status(self) -> BudgetStatus:
        """Return the current budget status snapshot.

        Returns:
            Immutable ``BudgetStatus`` with remaining budget, percentage
            used, and stop/warn flags.
        """
        # Hard cap kill-switch (issue #1320). When the hard cap is hit we
        # always advertise ``should_stop=True`` so the orchestrator stops
        # spawning, even when the soft budget is unlimited or unset.
        hard_trip = self.hard_budget_usd > 0 and self._spent_usd >= self.hard_budget_usd

        if self.budget_usd <= 0:
            return BudgetStatus(
                run_id=self.run_id,
                budget_usd=0.0,
                spent_usd=self._spent_usd,
                remaining_usd=float("inf"),
                percentage_used=0.0,
                should_warn=hard_trip,
                should_stop=hard_trip,
            )

        pct = self._spent_usd / self.budget_usd
        remaining = max(self.budget_usd - self._spent_usd, 0.0)
        return BudgetStatus(
            run_id=self.run_id,
            budget_usd=self.budget_usd,
            spent_usd=self._spent_usd,
            remaining_usd=remaining,
            percentage_used=pct,
            should_warn=pct >= self.warn_threshold or hard_trip,
            should_stop=pct >= self.hard_stop_threshold or hard_trip,
        )

    @property
    def spent_usd(self) -> float:
        """Total USD spent so far."""
        return self._spent_usd

    @property
    def usages(self) -> list[TokenUsage]:
        """Recent token usage entries (read-only copy).

        Returns at most :attr:`usage_buffer_size` rows; older rows are
        evicted to keep memory bounded (see prior audit). Per-agent and
        per-model analytics remain exact via accumulators, but anyone
        iterating this list for raw per-row analysis should consult the
        JSONL rotation files under :attr:`rotation_dir` if full history is
        required.
        """
        return list(self._usages)

    @property
    def total_usages_recorded(self) -> int:
        """Total usage records ever appended, including evicted rows."""
        return self._total_usages_recorded

    def cheaper_model_for(self, model: str) -> str | None:
        """Suggest a cheaper model when ``>=80%`` of the soft cap is spent.

        Returns ``None`` when the soft cap is unset or not yet warning.
        Delegates to :class:`SpendLedger.cheaper_model` when a ledger is
        attached so the reroute table stays in one place; otherwise uses
        a local 80% check against the soft cap.
        """
        if self.spend_ledger is not None:
            return self.spend_ledger.cheaper_model(model)  # type: ignore[no-any-return]
        # Fallback: local 80% check
        if self.budget_usd <= 0:
            return None
        pct = self._spent_usd / self.budget_usd
        if pct < self.warn_threshold:
            return None
        from bernstein.core.cost.spend_ledger import _REROUTE, DEFAULT_CHEAP_MODEL

        m = model.lower()
        for k, v in _REROUTE.items():
            if k in m and k != v:
                return v
        return DEFAULT_CHEAP_MODEL if DEFAULT_CHEAP_MODEL not in m else None

    def spent_for_agent(self, agent_id: str) -> float:
        """Return cumulative spend for one agent session."""
        return self._spent_by_agent.get(agent_id, 0.0)

    def spent_by_model(self) -> dict[str, float]:
        """Return cumulative spend by model."""
        return self._spent_by_model.copy()

    # ---- persistence ------------------------------------------------------

    def save(self, base_dir: Path) -> Path:
        """Persist cost data to ``.sdd/runtime/costs/{run_id}.json``.

        Creates the directory if it does not exist.

        Args:
            base_dir: The ``.sdd`` directory (or any parent under which
                ``runtime/costs/`` will be created).

        Returns:
            Path to the written JSON file.
        """
        costs_dir = base_dir / "runtime" / "costs"
        costs_dir.mkdir(parents=True, exist_ok=True)
        file_path = costs_dir / f"{self.run_id}.json"

        data: dict[str, Any] = {
            "run_id": self.run_id,
            "budget_usd": self.budget_usd,
            "hard_budget_usd": self.hard_budget_usd,
            "spent_usd": round(self._spent_usd, 6),
            "warn_threshold": self.warn_threshold,
            "critical_threshold": self.critical_threshold,
            "hard_stop_threshold": self.hard_stop_threshold,
            "usages": [u.to_dict() for u in self._usages],
            "cumulative_tokens": {
                # JSON doesn't support tuple keys; convert to string.
                # key is (agent_id, task_id, model)
                "|".join(k): list(v)
                for k, v in self._cumulative_tokens.items()
            },
            "envelopes": {name: cfg.to_dict() for name, cfg in self.envelopes.items()},
            "spent_by_envelope": self._spent_by_envelope.copy(),
            "calls_by_envelope": self._calls_by_envelope.copy(),
        }
        file_path.write_text(json.dumps(data, indent=2))
        return file_path

    @classmethod
    def load(cls, base_dir: Path, run_id: str) -> CostTracker | None:
        """Load a previously persisted CostTracker from disk.

        Args:
            base_dir: The ``.sdd`` directory.
            run_id: Run identifier to look up.

        Returns:
            Restored ``CostTracker``, or ``None`` if the file doesn't exist
            or is corrupt.
        """
        file_path = base_dir / "runtime" / "costs" / f"{run_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            tracker = cls(
                run_id=data["run_id"],
                budget_usd=float(data.get("budget_usd", 0.0)),
                hard_budget_usd=float(data.get("hard_budget_usd", 0.0)),
                warn_threshold=float(data.get("warn_threshold", DEFAULT_WARN_THRESHOLD)),
                critical_threshold=float(data.get("critical_threshold", DEFAULT_CRITICAL_THRESHOLD)),
                hard_stop_threshold=float(data.get("hard_stop_threshold", DEFAULT_HARD_STOP_THRESHOLD)),
            )
            for u_dict in data.get("usages", []):
                usage = TokenUsage.from_dict(u_dict)
                tracker._usages.append(usage)
                tracker._spent_usd += usage.cost_usd
                tracker._spent_by_agent[usage.agent_id] = (
                    tracker._spent_by_agent.get(usage.agent_id, 0.0) + usage.cost_usd
                )
                tracker._spent_by_model[usage.model] = tracker._spent_by_model.get(usage.model, 0.0) + usage.cost_usd
                # rebuild running accumulators so breakdowns survive
                # across reload (otherwise model_breakdowns() returns empty).
                tracker._update_accumulators(usage)

            # Restore cumulative token tracking for delta-safe recording
            raw_cumul = data.get("cumulative_tokens", {})
            for k_str, v_list in raw_cumul.items():
                key = tuple(k_str.split("|"))
                if len(key) == 3:
                    tracker._cumulative_tokens[key] = tuple(v_list)  # type: ignore[assignment]

            # Restore envelope state (issue #1405). Backwards compatible:
            # older snapshots without envelope blocks load as zero-state.
            raw_env_cfg = data.get("envelopes", {})
            if isinstance(raw_env_cfg, dict):
                env_map: dict[str, EnvelopeConfig] = {}
                for name, payload in cast("dict[str, Any]", raw_env_cfg).items():
                    if isinstance(payload, dict):
                        env_map[name] = EnvelopeConfig.from_dict(name, cast("dict[str, Any]", payload))
                tracker.envelopes = env_map
            raw_env_spent = data.get("spent_by_envelope", {})
            if isinstance(raw_env_spent, dict):
                tracker._spent_by_envelope = {k: float(v) for k, v in cast("dict[str, Any]", raw_env_spent).items()}
            raw_env_calls = data.get("calls_by_envelope", {})
            if isinstance(raw_env_calls, dict):
                tracker._calls_by_envelope = {k: int(v) for k, v in cast("dict[str, Any]", raw_env_calls).items()}

            # If we loaded usages but not envelope spend, derive it from
            # usage records so old snapshots still aggregate by envelope.
            if not tracker._spent_by_envelope and tracker._usages:
                for u in tracker._usages:
                    tracker._spent_by_envelope[u.quota_envelope] = (
                        tracker._spent_by_envelope.get(u.quota_envelope, 0.0) + u.cost_usd
                    )
                    tracker._calls_by_envelope[u.quota_envelope] = (
                        tracker._calls_by_envelope.get(u.quota_envelope, 0) + 1
                    )

            return tracker
        except Exception as exc:
            from bernstein.core.sanitize import sanitize_log

            logger.warning("Failed to load cost tracker for run %s: %s", sanitize_log(run_id), exc)
            return None

    # ---- reporting --------------------------------------------------------

    def shareable_summary(
        self,
        tasks_done: int = 0,
        tasks_failed: int = 0,
        total_duration_s: float = 0.0,
    ) -> str:
        """Return a markdown run-summary snippet suitable for sharing.

        Computes savings vs an all-Opus baseline using current usages.

        Args:
            tasks_done: Number of tasks that completed successfully.
            tasks_failed: Number of tasks that failed.
            total_duration_s: Wall-clock duration of the run in seconds.

        Returns:
            Multi-line markdown string.
        """
        # consult the running accumulator so the summary reflects
        # the entire run history even after older rows are evicted from the
        # in-memory ring buffer.
        savings = max(0.0, self._opus_baseline_savings_usd)
        actual = self._spent_usd
        single_agent = actual + savings
        savings_pct = (savings / single_agent * 100) if single_agent > 0 else 0.0

        mins = int(total_duration_s // 60)
        secs = int(total_duration_s % 60)
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        lines: list[str] = ["🎼 Bernstein run summary"]
        lines.append(f"   Tasks: {tasks_done} completed" + (f", {tasks_failed} failed" if tasks_failed else ""))
        if total_duration_s > 0:
            lines.append(f"   Time:  {time_str}")
        if single_agent > actual:
            lines.extend(
                (
                    f"   Cost:  ${actual:.2f} (vs ~${single_agent:.2f} single agent)",
                    f"   Saved: ${savings:.2f} ({savings_pct:.0f}%)",
                )
            )
        else:
            lines.append(f"   Cost:  ${actual:.2f}")
        return "\n".join(lines)

    # ---- breakdowns & projection ------------------------------------------

    def agent_summaries(self) -> list[AgentCostSummary]:
        """Build per-agent cost summaries from the accumulators.

        Uses the running per-agent accumulator so that summaries stay exact
        even after older ``_usages`` have been evicted.

        Returns:
            List of :class:`AgentCostSummary` sorted by total cost descending.
        """
        return [
            AgentCostSummary(
                agent_id=aid,
                total_cost_usd=round(float(d["total"]), 6),
                task_count=int(d["count"]),
                model_breakdown={m: round(c, 6) for m, c in cast("dict[str, float]", d["models"]).items()},
            )
            for aid, d in sorted(self._agent_accum.items(), key=lambda kv: float(kv[1]["total"]), reverse=True)
        ]

    def model_breakdowns(self) -> list[ModelCostBreakdown]:
        """Build per-model cost breakdowns from the accumulators.

        Uses the running per-model accumulator so that breakdowns stay
        exact even after older ``_usages`` have been evicted.

        Returns:
            List of :class:`ModelCostBreakdown` sorted by total cost descending.
        """
        return [
            ModelCostBreakdown(
                model=model,
                total_cost_usd=round(d["total"], 6),
                total_tokens=int(d["tokens"]),
                invocation_count=int(d["count"]),
                input_tokens=int(d["input"]),
                output_tokens=int(d["output"]),
                cache_read_tokens=int(d["cache_read"]),
                cache_write_tokens=int(d["cache_write"]),
            )
            for model, d in sorted(self._model_accum.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ]

    def project(self, tasks_done: int, tasks_remaining: int) -> RunCostProjection:
        """Project total run cost based on completed-task history.

        Uses ``current_cost / tasks_done`` as the per-task average and
        multiplies by ``tasks_remaining`` to estimate the remaining spend.
        Confidence is 0 with no data and approaches 1.0 after 5+ tasks.

        Args:
            tasks_done: Number of tasks completed so far.
            tasks_remaining: Number of tasks still outstanding.

        Returns:
            :class:`RunCostProjection` with estimate and confidence.
        """
        current = self._spent_usd
        avg_per_task = (current / tasks_done) if tasks_done > 0 else 0.0
        projected_total = current + avg_per_task * max(tasks_remaining, 0)
        confidence = min(tasks_done / 5.0, 1.0) if tasks_done > 0 else 0.0

        within_budget = True if self.budget_usd <= 0 else projected_total <= self.budget_usd

        return RunCostProjection(
            run_id=self.run_id,
            tasks_done=tasks_done,
            tasks_remaining=max(tasks_remaining, 0),
            current_cost_usd=round(current, 6),
            projected_total_usd=round(projected_total, 6),
            avg_cost_per_task_usd=round(avg_per_task, 6),
            budget_usd=self.budget_usd,
            within_budget=within_budget,
            confidence=round(confidence, 3),
        )

    def cache_savings_usd(self) -> float:
        """Estimate USD saved by prompt caching across the entire run.

        Uses the running :attr:`_cache_savings_usd` accumulator so the
        reported figure covers every usage ever recorded - not just the
        ones currently held in the bounded in-memory buffer.

        Returns:
            Estimated savings in USD (always >= 0).
        """
        return max(0.0, self._cache_savings_usd)

    def report(self, tasks_done: int = 0, tasks_remaining: int = 0) -> RunCostReport:
        """Build a full cost report for this run.

        Args:
            tasks_done: Tasks completed; used for projection (0 = no projection).
            tasks_remaining: Tasks still outstanding; used for projection.

        Returns:
            :class:`RunCostReport` with per-agent, per-model, and projection data.
        """
        projection: RunCostProjection | None = None
        if tasks_done > 0 or tasks_remaining > 0:
            projection = self.project(tasks_done, tasks_remaining)

        return RunCostReport(
            run_id=self.run_id,
            total_spent_usd=round(self._spent_usd, 6),
            budget_usd=self.budget_usd,
            per_agent=self.agent_summaries(),
            per_model=self.model_breakdowns(),
            projection=projection,
            cache_savings_usd=round(self.cache_savings_usd(), 6),
        )

    def save_metrics(self, metrics_dir: Path) -> Path:
        """Persist a cost report to ``.sdd/metrics/costs_{run_id}.json``.

        Creates the directory if it does not exist.  Handles zero/missing
        budget gracefully - ``budget_usd=0`` is written as-is (unlimited).

        Args:
            metrics_dir: The ``.sdd/metrics`` directory path.

        Returns:
            Path to the written JSON file.
        """
        from pathlib import Path as _Path

        metrics_path = _Path(str(metrics_dir))
        metrics_path.mkdir(parents=True, exist_ok=True)
        file_path = metrics_path / f"costs_{self.run_id}.json"
        r = self.report()
        file_path.write_text(json.dumps(r.to_dict(), indent=2))
        logger.debug("Cost report for run %s saved to %s", self.run_id, file_path)
        return file_path

    # ---- internal ---------------------------------------------------------

    def _update_accumulators(self, usage: TokenUsage) -> None:
        """Update running analytics counters for a single usage record.

        Called under :attr:`_lock` so callers do not need additional
        synchronisation. The accumulators let analytics survive eviction of
        older rows from the bounded :attr:`_usages` buffer.

        Args:
            usage: The newly-recorded :class:`TokenUsage`.
        """
        # Per-agent accumulator
        agent_bucket = self._agent_accum.get(usage.agent_id)
        if agent_bucket is None:
            agent_bucket = {"total": 0.0, "count": 0, "models": {}}
            self._agent_accum[usage.agent_id] = agent_bucket
        agent_bucket["total"] = float(agent_bucket["total"]) + usage.cost_usd
        agent_bucket["count"] = int(agent_bucket["count"]) + 1
        models_bucket = cast("dict[str, float]", agent_bucket["models"])
        models_bucket[usage.model] = models_bucket.get(usage.model, 0.0) + usage.cost_usd

        # Per-model accumulator
        model_bucket = self._model_accum.get(usage.model)
        if model_bucket is None:
            model_bucket = {
                "total": 0.0,
                "tokens": 0.0,
                "count": 0.0,
                "input": 0.0,
                "output": 0.0,
                "cache_read": 0.0,
                "cache_write": 0.0,
            }
            self._model_accum[usage.model] = model_bucket
        model_bucket["total"] += usage.cost_usd
        model_bucket["tokens"] += (
            usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        )
        model_bucket["count"] += 1
        model_bucket["input"] += usage.input_tokens
        model_bucket["output"] += usage.output_tokens
        model_bucket["cache_read"] += usage.cache_read_tokens
        model_bucket["cache_write"] += usage.cache_write_tokens

        # Running cache-read savings
        if usage.cache_read_tokens > 0:
            from bernstein.core.cost.cost import MODEL_COSTS_PER_1M_TOKENS

            model_lower = usage.model.lower()
            pricing: dict[str, float] | None = None
            for key, costs in MODEL_COSTS_PER_1M_TOKENS.items():
                if key in model_lower:
                    pricing = costs
                    break
            if pricing is not None:
                input_price = pricing.get("input", 0.0)
                cache_read_price = pricing.get("cache_read", input_price)
                self._cache_savings_usd += (usage.cache_read_tokens / 1_000_000.0) * (input_price - cache_read_price)

        # Running "vs all-Opus" baseline savings
        if "opus" not in usage.model.lower():
            total_tokens = usage.input_tokens + usage.output_tokens
            if total_tokens > 0:
                opus_cost_per_1k = _MODEL_COST_USD_PER_1K["opus"]
                opus_est = (total_tokens / 1000.0) * opus_cost_per_1k
                self._opus_baseline_savings_usd += max(opus_est - usage.cost_usd, 0.0)

    def _rotate_evicted(self, usage: TokenUsage) -> None:
        """Append an evicted usage row to a JSONL rotation file.

        Does nothing when :attr:`rotation_dir` is unset - in that case the
        row is simply dropped (its stats are still carried in the
        accumulators). Failures are logged and swallowed so that telemetry
        IO never blocks the orchestrator hot path.
        """
        rotation_dir = self.rotation_dir
        if rotation_dir is None:
            return
        try:
            rotation_dir.mkdir(parents=True, exist_ok=True)
            rotation_file = rotation_dir / f"usages-{self.run_id}.jsonl"
            with rotation_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(usage.to_dict()) + "\n")
        except OSError as exc:  # pragma: no cover - best-effort IO
            logger.debug("Failed to rotate evicted cost usage for run %s: %s", self.run_id, exc)

    def attach_retry_budget(self, budget: object) -> None:
        """Attach a :class:`~bernstein.core.cost.retry_budget.RetryBudget`.

        Imported lazily so importing ``cost_tracker`` does not pull in
        the retry budget module (and vice versa).  The attachment is a
        back-reference - the tracker does not mutate the budget.

        Args:
            budget: Any object that exposes ``attempts_left`` /
                ``is_exhausted``.  Typically a ``RetryBudget`` from
                :mod:`bernstein.core.cost.retry_budget`.
        """
        self._retry_budget = budget

    @property
    def retry_budget(self) -> object | None:
        """The attached retry budget, if any."""
        return getattr(self, "_retry_budget", None)

    def _emit_threshold_warnings(self, status: BudgetStatus) -> None:
        """Log warnings when budget thresholds are crossed.

        Each threshold is logged at most once per tracker lifetime.
        """
        if self.budget_usd <= 0:
            return

        if status.percentage_used >= self.hard_stop_threshold:
            logger.warning(
                "BUDGET EXCEEDED for run %s: $%.2f / $%.2f (%.0f%%) - stopping agent spawns",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
        elif status.percentage_used >= self.critical_threshold and not self._critical_warned:
            self._critical_warned = True
            logger.warning(
                "BUDGET CRITICAL for run %s: $%.2f / $%.2f (%.0f%%)",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
        elif status.percentage_used >= self.warn_threshold and not self._warned:
            self._warned = True
            logger.warning(
                "Budget warning for run %s: $%.2f / $%.2f (%.0f%%)",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
