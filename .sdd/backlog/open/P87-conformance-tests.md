# P87 — BOS Conformance Test Suite

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Third-party orchestrators claiming BOS compliance have no way to prove it, and users have no way to verify compatibility before adopting an alternative implementation.

## Solution
- Build a conformance test suite that any orchestrator can run to prove BOS compliance
- Implement as a pytest suite organized by spec section: task lifecycle, agent interface, verification protocol, scheduling contract
- Tests exercise the full task lifecycle: submit, assign, execute, verify, complete/fail
- Agent interface tests verify registration, capability declaration, and health check
- Verification protocol tests confirm check invocation and pass/fail handling
- Scheduling tests validate priority ordering and concurrency limits
- Publish as a standalone Python package: `bernstein-conformance`
- Include a runner script: `bernstein-conformance run --endpoint <url>`

## Acceptance
- [ ] pytest suite covers task lifecycle, agent interface, verification, scheduling
- [ ] Tests are runnable against any BOS-compliant endpoint
- [ ] Published as `bernstein-conformance` Python package
- [ ] Runner script accepts `--endpoint` argument for target orchestrator
- [ ] All tests map to specific BOS v1.0 spec sections
- [ ] Pass/fail report generated with per-section compliance status
