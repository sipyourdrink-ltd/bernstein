# D23 — Git Hooks Integration

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
Users want to run Bernstein quality checks automatically before pushing, but setting up git hooks manually is tedious and error-prone. There's no built-in way to integrate Bernstein into the git workflow.

## Solution
- Implement `bernstein hooks install` that creates a `.git/hooks/pre-push` script.
- The hook runs `bernstein doctor --quick` which performs fast checks only: lint, type checking, and critical tests.
- The hook exits non-zero on failure, blocking the push with a clear message: "Bernstein pre-push check failed. Fix issues above or push with --no-verify to skip."
- Implement `bernstein hooks uninstall` that removes the Bernstein-managed hook.
- If a pre-push hook already exists, append Bernstein's check to it rather than overwriting. Mark the inserted section with `# --- bernstein-hook-start ---` and `# --- bernstein-hook-end ---` comments for clean removal.
- Implement `bernstein hooks status` to show which hooks are currently installed.

## Acceptance
- [ ] `bernstein hooks install` creates a working `.git/hooks/pre-push` script
- [ ] The pre-push hook runs `bernstein doctor --quick` and blocks push on failure
- [ ] `bernstein hooks uninstall` cleanly removes the hook without affecting other hook content
- [ ] Existing pre-push hooks are preserved, with Bernstein's section appended
- [ ] `bernstein hooks status` correctly reports installed/not-installed state
