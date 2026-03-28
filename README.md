<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
  <img alt="Bernstein" src="docs/assets/logo-light.svg" width="340">
</picture>

<br>

### One command. Multiple AI agents. Your codebase moves forward while you sleep.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dashboard.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/dashboard.svg">
  <img alt="Bernstein Dashboard" src="docs/assets/dashboard.svg" width="700">
</picture>

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-2056-2ea44f)]()
[![License](https://img.shields.io/badge/license-PolyForm_NC-f89820)](LICENSE)

</div>

---

```bash
pipx install bernstein   # or: uv tool install bernstein
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

Bernstein takes a goal, breaks it into tasks, assigns them to AI coding agents running in parallel, verifies the output, and commits the results. You come back to working code, passing tests, and a clean git history.

**No framework to learn.** If you have [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), [Gemini CLI](https://github.com/google-gemini/gemini-cli), or [Qwen](https://github.com/QwenLM/Qwen-Agent) installed, Bernstein uses them. Agents spawn, work, exit. No context drift. No babysitting.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/architecture.svg">
  <img alt="Architecture" src="docs/assets/architecture.svg" width="650">
</picture>

The orchestrator is **deterministic Python** -- zero LLM tokens on coordination. A **janitor** verifies every result: tests pass, files exist, no regressions.

> [!TIP]
> Run `bernstein --headless` for CI pipelines and overnight runs. Add `--evolve` for continuous self-improvement.

## Quick start

```bash
bernstein -g "Add rate limiting and improve test coverage"  # inline goal
bernstein                                                    # from bernstein.yaml or backlog
bernstein --evolve --budget 5.00                             # self-improvement mode
```

<details>
<summary><strong>All CLI commands</strong></summary>

```bash
bernstein stop                     # graceful shutdown
bernstein cancel <task_id>         # cancel a task
bernstein cost                     # show cost summary
bernstein live                     # open live dashboard
bernstein init                     # initialize project
bernstein evolve review            # list evolution proposals
bernstein evolve approve <id>      # approve a proposal
bernstein benchmark run            # run golden benchmark suite
bernstein agents sync              # pull latest agent catalog
bernstein agents list              # list available agents
bernstein agents validate          # check catalog health
bernstein plan                     # show task backlog
bernstein logs                     # tail agent log output
bernstein demo                     # zero-to-running demo
bernstein ideate                   # run creative evolution pipeline
bernstein retro                    # generate retrospective report
```

</details>

## Agent catalogs

Hire specialist agents from [Agency](https://github.com/msitarzewski/agency-agents) (100+ agents, default) or plug in your own:

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

## Self-evolution

Leave it running. It gets better.

```bash
bernstein --evolve --max-cycles 10 --budget 5.00
```

Analyzes metrics, proposes changes to prompts and routing rules, sandboxes them, and auto-applies what passes. Critical files are SHA-locked. Circuit breaker halts on test regression. Risk-stratified: L0 auto-apply, L1 sandbox-first, L2 human review, L3 blocked.

<details>
<summary><strong>Supported agents</strong></summary>

| Agent | CLI flag | Notes |
|-------|----------|-------|
| Claude Code | `--cli claude` | Default. Full tool-use, file editing, tests. |
| Codex CLI | `--cli codex` | OpenAI Codex. |
| Gemini CLI | `--cli gemini` | Google Gemini. |
| Qwen | `--cli qwen` | Local-friendly, Alibaba Qwen. |

</details>

<details>
<summary><strong>Specialist roles</strong></summary>

`manager` `backend` `frontend` `qa` `security` `architect` `devops` `reviewer` `docs` `ml-engineer` `prompt-engineer` `retrieval` `vp` `analyst` `resolver` `visionary`

Tasks default to `backend` if no role is specified. The orchestrator checks agent catalogs for a specialized match before falling back to built-in roles.

</details>

<details>
<summary><strong>Task server API</strong></summary>

```bash
# Create a task
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Add rate limiting", "role": "backend", "priority": 1}'

# List / status
curl http://127.0.0.1:8052/tasks?status=open
curl http://127.0.0.1:8052/status
```

Any tool, CI pipeline, Slack bot, or custom UI can create tasks and read status.

</details>

<details>
<summary><strong>How it compares</strong></summary>

|  | Bernstein | CrewAI | AutoGen | LangGraph |
|--|-----------|--------|---------|-----------|
| Scheduling | Deterministic code | LLM-based | LLM-based | Graph |
| Agent lifetime | Short (minutes) | Long-running | Long-running | Long-running |
| Verification | Built-in janitor | Manual | Manual | Manual |
| Self-evolution | Yes (risk-gated) | No | No | No |
| CLI agents | Claude/Codex/Gemini/Qwen | API-only | API-only | API-only |
| Agent catalogs | Yes (Agency + custom) | No | No | No |

</details>

## Origin

Built during a 47-hour sprint: 12 AI agents on a single laptop, 737 tickets closed (15.7/hour), 826 commits. [Full write-up](docs/rag-challenge-swarm-architecture.md). Every design decision here is a direct response to those findings.

## Contributing

PRs welcome. [CONTRIBUTING.md](CONTRIBUTING.md) | [Issues](https://github.com/chernistry/bernstein/issues)

## License

[PolyForm Noncommercial 1.0.0](LICENSE) -- Free for non-commercial use. Commercial licensing: [alex@alexchernysh.com](mailto:alex@alexchernysh.com)
