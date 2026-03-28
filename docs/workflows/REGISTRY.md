# Workflow Registry — Bernstein

**Last updated**: 2026-03-29 (updated for 510b Event-Driven Triggers)
**Maintainer**: Workflow Architect

---

## View 1: By Workflow

| Workflow | Spec file | Status | Trigger | Primary actor | Last reviewed |
|---|---|---|---|---|---|
| Event-Driven Agent Triggers | WORKFLOW-event-driven-triggers.md | Draft | Git push, CI failure, Slack msg, cron, file watch, webhook | TriggerManager | 2026-03-29 |
| VS Code Extension Publishing | WORKFLOW-extension-publishing.md | Draft | Git tag `ext-v*` | GitHub Actions | 2026-03-29 |
| VS Code Extension UX Interactions | WORKFLOW-extension-ux.md | Draft | Extension activation / user clicks | User / VS Code | 2026-03-29 |
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

### VS Code Extension Components
| Component | File(s) | Workflows |
|---|---|---|
| Extension activation | packages/vscode/src/extension.ts | VS Code Extension UX Interactions (STEP 1, STEP 4) |
| Tree providers | packages/vscode/src/AgentTreeProvider.ts, TaskTreeProvider.ts | VS Code Extension UX Interactions (STEP 3, STEP 5, STEP 6) |
| Dashboard provider | packages/vscode/src/DashboardProvider.ts | VS Code Extension UX Interactions (STEP 7, STEP 8) |
| BernsteinClient | packages/vscode/src/BernsteinClient.ts | VS Code Extension UX Interactions (all API interactions) |
| Status bar | packages/vscode/src/StatusBarManager.ts | VS Code Extension UX Interactions (all steps) |
| Output manager | packages/vscode/src/OutputManager.ts | VS Code Extension UX Interactions (STEP 5 — agent output) |
| Package.json | packages/vscode/package.json | VS Code Extension Publishing (STEP 2, STEP 6) |
| GitHub Actions workflow | .github/workflows/publish-extension.yml | VS Code Extension Publishing (STEP 1-9) |
| Extension icons | packages/vscode/media/bernstein-icon.{svg,png} | VS Code Extension Publishing (STEP 6); UX branding |
| README | packages/vscode/README.md | VS Code Extension Publishing (STEP 6, STEP 9) |
| CHANGELOG | packages/vscode/CHANGELOG.md | VS Code Extension Publishing (STEP 6, STEP 9) |

### Event-Driven Trigger Components
| Component | File(s) | Workflows it participates in |
|---|---|---|
| TriggerManager | src/bernstein/core/trigger_manager.py (new) | Event-Driven Agent Triggers (all steps) |
| TriggerEvent model | src/bernstein/core/models.py | Event-Driven Agent Triggers (STEP 1) |
| TriggerConfig model | src/bernstein/core/models.py | Event-Driven Agent Triggers (STEP 2) |
| GitHub push source | src/bernstein/core/trigger_sources/github_push.py (new) | Event-Driven Agent Triggers (STEP 1, STEP 2) |
| GitHub workflow_run source | src/bernstein/core/trigger_sources/github_workflow_run.py (new) | Event-Driven Agent Triggers (STEP 1, STEP 2) |
| Slack source | src/bernstein/core/trigger_sources/slack.py (new) | Event-Driven Agent Triggers (STEP 1, STEP 2) |
| Cron source | src/bernstein/core/trigger_sources/cron.py (new) | Event-Driven Agent Triggers (STEP 2b) |
| File watch source | src/bernstein/core/trigger_sources/file_watch.py (new) | Event-Driven Agent Triggers (STEP 2c) |
| Generic webhook source | src/bernstein/core/trigger_sources/webhook.py (new) | Event-Driven Agent Triggers (STEP 1, STEP 2) |
| Slack webhook route | src/bernstein/core/routes/webhooks.py (modified) | Event-Driven Agent Triggers (Slack ingestion) |
| Generic trigger route | src/bernstein/core/routes/webhooks.py (modified) | Event-Driven Agent Triggers (webhook ingestion) |
| Trigger config | .sdd/config/triggers.yaml (new) | Event-Driven Agent Triggers (STEP 2) |
| Trigger state files | .sdd/runtime/triggers/ (new dir) | Event-Driven Agent Triggers (STEP 3, STEP 4, STEP 6) |
| Trigger CLI commands | src/bernstein/cli/ (modified) | Event-Driven Agent Triggers (operator interface) |

