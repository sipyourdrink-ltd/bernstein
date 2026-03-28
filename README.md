<div align="center">

<img src="https://img.shields.io/badge/🎼-Bernstein-black?style=for-the-badge&labelColor=1a1a2e" alt="Bernstein">

### One command. Multiple AI agents. Your codebase moves forward while you sleep.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-1777-2ea44f)]()
[![License](https://img.shields.io/badge/license-PolyForm_NC-f89820)](LICENSE)

</div>

---

```bash
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

Bernstein takes a goal, breaks it into tasks, assigns them to AI coding agents running in parallel, verifies the output, and commits the results. You come back to working code, passing tests, and a clean git history.

**No API keys to wire up. No framework to learn.** If you have Claude Code, Codex CLI, or Gemini CLI installed, Bernstein can use them. Agents are short-lived -- they spawn, do the work, and exit. No context drift. No runaway processes. No babysitting.

## Who it's for

- **Solo developers** who want to mass-parallelize feature work, bug fixes, and refactoring
- **Small teams** that need to ship faster without hiring
- **Open source maintainers** drowning in issues and PRs
- **Anyone who's thought** "I wish I could run 6 copies of myself on this codebase"

## What happens when you run it

```
$ bernstein -g "Add rate limiting, improve test coverage, fix auth bug"

  BERNSTEIN  Agent Orchestra                           12:34:05
 ┌─────────────────────────┬───────────────────────────┐
 │ AGENTS                  │ TASKS                     │
 │                         │                           │
 │ ◉ BACKEND  SONNET 2:14 │ ⚡ BACKEND  Add rate limit │
 │   → Add rate limiting   │ ⚡ QA       Cover auth mod │
 │   implementing middlew… │ ⚡ BACKEND  Fix auth bug   │
 │                         │ ✓ MANAGER  Plan decompose │
 │ ◉ QA  SONNET 1:45      │                           │
 │   → Improve test cover… │                           │
 │   writing test cases f… │                           │
 │                         │                           │
 │ ◉ BACKEND  SONNET 0:32 │                           │
 │   → Fix auth token bug  │                           │
 │   reading auth module…  │                           │
 ├─────────────────────────┴───────────────────────────┤
 │ ACTIVITY                                            │
 │ backend  Added RateLimiter middleware to app.py     │
 │ qa       test_auth_refresh passed (12 new tests)    │
 ├─────────────────────────────────────────────────────┤
 │ 3/4  ▐████████████████████░░░░░░░░░░░░░░░▌ 75%     │
 └─────────────────────────────────────────────────────┘
```

1. A **manager agent** reads your codebase and decomposes the goal into scoped tasks
2. **Specialist agents** (backend, QA, security, etc.) pick up tasks and work in parallel
3. A **janitor** verifies each result -- tests pass, files exist, no regressions
4. You get commits. Done.

## Quick start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e .
```

Three ways to give it work:

```bash
# Inline goal -- Bernstein plans and executes
bernstein -g "Add JWT authentication with refresh tokens and tests"

# Seed file -- for repeatable setups
cat > bernstein.yaml << 'EOF'
goal: "Refactor the payments module and add integration tests"
cli: claude
EOF
bernstein

# Backlog -- drop .md task files in .sdd/backlog/open/
# Bernstein picks them up automatically
bernstein
```

```bash
bernstein                          # live dashboard
bernstein --headless               # no UI (CI, overnight runs)
bernstein --evolve                 # continuous self-improvement mode
bernstein --evolve --max-cycles 10 # limit evolution cycles
bernstein --evolve --budget 5.00   # stop at $5 spent
bernstein stop                     # graceful shutdown
bernstein cancel <task_id>         # cancel a queued or running task
bernstein cost                     # show cost summary
bernstein live                     # open live dashboard
bernstein init                     # initialize project
bernstein evolve                   # manage self-evolution proposals
bernstein benchmark                # run golden benchmark suite
bernstein agents sync              # pull latest agent catalog
bernstein agents list              # list available agents
```

## Supports your existing tools

No vendor lock-in. Bernstein works with the CLI agents you already have installed:

| Agent | CLI flag | Notes |
|-------|----------|-------|
| Claude Code | `--cli claude` | Default. Full tool-use, file editing, tests. |
| Codex CLI | `--cli codex` | OpenAI Codex. |
| Gemini CLI | `--cli gemini` | Google Gemini. |
| Qwen | `--cli qwen` | Local-friendly, Alibaba Qwen. |

Mix and match per task using routing rules, or let the orchestrator pick based on task complexity.

## How it works

```
You define a goal
    │
    ▼
Manager agent decomposes it into tasks
    │
    ▼
Orchestrator assigns tasks to specialist agents
    │                    (deterministic Python -- no LLM tokens wasted)
    ▼
Agents work in parallel ──► Janitor verifies output
    │                              │
    ▼                              ▼
Commits to your repo          Failed? Re-queue with context
```

The orchestrator is **deterministic code**, not an LLM. It doesn't "think" about scheduling -- it routes tasks by role, manages file ownership to prevent conflicts, and enforces capacity limits. Zero tokens spent on coordination.

Agents are **short-lived** (1-3 tasks, then exit). This is by design: no context window bloat, no sleeping processes, no memory leaks. Fresh agent, fresh context, focused work.

## Specialist roles

| Role | What it does |
|------|-------------|
| `manager` | Decomposes goals, creates tasks, coordinates |
| `backend` | APIs, data models, business logic |
| `frontend` | UI, components, styling |
| `qa` | Tests, coverage, edge cases |
| `security` | Vulnerability analysis, hardening |
| `architect` | System design, refactoring |
| `devops` | CI/CD, infrastructure |
| `reviewer` | Code review, quality gates |
| `docs` | Documentation, READMEs, API references |
| `ml-engineer` | ML pipelines, model integration |
| `prompt-engineer` | Prompt design and optimization |
| `retrieval` | Search, RAG, vector stores |
| `vp` | High-level planning and prioritization |

## Self-evolution

Leave Bernstein running and it gets better at its job:

```bash
bernstein --evolve                   # continuous improvement
bernstein --evolve --max-cycles 10   # with limits
bernstein --evolve --budget 5.00     # stop at $5 spent
```

It analyzes completion rates, detects bottlenecks, and proposes changes to prompts, routing rules, and batch sizes. Changes go through a safety pipeline:

| Risk | What changes | How |
|------|-------------|-----|
| L0 | Timeouts, batch sizes | Auto-apply |
| L1 | Prompts, templates | Sandbox + tests first |
| L2 | Routing logic | PR for human review |
| L3 | Core Python | Blocked |

Critical files are SHA-locked on boot. A circuit breaker halts evolution on any test regression.

## Agent catalogs

Hire specialist agents from external catalogs. [Agency](https://github.com/msitarzewski/agency-agents) is the default -- 100+ pre-built agents across engineering, QA, security, DevOps, and more.

```yaml
# bernstein.yaml
catalogs:
  - name: agency
    type: agency
    source: https://github.com/msitarzewski/agency-agents
    priority: 100
  - name: my-team
    type: generic
    path: ./our-agents/
    priority: 50
```

```bash
bernstein agents sync       # pull latest catalog
bernstein agents list       # see available agents
```

The orchestrator checks catalogs for a specialized agent before falling back to built-in roles.

## Task server API

Bernstein exposes an HTTP API. Plug in CI pipelines, Slack bots, custom UIs, or other agents:

```bash
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Add rate limiting", "role": "backend", "priority": 1}'

curl http://127.0.0.1:8052/tasks?status=open
curl http://127.0.0.1:8052/status
```

## How it compares

|  | Bernstein | CrewAI | AutoGen | LangGraph |
|--|-----------|--------|---------|-----------|
| Scheduling | Deterministic code | LLM-based | LLM-based | Graph |
| Agent lifetime | Short (minutes) | Long-running | Long-running | Long-running |
| Verification | Built-in janitor | Manual | Manual | Manual |
| Self-evolution | Yes (risk-gated) | No | No | No |
| CLI agent support | Claude/Codex/Gemini/Qwen | API-only | API-only | API-only |
| Agent catalogs | Yes (Agency + custom) | No | No | No |
| Zero coordination tokens | Yes | No | No | No |

## Origin

Built during a 47-hour sprint where 12 AI agents ran on a single laptop, closing 737 tickets (15.7/hour) across 826 commits. The [full write-up](docs/rag-challenge-swarm-architecture.md) documents the findings. Every design decision is a direct response to what worked and what failed.

## Project structure

```
src/bernstein/
├── adapters/      # CLI agent adapters
├── agents/        # agent catalog, providers
├── cli/           # CLI and TUI dashboard
├── core/          # orchestrator, server, spawner, janitor
├── evolution/     # self-improvement pipeline
└── templates/     # role prompts
.sdd/              # file-based state (backlog, metrics, config)
```

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [open issues](https://github.com/chernistry/bernstein/issues).

## License

[PolyForm Noncommercial 1.0.0](LICENSE) -- Free for non-commercial use. Commercial licensing: [alex@alexchernysh.com](mailto:alex@alexchernysh.com)
