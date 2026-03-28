# 629 — Cline and Roo Code Adapters

**Role:** backend
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** #612

## Problem

Bernstein can only orchestrate CLI agents (Claude Code, Codex, Gemini CLI). Cline (59K stars, 5M VS Code installs) and Roo Code are popular agent backends that Bernstein cannot use as workers. Integrating with their user base would accelerate adoption significantly.

## Design

Create Bernstein adapters for Cline and Roo Code as worker backends. Research how Cline and Roo Code can be invoked programmatically — both have API/CLI modes or can be driven via VS Code extension API. Implement adapters following the existing adapter pattern in `src/bernstein/adapters/`. Each adapter must support: task assignment (pass task description and context), status polling (check if agent is still working), result collection (get output and modified files), and graceful termination. Handle the VS Code dependency — these agents run inside VS Code, so the adapter may need to communicate via IPC or REST API. Document the setup requirements for each adapter.

## Files to modify

- `src/bernstein/adapters/cline.py` (new)
- `src/bernstein/adapters/roo_code.py` (new)
- `src/bernstein/adapters/base.py` (extend interface if needed)
- `docs/adapters/cline.md` (new)
- `docs/adapters/roo-code.md` (new)
- `tests/unit/test_cline_adapter.py` (new)

## Completion signal

- Bernstein can spawn and manage Cline as a worker agent
- Bernstein can spawn and manage Roo Code as a worker agent
- Adapter docs explain setup requirements
