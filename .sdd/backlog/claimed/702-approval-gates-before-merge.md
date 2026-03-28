# 702 — Approval Gates Before Merge

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein auto-merges agent work after janitor verification. Enterprise users and serious developers want a review step before agent code hits their branch. Competitor Shep has configurable approval gates. This is a top-3 feature request for any agent orchestrator. Without it, Bernstein is "a toy that auto-commits to your repo" — terrifying for real projects.

## Design

Add configurable approval gates between verification and merge:

### Modes
- `auto` (default for --headless): merge immediately after janitor passes
- `review` (default for interactive): show diff, ask for approval
- `pr` : create a PR instead of direct merge, let user review on GitHub

### Configuration
```yaml
# bernstein.yaml
approval: review  # auto | review | pr
```

CLI override: `bernstein -g "task" --approval pr`

### Review mode UX
After janitor verifies, show:
```
✓ Task "Add auth" complete (agent-1, 34s, $0.12)
  Files: +src/auth.py, +tests/test_auth.py, ~src/app.py
  Tests: 12 passed, 0 failed

  [a]pprove  [d]iff  [r]eject  [p]r
```

### PR mode
- Create branch `bernstein/task-{id}`
- Push verified code
- Create PR with task description, cost summary, test results
- Add labels: `bernstein`, `auto-generated`

## Files to modify

- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/git_ops.py`
- `src/bernstein/cli/main.py`
- `tests/unit/test_approval_gates.py` (new)

## Completion signal

- `bernstein -g "task" --approval review` pauses for user approval
- `bernstein -g "task" --approval pr` creates a GitHub PR
- Tests pass for all three modes
