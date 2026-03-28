# 740 — Team Coordination Patterns

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #734

## Problem

Agents work in complete isolation — each agent sees only its own task, with no awareness of what other agents are doing simultaneously. This causes:
1. **Naming conflicts**: two agents create `utils.py` with different contents
2. **API contract drift**: backend agent defines endpoint as POST, docs agent documents it as GET
3. **Duplicate work**: two agents both add the same import or helper function
4. **Integration failures**: changes compile individually but break when merged together

## Design

### Shared bulletin board (already exists, underused)

The `BulletinBoard` in `src/bernstein/core/bulletin.py` already exists but agents don't use it. Fix this:

1. **Auto-post on file creation**: when an agent creates a new file, auto-post to bulletin: "backend-abc123 created src/auth.py with classes: AuthMiddleware, TokenValidator"
2. **Auto-post on API definition**: detect route definitions and post: "backend-abc123 added POST /auth/login returning {token, refresh_token}"
3. **Inject bulletin summary into agent context**: when spawning an agent, include the last 10 bulletin messages so it knows what others are doing

### Coordination protocol

Add to every agent's system prompt:
```
## Team awareness
Other agents are working in parallel. Recent activity:
- backend-abc123: created src/auth.py (AuthMiddleware, TokenValidator)
- qa-def456: writing tests for src/users.py
- docs-ghi789: documenting API endpoints

If you need to create a shared utility, check if it already exists first.
If you define an API endpoint, use consistent naming with existing endpoints.
```

### Conflict prevention

Before spawning, the orchestrator checks:
- Are two tasks about to modify the same file? → serialize them (don't run in parallel)
- Do two tasks create files with the same name? → flag for review
- Do tasks have implicit dependencies the planner missed? → add dependency edge

### Post-parallel review (#734 synthesis agent)

After parallel tasks complete, the synthesis agent from #734 reviews the combined output for coordination issues before merging.

## Files to modify

- `src/bernstein/core/spawner.py` (inject bulletin into agent context)
- `src/bernstein/core/orchestrator.py` (conflict detection before spawn)
- `src/bernstein/core/bulletin.py` (auto-post helpers)
- `templates/prompts/team-awareness.md` (new)
- `tests/unit/test_coordination.py` (new)

## Completion signal

- Agents receive bulletin summary in their prompt
- File conflict detection prevents parallel edits to same file
- Integration failures reduced measurably
