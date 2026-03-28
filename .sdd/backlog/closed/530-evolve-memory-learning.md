# 530 — Evolution memory: learn from past cycles across sessions

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium

## Problem

Each --evolve session starts fresh. The visionary doesn't remember what was
proposed before, what worked, what was rejected. Failed experiments are repeated.
Successful patterns aren't reinforced.

Ruflo claims "self-learning neural capabilities" — learning from every task
execution, preventing catastrophic forgetting. Bernstein needs equivalent.

## Design

### Evolution memory store
- `.sdd/evolution/memory.jsonl` — append-only log of all proposals + outcomes
- Fields: proposal_hash, title, verdict, score, outcome (applied/reverted/rejected),
  impact_delta, timestamp, session_id

### Memory injection
- Before visionary generates proposals, inject:
  - "These proposals were tried and FAILED: [list]"
  - "These proposals SUCCEEDED and improved metrics by X%: [list]"
  - "These areas have been explored N times already: [list]"
- Visionary is prompted to: "Propose something we haven't tried yet"

### Pattern learning
- Track which types of proposals succeed (test improvements, config tweaks, etc.)
- Build simple frequency model: proposal_type -> success_rate
- Bias visionary towards high-success-rate categories
- Gradually increase ambition as confidence grows

### Cross-session persistence
- Memory survives server restarts (file-based)
- Can be shared across cluster nodes (via shared filesystem or DB)
- `bernstein evolve --memory-status` — shows learning trajectory

### Anti-stagnation
- If last N proposals are all from same category, force diversity
- Periodic "wild card" cycle: ignore memory, generate fully novel ideas
- Track "exploration score" — how diverse proposals have been

## Files to modify
- `src/bernstein/evolution/creative.py` — memory injection
- `src/bernstein/evolution/loop.py` — outcome recording
- New: `src/bernstein/evolution/memory.py` — memory store + retrieval
- `templates/roles/visionary/system_prompt.md` — memory-aware prompting

## Completion signal
- Evolve never re-proposes a previously rejected idea
- Success rate improves across sessions (measurable via evolve_cycles.jsonl)
- `bernstein evolve --memory-status` shows learning curve


---
**completed**: 2026-03-28 11:36:40
**task_id**: 214860dfe96b
**result**: Completed: [RETRY 1] 520 — GitHub Issues as evolve coordination layer
