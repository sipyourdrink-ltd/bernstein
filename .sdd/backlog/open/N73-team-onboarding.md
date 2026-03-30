# N73 — Team Onboarding Wizard

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
New teams face a steep onboarding curve — they must manually create workspaces, generate API keys, configure providers, and discover workflows, which delays time-to-value.

## Solution
- Implement `bernstein onboard` as a guided TUI wizard
- Steps: create workspace, invite team members (generate API keys per member), configure AI providers (with validation), run a first workflow together
- Use Rich TUI library for interactive prompts, progress indicators, and styled output
- Each step can be skipped or revisited
- On completion, output a summary of what was configured and next steps

## Acceptance
- [ ] `bernstein onboard` launches a guided TUI wizard
- [ ] Wizard creates a new workspace
- [ ] Wizard generates API keys for invited team members
- [ ] Wizard configures AI providers with validation
- [ ] Wizard runs a first workflow as a smoke test
- [ ] Rich TUI provides interactive prompts and styled output
- [ ] Completion summary shows what was configured and suggested next steps
