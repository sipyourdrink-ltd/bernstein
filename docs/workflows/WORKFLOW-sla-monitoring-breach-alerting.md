# WORKFLOW: SLA Monitoring with Breach Alerting
**Version**: 0.1
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-005

---

## Overview

The SLA monitoring workflow continuously evaluates configurable SLA definitions (e.g., "95% of tasks complete within 30 minutes") against rolling-window metric observations. When an SLA transitions from MET to WARNING or BREACHED, the system emits alerts through pluggable channels (webhook, bulletin, log). Operators can acknowledge alerts and view a dashboard of all SLA states.

---

## Actors
| Actor | Role in this workflow |
|---|---|
| Orchestrator tick loop | Feeds metric observations (task completion, duration, error rates) to the SLA monitor each tick |
| SLAMonitor | Evaluates SLA definitions against current metric values; emits alerts on status transitions |
| Alert callback chain | Delivers alerts to configured channels (webhook, bulletin board, log, Slack) |
| Task server API | Exposes SLA dashboard, alert history, and alert acknowledgement endpoints |
| Operator | Views dashboard, acknowledges alerts, configures SLA definitions |
| SOC 2 report | Reads SLA evaluation history for compliance evidence |

---

## Prerequisites
- Task server is running and healthy at `http://127.0.0.1:8052`
- At least one SLA definition is configured (defaults provided by `default_sla_definitions()`)
- Metric observations are being recorded (task completions, durations, errors)

---

## Trigger
**Primary**: Orchestrator tick loop calls `sla_monitor.evaluate()` once per tick (every 5–30 seconds depending on adaptive tick rate).
**Secondary**: Operator hits `GET /sla/dashboard` or `GET /sla/alerts` on demand.
**Configuration**: Operator creates/updates SLA definitions via `POST /sla/definitions` or `bernstein.yaml` → `sla:` section.

---

## Workflow Tree

### STEP 1: Record metric observations
**Actor**: Orchestrator tick loop
**Action**: After each tick, record observations for all tracked metrics:
  - `TASK_COMPLETION_RATE`: 1.0 for success, 0.0 for failure, per completed task
  - `TASK_DURATION_P95`/`P99`: elapsed seconds per completed task
  - `ERROR_RATE`: 1.0 for error, 0.0 for non-error, per completed task
  - `AGENT_AVAILABILITY`: 1.0 if agents available, 0.0 if pool exhausted
  - `RESPONSE_TIME`: API response latency (if instrumented)
**Timeout**: N/A (synchronous, in-process)
**Input**: `{ metric: SLAMetricKind, value: float, timestamp: float }`
**Output on SUCCESS**: Observation appended to in-memory ring buffer → GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(invalid_metric)`: Unknown metric kind → log warning, skip observation, no cleanup needed
  - `FAILURE(memory_pressure)`: Observation buffer too large → prune oldest entries, log warning, continue

**Observable states during this step**:
  - Customer sees: Nothing (background operation)
  - Operator sees: Metric observation count incrementing in `/status` dashboard
  - Database: No persistence at this step (in-memory only)
  - Logs: `[sla_monitor] recorded observation metric=task_completion_rate value=1.0`

---

### STEP 2: Evaluate SLA definitions
**Actor**: SLAMonitor.evaluate()
**Action**: For each registered SLA definition:
  1. Prune observations outside the rolling window (`window_seconds`)
  2. Compute current metric value from remaining observations
  3. Compare against `target` and `warning_threshold`
  4. Determine status: MET, WARNING, BREACHED, or UNKNOWN (insufficient data)
  5. Track breach duration if BREACHED (record breach start time)
  6. Detect status transitions (previous status → current status)
**Timeout**: Must complete within 100ms (evaluation is CPU-only, no IO)
**Input**: All registered SLA definitions + in-memory observations
**Output on SUCCESS**: `list[SLAEvaluation]` with status per SLA → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(no_observations)`: Metric has no data in window → status = UNKNOWN, no alert emitted
  - `FAILURE(computation_error)`: Division by zero or data corruption → log error, status = UNKNOWN for that SLA, continue evaluating others

