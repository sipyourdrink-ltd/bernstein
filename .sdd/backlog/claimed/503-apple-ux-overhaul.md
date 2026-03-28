# 503 — Apple-like UX overhaul: zero-friction first run, progressive disclosure

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
Apple test grade: D for first run, F for cost awareness, C- for error recovery. A developer who has never seen Bernstein gets a blank dashboard with no agents if anything is misconfigured, with zero explanation of what went wrong.

## Design Principles (from research)
- **P1 Clarity:** command names are verbs that say what they do
- **P2 Consistency:** same error format everywhere
- **P3 Immediate feedback:** every action produces visible response < 100ms
- **P4 Forgiveness:** destructive ops require confirmation
- **P5 Progressive disclosure:** simple default, powerful on demand

## Implementation

### 1. Startup feedback loop (biggest gap)
Currently bootstrap blocks with no output until dashboard appears. Add phase-by-phase messages:
```
→ Parsing seed file...
→ Starting task server on :8052...
→ Planning tasks (3 found)...
→ Spawning 3 agents...
→ Dashboard ready.
```
Use Rich `Status` spinner for each phase.

### 2. Error messages that teach (what/why/fix pattern)
Wrap ALL errors in three-part structure:
```
Error: Task server failed to start on port 8052
  Reason: Port already in use
  Fix: Run 'bernstein stop' first, or use --port 8053
```
Create `src/bernstein/cli/errors.py` with `BernsteinError(what, why, fix)` class.

### 3. Progressive disclosure in help
Two-tier help:
```
$ bernstein --help
Bernstein — multi-agent orchestration.

Usage:
  bernstein -g "Build auth with JWT"    Run with inline goal
  bernstein                             Run from bernstein.yaml
  bernstein status                      Check progress
  bernstein stop                        Stop everything

More: bernstein --help-all
```
Full flags (`--evolve`, `--max-cycles`, etc.) only in `--help-all` or subcommand help.

### 4. `bernstein doctor` command
Self-diagnostic:
- Check Python version
- Check CLI adapters installed (claude, codex, gemini)
- Check API keys set
- Check port 8052 available
- Check .sdd/ structure
- Check stale PID files
- Report all issues with fixes

### 5. `bernstein recap` command
Post-run summary: "14:03 started 5 tasks → 14:12 all done, 4 passed, 1 failed. $0.23 spent."
Reads from `.sdd/metrics/` and `.sdd/archive/tasks.jsonl`.

### 6. Cost estimation before spending
Before starting agents, show: "Estimated cost: $2-5 for 6 tasks with Sonnet. Press Enter to continue."
Based on task count × average tokens per role × model pricing.

### 7. Evolve mode safety
Require `--budget` or `--max-cycles` with `--evolve`. Show warning:
"Evolve mode will autonomously modify code. Budget: $5.00, max 10 cycles. Continue? [y/N]"

### 8. Chat input role detection
Dashboard chat input currently hardcodes `role=backend`. Auto-detect from task description keywords (test → qa, security → security, design → architect) or default to letting manager decide.

### 9. Dashboard `s` key confirmation
Require double-press or show confirmation dialog before killing all agents.

### 10. `--json` output mode
Add `--json` flag to `status`, `cost`, `recap` for CI/scripting integration.

### 11. Shell completions
`bernstein completions bash/zsh/fish` for power users.

### 12. Fix stale error messages
Change "Try bernstein start" → "Run bernstein" in all error messages (lines 520, 672, 1229, 1395).

## Files
- src/bernstein/cli/errors.py (new) — BernsteinError class
- src/bernstein/cli/main.py — progressive help, doctor, recap, completions
- src/bernstein/cli/dashboard.py — chat role detection, stop confirmation
- src/bernstein/core/bootstrap.py — startup feedback, cost estimation
- tests/unit/test_doctor.py (new)
- tests/unit/test_recap.py (new)

## Completion signals
- file_contains: src/bernstein/cli/errors.py :: BernsteinError
- file_contains: src/bernstein/cli/main.py :: def doctor
- file_contains: src/bernstein/cli/main.py :: def recap
