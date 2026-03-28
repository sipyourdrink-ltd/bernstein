# Workflow Registry — Bernstein

**Last updated**: 2026-03-28
**Maintainer**: Workflow Architect

---

## View 1: By Workflow

| Workflow | Spec file | Status | Trigger | Primary actor | Last reviewed |
|---|---|---|---|---|---|
| CI Failure Auto-Routing | WORKFLOW-ci-failure-routing.md | Approved | GitHub Actions `workflow_run` webhook | Webhook handler | 2026-03-28 |
| Rate-Limit-Aware Scheduling | WORKFLOW-rate-limit-aware-scheduling.md | Draft | Agent death / orchestrator tick | RateLimitTracker + TierAwareRouter | 2026-03-28 |
| Env Var Isolation for Agents | WORKFLOW-env-var-isolation.md | Approved | `adapter.spawn()` call | CLI Adapter + `build_filtered_env()` | 2026-03-28 |
| GitHub Issue to Task | — | Missing | GitHub `issues.opened` webhook | Webhook handler | — |
| PR Review Comment to Fix | — | Missing | GitHub `pull_request_review_comment` webhook | Webhook handler | — |
| Push to QA Verify | — | Missing | GitHub `push` webhook | Webhook handler | — |
| Task Retry / Escalation | — | Missing | Task failure event | Orchestrator | — |
| Agent Crash Recovery | — | Missing | Agent session goes orphaned | Janitor | — |
| Task Auto-Decomposition | — | Missing | LARGE scope task open | Orchestrator | — |

---

## View 2: By Component

| Component | File(s) | Workflows it participates in |
|---|---|---|
| Webhook route | src/bernstein/core/routes/webhooks.py | CI Failure Auto-Routing, GitHub Issue to Task, PR Review Comment to Fix, Push to QA Verify |
| GitHub mapper | src/bernstein/github_app/mapper.py | CI Failure Auto-Routing, GitHub Issue to Task, PR Review Comment to Fix, Push to QA Verify |
| Webhook parser | src/bernstein/github_app/webhooks.py | All GitHub-triggered workflows |
| CLI Adapters | src/bernstein/adapters/{claude,codex,gemini,qwen,aider,amp,generic,manager}.py | Env Var Isolation for Agents |
| Env isolation util | src/bernstein/adapters/env_isolation.py | Env Var Isolation for Agents |
| Task lifecycle | src/bernstein/core/task_lifecycle.py | Task Retry / Escalation, Task Auto-Decomposition, Rate-Limit-Aware Scheduling |
| Orchestrator | src/bernstein/core/orchestrator.py | Task Retry / Escalation, Task Auto-Decomposition, Agent Crash Recovery, Rate-Limit-Aware Scheduling |
| TierAwareRouter | src/bernstein/core/router.py | Rate-Limit-Aware Scheduling |
| MetricsCollector | src/bernstein/core/metrics.py | Rate-Limit-Aware Scheduling |
| RateLimitTracker | src/bernstein/core/rate_limit_tracker.py (new) | Rate-Limit-Aware Scheduling |
| Janitor | src/bernstein/core/janitor.py | Agent Crash Recovery |
| Git ops | src/bernstein/core/git_ops.py | CI Failure Auto-Routing, Task Auto-Decomposition |
| Task server | src/bernstein/core/server.py | All workflows (task CRUD) |
| Task store | src/bernstein/core/server.py | All workflows |

---

## View 3: By User Journey

### Operator Journeys
| What the operator does | Underlying workflow(s) | Entry point |
|---|---|---|
| CI fails after agent push | CI Failure Auto-Routing | GitHub Actions → webhook |
| Opens GitHub issue for a bug | GitHub Issue to Task | GitHub issue UI |
| Leaves actionable PR review comment | PR Review Comment to Fix | GitHub PR UI |
| Pushes code | Push to QA Verify | git push |

### System-to-System Journeys
| What happens automatically | Underlying workflow(s) | Trigger |
|---|---|---|
| GitHub Actions workflow fails | CI Failure Auto-Routing | workflow_run completed/failure webhook |
| Agent times out or crashes | Agent Crash Recovery | Orchestrator tick |
| Large task fails repeatedly | Task Auto-Decomposition | Orchestrator tick |
| Provider returns HTTP 429 | Rate-Limit-Aware Scheduling | Agent death + log scan |
| Provider throttle expires | Rate-Limit-Aware Scheduling | Orchestrator tick |

---

## View 4: By State (CI Fix Task)

| State | Entered by | Exited by | Workflows that can trigger exit |
|---|---|---|---|
| open | CI failure routing creates fix task | -> claimed | Task claim |
| claimed | Agent picks up fix task | -> in_progress, open (timeout) | Task lifecycle |
| in_progress | Agent begins working | -> done, failed | Agent completion |
| done | Agent fixes CI and marks complete | (terminal) | — |
| failed | Agent fails fix task | -> open [RETRY N] (if retries remain) | CI Failure Auto-Routing retry |
| failed (max retries) | Retry count ≥ 3 | (terminal — quarantine) | — |
