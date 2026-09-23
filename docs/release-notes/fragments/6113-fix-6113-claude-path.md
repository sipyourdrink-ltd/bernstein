---
pr: 6113
kind: test
area: orchestrator
---

- Fixed `test_inline_goal_resolves_gates_from_yaml` to pass on CI runners where the `claude` binary is not present. The test now mocks `preflight_checks` to skip binary existence verification while preserving the gate‑configuration assertion.
