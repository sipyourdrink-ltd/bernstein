"""Example plugin: cost-aware router.

Demonstrates how to implement a routing plugin that:

1. Tracks cumulative model costs from ``on_agent_spawned`` events.
2. Writes a **routing hints file** (``.sdd/runtime/routing_hints.json``)
   that the orchestrator reads on the next scheduling tick.
3. Uses ``on_task_created`` to pre-annotate high-cost tasks so the main
   router can steer them to cheaper models.

This pattern lets you plug in custom routing intelligence without touching
``bernstein/core/router.py``.  The orchestrator already reads
``.sdd/runtime/routing_hints.json`` when present — if it does not exist,
routing falls back to the standard tier-aware algorithm.

Usage — add to bernstein.yaml::

    plugins:
      - examples.plugins.custom_router_plugin:CostAwareRouter

Tune the daily budget cap via environment variable::

    export BERNSTEIN_DAILY_BUDGET_USD=5.00   # default: 10.00

How it works
------------

* ``on_agent_spawned`` → record the model name; approximate token cost later.
* ``on_task_completed`` / ``on_task_failed`` → update session totals; if
  cumulative spend exceeds the soft cap, downgrade the preferred model to
  ``haiku`` in the hints file.
* ``on_task_created`` → if the task role is ``"manager"`` or ``"architect"``,
  mark it as requiring a high-reasoning model so the router won't downgrade it
  even when the budget is tight.

The hints file schema::

    {
        "preferred_model": "haiku" | "sonnet" | "opus",
        "budget_remaining_usd": 4.23,
        "override_roles": {
            "manager": "opus",
            "architect": "opus"
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from bernstein.plugins import hookimpl

log = logging.getLogger(__name__)

# Approximate cost per 1k tokens (output) by short model alias.
_COST_PER_1K: dict[str, float] = {
    "haiku": 0.00025,
    "sonnet": 0.003,
    "opus": 0.015,
}

# Roles that must never be downgraded regardless of budget pressure.
_PROTECTED_ROLES: set[str] = {"manager", "architect", "security"}


class CostAwareRouter:
    """Routes tasks to cheaper models when the daily budget cap is approached.

    The plugin writes routing hints to ``.sdd/runtime/routing_hints.json``.
    The orchestrator reads this file on each scheduling tick and uses it to
    override the default provider selection.

    Args:
        daily_budget_usd: Soft daily budget cap in USD.  When cumulative cost
            exceeds this value the plugin downgrades the preferred model to
            ``haiku``.  Defaults to the ``BERNSTEIN_DAILY_BUDGET_USD`` env var
            or ``10.00``.
        workdir: Project root directory.  Defaults to the current directory.
    """

    def __init__(
        self,
        daily_budget_usd: float | None = None,
        workdir: Path | str | None = None,
    ) -> None:
        if daily_budget_usd is not None:
            self._budget = daily_budget_usd
        else:
            self._budget = float(os.getenv("BERNSTEIN_DAILY_BUDGET_USD", "10.00"))

        self._workdir = Path(workdir) if workdir else Path.cwd()
        self._hints_path = self._workdir / ".sdd" / "runtime" / "routing_hints.json"

        # In-memory spend tracking (resets on process restart).
        self._session_spend: dict[str, float] = {}  # session_id -> USD estimate
        self._total_spend: float = 0.0
        self._active_sessions: dict[str, str] = {}  # session_id -> model alias

        # Load existing hints (may have been written by a previous run).
        self._hints: dict[str, object] = self._load_hints()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        """Pre-annotate protected roles so they keep high-reasoning models."""
        if role in _PROTECTED_ROLES:
            overrides: dict[str, str] = {}
            existing = self._hints.get("override_roles")
            if isinstance(existing, dict):
                overrides = dict(existing)  # type: ignore[arg-type]
            overrides[role] = "opus"
            self._hints["override_roles"] = overrides
            self._write_hints()
            log.debug(
                "CostAwareRouter: pinned role %r → opus (protected) for task %s",
                role,
                task_id,
            )

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        """Record the model used by a new agent session."""
        # Normalise to short alias for cost lookup.
        alias = _resolve_alias(model)
        self._active_sessions[session_id] = alias
        log.debug(
            "CostAwareRouter: tracking session %s role=%s model=%s (alias=%s)",
            session_id,
            role,
            model,
            alias,
        )

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        """Estimate the cost of a completed session and update the budget."""
        alias = self._active_sessions.pop(session_id, "sonnet")
        # Rough estimate: assume 2 000 output tokens per session on average.
        estimated_tokens = 2_000
        cost_usd = (estimated_tokens / 1_000) * _COST_PER_1K.get(alias, 0.003)
        self._total_spend += cost_usd
        self._session_spend[session_id] = cost_usd

        self._update_preferred_model()
        self._write_hints()

        log.debug(
            "CostAwareRouter: session %s reaped (role=%s, outcome=%s, ~$%.4f, total ~$%.4f / $%.2f budget)",
            session_id,
            role,
            outcome,
            cost_usd,
            self._total_spend,
            self._budget,
        )

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Log budget state on failure — useful for post-mortem analysis."""
        log.warning(
            "CostAwareRouter: task %s failed (role=%s); session spend so far ~$%.4f / $%.2f budget",
            task_id,
            role,
            self._total_spend,
            self._budget,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update_preferred_model(self) -> None:
        """Downgrade or restore the preferred model based on budget usage."""
        fraction_used = self._total_spend / self._budget if self._budget > 0 else 0.0

        if fraction_used >= 0.90:
            preferred = "haiku"
        elif fraction_used >= 0.60:
            preferred = "sonnet"
        else:
            preferred = "sonnet"  # default

        self._hints["preferred_model"] = preferred
        self._hints["budget_remaining_usd"] = round(max(0.0, self._budget - self._total_spend), 4)

    def _load_hints(self) -> dict[str, object]:
        """Load existing routing hints from disk, or return defaults."""
        if self._hints_path.exists():
            try:
                raw: object = json.loads(self._hints_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw  # type: ignore[return-value]
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("CostAwareRouter: could not read hints file: %s", exc)
        return {
            "preferred_model": "sonnet",
            "budget_remaining_usd": self._budget,
            "override_roles": {},
        }

    def _write_hints(self) -> None:
        """Persist the current routing hints to disk."""
        try:
            self._hints_path.parent.mkdir(parents=True, exist_ok=True)
            self._hints_path.write_text(
                json.dumps(self._hints, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("CostAwareRouter: could not write hints file: %s", exc)

    @property
    def total_spend_usd(self) -> float:
        """Total estimated spend in USD for this process lifetime."""
        return self._total_spend

    @property
    def budget_remaining_usd(self) -> float:
        """Estimated remaining budget in USD."""
        return max(0.0, self._budget - self._total_spend)


def _resolve_alias(model: str) -> str:
    """Normalise a full model identifier to a short alias for cost lookup.

    Examples::

        "claude-haiku-4-5-20251001"  → "haiku"
        "claude-sonnet-4-6" → "sonnet"
        "claude-opus-4-6"   → "opus"
        "gemini-pro"        → "sonnet"  # fallback
    """
    model_lower = model.lower()
    if "haiku" in model_lower:
        return "haiku"
    if "opus" in model_lower:
        return "opus"
    if "sonnet" in model_lower:
        return "sonnet"
    return "sonnet"  # safe default
