# D06 — Compact Single-Line Progress Display

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
The current task output is verbose and multi-line, flooding the terminal during multi-task runs. Users lose context on overall progress and cannot quickly scan which tasks are active.

## Solution
- Replace the verbose multi-line task output with a compact single-line progress display
- Format: `[2/5 tasks] agent:claude working on "Add JWT auth" [45s, $0.12]`
- Update the line in-place using carriage return (`\r`) and terminal width detection
- Show: task count progress, active agent name, current task title (truncated to fit), elapsed time, and running cost
- When a task completes, briefly flash a completion line (green checkmark + task name + duration) before returning to the progress line
- On task failure, print a persistent red error line that stays visible above the progress line
- Fall back to non-interactive multi-line output when stdout is not a TTY (piped output)
- Use Rich's `Live` display as an alternative to raw `\r` for better terminal compatibility

## Acceptance
- [ ] During a multi-task run, only one progress line is visible at a time (no scrolling output)
- [ ] The progress line shows task count, agent name, task title, elapsed time, and cost
- [ ] Completed tasks flash a green confirmation line before the progress line resumes
- [ ] Failed tasks print a persistent red error line above the progress line
- [ ] Output degrades gracefully to multi-line when stdout is not a TTY
- [ ] Long task titles are truncated to fit within the terminal width
