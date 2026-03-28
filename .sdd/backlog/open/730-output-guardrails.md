# 730 — Output Guardrails: Secret Detection + Scope Enforcement

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Agents can accidentally commit secrets (API keys, passwords, tokens) or modify files outside their task's scope. There's no automated check between agent completion and merge. The janitor verifies functional signals (tests pass, files exist) but doesn't check for security violations or scope creep. One leaked API key in a public commit is a catastrophic event.

## Design

### Pre-merge guardrails
After an agent completes but before merging, run automated checks on the diff:

1. **Secret detection**: Scan diff for patterns matching API keys, tokens, passwords, private keys. Use regex patterns for common formats (AWS keys, GitHub tokens, JWT secrets, base64-encoded credentials). Flag any match as a hard block — never auto-merge.

2. **Scope enforcement**: Each task specifies which directories/files it's allowed to modify (derived from role + task description). If an agent modifies files outside its scope, flag for review. E.g., a `qa` role agent shouldn't be editing `src/bernstein/core/orchestrator.py`.

3. **Dangerous operation detection**: Flag deletions of critical files (README, LICENSE, pyproject.toml, CI configs), removal of tests, and large-scale deletions (>50% of a file removed).

### Statistics tracking
Track guardrail events in `.sdd/metrics/guardrails.jsonl`:
```json
{"timestamp": "...", "task_id": "T-001", "check": "secret_detection", "result": "pass"}
{"timestamp": "...", "task_id": "T-002", "check": "scope_enforcement", "result": "blocked", "files": ["pyproject.toml"]}
```

`bernstein doctor` includes guardrail stats: "42 tasks checked, 2 blocked (1 secret, 1 scope violation)".

### Configuration
```yaml
# bernstein.yaml
guardrails:
  secrets: true        # default
  scope: true          # default
  max_deletion_pct: 50 # flag if >50% of file deleted
```

## Files to modify

- `src/bernstein/core/guardrails.py` (new)
- `src/bernstein/core/janitor.py` (integrate guardrails before merge)
- `tests/unit/test_guardrails.py` (new)

## Completion signal

- Secret patterns detected in test diffs
- Scope violations flagged when agent edits out-of-scope files
- Guardrail stats visible in `bernstein doctor`
