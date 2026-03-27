<div align="center">

<img src="https://img.shields.io/badge/🎼-Bernstein-black?style=for-the-badge&labelColor=1a1a2e" alt="Bernstein">

### Agent orchestration for code that writes itself

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-1210+-2ea44f)]()
[![License](https://img.shields.io/badge/license-PolyForm_NC-f89820)](LICENSE)

</div>

---

```bash
bernstein
```

Bernstein is a multi-agent orchestrator. You define a goal or a backlog of tasks. It assigns them to AI coding agents, verifies the output, and adapts its own configuration between runs. The scheduler is deterministic Python — no LLM tokens wasted on coordination.

Works with **Claude Code**, **Codex CLI**, **Gemini CLI**, **Qwen**, and any CLI agent that takes a prompt and writes code. Agents are short-lived: spawn, do the work, exit. No context drift, no sleeping processes.

## Quick start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e .
```

Option A — drop a seed file:

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

Option B — put `.md` task files in `.sdd/backlog/open/` with YAML frontmatter. Bernstein loads them automatically on start.

## Commands

```
bernstein             Start from seed file or backlog
bernstein stop        Gracefully stop all agents and the task server
bernstein evolve      Manage self-evolution proposals
bernstein benchmark   Run the tiered golden benchmark suite
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

After each run, Bernstein analyzes metrics and proposes configuration changes. Changes are risk-stratified:

| Risk | Scope | Method |
|------|-------|--------|
| L0 | Routing, batch sizes, timeouts | Auto-apply |
| L1 | Prompts, role templates | Sandbox test first |
| L2 | Routing logic, strategies | PR for review |
| L3 | Python source | Blocked |

`InvariantsGuard` SHA-locks critical files on boot. `CircuitBreaker` halts evolution on test regression.

## Project structure

```
src/bernstein/
├── adapters/      # CLI agent adapters (claude, codex, gemini, qwen)
├── cli/           # CLI entry point
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