### Orchestration Core Components
| Component | File(s) | Workflows it participates in |
|---|---|---|
| Webhook route | src/bernstein/core/routes/webhooks.py | CI Failure Auto-Routing, GitHub Issue to Task, PR Review Comment to Fix, Push to QA Verify, Event-Driven Agent Triggers |
| GitHub mapper | src/bernstein/github_app/mapper.py | CI Failure Auto-Routing, GitHub Issue to Task, PR Review Comment to Fix, Push to QA Verify, Event-Driven Agent Triggers (Phase 1 adapter) |
| Webhook parser | src/bernstein/github_app/webhooks.py | All GitHub-triggered workflows, Event-Driven Agent Triggers |
| CLI Adapters | src/bernstein/adapters/{claude,codex,gemini,qwen,aider,amp,generic,manager}.py | Env Var Isolation for Agents |
| Env isolation util | src/bernstein/adapters/env_isolation.py | Env Var Isolation for Agents |
| Task lifecycle | src/bernstein/core/task_lifecycle.py | Task Retry / Escalation, Task Auto-Decomposition, Rate-Limit-Aware Scheduling |
| Orchestrator | src/bernstein/core/orchestrator.py | Task Retry / Escalation, Task Auto-Decomposition, Agent Crash Recovery, Rate-Limit-Aware Scheduling, Event-Driven Agent Triggers (cron + file-watch tick) |
| TierAwareRouter | src/bernstein/core/router.py | Rate-Limit-Aware Scheduling |
| MetricsCollector | src/bernstein/core/metrics.py | Rate-Limit-Aware Scheduling |
| RateLimitTracker | src/bernstein/core/rate_limit_tracker.py (new) | Rate-Limit-Aware Scheduling |
| Janitor | src/bernstein/core/janitor.py | Agent Crash Recovery |
| Git ops | src/bernstein/core/git_ops.py | CI Failure Auto-Routing, Task Auto-Decomposition |
| Task server | src/bernstein/core/server.py | All workflows (task CRUD) |
| Task store | src/bernstein/core/server.py | All workflows |
| Notification manager | src/bernstein/core/notifications.py | Event-Driven Agent Triggers (optional fire notifications) |

---

## View 3: By User Journey

### Operator Journeys
| What the operator does | Underlying workflow(s) | Entry point |
|---|---|---|
| CI fails after agent push | CI Failure Auto-Routing, Event-Driven Agent Triggers | GitHub Actions → webhook |
| Opens GitHub issue for a bug | GitHub Issue to Task, Event-Driven Agent Triggers | GitHub issue UI |
| Leaves actionable PR review comment | PR Review Comment to Fix, Event-Driven Agent Triggers | GitHub PR UI |
| Pushes code | Push to QA Verify, Event-Driven Agent Triggers | git push |
| Sends @bernstein message in Slack | Event-Driven Agent Triggers | Slack channel message |
| Configures triggers in triggers.yaml | Event-Driven Agent Triggers | `.sdd/config/triggers.yaml` edit |
| Checks trigger fire history | Event-Driven Agent Triggers | `bernstein triggers history` CLI |
| Manually fires a trigger for testing | Event-Driven Agent Triggers | `bernstein triggers fire <name>` CLI |
| Sends POST to custom webhook | Event-Driven Agent Triggers | `POST /webhooks/trigger/{path}` |

### System-to-System Journeys
| What happens automatically | Underlying workflow(s) | Trigger |
|---|---|---|
| GitHub Actions workflow fails | CI Failure Auto-Routing, Event-Driven Agent Triggers | workflow_run completed/failure webhook |
| Agent times out or crashes | Agent Crash Recovery | Orchestrator tick |
| Large task fails repeatedly | Task Auto-Decomposition | Orchestrator tick |
| Provider returns HTTP 429 | Rate-Limit-Aware Scheduling | Agent death + log scan |
| Provider throttle expires | Rate-Limit-Aware Scheduling | Orchestrator tick |
| Cron schedule fires (e.g., nightly evolution) | Event-Driven Agent Triggers | Orchestrator tick evaluates cron expression |
| Source files change on disk | Event-Driven Agent Triggers | Watchdog filesystem observer |
| Push event from GitHub matches trigger rule | Event-Driven Agent Triggers | GitHub push webhook → TriggerManager |
| External system sends webhook | Event-Driven Agent Triggers | POST /webhooks/trigger/{path} |
| Trigger rate limit exceeded (>20 tasks/min) | Event-Driven Agent Triggers (ABORT_CLEANUP) | TriggerManager global rate counter |

---

## View 4: By State

### CI Fix Task State Map
| State | Entered by | Exited by | Workflows that can trigger exit |
|---|---|---|---|
| open | CI failure routing creates fix task | -> claimed | Task claim |
| claimed | Agent picks up fix task | -> in_progress, open (timeout) | Task lifecycle |
| in_progress | Agent begins working | -> done, failed | Agent completion |
| done | Agent fixes CI and marks complete | (terminal) | — |
| failed | Agent fails fix task | -> open [RETRY N] (if retries remain) | CI Failure Auto-Routing retry |
| failed (max retries) | Retry count ≥ 3 | (terminal — quarantine) | — |

### Event-Driven Trigger Task State Map
| State | Entered by | Exited by | Workflows that can trigger exit |
|---|---|---|---|
| open | TriggerManager creates task (STEP 6) | -> claimed | Orchestrator tick |
| claimed | Orchestrator assigns to agent | -> in_progress, open (timeout) | Spawner / claim timeout |
| in_progress | Agent begins work | -> done, failed | Agent completion |
| done | Agent completes triggered task | (terminal) | — |
| failed | Agent fails triggered task | -> open (retry, if retryable trigger) | Task lifecycle retry |
| failed (max retries) | Retry count exhausted | (terminal — quarantine) | — |

### Trigger System State Map
| State | Entered by | Exited by | Workflows that can trigger exit |
|---|---|---|---|
| enabled | Server startup + valid config | -> disabled (config error / rate limit) | ABORT_CLEANUP |
| disabled | Config parse error, global rate limit exceeded | -> enabled (config fixed, operator clears marker) | Operator intervention |
| cooldown | Trigger fired recently | -> ready (cooldown_s elapsed) | Time-based |
| ready | Cooldown expired or no prior fire | -> cooldown (trigger fires) | Event-Driven Agent Triggers (STEP 6) |
