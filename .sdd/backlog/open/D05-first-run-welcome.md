# D05 — First-Run Welcome Message

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When a user runs Bernstein for the first time without a config file, they get a raw error or confusing output. There is no onboarding guidance to help them take the right next step.

## Solution
- Detect first-run condition: no `bernstein.yaml` found in the current directory or parent directories
- Display a styled welcome box using Rich's `Panel` widget with a border and title
- Welcome box content should include three numbered next steps:
  1. Run `bernstein init` to create a project configuration
  2. Run `bernstein quickstart` to try a demo workflow instantly
  3. Visit docs at `docs.bernstein.dev` for full guide
- Use Rich markup for styling: bold for commands, dim for URLs
- Only show the welcome message on commands that require a config file (not on `bernstein init`, `bernstein quickstart`, `bernstein completions`, `bernstein doctor`, or `bernstein --version`)
- Suppress the welcome message if `--quiet` flag is set or `BERNSTEIN_QUIET=1` env var is present

## Acceptance
- [ ] Running any config-dependent command without `bernstein.yaml` shows the welcome panel
- [ ] The welcome panel lists all three next steps with correct command names
- [ ] `bernstein init` and `bernstein quickstart` do NOT show the welcome message
- [ ] `bernstein --version` does NOT show the welcome message
- [ ] Setting `BERNSTEIN_QUIET=1` suppresses the welcome message
- [ ] The panel renders correctly in terminals 80 columns wide and wider
