<div align="center">

<img src="https://img.shields.io/badge/🎼-Bernstein-black?style=for-the-badge&labelColor=1a1a2e" alt="Bernstein">

### Agent orchestration for code that writes itself

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-1305+-2ea44f)]()
[![License](https://img.shields.io/badge/license-PolyForm_NC-f89820)](LICENSE)

</div>

---

```bash
bernstein
```

Bernstein is a multi-agent orchestrator. You define a goal or a backlog of tasks. It assigns them to AI coding agents, verifies the output, and adapts its own configuration between runs. The scheduler is deterministic Python — no LLM tokens wasted on coordination.

Works with **Claude Code**, **Codex CLI**, **Gemini CLI**, and **Qwen**. Agents are short-lived: spawn, do the work, exit. No context drift, no sleeping processes.

## Quick start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e .
```

Option A — inline goal:

```bash
bernstein -g "Add JWT authentication with refresh tokens and tests"
```

Option B — seed file:

```yaml
# bernstein.yaml
goal: "Add JWT authentication with refresh tokens and tests"
cli: claude
```

```bash
bernstein                      # starts agents, shows live dashboard
bernstein --headless           # run without dashboard (overnight/CI)
bernstein --evolve             # continuous self-improvement mode
bernstein --evolve \
  --max-cycles 10 \
  --budget 5.00 \
  --interval 300               # evolve with limits
bernstein stop                 # graceful shutdown
```

Option C — put `.md` task files in `.sdd/backlog/open/` with YAML frontmatter. Bernstein loads them automatically on start.

## Commands

```
bernstein             Start from seed file, inline goal, or backlog
bernstein stop        Gracefully stop all agents and the task server
bernstein benchmark   Run the tiered golden benchmark suite
bernstein evolve      Manage self-evolution proposals
bernstein cost        Show agent spend: cost, tokens, and duration per model
```

### `bernstein evolve` subcommands

```
bernstein evolve review           List proposals pending human review
bernstein evolve approve <id>     Approve a specific proposal
bernstein evolve run              Run the autoresearch evolution loop
```

### `bernstein benchmark` subcommands

```
bernstein benchmark run                  Run all benchmark tiers
bernstein benchmark run --tier smoke     Smoke tier only
bernstein benchmark run --tier stretch   Stretch tier only
```

## Architecture

```
bernstein
    │
    ├── Task Server (HTTP :8052)     ← agents pull tasks, report completion
    ├── Orchestrator (Python)        ← deterministic scheduler, no LLM
    ├── Spawner                      ← launches CLI agents per task batch
    ├── Janitor                      ← verifies done tasks (tests pass? files exist?)
    └── Evolution                    ← adjusts prompts/config between runs
```

Agents are spawned fresh per task batch (1-3 tasks), work in the same repo, then exit. File ownership prevents concurrent edits to the same file. The orchestrator polls every 10s, spawns when capacity allows (max 6 agents default), and reaps stale processes.

## Adapters

| Adapter | CLI | Notes |
|---------|-----|-------|
| Claude Code | `claude` | Default. Full tool-use, file editing, tests. |
| Codex CLI | `codex` | OpenAI Codex — lightweight, fast. |
| Gemini CLI | `gemini` | Google Gemini models. |
| Qwen | `qwen` | Local-friendly, Alibaba Qwen models. |

Set `cli: <adapter>` in `bernstein.yaml`, or pass `--cli <adapter>` on the command line.

## Roles

Bernstein routes tasks to specialist agents based on the `role` field. The following roles are available:

| Role | Purpose |
|------|---------|
| `manager` | Plans work, decomposes goals, coordinates other roles |
| `backend` | Server-side code, APIs, data models |
| `frontend` | UI, components, browser-side logic |
| `qa` | Tests, coverage, regression prevention |
| `security` | Vulnerability analysis, hardening, auth |
| `devops` | CI/CD, deployment, infrastructure |
| `architect` | System design, ADRs, structural decisions |
| `docs` | Documentation, READMEs, changelogs |
| `ml-engineer` | Models, training pipelines, inference |
| `prompt-engineer` | Prompt design, LLM integration |
| `reviewer` | Code review, quality gates |
| `retrieval` | RAG, embeddings, vector search |
| `vp` | High-level strategy, cross-team decisions |

Tasks default to `backend` if no role is specified.

## Task server API

Any tool, agent, or framework can interact with Bernstein programmatically:

```bash
# Create a task
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Add rate limiting", "role": "backend", "priority": 1}'

# List tasks
curl http://127.0.0.1:8052/tasks?status=open

# Mark done
curl -X POST http://127.0.0.1:8052/tasks/{id}/complete \
  -d '{"result_summary": "Added rate limiter middleware"}'

# Dashboard
curl http://127.0.0.1:8052/status
```

This is the integration layer. Other agents, CI pipelines, Slack bots, or custom UIs can create tasks and read status without knowing anything about Bernstein internals.

## Self-evolution

After each run, Bernstein analyzes metrics and proposes configuration changes. The pipeline:

```
metrics → analysis → proposal → sandbox → gate → apply
```

1. **Metrics** — aggregate task completion rates, failure patterns, cost per role
2. **Analysis** — detect regressions, bottlenecks, and improvement opportunities
3. **Proposal** — generate a concrete change (prompt tweak, routing rule, batch size)
4. **Sandbox** — run the proposed change against a test suite in isolation
5. **Gate** — risk-stratified acceptance check; reject on test regression
6. **Apply** — write the change to config/templates if it passes

Changes are risk-stratified:

| Risk | Scope | Method |
|------|-------|--------|
| L0 | Routing, batch sizes, timeouts | Auto-apply |
| L1 | Prompts, role templates | Sandbox test first |
| L2 | Routing logic, strategies | PR for review |
| L3 | Python source | Blocked |

`InvariantsGuard` SHA-locks critical files on boot. `CircuitBreaker` halts evolution on test regression.

Continuous evolution mode:

```bash
bernstein --evolve                        # evolve indefinitely
bernstein --evolve --max-cycles 10        # stop after 10 cycles
bernstein --evolve --budget 5.00          # stop after $5 spent
bernstein evolve run --window 2h          # dedicated evolution session
```

## Project structure

```
src/bernstein/
├── adapters/      # CLI agent adapters (claude, codex, gemini, qwen)
├── cli/           # CLI entry points
├── core/          # orchestrator, server, spawner, janitor, evolution
├── evolution/     # metrics aggregation, proposal generation, safety gates
└── templates/     # role system prompts
.sdd/              # file-based runtime state (backlog, metrics, config)
```

## How it compares

|  | Bernstein | CrewAI | AutoGen | LangGraph |
|--|-----------|--------|---------|-----------|
| Scheduling | Deterministic code | LLM | LLM | Graph |
| Agent lifetime | Short (minutes) | Long-running | Long-running | Long-running |
| Verification | Built-in janitor | Manual | Manual | Manual |
| Self-evolution | Risk-gated (L0–L3) + continuous `--evolve` mode | No | No | No |
| Works with CLI agents | Yes | No | No | No |
| Multi-provider | Claude/Codex/Gemini/Qwen | API-only | API-only | API-only |

## Origin

Built during a 47-hour sprint where 12 AI agents ran on a single laptop, closing 737 tickets (15.7/hour) across 826 commits. The [full write-up](docs/rag-challenge-swarm-architecture.md) documents what worked and what failed. Every design decision here is a direct response to those findings.

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [open issues](https://github.com/chernistry/bernstein/issues).

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — Free for non-commercial use. Commercial licensing: [alex@alexchernysh.com](mailto:alex@alexchernysh.com)
