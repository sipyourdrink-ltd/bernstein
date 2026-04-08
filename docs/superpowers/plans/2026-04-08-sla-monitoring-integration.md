# SLA Monitoring Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing `sla_monitor.py` into the orchestrator tick loop, expose SLA status via REST API routes, and connect breach alerts to the notification system — making the SLA monitor operational end-to-end.

**Architecture:** `SLAMonitor` already implements breach detection, alert generation, and state persistence. This plan integrates it: (1) orchestrator feeds task metrics into the monitor each tick, (2) new `/sla` REST routes expose dashboard/alerts, (3) alert callbacks dispatch to `NotificationManager`. No new monitoring logic is needed — only wiring.

**Tech Stack:** Python 3.12+, FastAPI, existing `SLAMonitor`, `NotificationManager`, `MetricsCollector`

---

## Discovery Audit — Current State

### What EXISTS and is TESTED:
- `src/bernstein/core/sla_monitor.py` — Full `SLAMonitor` with `SLADefinition`, `SLAEvaluation`, `SLAAlert`, rolling windows, breach duration tracking, alert callbacks, `from_config()`, `save_state()`, `get_dashboard()`
- `tests/unit/test_sla_monitor.py` — 445 lines covering all status transitions, alert generation, persistence, config loading
- `src/bernstein/core/notifications.py` — `NotificationManager` with Slack/Discord/Telegram/PagerDuty/email/webhook/desktop dispatch
- `src/bernstein/core/alert_rules.py` — `AlertManager` with cooldown-aware rule evaluation

### What is NOT INTEGRATED:
1. **Orchestrator** (`orchestrator.py`) uses `SLOTracker` (from `slo.py`) but does NOT instantiate or feed `SLAMonitor`
2. **No `/sla` REST routes** — `/slo` routes exist but no SLA equivalents
3. **Alert callback not wired** — `SLAMonitor` accepts `alert_callback` but nothing connects it to `NotificationManager`
4. **No metric feeding** — task completion/failure events don't record observations into `SLAMonitor`
5. **No config-driven SLA loading** — definitions are created programmatically, no YAML/config file loading

### What is OUT OF SCOPE:
- Modifying `SLAMonitor` internals (already works correctly)
- Adding new metric types (existing 6 metric kinds are sufficient)
- UI/TUI changes (API-only)

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `src/bernstein/core/routes/sla.py` | REST API routes for SLA dashboard, alerts, acknowledgment |
| Modify | `src/bernstein/core/orchestrator.py:99,535,1287-1312` | Instantiate `SLAMonitor`, feed metrics, wire alert callback |
| Create | `tests/unit/test_sla_routes.py` | Route-level tests for `/sla` endpoints |
| Create | `tests/unit/test_sla_orchestrator_integration.py` | Integration test: orchestrator feeds SLAMonitor |

---

### Task 1: Create SLA REST Routes

**Files:**
- Create: `src/bernstein/core/routes/sla.py`
- Test: `tests/unit/test_sla_routes.py`

- [ ] **Step 1: Write the failing test for GET /sla/dashboard**

```python
"""Tests for SLA monitoring REST routes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.routes.sla import router
from bernstein.core.sla_monitor import (
    SLADefinition,
    SLAMetricKind,
    SLAMonitor,
    default_sla_definitions,
)


@pytest.fixture()
def app() -> FastAPI:
    """Create a FastAPI app with SLA routes and a pre-configured monitor."""
    app = FastAPI()
    app.include_router(router)
    monitor = SLAMonitor(definitions=default_sla_definitions())
    app.state.sla_monitor = monitor
    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


class TestSLADashboardRoute:
    def test_get_dashboard(self, client: TestClient) -> None:
        resp = client.get("/sla/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "slas" in data
        assert "active_alerts" in data
        assert "total_alerts" in data
        assert len(data["slas"]) == len(default_sla_definitions())

    def test_dashboard_with_observations(self, app: FastAPI, client: TestClient) -> None:
        monitor: SLAMonitor = app.state.sla_monitor
        now = 1000.0
        for i in range(20):
            monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        resp = client.get("/sla/dashboard")
        assert resp.status_code == 200
        slas = resp.json()["slas"]
        completion_sla = next(s for s in slas if s["metric"] == "task_completion_rate")
        assert completion_sla["status"] in ("met", "unknown")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_sla_routes.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bernstein.core.routes.sla'`

- [ ] **Step 3: Write the SLA routes module**

