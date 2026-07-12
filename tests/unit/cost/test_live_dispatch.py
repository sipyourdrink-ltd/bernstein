"""Live batch-route + cache-window fan-out wiring tests (#2354).

Covers the bridge between the run spawn loop and the deterministic cost policy
decisions: adapter resolution, the capability-gated batch route, the sealed
``cost.batch_route`` receipt, and the cache-window fan-out helper (one warm-up
strictly before the M cache-hitting worker calls).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.cost.scheduling.live_dispatch import (
    decide_batch_route,
    is_batch_eligible,
    resolve_task_adapter,
    run_cache_window_fanout,
    seal_batch_route,
)


def _task(make_task: Any, *, batch_eligible: bool | None = None, **overrides: Any) -> Any:
    """Build a real Task via the factory, applying the batch-eligibility flag."""
    task = make_task(**overrides)
    task.batch_eligible = batch_eligible
    return task


def _orch(
    *, default_adapter: str | None = None, role_policy: Any = None, workdir: Path | None = None
) -> SimpleNamespace:
    spawner = SimpleNamespace(default_adapter_name=default_adapter, role_model_policy=role_policy)
    return SimpleNamespace(_spawner=spawner, _workdir=workdir, _run_id="run-live")


# ---------------------------------------------------------------------------
# Adapter resolution
# ---------------------------------------------------------------------------


def test_resolve_task_adapter_prefers_role_policy_pin(make_task: Any) -> None:
    orch = _orch(default_adapter="claude", role_policy={"backend": {"adapter": "openai_agents"}})
    assert resolve_task_adapter(orch, _task(make_task, role="backend")) == "openai_agents"


def test_resolve_task_adapter_falls_back_to_default_adapter(make_task: Any) -> None:
    orch = _orch(default_adapter="claude", role_policy={})
    assert resolve_task_adapter(orch, _task(make_task)) == "claude"


def test_resolve_task_adapter_conservative_fallback_is_not_batch_capable(make_task: Any) -> None:
    orch = _orch(default_adapter=None, role_policy=None)
    # An unresolvable adapter falls back to a non-batch-capable name so work is
    # never routed to a batch surface that may not exist.
    adapter = resolve_task_adapter(orch, _task(make_task))
    decision = decide_batch_route(orch, _task(make_task, batch_eligible=True))
    assert adapter == "generic"
    assert decision.route == "interactive"


# ---------------------------------------------------------------------------
# Eligibility + capability-gated route
# ---------------------------------------------------------------------------


def test_is_batch_eligible_reads_explicit_flag(make_task: Any) -> None:
    assert is_batch_eligible(_task(make_task, batch_eligible=True)) is True
    assert is_batch_eligible(_task(make_task, batch_eligible=False)) is False


def test_is_batch_eligible_realtime_role_is_not_eligible(make_task: Any) -> None:
    assert is_batch_eligible(_task(make_task, role="manager", batch_eligible=None)) is False


def test_decide_batch_route_eligible_on_capable_adapter_routes_to_batch(make_task: Any) -> None:
    orch = _orch(default_adapter="claude")
    decision = decide_batch_route(orch, _task(make_task, batch_eligible=True))
    assert decision.route == "batch"
    assert decision.adapter_capable is True
    assert decision.refused_reason == ""


def test_decide_batch_route_eligible_on_incapable_adapter_is_refused(make_task: Any) -> None:
    orch = _orch(default_adapter="mock")
    decision = decide_batch_route(orch, _task(make_task, batch_eligible=True))
    assert decision.route == "interactive"
    assert decision.adapter_capable is False
    assert decision.refused_reason  # a reason is recorded, not faked onto a missing surface


def test_decide_batch_route_non_eligible_never_routes_to_batch(make_task: Any) -> None:
    orch = _orch(default_adapter="claude")
    decision = decide_batch_route(orch, _task(make_task, batch_eligible=False))
    assert decision.route == "interactive"
    assert decision.refused_reason == ""  # simply never a batch candidate


# ---------------------------------------------------------------------------
# Sealed receipt
# ---------------------------------------------------------------------------


def test_seal_batch_route_records_verifiable_audit_event(tmp_path: Path, make_task: Any) -> None:
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_COST_BATCH_ROUTE, AuditChainStore

    orch = _orch(default_adapter="claude", workdir=tmp_path)
    decision = decide_batch_route(orch, _task(make_task, id="T-seal", batch_eligible=True))

    seal_batch_route(orch, decision)

    hmac_key = load_or_create_audit_key()
    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=hmac_key)
    routes = chain.query(event_type=EVENT_COST_BATCH_ROUTE)
    assert len(routes) == 1
    assert routes[0].details["route"] == "batch"
    assert routes[0].details["task_id"] == "T-seal"
    assert routes[0].details["adapter"] == "claude"
    ok, errors = chain.verify()
    assert ok, errors


def test_seal_batch_route_without_workdir_is_a_noop(make_task: Any) -> None:
    orch = _orch(default_adapter="claude", workdir=None)
    decision = decide_batch_route(orch, _task(make_task, batch_eligible=True))
    # No workdir -> nothing to anchor; must not raise.
    seal_batch_route(orch, decision)


# ---------------------------------------------------------------------------
# Cache-window fan-out helper
# ---------------------------------------------------------------------------


def test_run_cache_window_fanout_capable_enabled_warms_once_then_m_hits() -> None:
    order: list[str] = []
    result = run_cache_window_fanout(
        adapter="claude",
        prefix="shared-prefix",
        worker_count=4,
        warmup_call=lambda: order.append("warmup"),
        worker_call=lambda i: (order.append(f"w{i}"), True)[1],
        enabled=True,
    )
    assert order == ["warmup", "w0", "w1", "w2", "w3"]  # warm-up strictly first
    assert result.warmup_calls_made == 1
    assert result.worker_calls_made == 4
    assert result.cache_hits == 4


def test_run_cache_window_fanout_disabled_issues_no_warmup() -> None:
    warmups: list[int] = []
    result = run_cache_window_fanout(
        adapter="claude",
        prefix="shared",
        worker_count=3,
        warmup_call=lambda: warmups.append(1),
        worker_call=lambda i: False,
        enabled=False,
    )
    assert warmups == []
    assert result.warmup_calls_made == 0
    assert result.worker_calls_made == 3


def test_run_cache_window_fanout_incapable_adapter_issues_no_warmup() -> None:
    warmups: list[int] = []
    result = run_cache_window_fanout(
        adapter="mock",
        prefix="shared",
        worker_count=2,
        warmup_call=lambda: warmups.append(1),
        worker_call=lambda i: False,
        enabled=True,
    )
    assert warmups == []
    assert result.warmup_calls_made == 0
