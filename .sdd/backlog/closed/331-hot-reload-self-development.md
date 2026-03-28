# 331 — Hot-Reload for Self-Development Mode

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** none

## Problem

When Bernstein agents modify Bernstein's own source code (self-evolution), the running server/orchestrator uses the OLD code. Changes only take effect after a full restart, which kills all agents and loses progress. This makes self-development painfully slow.

## Design

### Uvicorn reload
The task server already runs under uvicorn. Add `--reload` flag when running in evolve mode:
```python
# In bootstrap.py, when starting server:
if evolve_mode:
    cmd.extend(["--reload", "--reload-dir", "src/bernstein"])
```

This makes the HTTP server auto-restart when source files change, without touching agent processes (they're separate processes).

### Orchestrator watchdog
The orchestrator runs as a separate process. Add a file watcher:
1. Watch `src/bernstein/core/orchestrator.py` and key modules for changes
2. On change: save session state → exec() self with same args → resume from saved state
3. Use `os.execv()` for in-place process replacement (preserves PID)
4. Agents are separate processes — they survive the orchestrator restart

### Agent isolation
Agents run as independent processes (`subprocess.Popen` with `start_new_session=True`). They communicate via HTTP to the task server. If the server restarts (uvicorn reload), agents retry HTTP calls automatically (httpx has retry built in).

### TUI/Dashboard
The TUI polls the server — if the server restarts, TUI reconnects automatically. The web dashboard uses SSE — browser auto-reconnects on SSE drop.

### What survives a reload
- Agent processes (separate PIDs, not children of orchestrator)
- Task state (in server memory, persisted to JSONL)
- Cost tracking (persisted to .sdd/)
- Session state (persisted before reload)

### What restarts
- Server process (uvicorn reload)
- Orchestrator process (exec self)
- TUI reconnects automatically

## Files to modify

- `src/bernstein/core/bootstrap.py` (add --reload flag)
- `src/bernstein/core/orchestrator.py` (file watcher + exec self)
- `tests/unit/test_hot_reload.py` (new)

## Completion signal

- Agent modifies server.py → server auto-restarts → agents continue working
- Agent modifies orchestrator.py → orchestrator restarts → resumes from session
- Zero agent deaths during hot-reload