```python
"""SLA (Service Level Agreement) monitoring REST routes.

Exposes SLA dashboard, alert history, and alert acknowledgment via REST API.
Mirrors the ``/slo`` routes pattern for consistency.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.sla_monitor import SLAMonitor, default_sla_definitions

logger = logging.getLogger(__name__)

router = APIRouter()

_monitor: SLAMonitor | None = None


def _get_monitor(request: Request) -> SLAMonitor:
    """Return the SLAMonitor from app state (prefer) or module fallback."""
    app_monitor = getattr(request.app.state, "sla_monitor", None)
    if app_monitor is not None:
        return app_monitor  # type: ignore[return-value]
    global _monitor  # noqa: PLW0603
    if _monitor is None:
        _monitor = SLAMonitor(definitions=default_sla_definitions())
    return _monitor


@router.get("/sla/dashboard")
def get_sla_dashboard(request: Request) -> JSONResponse:
    """Return current SLA dashboard with all evaluations and active alerts."""
    monitor = _get_monitor(request)
    return JSONResponse(monitor.get_dashboard())


@router.get("/sla/alerts")
def get_sla_alerts(request: Request, unacknowledged: bool = False) -> JSONResponse:
    """Return SLA alert history."""
    monitor = _get_monitor(request)
    alerts = monitor.get_alerts(unacknowledged_only=unacknowledged)
    return JSONResponse({"alerts": [a.to_dict() for a in alerts], "count": len(alerts)})


@router.post("/sla/alerts/{index}/acknowledge")
def acknowledge_sla_alert(request: Request, index: int) -> JSONResponse:
    """Acknowledge an SLA alert by index."""
    monitor = _get_monitor(request)
    if monitor.acknowledge_alert(index):
        return JSONResponse({"status": "acknowledged"})
    return JSONResponse({"error": "alert not found"}, status_code=404)


@router.get("/sla/definitions")
def get_sla_definitions(request: Request) -> JSONResponse:
    """Return all configured SLA definitions."""
    monitor = _get_monitor(request)
    # Access internal definitions via dashboard (avoid exposing private attrs)
    dashboard = monitor.get_dashboard()
    return JSONResponse({"definitions": dashboard["slas"]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_sla_routes.py -x -q`
Expected: PASS

- [ ] **Step 5: Write tests for alert and acknowledgment routes**

Add to `tests/unit/test_sla_routes.py`:

```python
class TestSLAAlertRoutes:
    def test_get_alerts_empty(self, client: TestClient) -> None:
        resp = client.get("/sla/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["alerts"] == []
        assert data["count"] == 0

    def test_get_alerts_after_breach(self, app: FastAPI, client: TestClient) -> None:
        monitor: SLAMonitor = app.state.sla_monitor
        now = 1000.0
        for i in range(10):
            monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        monitor.evaluate(now=now + 10)
        resp = client.get("/sla/alerts")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_get_unacknowledged_alerts(self, app: FastAPI, client: TestClient) -> None:
        monitor: SLAMonitor = app.state.sla_monitor
        now = 1000.0
        for i in range(10):
            monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        monitor.evaluate(now=now + 10)
        resp = client.get("/sla/alerts?unacknowledged=true")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_acknowledge_alert(self, app: FastAPI, client: TestClient) -> None:
        monitor: SLAMonitor = app.state.sla_monitor
        now = 1000.0
        for i in range(10):
            monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        monitor.evaluate(now=now + 10)
        resp = client.post("/sla/alerts/0/acknowledge")
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"
        # Verify it's now acknowledged
        resp2 = client.get("/sla/alerts?unacknowledged=true")
        assert resp2.json()["count"] == 0

    def test_acknowledge_nonexistent_alert(self, client: TestClient) -> None:
        resp = client.post("/sla/alerts/999/acknowledge")
        assert resp.status_code == 404


class TestSLADefinitionsRoute:
    def test_get_definitions(self, client: TestClient) -> None:
        resp = client.get("/sla/definitions")
        assert resp.status_code == 200
        data = resp.json()
        assert "definitions" in data
        assert len(data["definitions"]) == len(default_sla_definitions())
```

- [ ] **Step 6: Run all route tests**

Run: `uv run pytest tests/unit/test_sla_routes.py -x -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/bernstein/core/routes/sla.py tests/unit/test_sla_routes.py
git commit -m "feat(sla): add REST API routes for SLA dashboard, alerts, and definitions"
```

---

### Task 2: Wire SLAMonitor into Orchestrator Tick Loop

**Files:**
- Modify: `src/bernstein/core/orchestrator.py:99,535,1285-1312`
- Test: `tests/unit/test_sla_orchestrator_integration.py`

