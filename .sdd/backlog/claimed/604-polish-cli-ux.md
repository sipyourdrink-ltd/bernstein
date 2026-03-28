# 604 — Polish CLI UX

**Role:** frontend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #601

## Problem

The current CLI output is plain text with minimal formatting. CLI tools have a 46% "most loved" rate among developers, but first impressions determine adoption. Without rich terminal output, progress indicators, and real-time cost display, Bernstein looks unpolished compared to modern CLI tools.

## Design

Overhaul the CLI using the Rich library for terminal output. Add a live dashboard during `bernstein run` showing: active agents with status indicators, real-time cost burn (from the cost tracker), task progress bars, and a scrolling log of agent actions. Use Rich panels, tables, and spinners for structured output. Add color-coded status: green for success, yellow for in-progress, red for failures. Ensure graceful degradation in non-TTY environments (CI, piped output). Keep the plain-text fallback for `--no-color` mode. The `bernstein status` command should show a clean summary table.

## Files to modify

- `src/bernstein/cli/run.py`
- `src/bernstein/cli/status.py`
- `src/bernstein/cli/live.py`
- `src/bernstein/cli/ui.py` (new — shared Rich components)
- `pyproject.toml` (add rich dependency)

## Completion signal

- `bernstein run` shows live dashboard with agent status, cost, and progress
- `bernstein status` shows formatted summary table
- Output degrades gracefully when piped or `--no-color` is used
