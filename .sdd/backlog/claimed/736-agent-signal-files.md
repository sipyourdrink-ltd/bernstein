# 736 — Agent Signal Files (WAKEUP / SHUTDOWN / HEARTBEAT)

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein agents frequently "hang" — the CLI process is alive but the agent stopped making progress (token limit hit, network timeout, stuck in a loop). We have no way to poke them back to life or tell them to gracefully save and exit. The only option is kill -9, which loses all in-progress work. This causes a high failure rate.

## Design

### Signal file protocol
Agents periodically check `.sdd/runtime/signals/{session_id}/` for signal files:

#### WAKEUP signal
File: `.sdd/runtime/signals/{session_id}/WAKEUP`

When the orchestrator detects an agent hasn't made progress (no file changes, no heartbeat update) for N seconds:
1. Write a WAKEUP file with instructions
2. The agent's system prompt includes: "Check .sdd/runtime/signals/{your_session_id}/ periodically"
3. Content of WAKEUP:
```markdown
# WAKEUP — You may be stuck
Your task: {task_title}
Time elapsed: {elapsed}
Last activity: {last_activity_ago} ago

If you're stuck:
1. Save your current progress (git add + commit WIP)
2. Report status to task server
3. Continue working or exit if blocked
```

#### SHUTDOWN signal
File: `.sdd/runtime/signals/{session_id}/SHUTDOWN`

When `bernstein stop` is called or budget is hit:
1. Write SHUTDOWN file to each active agent
2. Content:
```markdown
# SHUTDOWN — Save and exit
Reason: {reason}
You have 30 seconds to:
1. Save all current work (git add + commit "[WIP] {task_title}")
2. Report partial progress to task server
3. Exit cleanly
```

#### HEARTBEAT (agent → orchestrator)
File: `.sdd/runtime/heartbeats/{session_id}.json`
Agent writes every 30 seconds:
```json
{"timestamp": 1711641600, "files_changed": 3, "status": "working", "current_file": "src/auth.py"}
```

### Stale agent detection
Orchestrator tick loop checks heartbeats:
- No heartbeat for 60s → write WAKEUP
- No heartbeat for 120s → write SHUTDOWN
- No heartbeat for 180s → kill process, mark task for retry

### System prompt injection
Add to every agent's system prompt:
```
IMPORTANT: Every 60 seconds, check for signal files:
  cat .sdd/runtime/signals/{SESSION_ID}/WAKEUP 2>/dev/null
  cat .sdd/runtime/signals/{SESSION_ID}/SHUTDOWN 2>/dev/null
If SHUTDOWN exists, immediately: git add -A && git commit -m "[WIP] {task}" && exit
```

### Alternative: stdin injection
For Claude Code specifically, we could potentially write to the agent's stdin pipe to inject a "check status" message. This is adapter-specific and riskier but more reliable than hoping the agent reads a file.

## Files to modify

- `src/bernstein/core/spawner.py` (inject signal check into system prompt)
- `src/bernstein/core/orchestrator.py` (stale detection, write signals)
- `src/bernstein/core/models.py` (heartbeat dataclass)
- `templates/prompts/signal-check.md` (new — signal check instructions)
- `tests/unit/test_agent_signals.py` (new)

## Completion signal

- WAKEUP signal written when agent is stale for 60s
- SHUTDOWN signal triggers graceful save+exit
- Heartbeat file updated by agents every 30s
- Stale detection → WAKEUP → SHUTDOWN → kill cascade works
