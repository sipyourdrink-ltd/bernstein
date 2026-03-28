# 346e — Roo Code CLI Adapter

**Role:** backend
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Roo Code (fork of Cline, 20K+ stars) is one of the most popular VS Code AI coding extensions. It has a CLI mode. Adding an adapter means Bernstein can orchestrate Roo Code agents alongside Claude/Codex/Gemini — expanding our user base to the Roo Code community.

## Design

Implement `src/bernstein/adapters/roo_code.py`:
- Detect binary: `which roo-code` or `which roo`
- Spawn with prompt via CLI flags (check Roo Code's --help for headless/non-interactive mode)
- Map Bernstein model configs to Roo Code's model settings
- Handle Roo Code's file editing patterns
- Register in adapter registry

### Reference
- `src/bernstein/adapters/claude.py` — reference adapter
- `src/bernstein/adapters/base.py` — CLIAdapter protocol
- Roo Code GitHub: github.com/RooCodeInc/Roo-Code

## Files to modify

- `src/bernstein/adapters/roo_code.py` (new)
- `src/bernstein/agents/registry.py` (register)
- `tests/unit/test_adapter_roo_code.py` (new)

## Completion signal

- `bernstein -g "task" --cli roo-code` works
- Registered in auto-discovery
- Tests pass
