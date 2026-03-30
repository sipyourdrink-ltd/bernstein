# D12 — `bernstein doctor --fix` Auto-Fix Mode

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
`bernstein doctor` reports diagnostic issues but users must manually fix each one. Common fixes are mechanical (create a directory, install a dependency) and could be automated to save time.

## Solution
- Add a `--fix` flag to the existing `bernstein doctor` command
- For each diagnostic check that fails, define an associated auto-fix action:
  - Missing `.sdd/` directory -> create it with the standard subdirectory structure (`traces/`, `runs/`, `backlog/`)
  - Missing adapter (e.g., claude, openai) -> print the exact `pip install` or `bernstein adapter install` command and offer to run it
  - Invalid or missing `bernstein.yaml` -> offer to run `bernstein init`
  - Wrong Python version -> print upgrade instructions with the minimum required version
  - Missing API keys -> print instructions for setting the required environment variable
- Without `--fix`, behavior is unchanged: just report pass/fail for each check
- With `--fix`, prompt the user (y/n) before each auto-fix action unless `--yes` is also passed
- Print a final summary of fixes applied and any remaining issues that require manual intervention

## Acceptance
- [ ] `bernstein doctor` without `--fix` behaves exactly as before (report only)
- [ ] `bernstein doctor --fix` with a missing `.sdd/` directory creates it with standard subdirectories
- [ ] `bernstein doctor --fix` with a missing adapter prints the install command and offers to run it
- [ ] `bernstein doctor --fix` with a missing `bernstein.yaml` offers to run `bernstein init`
- [ ] `bernstein doctor --fix --yes` applies all fixes without prompting
- [ ] A final summary lists fixes applied and remaining manual issues
