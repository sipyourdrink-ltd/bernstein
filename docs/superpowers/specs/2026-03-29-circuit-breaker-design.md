# Agent Kill Switch with Purpose Enforcement

**Date:** 2026-03-29
**Scope:** Small — runtime circuit breaker for misbehaving agents
**Task:** 510d

## Problem

Agents can misbehave by editing files outside their task boundary (scope
violation) or consuming excessive resources. 60% of orgs cannot terminate
misbehaving agents in real time (Cisco 2026). Bernstein currently only runs
guardrails at merge time — after the damage is done.

## Existing Infrastructure

- Basic kill via `.kill` signal files (plain timestamp string)
- `POST /agents/{session_id}/kill` HTTP endpoint
- `check_kill_signals()` in `agent_lifecycle.py` (processed each tick)
- `guardrails.py` — scope/secret checks, runs at merge time (too late)
- `token_monitor.py` — kills token-runaway agents (no file changes + > 50k tokens)

## What's Missing

1. **Kill reason** — kill files contain only a timestamp; no reason stored
2. **Runtime scope monitoring** — no real-time check of which files an agent is touching
3. **Kill audit log** — no record of why agents were killed
4. **Quarantine** — violation kills should preserve the agent's branch for review

## Design

### 1. `KillReason` enum (models.py)

```python
class KillReason(str, Enum):
    SCOPE_VIOLATION = "scope_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    GUARDRAIL_VIOLATION = "guardrail_violation"
    MANUAL = "manual"
    TIMEOUT = "timeout"
    STALLED = "stalled"
```

### 2. Structured kill files

Upgrade `.kill` files from plain timestamp to JSON (backward-compatible):

```json
{
  "ts": 1234567890.0,
  "reason": "scope_violation",
  "detail": "2 file(s) modified outside task scope: src/other/file.py",
  "files": ["src/other/file.py"],
  "requester": "circuit_breaker"
}
```

Old-format files (plain float string) continue to work.

### 3. `circuit_breaker.py` (new module)

**`check_scope_violations(orch, result)`**
- Iterates active agent sessions with tasks that have `owned_files`
- Gets worktree path via `spawner.get_worktree_path(session.id)`
- If no worktree: skips (scope checked at merge time by guardrails)
- Runs `git -C <worktree> diff --name-only HEAD` to find modified files
- Compares against `task.owned_files` prefix matching
- On violation: calls `enforce_kill_signal()`, appends session_id to `result.reaped`

**`enforce_kill_signal(workdir, session_id, reason, detail, files)`**
- Writes structured JSON to `.sdd/runtime/{session_id}.kill`
- Calls `log_kill_event()` to write to audit log
- Calls `write_quarantine_metadata()` for violation reasons

**`log_kill_event(workdir, session_id, reason, detail, files)`**
- Appends one JSON line to `.sdd/metrics/kill_audit.jsonl`

**`write_quarantine_metadata(workdir, session_id, reason, detail, files, branch)`**
- Writes `.sdd/quarantine/{session_id}.json` with full context
- Branch is preserved for human review; orchestrator skips merge on violation kills

### 4. Route upgrade (`routes/agents.py`)

`POST /agents/{session_id}/kill` accepts optional JSON body:

```json
{"reason": "manual", "detail": "User-requested termination"}
```

Writes structured JSON kill file (reason defaults to `"manual"`, requester to `"api"`).

### 5. `check_kill_signals()` upgrade

Parses JSON kill files, extracts reason, logs it. Calls `log_kill_event()` with the
reason so all kills (including manual API kills) appear in the audit log.

### 6. Orchestrator integration

Adds `check_scope_violations(self, result)` call after step 4d-ii (token growth) in
the tick loop.

## Data Flow

```
Circuit breaker tick check
    → git diff in worktree
    → files outside owned_files?
    → enforce_kill_signal()
        → write .kill file (JSON)
        → log_kill_event() → kill_audit.jsonl
        → write_quarantine_metadata() → quarantine/{id}.json
    → check_kill_signals() (next tick)
        → spawner.kill(session)
        → result.reaped.append(session_id)
```

## Files Changed

| File | Change |
|------|--------|
| `src/bernstein/core/models.py` | Add `KillReason` enum |
| `src/bernstein/core/circuit_breaker.py` | **New** — runtime enforcement |
| `src/bernstein/core/routes/agents.py` | Accept structured kill body |
| `src/bernstein/core/agent_lifecycle.py` | Parse JSON kill files, log reason |
| `src/bernstein/core/orchestrator.py` | Call `check_scope_violations` in tick |
| `tests/unit/test_circuit_breaker.py` | **New** — unit tests |

## Not in Scope

- Per-agent cost budget (token_monitor already handles runaway consumption)
- Secrets detection at runtime (too expensive; runs at merge via guardrails)
- Network monitoring or container isolation
