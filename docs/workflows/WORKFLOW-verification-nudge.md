# WORKFLOW: Verification Nudge for Unverified Completions
**Version**: 1.0
**Date**: 2026-04-04
**Author**: Workflow Architect
**Status**: Approved
**Implements**: Task d09d87a95c91

---

## Overview

Tracks when agents complete tasks without running any verification (tests,
quality gates, or completion signals) and surfaces alerts when the ratio of
unverified completions exceeds a configurable threshold.  An append-only JSONL
ledger persists records across crashes and restarts.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Agent | Completes a task; its log summary is checked for verification evidence |
| Orchestrator | Processes completed tasks through the task completion pipeline |
| VerificationNudgeTracker | Records verification status and computes summary |
| Status API | Exposes nudge summary at `/status/` endpoint |
| CLI status | Displays nudge alerts in `bernstein status` output |
| TUI dashboard | Fires toast alerts when threshold exceeded |

---

## Prerequisites

- `.sdd/metrics/` directory exists (created on first write).
- Task completion pipeline is active (`process_completed_tasks()`).

---

## Trigger

A task transitions to `completed` status.  The orchestrator calls
`process_completed_tasks()` which invokes the verification nudge recording.

---

## Single Source of Truth

**`src/bernstein/core/verification_nudge.py`** — tracker, records, summary.
**`src/bernstein/core/models.py`** — `Task.verification_count` and `Task.flagged_unverified` fields.

---

## Verification Evidence

A task is considered **verified** if any of these are true:

| Evidence type | Source | Field checked |
|---|---|---|
| Tests run | Agent log summary | `tests_run: bool` |
| Quality gates run | Quality gate result object | `quality_gates_run: bool` |
| Completion signals checked | Janitor verify_task() call | `completion_signals_checked: bool` |

Logic: `verified = tests_run OR quality_gates_run OR completion_signals_checked`

A task with none of these is **unverified**.

---

## Thresholds

| Parameter | Default | Description |
|---|---|---|
| `DEFAULT_NUDGE_THRESHOLD` | 0.3 (30%) | Unverified ratio above which alerts fire |
| `MIN_COMPLETIONS_FOR_NUDGE` | 3 | Minimum completions before threshold evaluation |

Threshold formula:
```
threshold_exceeded = total >= MIN_COMPLETIONS_FOR_NUDGE AND unverified_ratio > nudge_threshold
```

Uses strict `>` (not `>=`) — exactly 30% does not trigger.

---

## Workflow Tree

### STEP 1: Task Completes
**Actor**: Agent
**Action**: Agent finishes work and posts completion to task server.
**Output**: Task marked `completed`.

### STEP 2: Collect Verification Evidence
**Actor**: Orchestrator (task_completion.py)
**Action**: Extract verification signals from agent log summary and quality gate results.
**Input**: Task completion data, quality gate result, janitor verify_task status.
**Output**: `tests_run`, `quality_gates_run`, `completion_signals_checked` booleans.

### STEP 3: Record in Nudge Tracker
**Actor**: VerificationNudgeTracker
**Action**: `tracker.record(task_id, session_id, tests_run, quality_gates_run, completion_signals_checked)`
- Creates a `VerificationRecord` with `verified = any(tests_run, qg_run, signals_checked)`.
- Appends to in-memory list.
- Appends to JSONL ledger at `.sdd/metrics/verification_nudges.jsonl`.
**Output on SUCCESS**: `VerificationRecord` created.
**Output on FAILURE (disk write)**: Record kept in memory; warning logged.

### STEP 4: Stamp Task Fields
**Actor**: Orchestrator
**Action**: Set `task.verification_count` and `task.flagged_unverified` on the task object.
- `verification_count`: count of verification types that ran (0-3).
- `flagged_unverified`: True if `verified == False`.

