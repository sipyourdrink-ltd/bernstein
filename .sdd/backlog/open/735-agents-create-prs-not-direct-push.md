# 735 — Agents Create PRs Instead of Direct Push

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #702

## Problem

Bernstein agents currently push directly to master after janitor verification. With Kilo Code automated PR review now active, agent work should go through PRs to get AI code review before merging. This creates a two-layer verification: janitor (functional: tests pass, files exist) + Kilo Code (quality: security, style, bugs). Direct push bypasses the quality review entirely.

## Design

### Default: PR mode
Change the default merge strategy from direct-push to PR creation:

1. After janitor verifies a task, create a branch `bernstein/task-{id}`
2. Push the verified code to the branch
3. Create a PR with:
   - Title: task title
   - Body: task description, cost summary, test results, agent role/model
   - Labels: `bernstein`, `auto-generated`, role name
4. Kilo Code automatically reviews the PR
5. If auto-merge enabled: merge after review passes
6. If manual merge: wait for human approval

### Configuration
```yaml
# bernstein.yaml
merge_strategy: pr  # pr | direct | review (interactive)
auto_merge: true     # auto-merge PR after code review passes
pr_labels: [bernstein, auto-generated]
```

### CLI override
`bernstein -g "task" --merge direct` for quick direct-push mode.

### GitHub integration
- Use `gh pr create` for PR creation
- Use `gh pr merge --auto` for auto-merge after checks pass
- PR body includes structured metadata for traceability

## Files to modify

- `src/bernstein/core/git_ops.py` (PR creation logic)
- `src/bernstein/core/orchestrator.py` (use PR instead of direct merge)
- `src/bernstein/core/janitor.py` (PR creation after verification)
- `tests/unit/test_git_ops.py` (extend)

## Completion signal

- Agent work creates PRs instead of direct push by default
- Kilo Code reviews every agent PR
- Auto-merge works after review passes
- `--merge direct` still works for quick mode