- [ ] **Step 1: Write the integration test**

```python
"""Integration tests for SLA monitor wiring in orchestrator."""

from __future__ import annotations

from bernstein.core.sla_monitor import (
    SLAAlert,
    SLAMetricKind,
    SLAMonitor,
    SLAStatus,
    default_sla_definitions,
)


class TestSLAMonitorMetricFeeding:
    """Test that task completion metrics feed into SLAMonitor."""

    def test_task_success_records_completion_rate(self) -> None:
        monitor = SLAMonitor(definitions=default_sla_definitions())
        now = 1000.0
        # Simulate feeding task completion observations
        for i in range(20):
            monitor.record_observation(
                SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i
            )
        results = monitor.evaluate(now=now + 20)
        completion = next(
            r for r in results if r.metric == SLAMetricKind.TASK_COMPLETION_RATE
        )
        assert completion.status == SLAStatus.MET

    def test_task_failure_records_error_rate(self) -> None:
        monitor = SLAMonitor(definitions=default_sla_definitions())
        now = 1000.0
        # 50% error rate — should breach the 10% target
        for i in range(10):
            monitor.record_observation(SLAMetricKind.ERROR_RATE, 0.5, timestamp=now + i)
        results = monitor.evaluate(now=now + 10)
        error_sla = next(r for r in results if r.metric == SLAMetricKind.ERROR_RATE)
        assert error_sla.status == SLAStatus.BREACHED

    def test_task_duration_records_p95(self) -> None:
        monitor = SLAMonitor(definitions=default_sla_definitions())
        now = 1000.0
        # Record durations under 30min (1800s) target
        for i in range(20):
            monitor.record_observation(
                SLAMetricKind.TASK_DURATION_P95, 600.0, timestamp=now + i
            )
        results = monitor.evaluate(now=now + 20)
        duration = next(
            r for r in results if r.metric == SLAMetricKind.TASK_DURATION_P95
        )
        assert duration.status == SLAStatus.MET

    def test_alert_callback_invoked_on_breach(self) -> None:
        alerts: list[SLAAlert] = []
        monitor = SLAMonitor(
            definitions=default_sla_definitions(),
            alert_callback=lambda a: alerts.append(a),
        )
        now = 1000.0
        for i in range(10):
            monitor.record_observation(
                SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i
            )
        monitor.evaluate(now=now + 10)
        assert len(alerts) >= 1
        assert any(a.alert_type == "breached" for a in alerts)
```

- [ ] **Step 2: Run test to verify it passes (pure SLAMonitor, no orchestrator changes yet)**

Run: `uv run pytest tests/unit/test_sla_orchestrator_integration.py -x -q`
Expected: PASS (these test the SLAMonitor behavior the orchestrator will use)

- [ ] **Step 3: Add SLAMonitor import and initialization to orchestrator**

In `src/bernstein/core/orchestrator.py`, add import at line ~99 (near existing SLO import):

```python
from bernstein.core.sla_monitor import SLAMonitor, SLAMetricKind, default_sla_definitions
```

In `__init__` around line ~535 (near `self._slo_tracker = SLOTracker()`), add:

```python
self._sla_monitor = SLAMonitor(
    definitions=default_sla_definitions(),
    alert_callback=self._on_sla_alert,
)
```

Add the alert callback method to the `Orchestrator` class:

```python
def _on_sla_alert(self, alert: SLAAlert) -> None:
    """Forward SLA alerts to the notification system."""
    from bernstein.core.sla_monitor import SLAAlert as _SLAAlert  # noqa: F811

    event = "incident.critical" if alert.severity == "critical" else "budget.warning"
    self._notify(
        event,
        title=f"SLA Alert: {alert.sla_name}",
        body=alert.message,
        metadata={"alert_type": alert.alert_type, "severity": alert.severity},
    )
```

- [ ] **Step 4: Feed task metrics into SLAMonitor in the slow tick**

In the `_run_slow` block (around line 1287), after the existing SLO update code, add:

