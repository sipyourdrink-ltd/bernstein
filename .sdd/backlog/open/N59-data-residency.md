# N59 — Data Residency Controls

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Enterprise customers in regulated industries cannot ensure their data stays within required geographic boundaries, as Bernstein has no mechanism to control which region providers operate in.

## Solution
- Add `residency: eu|us|ap` field to bernstein.yaml
- Orchestrator reads the residency setting and routes tasks only to providers in the specified region
- Validate provider region against provider metadata at task dispatch time
- Emit a warning if no provider is available in the configured region
- Block execution (with override flag) if residency constraint cannot be satisfied

## Acceptance
- [ ] bernstein.yaml supports `residency: eu|us|ap` setting
- [ ] Orchestrator routes tasks only to providers matching the residency region
- [ ] Provider metadata includes region information for validation
- [ ] Warning is emitted when no provider is available in the specified region
- [ ] Execution is blocked by default if residency cannot be satisfied
- [ ] `--force` flag allows override with logged warning