**Observable states during this step**:
  - Customer sees: Nothing
  - Operator sees: SLA statuses updated on dashboard (MET/WARNING/BREACHED/UNKNOWN)
  - Database: `breach_start` dict updated in memory; persisted via `save_state()` on graceful shutdown
  - Logs: `[sla_monitor] evaluated 3 SLAs: 2 met, 0 warning, 1 breached`

**Metric direction logic** (critical implementation detail):
  - Higher-is-better metrics (completion rate, availability): `current >= target` → MET
  - Lower-is-better metrics (duration, error rate, response time): `current <= target` → MET
  - WARNING zone: between target and warning_threshold (direction-dependent)

---

### STEP 3: Emit alerts on status transitions
**Actor**: SLAMonitor._emit_alert()
**Action**: For each SLA where status changed:
  - `* → WARNING` (not from WARNING/BREACHED): Emit `imminent` alert (severity: warning)
  - `* → BREACHED` (not from BREACHED): Emit `breached` alert (severity: from SLA definition)
  - `WARNING/BREACHED → MET`: Emit `recovered` alert (severity: info)
  - No transition (same status as last eval): No alert emitted (prevents duplicate spam)
**Timeout**: 5s per callback invocation
**Input**: `SLAAlert { sla_name, alert_type, severity, message, evaluation, created_at }`
**Output on SUCCESS**: Alert appended to history + callback invoked → GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(callback_error)`: Alert callback throws → log warning, alert still stored in history, continue
  - `FAILURE(callback_timeout)`: Callback takes >5s → treat as callback_error, do not retry

**Observable states during this step**:
  - Customer sees: Nothing (internal alerting)
  - Operator sees: New alert in `/sla/alerts` endpoint; webhook/Slack notification if configured
  - Database: Alert appended to `_alerts` list (in-memory); persisted count in `save_state()`
  - Logs: `[sla_monitor] SLA alert: [breached] task_completion_rate — SLA 'task_completion_rate' BREACHED: 0.8500 (target: 0.90)`

**Alert deduplication rules**:
  - Only one alert per SLA per transition (MET→WARNING = 1 alert, not N)
  - Sustained BREACHED state does NOT re-alert (breach duration tracked instead)
  - Recovery from BREACHED→MET emits exactly one `recovered` alert

---

### STEP 4: Persist state (periodic)
**Actor**: SLAMonitor.save_state()
**Action**: Serialize current state to disk:
  - All SLA definitions (name, metric, target, thresholds)
  - Breach start times per SLA
  - Last known status per SLA
  - Alert count
**Timeout**: 1s (file write)
**Input**: `Path` to state file (e.g., `.sdd/runtime/sla_state.json`)
**Output on SUCCESS**: State file written → cycle complete, return to STEP 1 on next tick
**Output on FAILURE**:
  - `FAILURE(disk_full)`: Cannot write state → log error, continue operating from memory (state will be lost on restart)
  - `FAILURE(permission_error)`: Cannot write to path → log error, continue

**Observable states during this step**:
  - Customer sees: Nothing
  - Operator sees: `.sdd/runtime/sla_state.json` updated on disk
  - Database: State file updated
  - Logs: `[sla_monitor] state saved to .sdd/runtime/sla_state.json`

---

### STEP 5: Serve dashboard and alerts (on demand)
**Actor**: Task server API routes
**Action**: Expose SLA state via HTTP endpoints:
  - `GET /sla/dashboard` → calls `sla_monitor.get_dashboard()` → returns all SLA statuses + active alerts
  - `GET /sla/alerts?unacknowledged_only=true` → returns alert history
  - `POST /sla/alerts/{index}/acknowledge` → marks alert as acknowledged
  - `GET /sla/definitions` → lists current SLA definitions
  - `POST /sla/definitions` → adds/updates an SLA definition
  - `DELETE /sla/definitions/{name}` → removes an SLA definition
**Timeout**: 5s per request
**Input**: HTTP request
**Output on SUCCESS**: JSON response with SLA data
**Output on FAILURE**:
  - `FAILURE(monitor_not_initialized)`: SLA monitor not wired → return 503 with message
  - `FAILURE(invalid_index)`: Alert index out of range → return 404
  - `FAILURE(invalid_definition)`: Bad SLA definition payload → return 422 with validation errors

**Observable states during this step**:
  - Customer sees: N/A (operator-facing)
  - Operator sees: Dashboard with SLA status cards, alert list, acknowledge buttons
  - Database: No writes (read-only except acknowledge)
  - Logs: Standard access logs

---

## State Transitions

```
[unknown] -> (first observation recorded, evaluate called) -> [met | warning | breached]
[met] -> (metric degrades past warning_threshold) -> [warning]
[met] -> (metric degrades past target) -> [breached]
[warning] -> (metric degrades past target) -> [breached]
[warning] -> (metric recovers above target) -> [met]
[breached] -> (metric recovers above target) -> [met]
[breached] -> (metric recovers to warning zone) -> [warning]  // NOTE: not currently implemented — goes directly to MET
```

**Gap found**: The current implementation transitions directly from BREACHED to MET if `current >= target`. There is no BREACHED → WARNING transition. An SLA that improves from BREACHED to the warning zone still shows as BREACHED until it fully meets the target. This may be intentional (conservative) but should be explicitly documented as a design decision.

---

## Handoff Contracts

### Orchestrator tick loop → SLAMonitor
**Method**: Direct Python call (in-process, no HTTP)
**Payload**:
```python
sla_monitor.record_observation(
    metric=SLAMetricKind.TASK_COMPLETION_RATE,
    value=1.0,  # float: 0.0-1.0 for rates, seconds for durations
    timestamp=time.time(),  # optional, defaults to now
)
```
**Success response**: None (void)
**Failure response**: Raises no exceptions (logs warnings internally)
**Timeout**: N/A (synchronous)

### SLAMonitor → Alert callback
**Method**: Python callable `Callable[[SLAAlert], None]`
**Payload**:
```python
SLAAlert(
    sla_name="task_completion_rate",
    alert_type="breached",  # "imminent" | "breached" | "recovered"
    severity="critical",    # from SLA definition
    message="SLA 'task_completion_rate' BREACHED: 0.8500 (target: 0.90)",
    evaluation=SLAEvaluation(...),
    created_at=1712563200.0,
)
```
**Success response**: None (void)
**Failure response**: Any exception → caught and logged, alert still stored
**Timeout**: 5s recommended (not enforced in current implementation)

### API → SLAMonitor.get_dashboard()
**Endpoint**: `GET /sla/dashboard`
**Payload**: None
**Success response**:
```json
{
  "slas": [
    {
      "name": "task_completion_rate",
      "metric": "task_completion_rate",
      "target": 0.90,
      "current": 0.92,
      "status": "met",
      "breach_duration_s": 0.0
    }
  ],
  "active_alerts": [],
  "total_alerts": 5
}
```
**Failure response**:
```json
{
  "detail": "SLA monitor not initialized"
}
```
**Timeout**: 5s

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| In-memory observations | Step 1 | Window pruning (Step 2) | Automatic — observations outside `window_seconds` are discarded |
| In-memory alert history | Step 3 | Never (grows unbounded) | **GAP**: No alert history pruning — potential memory leak over long runs |
| State file on disk | Step 4 | Orchestrator shutdown / manual deletion | File delete |
| Breach start timestamps | Step 2 | SLA status recovery (Step 2) | Automatic — cleared when status returns to MET |

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | SLAMonitor is never instantiated or wired into the orchestrator tick loop | Critical | Steps 1-2 | No route exposes SLA data. No tick callback records observations. The module is dead code. |
| RC-2 | Alert history grows unbounded — no pruning, no max size | Medium | Cleanup Inventory | Add configurable max_alerts or time-based pruning |
| RC-3 | No API routes exist for `/sla/*` endpoints | High | Step 5 | Routes must be created in `src/bernstein/core/routes/` |
| RC-4 | `save_state()` exists but is never called — state is lost on restart | High | Step 4 | Wire to graceful shutdown hook or periodic save |
| RC-5 | `from_config()` exists but no config file schema or loader references it | Medium | Prerequisites | Define `sla:` section in `bernstein.yaml` schema |
| RC-6 | No BREACHED → WARNING transition path | Low | State Transitions | Document as intentional or implement the transition |
| RC-7 | Alert callback timeout is not enforced (no `asyncio.wait_for` or threading timeout) | Medium | Step 3 | A slow callback blocks the tick loop |
| RC-8 | Only referenced by `soc2_report.py` as an evidence type string — no actual data flow | Low | Actors | SOC 2 integration is aspirational, not functional |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — all SLAs met | Record 100% completion rate, evaluate | All SLAs status=MET, no alerts |
| TC-02: Warning threshold crossed | Record 92% completion (warning=0.92, target=0.90) | Status=WARNING, `imminent` alert emitted |
| TC-03: Breach threshold crossed | Record 85% completion | Status=BREACHED, `breached` alert emitted with severity from definition |
| TC-04: Recovery from breach | Breach then record 95% completion | Status=MET, `recovered` alert emitted |
| TC-05: No duplicate alerts on sustained breach | Evaluate twice while breached | Only one `breached` alert total |
| TC-06: Breach duration tracking | Breach at T=0, evaluate at T=60 | `breach_duration_s` = 60.0 |
| TC-07: Window pruning | Record observation, advance time past window | Observation excluded from metric computation |
| TC-08: Insufficient data | Evaluate with no observations | Status=UNKNOWN, no alerts |
| TC-09: Lower-is-better metric (error rate) | Record 12% error rate (target=10%) | Status=BREACHED |
| TC-10: Alert callback failure | Callback raises exception | Alert still stored in history, warning logged |
| TC-11: Dashboard endpoint | GET /sla/dashboard | Returns all SLA statuses and active alerts |
| TC-12: Alert acknowledgement | POST /sla/alerts/0/acknowledge | Alert marked acknowledged, excluded from active_alerts |
| TC-13: Add/remove SLA definition | POST then DELETE /sla/definitions | Definition created then removed, no orphan state |
| TC-14: State persistence round-trip | save_state() then new SLAMonitor from file | Definitions and breach state restored |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Orchestrator tick loop is the right place to feed observations | Not verified — tick loop code does not reference SLA monitor | Observations never recorded; SLA monitor stays dead code |
| A2 | In-memory storage is sufficient for observation windows (1 hour default) | Not verified | At 1 observation/task/tick, ~3600 observations/hour/metric — acceptable |
| A3 | Alert callback is synchronous and should not block the tick loop | Verified: callback is sync, no timeout enforcement | Slow webhook delivery blocks orchestrator |
| A4 | `save_state()` preserves enough to resume after restart | Verified: saves definitions, breach_start, last_status | Observations are NOT saved — metric history is lost on restart, first eval after restart will be UNKNOWN |
| A5 | The `/sla/*` route prefix is available and not conflicting | Not verified | Route collision possible |

## Open Questions

- Should observations be persisted to disk (e.g., append to JSONL) so metric history survives restarts? Current implementation loses all observations on restart.
- Should there be a maximum alert history size with automatic pruning?
- Should the BREACHED → WARNING transition be implemented, or is direct BREACHED → MET intentional?
- What alert delivery channels are required beyond the callback? (Webhook, Slack, bulletin board, email?)
- Should SLA definitions be hot-reloadable from `bernstein.yaml` or only via API?
- Should SLA breaches trigger orchestrator behavior changes (e.g., pause spawning, increase priority of completing existing tasks)?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created — discovered SLA monitor is dead code (RC-1) | Documented all integration gaps |
| 2026-04-08 | No API routes, no tick wiring, no config loading, no state persistence calls | Spec defines required wiring points for implementation |
