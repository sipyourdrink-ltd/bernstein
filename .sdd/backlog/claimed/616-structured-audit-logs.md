# 616 — Structured Audit Logs

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

There is no structured audit trail for agent actions. Enterprise customers require tamper-evident audit logs for compliance. The current logging is unstructured text that cannot be queried or verified for integrity.

## Design

Implement a structured audit log system that records every agent action, model call, tool invocation, and state change in a queryable format. Each log entry includes: timestamp, run ID, agent ID, action type, input/output summary, model used, token count, and cost. Store logs as append-only JSONL files in `.sdd/audit/`. Add cryptographic integrity verification using hash chains — each entry includes the hash of the previous entry, making tampering detectable. Provide a `bernstein audit` CLI command to query logs (by run, agent, time range, action type) and verify integrity. Support log export to external systems via structured JSON output.

## Files to modify

- `src/bernstein/core/audit.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `src/bernstein/cli/audit.py` (new)
- `tests/unit/test_audit.py` (new)

## Completion signal

- Every agent action produces a structured audit log entry
- `bernstein audit --run {id}` shows all actions for a run
- `bernstein audit --verify` confirms log integrity via hash chain