```python
# --- SLA monitoring (ENT-005) ---
now_ts = time.time()
# Feed completion rate: 1.0 for success, 0.0 for failure
for t in done_tasks:
    self._sla_monitor.record_observation(
        SLAMetricKind.TASK_COMPLETION_RATE,
        1.0,
        timestamp=now_ts,
    )
for t in failed_this_tick:
    self._sla_monitor.record_observation(
        SLAMetricKind.TASK_COMPLETION_RATE,
        0.0,
        timestamp=now_ts,
    )
    self._sla_monitor.record_observation(
        SLAMetricKind.ERROR_RATE,
        1.0,
        timestamp=now_ts,
    )
# Feed task durations
for t in done_tasks:
    if hasattr(t, "started_at") and t.started_at and hasattr(t, "completed_at") and t.completed_at:
        duration_s = t.completed_at - t.started_at
        self._sla_monitor.record_observation(
            SLAMetricKind.TASK_DURATION_P95,
            duration_s,
            timestamp=now_ts,
        )
# Evaluate all SLAs (triggers alerts via callback)
self._sla_monitor.evaluate(now=now_ts)
# Persist state
sla_state_path = self._workdir / ".sdd" / "metrics" / "sla_state.json"
self._sla_monitor.save_state(sla_state_path)
```

- [ ] **Step 5: Expose SLAMonitor on app state for routes**

In the server/app setup code where `app.state.slo_tracker` is set, add:

```python
app.state.sla_monitor = self._sla_monitor
```

And include the SLA router:

```python
from bernstein.core.routes.sla import router as sla_router
app.include_router(sla_router)
```

- [ ] **Step 6: Run existing SLA tests to verify no regressions**

Run: `uv run pytest tests/unit/test_sla_monitor.py tests/unit/test_sla_routes.py tests/unit/test_sla_orchestrator_integration.py -x -q`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/bernstein/core/orchestrator.py tests/unit/test_sla_orchestrator_integration.py
git commit -m "feat(sla): wire SLAMonitor into orchestrator tick loop with notification alerts"
```

---

## Workflow Spec: SLA Monitoring Evaluation Cycle

```
TRIGGER: Orchestrator slow tick (every 30 ticks, ~30s)

STEP 1: Collect task outcomes from current tick
  Actor: Orchestrator._tick()
  Input: done_tasks (list[Task]), failed_tasks (list[Task])
  Action: For each done task, record_observation(TASK_COMPLETION_RATE, 1.0)
          For each failed task, record_observation(TASK_COMPLETION_RATE, 0.0) + record_observation(ERROR_RATE, 1.0)
          For each done task with timestamps, record_observation(TASK_DURATION_P95, duration_s)
  Timeout: N/A (in-memory, <1ms)
  Output: Observations appended to rolling windows

STEP 2: Evaluate all SLA definitions
  Actor: SLAMonitor.evaluate()
  Action: For each SLADefinition, compute metric from rolling window,
          compare against target/warning_threshold, track breach duration
  Output on SUCCESS: List[SLAEvaluation]
  Side effects:
    - If status transitions WARNING→BREACHED or UNKNOWN→BREACHED: emit SLAAlert(type="breached")
    - If status transitions UNKNOWN→WARNING or MET→WARNING: emit SLAAlert(type="imminent")
    - If status transitions BREACHED→MET or WARNING→MET: emit SLAAlert(type="recovered")

STEP 3: Alert dispatch (via callback)
  Actor: Orchestrator._on_sla_alert() → NotificationManager
  Action: Map alert severity to notification event, dispatch to subscribed targets
  Failure: Notification dispatch failures are swallowed (logged, never crash the tick)

STEP 4: Persist state
  Actor: SLAMonitor.save_state()
  Action: Write definitions + breach timestamps + alert count to .sdd/metrics/sla_state.json
  Failure: IO error logged, non-fatal

STATE TRANSITIONS:
  [unknown] → (data arrives, meets target) → [met]
  [unknown] → (data arrives, in warning zone) → [warning] + alert(imminent)
  [unknown] → (data arrives, below target) → [breached] + alert(breached)
  [met] → (degrades into warning zone) → [warning] + alert(imminent)
  [warning] → (degrades below target) → [breached] + alert(breached)
  [breached] → (recovers above target) → [met] + alert(recovered)
  [warning] → (recovers above target) → [met] + alert(recovered)
```

---

## Assumptions

| # | Assumption | Verified | Risk if wrong |
|---|-----------|----------|---------------|
| A1 | `done_tasks` and `failed_this_tick` are available in the slow tick scope | Needs verification at integration time | Metric feeding silently produces no data |
| A2 | Task objects have `started_at` and `completed_at` float timestamps | Needs verification against Task model | Duration metrics won't populate |
| A3 | `self._notify()` exists on Orchestrator and dispatches to NotificationManager | Verified in orchestrator.py | Alert callbacks silently fail |
| A4 | SLA routes module will be auto-discovered by app router setup | Needs verification at integration time | Routes 404 until manually included |
