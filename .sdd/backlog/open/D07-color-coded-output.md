# D07 — Standardized Color-Coded Output

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
CLI output uses inconsistent colors and styles across commands. Users cannot quickly scan output to distinguish successes from errors, or metadata from actionable content.

## Solution
- Define a standard color scheme and apply it consistently across all CLI output:
  - Green: success states, completed tasks, passing tests
  - Yellow: in-progress states, warnings, pending items
  - Red: errors, failed tasks, failing tests
  - Dim gray: metadata, timestamps, IDs, file paths
- Create a shared `theme.py` module in `src/bernstein/cli/` that defines Rich theme styles and helper functions (e.g., `success()`, `warning()`, `error()`, `meta()`)
- Refactor all existing `click.echo()` and `print()` calls to use the shared theme
- Use Rich `Console` with a custom `Theme` object for consistent styling
- Respect `NO_COLOR` environment variable (https://no-color.org/) to disable colors
- Respect `--no-color` global CLI flag

## Acceptance
- [ ] Success messages (task complete, tests passed) render in green
- [ ] In-progress and warning messages render in yellow
- [ ] Error messages render in red
- [ ] Metadata (timestamps, task IDs, file paths) renders in dim gray
- [ ] All CLI commands use the shared theme module for output styling
- [ ] Setting `NO_COLOR=1` disables all color output
- [ ] Passing `--no-color` flag disables all color output
