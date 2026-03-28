# 332 — Zero-Config Agent Setup

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** none

## Problem

Users have to manually configure which CLI agent to use, set API keys, choose models. Most users just want to type `bernstein -g "do X"` and have it work. The system should auto-detect everything and just go.

## Design

### On first run (no bernstein.yaml)

1. **Auto-detect installed agents** (already in agent_discovery.py):
   - `which claude` → Claude Code
   - `which codex` → Codex CLI (check `codex login status`)
   - `which gemini` → Gemini CLI
   - `which aider` → Aider

2. **Auto-detect authentication**:
   - Claude: `ANTHROPIC_API_KEY` or OAuth session
   - Codex: `codex login status` (ChatGPT login = free tier)
   - Gemini: `GOOGLE_API_KEY` or gcloud auth (1M free tokens/day)

3. **Auto-select primary agent**:
   - If Claude Code installed + authenticated → use as primary
   - If only Codex → use Codex
   - If multiple → use all (auto-routing by role)
   - Print: "Found: Claude Code (opus/sonnet), Codex (o4-mini). Using auto-routing."

4. **Auto-generate bernstein.yaml** (already partially in init):
   ```yaml
   cli: auto  # detected: claude, codex
   # No manual config needed — Bernstein picks the best agent per task
   ```

5. **Skip init entirely** — if no `.sdd/` exists, auto-create on first `bernstein -g`:
   ```
   $ bernstein -g "Add tests"
   No project setup found. Auto-detecting...
   ✓ Found: Claude Code (sonnet/opus), Codex (o4-mini)
   ✓ Created .sdd/ and bernstein.yaml
   Starting orchestration...
   ```

### On subsequent runs

- Read bernstein.yaml (or use defaults)
- Re-check agent availability (cached 5 min)
- If a previously-available agent is gone, warn but continue with others

### User overrides (opt-in, not required)

```bash
bernstein -g "task" --cli claude        # force specific agent
bernstein -g "task" --model opus        # force specific model
bernstein config set cli codex          # set default persistently
```

### The UX principle

**Zero mandatory configuration.** Everything has a smart default. Power users can override anything. First-time users type one command and it works.

## Files to modify

- `src/bernstein/cli/main.py` (auto-init on first run)
- `src/bernstein/cli/run_cmd.py` (skip init if auto-detectable)
- `src/bernstein/core/bootstrap.py` (auto-detect in bootstrap)
- `src/bernstein/core/agent_discovery.py` (improve detection UX)
- `tests/unit/test_zero_config.py` (new)

## Completion signal

- `bernstein -g "Add tests"` works on a fresh machine with only Claude Code installed
- No bernstein.yaml required
- No `bernstein init` required
- Auto-routing works when multiple agents detected
- Clear one-line message about what was detected
