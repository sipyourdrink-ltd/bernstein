# WORKFLOW: Audit Log Integrity Verification on Startup
**Version**: 1.0
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-003

---

## Overview

When the orchestrator starts, it verifies the HMAC chain of the last N audit log entries (configurable, default 100) before entering the main tick loop. If any entries have been tampered with or the chain is broken, the orchestrator logs a warning and records the result. This catches tampering that occurred while the orchestrator was offline, before any new audit events are appended to a potentially compromised chain.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator (`orchestrator.py`) | Triggers verification during `run()` startup |
| `audit_integrity.py` | Loads tail entries, recomputes HMACs, returns result |
| Audit log files (`.sdd/audit/*.jsonl`) | Source of HMAC-chained entries to verify |
| HMAC key file (`.sdd/config/audit-key`) | Secret used for HMAC computation |
| Logger | Records pass/fail and error details |
| Recorder (`RunRecorder`) | Persists the integrity check result for evidence |

---

## Prerequisites

- `.sdd/` directory exists (orchestrator creates it if absent)
- Audit mode is active (`BERNSTEIN_AUDIT=1` or compliance preset with `audit_logging=True`)
- HMAC key file at `.sdd/config/audit-key` exists (created on first audit log write)
- At least one `.jsonl` audit log file exists (otherwise: valid result with zero entries, warning logged)

---

## Trigger

Orchestrator `run()` method, after WAL recovery and zombie cleanup complete, before the first tick loop iteration.

Insertion point in `orchestrator.py`: between the zombie cleanup block (line ~1480) and `consecutive_failures = 0` (line ~1481).

---

## Workflow Tree

### STEP 1: Check audit mode

**Actor**: Orchestrator
**Action**: Check whether `self._audit_mode` is `True` and `self._audit_log` is not `None`.
**Timeout**: N/A (in-memory check)
**Input**: `self._audit_mode: bool`, `self._audit_log: AuditLog | None`
**Output on SUCCESS** (`audit_mode=True`): -> GO TO STEP 2
**Output on SKIP** (`audit_mode=False`): -> DONE (no verification needed; this is not a failure)

**Observable states during this step**:
- Customer sees: N/A (startup sequence, no UI)
- Operator sees: No additional log output
- Database: No change
- Logs: None

---

### STEP 2: Load configuration

**Actor**: Orchestrator
**Action**: Determine the entry count `N` to verify. Source: `BERNSTEIN_AUDIT_VERIFY_COUNT` env var, or compliance config `audit_verify_count` field, or default `100`.
**Timeout**: N/A (config read)
**Input**: Environment variables, compliance config
**Output on SUCCESS**: `count: int` (number of tail entries to verify) -> GO TO STEP 3

**Observable states during this step**:
- Logs: None

---

### STEP 3: Call `verify_on_startup(sdd_dir, count)`

**Actor**: `audit_integrity.py`
**Action**: Execute the existing `verify_on_startup()` function which:
  1. Resolves `audit_dir = sdd_dir / "audit"`
  2. Checks `audit_dir` exists (if not: returns valid with warning)
  3. Loads HMAC key from `.sdd/config/audit-key` (if missing: returns valid with warning)
  4. Loads the last `count` entries from JSONL files (reverse chronological scan)
  5. Counts total entries across all files
  6. For each entry in the window: verifies `prev_hmac` chain linkage + recomputes HMAC
  7. Returns `IntegrityCheckResult`
**Timeout**: 30s — audit log reads on large files could be slow; the check should not block startup indefinitely
**Input**: `sdd_dir: Path`, `count: int`
**Output on SUCCESS** (`result.valid=True`):
  - `IntegrityCheckResult(valid=True, entries_checked=N, entries_total=M, ...)` -> GO TO STEP 4
**Output on FAILURE** (`result.valid=False`):
  - `IntegrityCheckResult(valid=False, errors=[...], ...)` -> GO TO STEP 5
**Output on EXCEPTION** (file I/O error, unexpected crash):
  - `FAILURE(exception)`: -> GO TO STEP 6

**Observable states during this step**:
- Customer sees: N/A
- Operator sees: Nothing yet (result logged in next step)
- Database: No change
- Logs: `audit_integrity.py` logs pass/fail with entry counts and timing

---

### STEP 4: Record valid result

**Actor**: Orchestrator
**Action**: Record the integrity check result via `self._recorder.record()` and continue startup.
**Input**: `IntegrityCheckResult`
**Output**: -> DONE (continue to tick loop)

