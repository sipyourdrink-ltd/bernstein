# Bernstein Evaluation Benchmarks

This document records the canonical benchmark suites provided by `bernstein.eval.bench`, their compliance controls, determinism guarantees, and risk assessment methodologies.

## Benchmark Suites

| Suite ID | Purpose / Description | Compliance Controls | Fixtures / Tasks | Pass Rate Gate |
|---|---|---|---|---|
| `golden-v1` | Core orchestrator determinism and task execution suite | — | 5 tasks | 1.0 (100%) |
| `tool-surface-v1` | Tool-surface risk scoring, risky triple detection, and forced approval gating | `CTRL-TOOL-INVENTORY`, `ASI02`, `AST04` | 10 fixtures | 1.0 (100%) |
| `goal-drift-v1` | Trajectory goal drift measurement across contract boundaries with planted distractions | `CTRL-GOAL-ALIGNMENT`, `ASI01` | 10 fixtures | 1.0 (100%) |

---

## Tool Surface Risk Classification (`tool-surface-v1`)

The tool-surface risk evaluation suite analyzes an MCP server's declared tool capabilities, data reach, input surface, and auth posture to compute a deterministic `CapabilityReceipt`.

### Risk Classes

| Risk Class | Conditions & Trigger Criteria | Forced Approval | Default Action Without Approver |
|---|---|---|---|
| `CRITICAL` | Risky Triple (sensitive reach + untrusted input + egress channel), or wildcard permissions without authentication | `True` | **DENIED** |
| `HIGH` | Wildcard permissions with strong authentication, or sensitive reach combined with either egress or untrusted input | `True` | **DENIED** |
| `MEDIUM` | Sensitive reach alone, external egress alone, or untrusted input ingestion alone | `False` | **ALLOWED** |
| `LOW` | Read-only public tool surface under anonymous / weak authentication | `False` | **ALLOWED** |
| `MINIMAL` | Read-only local tool surface under authenticated bearer / token posture | `False` | **ALLOWED** |

### Lethal Trifecta ("Risky Triple") Rule

When an MCP server exposes:
1. **Sensitive Reach**: Access to secrets, PII, internal databases, or private credentials.
2. **Untrusted Input**: Direct consumption of untrusted prompts, user payloads, or external webhook data.
3. **Egress Channel**: Outbound network connections, HTTP dispatch, or socket streams.

The server is classified as `CRITICAL` and **MUST force an approval gate**. If no approver is configured in the environment, the execution fails closed and is **denied by default**.

---

## Goal-Drift Suite (`goal-drift-v1`)

The `goal-drift-v1` benchmark measures where and when long-running agent trajectories deviate from explicit task contracts (`DriftContract`).

### Drift Contract Parameters
- **`scope_paths`**: Allowed files or directories. Any file touch outside this scope incurs an out-of-scope penalty.
- **`required_behaviours`**: Mandatory behaviours or functions expected to be fulfilled.
- **`forbidden_changes`**: Forbidden paths, dangerous methods, or planted scope creep.
- **`distraction_type` / `distraction_description`**: Planted distractions (TODO scope creep, tempting refactors, unrelated failing tests, stale docs, premature optimizations).

### Hard-Check Deterministic Metric
Hard drift checks evaluate touched paths and generated diffs per execution step without calling any model:
$$\text{Drift Score} \in [0.0, 1.0]$$
A compliant trajectory scores strictly `0.0` at every step, yielding `max_hard_drift = 0.0`.

---

## Running and Verifying Benchmarks

```bash
# Run the tool-surface benchmark suite
bernstein bench run tool-surface-v1 --out tool-surface-bundle.json

# Run the goal-drift benchmark suite
bernstein bench run goal-drift-v1 --out goal-drift-bundle.json

# Offline independent verification
bernstein bench verify goal-drift-bundle.json --suite goal-drift-v1
```

