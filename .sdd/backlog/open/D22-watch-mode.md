# D22 — Watch Mode for Continuous Re-Execution

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
During iterative development, users must manually re-run Bernstein after every code change. This breaks the flow, especially when experimenting with configs or fixing issues from a previous run.

## Solution
- Implement `bernstein watch` that monitors the working directory for file changes.
- Use the `watchdog` library to observe filesystem events (create, modify, delete).
- On detected change, determine which tasks are affected by the changed files (based on task file scope in the config).
- Re-run only the affected tasks, not the entire goal.
- Apply a 2-second debounce to avoid triggering on rapid successive saves.
- Display on change detection: "Detected change in src/auth.py -> re-running task #3".
- Ignore changes in `.sdd/`, `.git/`, `__pycache__/`, `node_modules/`, and other common non-source directories.
- Support `--glob <pattern>` to restrict watched files (e.g., `--glob "src/**/*.py"`).
- Ctrl+C exits watch mode cleanly.

## Acceptance
- [ ] `bernstein watch` starts monitoring and displays "Watching for changes..."
- [ ] File modifications trigger re-execution of affected tasks within 2-3 seconds
- [ ] Changes in `.sdd/`, `.git/`, and other ignored directories do not trigger re-runs
- [ ] The console message correctly identifies the changed file and affected task
- [ ] `--glob` flag restricts watching to files matching the pattern
- [ ] Ctrl+C exits cleanly without error output