**Observable states during this step**:
- Operator sees: Log line: `"Audit integrity check passed: N entries verified in X.Xms"`
- Logs: `[INFO] [bernstein.core.audit_integrity] Audit integrity check passed: N entries verified in X.Xms`
- Recorder: `event_type="audit_integrity_check"`, `valid=True`, `entries_checked=N`

---

### STEP 5: Handle integrity violation (WARNING path)

**Actor**: Orchestrator
**Action**: The HMAC chain is broken or entries were tampered with. This is a **warning, not a fatal error** — the orchestrator continues startup but surfaces the issue prominently.
**Input**: `IntegrityCheckResult` with `valid=False` and `errors=[...]`
**Actions** (in order):
  1. Log `WARNING`: `"AUDIT INTEGRITY WARNING: N error(s) detected in the audit log. The HMAC chain may have been tampered with."`
  2. Log each individual error at `WARNING` level
  3. Record via `self._recorder.record("audit_integrity_check", valid=False, errors=result.errors)`
  4. If audit log exists, write a new audit event: `event_type="integrity.violation_detected"` with the error details, so the violation is itself part of the immutable record
  5. Post bulletin: `BulletinMessage(channel="security", body="Audit integrity check failed: N errors")` so running agents are aware
  6. Continue startup (do NOT abort — the operator may need the orchestrator running to investigate)
**Output**: -> DONE (continue to tick loop with warning state)

**Observable states during this step**:
- Operator sees: WARNING log lines with specific error details (file:line, type of mismatch)
- Recorder: `event_type="audit_integrity_check"`, `valid=False`
- Audit log: New entry `integrity.violation_detected` appended
- Bulletin board: Security bulletin posted
- Logs: `[WARNING] AUDIT INTEGRITY WARNING: ...`

**Design decision — warn, don't abort**:
The orchestrator must continue running so operators can investigate. An abort would prevent legitimate recovery. If strict mode is needed (e.g., REGULATED or HIPAA compliance presets), see Open Question OQ-1.

---

### STEP 6: Handle unexpected exception (GRACEFUL DEGRADATION)

**Actor**: Orchestrator
**Action**: The integrity check itself threw an unexpected exception (corrupted file, permission error, etc.). Treat as non-fatal — same pattern as WAL recovery and zombie cleanup.
**Input**: Exception from `verify_on_startup()`
**Actions**:
  1. Log `EXCEPTION`: `"Audit integrity check failed (non-fatal) — continuing startup"`
  2. Record via `self._recorder.record("audit_integrity_check", error=str(exc))`
  3. Continue startup
**Output**: -> DONE (continue to tick loop)

**Observable states during this step**:
- Operator sees: Exception traceback in logs
- Recorder: `event_type="audit_integrity_check"`, `error="..."`
- Logs: `[ERROR] Audit integrity check failed (non-fatal) — continuing startup` + traceback

---

## State Transitions

```
[startup] -> (audit_mode=False) -> [tick_loop] (no check performed)
[startup] -> (audit_mode=True, check passes) -> [tick_loop] (result recorded)
[startup] -> (audit_mode=True, check fails) -> [tick_loop] (warning logged, bulletin posted)
[startup] -> (audit_mode=True, check throws) -> [tick_loop] (exception logged, graceful degradation)
```

---

## Handoff Contracts

### Orchestrator.run() -> audit_integrity.verify_on_startup()

**Function call**: `verify_on_startup(sdd_dir, count)`
**Input**:
```python
sdd_dir: Path  # e.g. Path("/project/.sdd")
count: int     # default 100, configurable
```
**Success response**:
```python
IntegrityCheckResult(
    valid=True,
    entries_checked=100,
    entries_total=5432,
    errors=[],
    warnings=[],
    checked_at="2026-04-08T12:00:00Z",
    duration_ms=14.2,
)
```
**Failure response** (integrity violation — not an exception):
```python
IntegrityCheckResult(
    valid=False,
    entries_checked=100,
    entries_total=5432,
    errors=["2026-04-07.jsonl:42: HMAC mismatch — stored abc123... != computed def456..."],
    warnings=[],
    checked_at="2026-04-08T12:00:00Z",
    duration_ms=18.7,
)
```
**Timeout**: 30s — if exceeded, treat as exception (STEP 6)
**On exception**: Catch `Exception`, log traceback, continue startup

---

## Cleanup Inventory

This workflow creates no resources that require cleanup. It is read-only except for:

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Audit event (`integrity.violation_detected`) | Step 5 | Never (immutable log) | N/A |
| Bulletin message | Step 5 | Auto-expires per bulletin TTL | BulletinBoard TTL |
| Recorder entry | Steps 4/5/6 | Never (immutable run record) | N/A |

---

