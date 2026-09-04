# Bernstein Benchmark Catalog

Verified benchmark suites and evaluation harnesses available in Bernstein.

| Suite | Domain | Metric | Target Floor | Enforced Controls |
|---|---|---|---|---|
| `golden-v1` | Core Determinism | Multiplicative Verdict Replay | 100% | Reproducibility |
| `authority-v1` | Agentic Security & Containment | Per-Level Authority Containment Rate | 1.0 (100%) | `CTL-AUTH-01`, `CTL-SEC-02`, `CTL-OVS-01` |

## Suite Overview

### `authority-v1` (Issue #5452)
- **Description**: 20 tasks across 5 hierarchical authority levels (L0 read-only to L4 unattended side effects). Evaluates containment when executed with a never-declining adapter.
- **Corpus**: `eval/cases/authority/`
- **Execution**: `bernstein bench run authority-v1 --out bundle.json`
- **Verification**: `bernstein bench verify bundle.json --suite authority-v1`
