# 506 — Ticket re-evaluation system: VP gate for strategic pivots

**Role:** architect
**Priority:** 2
**Scope:** medium
**Complexity:** high

## Problem
During development, an agent may discover something that changes the strategic direction — a security vulnerability that requires architectural rework, a competitor move that makes a feature obsolete, or a technical insight that enables a simpler approach. Currently, this discovery dies with the agent session. Future tickets proceed with stale assumptions.

## Design

### 1. Discovery signals
When an agent encounters a pivotal finding, it writes to `.sdd/signals/pivots.jsonl`:
```json
{
  "timestamp": "2026-03-28T12:00:00Z",
  "agent_id": "backend-abc123",
  "task_id": "501",
  "signal_type": "strategic_pivot",
  "severity": "high",
  "summary": "Port 8052 hardcoding is a systemic pattern — 14 files reference it. Fixing #501 requires refactoring all of them.",
  "affected_tickets": ["501", "503", "416"],
  "proposed_action": "Create a centralized config system before fixing individual port references"
}
```

### 2. VP review gate
- **Small pivots** (severity: low/medium, affects 1-2 tickets): Agent updates the ticket description directly, adds a `[REVISED]` marker.
- **Large pivots** (severity: high, affects 3+ tickets OR changes scope/priority): Routed to VP role for evaluation.
- VP agent reviews the signal, affected tickets, and decides: APPROVE (update tickets) / REJECT (note and proceed) / ESCALATE (pause work, notify human).

### 3. Ticket mutation rules
- Only VP role can change ticket priority or scope
- Any role can add context/notes to a ticket
- Closed/done tickets are never modified
- Changes are logged in `.sdd/signals/ticket_changes.jsonl` with before/after

### 4. Evolution integration
The creative pipeline (#415) and evolution loop check `pivots.jsonl` before generating new proposals. If an unresolved high-severity pivot exists, the visionary agent considers it. The analyst agent checks new proposals against pending pivots for consistency.

## Files
- src/bernstein/core/signals.py (new) — PivotSignal model, signal filing
- src/bernstein/core/orchestrator.py — check pivots before spawning, route to VP
- templates/roles/vp/system_prompt.md — VP pivot evaluation instructions
- tests/unit/test_signals.py (new)

## Completion signals
- file_contains: src/bernstein/core/signals.py :: PivotSignal
- path_exists: templates/roles/vp/system_prompt.md
