# 724 — Sidecar TUI Compatibility

**Role:** backend
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** none

## Problem

Sidecar (854 stars, Go TUI) is the best conversation viewer for CLI agents — supports 10+ agents, has git integration, workspace management. It's complementary to Bernstein, not competitive. If Bernstein agents produce Sidecar-compatible conversation logs, Sidecar users can monitor Bernstein runs with their favorite tool. This is a low-effort partnership play that taps into Sidecar's community.

## Design

### Sidecar conversation format
Write agent session transcripts in a format Sidecar can read:
- One JSON file per session in a location Sidecar knows
- Include: prompts sent, responses received, tool calls, file changes

### Integration
- When Sidecar is detected (binary in PATH), log in compatible format
- Add note to docs: "Works great with Sidecar for conversation monitoring"

## Files to modify

- `src/bernstein/core/spawner.py` (add Sidecar-format logging)
- `docs/integrations/sidecar.md` (new)

## Completion signal

- Sidecar can display Bernstein agent conversations
- Documented in integrations guide
