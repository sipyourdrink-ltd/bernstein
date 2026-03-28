# 507 — bernstein init creates bernstein.yaml and .gitignore entry

**Role:** backend
**Priority:** 2
**Scope:** small
**Complexity:** low

## Problem
`bernstein init` creates `.sdd/` structure but doesn't create `bernstein.yaml` in the project root. User has to manually create it or know about the `-g` flag. Also doesn't add `.sdd/runtime/` to `.gitignore`.

## Implementation
1. `bernstein init` creates `bernstein.yaml` with commented template:
```yaml
# Bernstein orchestration config
# goal: "Describe your goal here"
# cli: claude  # or codex, gemini, qwen
```
2. Appends `.sdd/runtime/` to `.gitignore` if not already present
3. Prints: "Created bernstein.yaml — edit the goal and run `bernstein`"

## Files
- src/bernstein/cli/main.py — update init command

## Completion signals
- file_contains: src/bernstein/cli/main.py :: bernstein.yaml
