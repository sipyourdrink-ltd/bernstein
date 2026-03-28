# 723 — Domain-Based Automatic Model Routing

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

CCG Workflow (4.5K stars) automatically routes frontend tasks to Gemini, backend to Codex, and orchestration to Claude. Users love this because different models excel at different domains. Bernstein's router picks models by task scope/complexity but not by domain. Adding domain routing makes the "mix models in one run" story concrete and demonstrable.

## Design

### Domain detection
From task role + file patterns, infer domain:
- `frontend` role or `*.tsx/*.jsx/*.css` → Gemini (fast, good at UI)
- `backend` role or `*.py/*.go/*.rs` → Claude Sonnet (strong reasoning)
- `architect` role → Claude Opus (complex planning)
- `qa` role → cheapest available (tests are well-defined)
- `docs` role → cheapest available

### Configuration
```yaml
# bernstein.yaml
routing:
  frontend: gemini
  backend: claude-sonnet
  architect: claude-opus
  qa: auto  # cheapest
  docs: auto
```

### Override
Tasks can still specify explicit `model` field to override routing.

## Files to modify

- `src/bernstein/core/router.py` (add domain routing)
- `tests/unit/test_router.py` (add domain routing tests)

## Completion signal

- Frontend tasks auto-route to Gemini, backend to Claude
- Configurable via bernstein.yaml
- Explicit model override still works
