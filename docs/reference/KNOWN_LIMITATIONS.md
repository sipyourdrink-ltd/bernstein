# Known Limitations

Bernstein ships a lot of functionality, but several constraints still matter in practice. This page highlights the most relevant ones so users can plan safely.

---

## 1) Adapter parity is not perfect

**What:** Bernstein ships 40+ CLI adapters, but different CLI agents expose different capabilities and process semantics.

**Impact:** Stop/restart behavior, output shape, structured output support, and error handling can vary by adapter. The conformance harness (`adapters/conformance.py`) helps catch regressions across adapters.

**Workaround:**
- Run `bernstein doctor` before long runs.
- Run `bernstein test-adapter <name>` to smoke-test specific adapters.
- Prefer proven adapters (claude, codex, gemini) in production workflows.
- Use `bernstein stop` for controlled shutdown; use force-stop only when needed.

---

## 2) Multi-node execution is an advanced path

**What:** Bernstein has worker/cluster primitives and container execution support, but default operation remains single-host orchestration.

**Impact:** Large fan-out workloads can still bottleneck on one machine if you do not explicitly operate a distributed topology.

**Workaround:**
- Keep concurrency conservative.
- Use workspace decomposition and staged plans.
- Treat cluster/worker setups as advanced operations that require explicit validation in your environment.
- For a single long-running goal you want to detach from and reattach later, use `bernstein run-service submit`/`attach` (below) rather than a full cluster.

**Detached single-host runs (shipped):** `bernstein run-service` decouples a run from the invoking terminal on one host. A session-detached supervisor owns execution while the durable work ledger owns state; `attach` proves ledger continuity across the detach boundary before rendering progress, and every lifecycle boundary is a signed audit-chain receipt. A supervisor killed mid-run resumes from the ledger tip with zero lost completed tasks. `bernstein worker` remains the multi-host fan-out path.

**Off-host execution on ssh (shipped):** `bernstein run-service submit --backend ssh` runs each task of a detached goal on another host over ssh, in its own isolated remote git worktree (one branch per task), and appends a signed `run.ssh_task` receipt binding that worktree so an offline verifier can prove each task ran in isolation across the ssh boundary. A supervisor killed mid-run resumes on the ssh backend from the ledger tip with zero lost completed tasks. Remote credentials are resolved from the credential vault only (`--ssh-secret ENV=PROVIDER`) and never reach the ledger or the receipts. Enabling the hosted sandbox backends in the existing registry (`core/sandbox/backends/`) behind an optional extra remains a later increment.

---

## 3) Some observability is near real-time, not instant

**What:** Bernstein provides SSE endpoints and metrics, but parts of the terminal UX still rely on polling/log aggregation.

**Impact:** Short lag can appear between underlying task/agent events and what the UI shows.

**Workaround:**
- Use API/SSE endpoints for automation and dashboards.
- Use `bernstein logs` for immediate diagnostics when investigating live behavior.

---

## 4) Retry and routing are intelligent but not omniscient

**What:** Retry escalation, routing, and cost controls are implemented, but provider limits and external failures are still discovered reactively in many cases.

**Impact:** First failures can still happen before fallback logic stabilizes execution.

**Workaround:**
- Set explicit budgets.
- Use deterministic completion signals/tests.
- Monitor early-run behavior and tune config for your environment.
- Declare per-role fallback chains under `provider_availability` so dispatch
  probes provider health before spawning and fails over deterministically;
  run `bernstein doctor --failover-drill` (for example in CI) to find broken
  chains before an outage does.

---

## 5) Verification quality depends on project quality

**What:** Bernstein's gates and janitor checks can only validate what your project exposes (tests, linters, static checks, completion signals).

**Impact:** Weak test suites reduce confidence in "done" outcomes.

**Workaround:**
- Maintain strong tests and static checks.
- Add explicit `completion_signals` for critical tasks.
- Use review/audit workflows for high-risk changes.

---

## 6) Cost projections are estimates

**What:** Pre-run/early-run cost estimates are approximate and can drift for complex iterative tasks.

**Impact:** Expected and actual spend can diverge.

**Workaround:**
- Set hard budgets.
- Monitor spend via `bernstein cost` and cost endpoints.
- Use anomaly detection and budget thresholds as guardrails.

---

## 7) Documentation lag

**What:** Bernstein evolves quickly; some docs may lag short-term behind newly shipped features.

**Workaround:**
- Cross-check CLI (`bernstein --help`) and API routes when implementing automation.
- Prefer core reference docs (`GETTING_STARTED`, `CONFIG`, `FEATURE_MATRIX`) over older narrative pages.
- Use `bernstein debug` to generate a debug bundle for comprehensive triage.

---

## 8) Protocol negotiation is best-effort

**What:** Protocol negotiation (`protocol_negotiation.py`) detects version compatibility at connection time, but not all agents support all protocol versions.

**Impact:** Mixed-version deployments may see fallback behavior or reduced functionality when newer protocol features are unavailable on the remote side.

**Workaround:**
- Keep agent CLIs updated to versions that support the protocol features you need.
- Check the schema registry (`schema_registry.py`) for supported message versions.
- Use `bernstein test-adapter` to validate protocol support before production runs.

---

*Last updated: 2026-07-16.*