## Spec vs Reality Audit

### Critical gap: `verify_on_startup()` is never called

**Finding**: `audit_integrity.py:252` defines `verify_on_startup()`. Tests exist in `test_audit_integrity.py`. But `orchestrator.py` never imports or calls it. The function is dead code.

**Resolution**: Wire the call into `Orchestrator.run()` at the insertion point specified in the Trigger section. The call pattern should match the existing WAL recovery and zombie cleanup blocks:

```python
# Audit integrity check: verify HMAC chain of recent entries.
try:
    from bernstein.core.audit_integrity import verify_on_startup
    _integrity = verify_on_startup(self._workdir / ".sdd", count=...)
    self._recorder.record(
        "audit_integrity_check",
        valid=_integrity.valid,
        entries_checked=_integrity.entries_checked,
        entries_total=_integrity.entries_total,
        errors=_integrity.errors,
        duration_ms=_integrity.duration_ms,
    )
    if not _integrity.valid and self._audit_log is not None:
        self._audit_log.log(
            "integrity.violation_detected",
            actor="orchestrator",
            resource_type="audit_log",
            resource_id="startup_check",
            details={"errors": _integrity.errors},
        )
        self._post_bulletin("security", f"Audit integrity check failed: {len(_integrity.errors)} error(s)")
except Exception:
    logger.exception("Audit integrity check failed (non-fatal) — continuing startup")
```

### Conditional gating: only run when audit mode is active

The check should be gated on `self._audit_mode`. When audit mode is off, there are no audit logs to verify and no HMAC key — the check is meaningless.

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — valid chain | Orchestrator starts with 100 valid entries | INFO log, recorder event with `valid=True` |
| TC-02: No audit directory | Fresh project, no `.sdd/audit/` | Valid result with warning, startup continues |
| TC-03: No HMAC key | Audit dir exists but key file missing | Valid result with warning, startup continues |
| TC-04: Empty audit directory | Dir exists, no `.jsonl` files | Valid result with warning, startup continues |
| TC-05: Tampered HMAC | Entry HMAC modified | WARNING log, bulletin posted, recorder event with `valid=False` |
| TC-06: Broken chain | `prev_hmac` linkage broken | WARNING log, bulletin posted, recorder event with `valid=False` |
| TC-07: Partial check | 500 entries exist, count=100 | Only last 100 verified, `entries_total=500` |
| TC-08: File I/O exception | Audit file unreadable (permission error) | Exception caught, logged, startup continues |
| TC-09: Audit mode disabled | `audit_mode=False` | No check performed, no log output |
| TC-10: Custom count via env var | `BERNSTEIN_AUDIT_VERIFY_COUNT=50` | 50 entries verified |
| TC-11: Violation logged to audit | HMAC tampered | New `integrity.violation_detected` event appended |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `verify_on_startup()` is called after the AuditLog is initialized in `__init__` | Verified: AuditLog created at orchestrator.py:639-640, `run()` is called after `__init__` | If AuditLog not initialized, `self._audit_log.log()` in step 5 would fail |
| A2 | HMAC key file is not rotated while orchestrator is stopped | Not verified — key rotation workflow not specified | Rotated key would cause all entries to fail verification |
| A3 | Audit log files are not modified externally except by tampering | Assumption — log rotation/archiving could truncate files | Archived files would not be in the verification window |
| A4 | `_post_bulletin` is safe to call during startup (before tick loop) | Verified: BulletinBoard initialized in `__init__` | Bulletin post would fail silently |
| A5 | The check completes in under 30s for typical audit logs (100 entries) | Verified: each entry is ~500 bytes JSON, 100 HMAC computations | Risk for very large entries with big `details` dicts |

## Open Questions

- **OQ-1**: Should REGULATED or HIPAA compliance presets treat integrity violations as fatal (abort startup)? Current design always warns and continues. A `strict_integrity_check: bool` config flag could gate this.
- **OQ-2**: Should the configurable count come from `bernstein.yaml` (under `compliance:` section), an environment variable (`BERNSTEIN_AUDIT_VERIFY_COUNT`), or both? Current spec supports both with env var taking precedence.
- **OQ-3**: Should the integrity check result be exposed via the `/status` API endpoint so monitoring dashboards can alert on it?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created. `verify_on_startup()` exists but is dead code — never called from orchestrator. | Spec documents wiring pattern and insertion point. |
| 2026-04-08 | Tests exist in `test_audit_integrity.py` covering valid chain, tampered HMAC, broken chain, no-dir, no-key, partial check. | Tests are comprehensive for the function itself; integration test for orchestrator startup wiring is missing. |
