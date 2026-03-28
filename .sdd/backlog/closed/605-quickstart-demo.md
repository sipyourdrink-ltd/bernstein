# 605 — Quickstart Demo

**Role:** docs
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

There is no quickstart demo or one-command setup experience. 68% of developers start with documentation, and if the first 3 minutes don't work, they leave. The install-to-first-run path has not been tested or optimized for new users.

## Design

Create a 3-minute quickstart demo video and optimize the one-command setup experience. `pip install bernstein && bernstein run "add feature X"` must work flawlessly on a fresh machine. Test on macOS, Ubuntu, and WSL. Record a screencast showing: install, init, first run with visible agent orchestration, and result. Write a quickstart guide (README section, not separate file) that mirrors the video. Fix any rough edges in the install path: missing defaults, confusing prompts, unclear error messages. The demo should use a sample project included in the repo under `examples/`.

## Files to modify

- `README.md` (quickstart section)
- `src/bernstein/cli/init.py` (smooth defaults)
- `src/bernstein/cli/run.py` (first-run experience)
- `examples/quickstart/` (new sample project)

## Completion signal

- `pip install bernstein && bernstein init && bernstein run` works on a fresh virtualenv
- Demo video script/recording exists
- README quickstart section matches the video flow
