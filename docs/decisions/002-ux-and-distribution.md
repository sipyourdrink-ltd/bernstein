# ADR-002: UX and Distribution Model

**Status**: Proposed
**Date**: 2026-03-22
**Context**: Bernstein needs a user interaction model that balances fast onboarding, daily ergonomics, monitoring visibility, and enterprise readiness.

---

## Options Evaluated

### Option A: CLI-only

```bash
pip install bernstein
bernstein init
bernstein -g "Build a REST API for user management"
bernstein status
bernstein add-task "Add rate limiting"
```

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Time to first value | **Fast** (2-3 min) | pip install, one command, agents start working |
| Ongoing friction | **Medium** | Fine for starting work, poor for monitoring 6+ agents live |
| Monitoring capability | **Weak** | `bernstein status` is a snapshot, not a live view. Polling via `watch` is crude. |
| Enterprise readiness | **Low** | No audit trail UI, no access controls, no team visibility |
| Implementation cost | **Low** | Click + Rich already in deps. 1-2 weeks for solid CLI. |

**Verdict**: Necessary as the foundation layer. Every other option builds on top of this. But insufficient alone once you have more than 2-3 agents running.

---

### Option B: CLI + Web Dashboard

```bash
bernstein --dashboard
# Opens localhost:8080 with real-time agent status, task board, cost tracker
```

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Time to first value | **Fast** (2-3 min) | Same as CLI, dashboard is opt-in bonus |
| Ongoing friction | **Low** | Visual Kanban board, live agent heartbeats, cost meter. Replaces constant `status` polling. |
| Monitoring capability | **Strong** | Real-time WebSocket updates, task flow visualization, cost burn-down. |
| Enterprise readiness | **Medium** | Visual audit trail, but still single-user localhost. Needs auth layer for team use. |
| Implementation cost | **Medium-High** | FastAPI backend already exists (task server). Frontend is the cost: React/Svelte + WebSocket + task board UI. 3-5 weeks for a useful v1. |

**Verdict**: High value but should be Phase 2, not Phase 1. The task server already serves JSON at localhost:8052 -- a dashboard is a natural read-only layer on top of it. Risk: frontend maintenance burden. Mitigation: keep it minimal (status + task board + cost), no complex state management.

---

### Option C: "Seed" concept -- declarative config file

```yaml
# bernstein.yaml
goal: "Build a REST API for user management"
budget: "$20"
team: auto
cli: auto    # auto-detects installed agents; or set explicitly: claude, codex, gemini, qwen
```

```bash
bernstein   # reads bernstein.yaml, plans, spawns, works
```

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Time to first value | **Fastest** (1 min) | Write YAML, run one word. Lowest possible friction. |
| Ongoing friction | **Very low for starting**, but **high for mid-run adjustments** | Great for "fire and forget." Bad for "actually I need to change the plan." |
| Monitoring capability | **None inherent** | Still needs CLI status or dashboard for visibility |
| Enterprise readiness | **Medium** | Declarative configs are version-controllable, reviewable, reproducible. Good for gitops. |
| Implementation cost | **Low** | YAML parsing + mapping to existing CLI commands. 1 week. |

**Verdict**: Excellent as a convenience layer on top of CLI. The YAML file is not a replacement for the CLI -- it is a preset. Think `docker-compose.yml`: you still need `docker` commands, but compose gives you a one-command "bring up the whole stack" experience. This should ship with Phase 1.

---

### Option D: "Life seed" -- self-evolving watcher

`.bernstein/seed.md` lives in the repo. Bernstein watches it. When it changes, it re-plans.

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Time to first value | **Slow** (5-10 min) | User has to understand the watcher model, write a spec in the right format, trust the system to react. |
| Ongoing friction | **Low once understood** | Edit the spec, Bernstein adapts. Feels like a living document. |
| Monitoring capability | **Unclear** | The watcher itself needs monitoring. Who watches the watcher? |
| Enterprise readiness | **Low** | Implicit control flow is hard to audit. "Why did it start doing X?" -> "Because someone edited seed.md line 47." Non-obvious causality. |
| Implementation cost | **Medium** | File watcher (watchdog/inotify), diff detection, re-planning logic. 2-3 weeks. |

**Verdict**: Interesting but dangerous. The rag_challenge experience showed that implicit triggers cause confusion. Our agents worked best when commands were explicit and state was inspectable. A file-watcher that re-plans on every edit risks runaway re-planning, wasted budget, and "I saved a typo fix and it respawned 5 agents." This is a Phase 3+ experiment, not a core interaction model. If pursued, it needs a confirmation step ("Detected spec change. Re-plan? [y/N]").

---

### Option E: GitHub-native (GitHub App)

Create issues with `bernstein` label, agents pick them up, produce PRs.

| Dimension | Rating | Notes |
|-----------|--------|-------|
| Time to first value | **Slow** (15-30 min) | Install GitHub App, configure permissions, create first labeled issue, wait for agent pickup. |
| Ongoing friction | **Low for teams** | Familiar issue/PR workflow. Humans review PRs as normal. |
| Monitoring capability | **Strong** | GitHub's existing issue board, PR timeline, check runs. No custom UI needed. |
| Enterprise readiness | **Highest** | Audit trail built-in. Branch protection. PR reviews. RBAC via GitHub permissions. SOC2-friendly. |
| Implementation cost | **High** | GitHub App registration, webhook handling, OAuth, API rate limit management, CI integration. 6-8 weeks for production quality. |

