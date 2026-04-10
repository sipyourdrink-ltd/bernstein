"""Agent comparison view API — per-provider/model performance metrics.

Aggregates session data by (provider, model) to expose success rates, costs,
completion times, and quality gate pass rates for side-by-side comparison
in the TUI/web dashboard.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, computed_field

router = APIRouter(tags=["agent-comparison"])


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


class AgentMetrics(BaseModel):
    """Aggregated performance metrics for a single (adapter, model) pair."""

    adapter: str
    model: str
    total_tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    avg_completion_secs: float = 0.0
    total_cost_usd: float = 0.0
    quality_gate_pass_rate: float = 1.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_rate(self) -> float:
        """Fraction of tasks that succeeded (0.0-1.0)."""
        if self.total_tasks == 0:
            return 0.0
        return self.succeeded / self.total_tasks

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_per_task(self) -> float:
        """Average cost per task in USD."""
        if self.total_tasks == 0:
            return 0.0
        return self.total_cost_usd / self.total_tasks


# ---------------------------------------------------------------------------
# Aggregation logic
# ---------------------------------------------------------------------------


def compute_agent_metrics(sessions: list[dict[str, Any]]) -> list[AgentMetrics]:
    """Group session dicts by (provider, model) and compute aggregate metrics.

    Each session dict is expected to have at least:
      - ``provider`` (str): adapter/provider name (e.g. ``"claude"``)
      - ``model`` (str): model identifier (e.g. ``"opus"``)
      - ``status`` (str): ``"done"`` or ``"failed"``
      - ``duration_s`` (float): wall-clock seconds for the session
      - ``cost_usd`` (float): total cost for this session
      - ``quality_gate_passed`` (bool): whether quality gates passed

    Missing fields are defaulted to safe values.

    Args:
        sessions: List of session dicts from runtime state.

    Returns:
        Sorted list of :class:`AgentMetrics` (by adapter, then model).
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for session in sessions:
        provider = str(session.get("provider") or "unknown")
        model = str(session.get("model") or "unknown")
        key = (provider, model)
        groups.setdefault(key, []).append(session)

    result: list[AgentMetrics] = []

    for (adapter, model), group in sorted(groups.items()):
        total = len(group)
        succeeded = sum(1 for s in group if s.get("status") == "done")
        failed = sum(1 for s in group if s.get("status") == "failed")

        durations = [float(s.get("duration_s", 0.0)) for s in group if s.get("duration_s")]
        avg_completion = sum(durations) / len(durations) if durations else 0.0

        total_cost = sum(float(s.get("cost_usd", 0.0)) for s in group)

        gate_checks = [s for s in group if "quality_gate_passed" in s]
        if gate_checks:
            gate_pass_rate = sum(1 for s in gate_checks if s["quality_gate_passed"]) / len(gate_checks)
        else:
            gate_pass_rate = 1.0

        result.append(
            AgentMetrics(
                adapter=adapter,
                model=model,
                total_tasks=total,
                succeeded=succeeded,
                failed=failed,
                avg_completion_secs=round(avg_completion, 2),
                total_cost_usd=round(total_cost, 6),
                quality_gate_pass_rate=round(gate_pass_rate, 4),
            )
        )

    return result


# ---------------------------------------------------------------------------
# Helper to extract session dicts from the task store
# ---------------------------------------------------------------------------


def _extract_sessions(request: Request) -> list[dict[str, Any]]:
    """Build session dicts from the task store's agent sessions and tasks.

    Joins agent session metadata (provider, model, status, duration) with
    per-task cost data and quality gate results.

    Args:
        request: FastAPI request with app.state.store attached.

    Returns:
        List of session dicts ready for :func:`compute_agent_metrics`.
    """
    import time

    store = request.app.state.store
    agents: dict[str, Any] = store.agents
    tasks: dict[str, Any] = getattr(store, "_tasks", {})

    sessions: list[dict[str, Any]] = []

    for _agent_id, agent in agents.items():
        provider = getattr(agent, "provider", None) or "unknown"
        model_cfg = getattr(agent, "model_config", None)
        model = getattr(model_cfg, "model", "unknown") if model_cfg else "unknown"

        agent_status = getattr(agent, "status", "working")
        spawn_ts = getattr(agent, "spawn_ts", 0.0)
        now = time.time()
        duration_s = now - spawn_ts if spawn_ts > 0 else 0.0

        # Map agent status to done/failed
        if agent_status == "dead":
            exit_code = getattr(agent, "exit_code", None)
            status = "done" if exit_code == 0 else "failed"
        elif agent_status in ("idle",):
            status = "done"
        else:
            status = "working"

        # Sum cost from associated tasks
        task_ids: list[str] = getattr(agent, "task_ids", [])
        cost_usd = 0.0
        quality_gate_passed = True
        for tid in task_ids:
            task = tasks.get(tid)
            if task is None:
                continue
            task_cost = getattr(task, "cost_usd", 0.0)
            if task_cost:
                cost_usd += task_cost
            task_status = getattr(task, "status", None)
            if task_status is not None and hasattr(task_status, "value") and task_status.value == "failed":
                quality_gate_passed = False

        sessions.append(
            {
                "provider": provider,
                "model": model,
                "status": status,
                "duration_s": duration_s,
                "cost_usd": cost_usd,
                "quality_gate_passed": quality_gate_passed,
            }
        )

    return sessions


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/agents/comparison", response_model=list[AgentMetrics])
def get_agent_comparison(request: Request) -> JSONResponse:
    """Return per-(adapter, model) performance comparison metrics.

    Aggregates data from all agent sessions in the current run:
    success rate, average completion time, cost per task, and
    quality gate pass rate.

    Returns:
        JSON list of :class:`AgentMetrics` objects sorted by adapter
        then model.
    """
    sessions = _extract_sessions(request)
    metrics = compute_agent_metrics(sessions)
    return JSONResponse(content=[m.model_dump() for m in metrics])
