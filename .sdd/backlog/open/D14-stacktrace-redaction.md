# D14 — Stack Trace Redaction in User-Facing Output

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When errors occur, Bernstein dumps full internal stack traces to the terminal. Users see dozens of internal frames that are meaningless to them, burying the actual error message.

## Solution
- Add an error formatting layer that intercepts exceptions before they reach the terminal.
- Strip all internal Bernstein frames from the displayed output; show only the top-level error message and error code.
- Format the user-facing output as: `Error [CODE]: [message]\nRun 'bernstein logs --verbose' for full details.`
- Implement `bernstein logs --verbose` to read and display the full unredacted trace from `.sdd/runs/latest/errors.log`.
- Preserve full traces in log files unconditionally.

## Acceptance
- [ ] User-facing error output contains only the error message and code, no internal frames
- [ ] `bernstein logs --verbose` displays the complete unredacted stack trace
- [ ] Full stack traces are always persisted to `.sdd/runs/latest/errors.log`
- [ ] Third-party library frames are also stripped from user-facing output
- [ ] Error codes are consistent and documented (e.g., E001 for provider errors, E002 for config errors)