**Verdict**: The strongest enterprise story but the weakest developer story. The latency penalty is real: GitHub webhook -> spawn agent -> agent works -> push -> PR created is 30-60+ seconds of plumbing overhead per task. For a solo developer running Bernstein locally, this is pure friction. For a team of 10 engineers sharing a Bernstein instance, this is the correct model. This is Phase 3, and it should be a separate distribution channel, not the primary interface.

---

## Comparison Matrix

| Dimension | A: CLI | B: CLI+Dash | C: Seed | D: Life seed | E: GitHub |
|-----------|--------|-------------|---------|--------------|-----------|
| Time to first value | 2-3 min | 2-3 min | 1 min | 5-10 min | 15-30 min |
| Ongoing friction | Medium | Low | Low* | Low* | Low (teams) |
| Monitoring | Weak | Strong | None | Unclear | Strong |
| Enterprise ready | Low | Medium | Medium | Low | High |
| Impl. cost | Low | Med-High | Low | Medium | High |
| Solo dev fit | Good | Great | Great | Risky | Poor |
| Team fit | Poor | Good | Medium | Poor | Great |

*Low for steady-state. High for mid-run adjustments.

---

## Recommendation: Layered approach (C + A + B, then E)

The options are not mutually exclusive. They are layers.

### Phase 1 (MVP): CLI + Seed file

Ship with both interaction modes from day one:

```bash
# Imperative mode — full control
bernstein -g "Build a REST API"
bernstein status
bernstein add-task "Add rate limiting"

# Declarative mode — fire and forget
cat bernstein.yaml   # goal, budget, team, cli
bernstein            # reads config, does everything
```

The seed file (`bernstein.yaml`) is the "easy button." The CLI is the "control panel." Both talk to the same task server underneath.

**Why this wins**: A new user can see results in under 2 minutes. The YAML file is shareable, version-controllable, and self-documenting. The CLI provides escape hatches for mid-run adjustments. Implementation cost is low because the CLI is already planned and YAML parsing is trivial.

The `bernstein.yaml` name echoes `docker-compose.yml` -- developers immediately understand the pattern: "This file describes what I want; the tool figures out how to do it."

### Phase 2: Web Dashboard

Once the task server is stable, add `bernstein --dashboard`:

- Real-time agent status grid (heartbeat indicators)
- Task Kanban board (backlog / in-progress / done)
- Cost burn-down chart
- Log viewer per agent
- WebSocket-driven, no polling

Keep it read-only initially. The CLI remains the control interface. The dashboard is for monitoring.

Technology: keep it minimal. A single-page app served by the existing FastAPI server. Use SSE (server-sent events) over WebSocket to avoid connection management complexity. HTMX or Alpine.js over React to minimize frontend maintenance surface.

### Phase 3: GitHub App (enterprise distribution)

For teams and enterprises, ship Bernstein as a GitHub App. This is a separate distribution channel with its own onboarding:

1. Install Bernstein GitHub App on org
2. Add `bernstein.yaml` to repo (same format as local)
3. Create issues, label them `bernstein`
4. Agents run in Bernstein's cloud infra (or self-hosted runner)
5. PRs land with full provenance

This requires a hosted service or self-hosted runner infrastructure. It is a product expansion, not a feature addition.

### Option D (life seed): Shelved

File-watching with automatic re-planning is shelved. The risk/reward ratio is poor for the current stage. The rag_challenge experience showed that implicit state changes cause agent churn and wasted budget. Explicit commands beat implicit watchers.

If revisited later, the minimum viable version is: watch `bernstein.yaml` for changes, show a diff, prompt for confirmation before re-planning. Never auto-execute.

---

## Distribution Model

```
Phase 1:  pip install bernstein
          -> CLI + seed file
          -> Local only, single user

Phase 2:  pip install bernstein[dashboard]
          -> Adds web dashboard dependency
          -> Still local, but visually monitorable

Phase 3:  GitHub App + bernstein-cloud
          -> Team/enterprise distribution
          -> Hosted or self-hosted runner
          -> GitHub issues as input, PRs as output
```

The pip package remains the core. Dashboard is an optional extra. GitHub App is a separate product surface.

---

## Open Questions

1. **Should `bernstein.yaml` support multi-cell configs?** E.g., defining cells with sub-goals. Defer until single-cell is proven.
2. **Should the dashboard allow task creation / re-prioritization?** Starting read-only is safer. Adding write operations is Phase 2.5.
3. **Should `bernstein` without arguments look for `bernstein.yaml` AND `.bernstein/` directory?** Probably yes -- check for config file first, then fall back to interactive CLI prompt.
4. **Notification model**: Should Bernstein notify the user when done? (Desktop notification, terminal bell, Slack webhook?) Worth adding a `notify` config option in the YAML.

---

## Decision

**Adopt the layered approach: Phase 1 ships CLI + seed file. Phase 2 adds dashboard. Phase 3 adds GitHub App.**

The seed file (`bernstein.yaml`) is the signature UX innovation. It makes Bernstein feel like infrastructure ("declare what you want, it happens") rather than a tool you have to babysit. Combined with the CLI for control and the dashboard for visibility, this covers solo developers through small teams with minimal implementation cost.
