# 501 — Fix critical UX blockers: broken aliases, missing pre-flight checks

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
Three blockers make Bernstein unusable for first-time users:

### Bug 1: `bernstein init` and other aliases are broken
At main.py:2177, aliases like `init`, `run`, `start`, `status` are created via `click.Command("init", callback=init)` which wraps the raw callback in a bare Command, losing all options and docstrings. `bernstein init --dir /path` crashes.

Fix: Use `cli.add_command(overture, "init")` referencing the decorated command object, not rewrapping the callback.

### Bug 2: No pre-flight check for CLI agent binary
When Claude Code (or selected adapter) isn't installed, the spawner silently fails. User sees dashboard with "Waiting for agents..." forever. Error is buried in `.sdd/runtime/spawner.log`.

Fix: Run `shutil.which("claude")` (or selected CLI) before starting the server. Fail fast with actionable message: "claude not found in PATH. Install: https://claude.ai/code"

### Bug 3: No API key validation
If ANTHROPIC_API_KEY is missing, agents spawn and immediately die. User sees agents appearing and dying in dashboard with no explanation.

Fix: Call `adapter.detect_tier()` in bootstrap. If key missing, print exact env var name needed and exit.

### Bug 4: Port conflict not detected
Port 8052 hardcoded. If occupied, server silently fails. Bootstrap says "Server did not respond within timeout -- proceeding anyway" — this is actively harmful.

Fix: Check port before starting server. Never "proceed anyway" on server timeout — it's a fatal error.

## Files
- src/bernstein/cli/main.py — fix aliases, add pre-flight checks
- src/bernstein/core/bootstrap.py — add adapter/key/port validation
- tests/unit/test_bootstrap.py — test pre-flight checks

## Completion signals
- test_passes: uv run pytest tests/unit/test_bootstrap.py -x -q
