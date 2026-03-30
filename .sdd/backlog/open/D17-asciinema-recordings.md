# D17 — Asciinema Terminal Recordings for Documentation

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
The README describes what Bernstein does, but prospective users can't see it in action. Terminal recordings are the most convincing demo format for CLI tools.

## Solution
- Install asciinema and record 5 terminal sessions:
  1. **First run + init** — `bernstein init` walkthrough, showing the interactive wizard and generated config.
  2. **Parallel task execution** — A multi-task run showing parallel progress bars and task completion.
  3. **CI autofix** — A failing CI run, then `bernstein autofix` resolving the issues.
  4. **Cost breakdown** — A run completing with the cost summary table displayed.
  5. **Self-evolution** — `bernstein evolve` showing the self-improvement cycle.
- Save recordings as `.cast` files in `docs/recordings/`.
- Embed in the README using asciinema player links (e.g., `[![asciicast](https://asciinema.org/a/ID.svg)](https://asciinema.org/a/ID)`).
- Add a `docs/recordings/README.md` describing each recording.

## Acceptance
- [ ] 5 `.cast` files exist in `docs/recordings/`
- [ ] Each recording is under 2 minutes in duration
- [ ] README embeds at least the "first run + init" recording as the primary demo
- [ ] All recordings are uploaded to asciinema.org and links are valid
- [ ] `docs/recordings/README.md` lists all recordings with descriptions
