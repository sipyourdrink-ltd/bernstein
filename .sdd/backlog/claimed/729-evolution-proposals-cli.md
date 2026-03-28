# 729 — Wire CLI Evolution Proposals to Orchestrator

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

`bernstein evolve review` shows evolution proposals and `bernstein evolve approve <id>` approves them, but approved proposals don't flow back into the orchestrator's pending queue automatically. The user has to restart the orchestrator for approved proposals to be picked up. The pipeline is broken at the "approved → enqueued" junction.

## Design

### Fix the pipeline
1. When `bernstein evolve approve <id>` is called:
   - Write the approved proposal to `.sdd/upgrades/pending.json`
   - POST to the running task server to enqueue it immediately (if server is alive)
2. The orchestrator's tick loop should check `.sdd/upgrades/pending.json` each cycle
   - If entries exist, convert them to tasks and enqueue
   - Mark as "enqueued" in pending.json to avoid double-processing

### Also fix
- `bernstein evolve review` should show proposal status (pending/approved/applied/rejected)
- `bernstein evolve reject <id>` should be a thing (currently missing)

## Files to modify

- `src/bernstein/cli/main.py` (evolve approve → POST to server)
- `src/bernstein/core/orchestrator.py` (check pending.json in tick)
- `src/bernstein/core/evolution.py` (reject support)
- `tests/unit/test_evolution.py` (extend)

## Completion signal

- `bernstein evolve approve 1` immediately enqueues the proposal
- Running orchestrator picks it up within one tick cycle
- `bernstein evolve reject` works
