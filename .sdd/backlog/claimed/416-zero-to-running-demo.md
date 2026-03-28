# 416 — Zero-to-running in 60 seconds: bernstein demo command

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
First-run experience is the #1 adoption blocker (leverage score 0.92 from scenario analysis). Users bounce during setup because there's no guided quickstart. `bernstein init` creates structure but doesn't seed example tasks. There's no "hello world" workflow that shows value in under 2 minutes.

Competitors like CrewAI have `crewai create` with templates. Without an equivalent, we lose users at the door.

## Implementation
Add `bernstein demo` command:
1. Create a temp project directory with a simple Python codebase (Flask hello-world, ~5 files)
2. Seed `.sdd/backlog/open/` with 3 small tasks: "Add health check endpoint", "Add tests for app.py", "Add error handling middleware"
3. Run orchestrator for 2 minutes with visible dashboard progress
4. Print summary: tasks completed, files changed, tests passing
5. Print cost estimate BEFORE starting ("This demo will use ~$0.15 in API credits")
6. Works with only Claude Code installed (detect available adapters, use first found)
7. `--dry-run` flag: show what would happen without spawning agents

## Files
- src/bernstein/cli/main.py — add demo command
- templates/demo/ (new) — sample project files
- tests/unit/test_cli_demo.py (new)

## Completion signals
- file_contains: src/bernstein/cli/main.py :: def demo
- path_exists: templates/demo/