### STEP 5: Surface Alerts
**Actor**: Status API / CLI / Dashboard (on next query)
**Action**: Call `load_nudge_summary(metrics_dir)` or read tracker summary.
**Output**: `NudgeSummary` with counts, ratio, and `threshold_exceeded` flag.

**Display paths**:

| Surface | Condition | Display |
|---|---|---|
| `/status/` API | Always | `verification_nudge` object in JSON response |
| `bernstein status` CLI | `threshold_exceeded` | Red "ALERT" with counts and ratio |
| `bernstein status` CLI | unverified > 0 | Yellow "Notice" with counts |
| TUI dashboard | `threshold_exceeded` (first time) | Toast notification, severity=warning, 10s timeout |

---

## State Transitions

```
[task completed] -> collect evidence -> [record created]
[record created] -> stamp task fields -> [task.flagged_unverified set]
[summary queried] -> evaluate threshold -> [alert surfaced or not]
```

---

## Handoff Contracts

### Orchestrator -> VerificationNudgeTracker
**Method**: `tracker.record(task_id=..., session_id=..., tests_run=..., quality_gates_run=..., completion_signals_checked=...)`
**Success**: Returns `VerificationRecord`.
**Failure**: Disk write failure logged as warning; in-memory state preserved.

### VerificationNudgeTracker -> JSONL Ledger
**Path**: `.sdd/metrics/verification_nudges.jsonl`
**Format**: One JSON object per line.
**Schema**:
```json
{
  "task_id": "string",
  "session_id": "string",
  "timestamp": 1712200000.0,
  "tests_run": false,
  "quality_gates_run": false,
  "completion_signals_checked": false,
  "verified": false
}
```

### Status API -> NudgeSummary
**Endpoint**: `GET /status/`
**Response field**: `verification_nudge`
```json
{
  "total_completions": 10,
  "verified_count": 6,
  "unverified_count": 4,
  "unverified_ratio": 0.4,
  "threshold_exceeded": true,
  "nudge_threshold": 0.3,
  "recent_unverified": ["task-a", "task-b", "task-c"]
}
```

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| JSONL ledger lines | Step 3 | Manual / session reset | Delete file or `tracker.reset()` |
| In-memory records | Step 3 | Process exit or `reset()` | Garbage collected |
| Task field stamps | Step 4 | Task store lifecycle | Follows task TTL |

---

## Test Cases

All tests in `tests/unit/test_verification_nudge.py` (44 tests, 8 classes).

| Test | Class | What it covers |
|---|---|---|
| TC-01: Record serialization | TestVerificationRecord | to_dict/from_dict round-trip |
| TC-02: Verification logic | TestVerificationRecord | verified=True when any evidence present |
| TC-03: Tracker recording | TestVerificationNudgeTracker | Record creation, summary, dedup |
| TC-04: Persistence | TestVerificationNudgePersistence | JSONL write/read, crash recovery |
| TC-05: Summary math | TestNudgeSummary | Ratio, threshold evaluation, serialization |
| TC-06: One-shot loading | TestLoadNudgeSummary | load_nudge_summary() from disk |
| TC-07: Evidence combinations | TestVerificationEvidence | All 8 boolean combinations (parametrized) |
| TC-08: Task model fields | TestTaskVerificationFields | verification_count, flagged_unverified defaults |
| TC-09: Alert threshold | TestNudgeSummaryAlert | Threshold boundary conditions |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Agent log summary reliably indicates whether tests were run | task_completion.py | False negatives: verified tasks marked unverified |
| A2 | JSONL append is atomic enough for crash safety | OS-level write | Partial line on crash; loader skips malformed lines |
| A3 | MIN_COMPLETIONS_FOR_NUDGE=3 avoids noisy alerts early in a run | Configurable | Could be too low for large runs |

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-04 | Initial spec created from implemented code | All code paths verified against 44 tests |
| 2026-04-04 | Full integration chain verified: models → task_completion → tracker → API → CLI → dashboard | No gaps found |
