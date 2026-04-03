<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
  <img alt="Bernstein" src="docs/assets/logo-light.svg" width="340">
</picture>

### Declarative agent orchestration for engineering teams.
### One YAML. Multiple coding agents. Ship while you sleep.

[![CI](https://github.com/chernistry/bernstein/actions/workflows/ci.yml/badge.svg)](https://github.com/chernistry/bernstein/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/chernistry/bernstein/graph/badge.svg)](https://codecov.io/gh/chernistry/bernstein)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-1.0%2C%201.1-blue)](docs/compatibility.md)
[![A2A Compatible](https://img.shields.io/badge/A2A-0.2%2C%200.3-blue)](docs/compatibility.md)
[![Sponsor](https://img.shields.io/badge/sponsor-GitHub%20%2F%20OpenCollective-ff69b4?logo=github&logoColor=white)](https://github.com/sponsors/chernistry)

[Documentation](docs/GETTING_STARTED.md) · [Architecture](docs/ARCHITECTURE.md) · [Benchmarks](docs/BENCHMARKS.md) · [Changelog](docs/CHANGELOG.md)

</div>

---

If you're running one agent at a time, you're leaving performance on the table. Bernstein takes a goal, breaks it into tasks, assigns them to AI coding agents running in parallel, verifies the output, and commits the results. You come back to working code, passing tests, and a clean git history.

> **Think of it as what Kubernetes did for containers, but for AI coding agents.** You declare a goal. The control plane decomposes it into tasks. Short-lived agents execute them in isolated git worktrees — like pods. A janitor verifies the output before anything lands.

```bash
pip install bernstein                    # any platform
# or
brew tap chernistry/tap && brew install bernstein  # macOS / Linux
# or
pipx install bernstein                   # isolated install
# or
uv tool install bernstein                # fastest (Rust-based)

# Run:
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

**1.78× faster** than single-agent execution, verified on internal benchmarks. See [benchmarks](docs/BENCHMARKS.md) for methodology and reproduction steps.

## What it is

Bernstein is a deterministic orchestrator for CLI coding agents. It schedules tasks in parallel across any installed agent — Claude Code, Codex, Cursor, Gemini, Aider, and more — with git worktree isolation, janitor-verified output, and file-based state you can inspect, back up, and recover from. No vendor lock-in. No framework to learn. Your agents, your models, your backlog.

## 5-minute setup

```bash
# 1. Install (pick one)
pipx install bernstein

# 2. Init your project (creates .sdd/ workspace + bernstein.yaml)
cd your-project
bernstein init

# 3. Run — pass a goal inline or let bernstein.yaml guide the run
bernstein -g "Add rate limiting and improve test coverage"
```

That's it. Your agents spawn, work in parallel, verify their output, and exit. Watch progress in the terminal dashboard.

## Supported agents

Bernstein ships with adapters for 12 CLI agents. If you have any of these installed, Bernstein uses them — no API key plumbing required:

| Agent | Models | Install |
|-------|--------|---------|
| [Aider](https://aider.chat) | Any OpenAI/Anthropic-compatible model | `pip install aider-chat` |
| [Amp](https://ampcode.com) | opus 4.6, gpt-5.4 | `brew install amp` |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | opus 4.6, sonnet 4.6, haiku 4.5 | `npm install -g @anthropic-ai/claude-code` |
| [Codex CLI](https://github.com/openai/codex) | gpt-5.4, o3, o4-mini | `npm install -g @openai/codex` |
| [Cursor](https://www.cursor.com) | sonnet 4.6, opus 4.6, gpt-5.4 | [Cursor app](https://www.cursor.com) (sign in via app) |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | gemini-3-pro, 3-flash | `npm install -g @google/gemini-cli` |
| [Goose](https://block.github.io/goose/) | Any provider | Install Goose CLI |
| [Kilo](https://kilo.dev) | Configurable | `npm install -g kilo` |
| [Kiro](https://kiro.dev) | Multi-provider | Install Kiro CLI |
| [OpenCode](https://opencode.ai) | Multi-provider | Install OpenCode CLI |
| [Qwen](https://github.com/QwenLM/Qwen-Agent) | qwen3-coder, qwen-max | `npm install -g qwen-code` |
| [Roo Code](https://github.com/RooVetGit/Roo-Code) | opus 4.6, sonnet 4.6, gpt-4o | VS Code extension (headless CLI) |

Prefer a different agent? Bring your own — the `generic` adapter accepts any CLI tool with a `--prompt-flag` interface. Mix models in the same run: cheap free-tier agents for boilerplate, heavy models for architecture.

## Shipped features

Only capabilities that ship with v1.4.11. Full matrix at [FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md).

- **Deterministic scheduling** — zero LLM tokens on coordination. The orchestrator is plain Python.
- **Parallel execution** — spawn multiple agents across roles (backend, qa, docs, security) simultaneously.
- **Git worktree isolation** — every agent works in its own branch. Your main branch stays clean.
- **Janitor verification** — concrete signals (tests pass, files exist, no regressions) before anything lands.
- **Quality gates** — lint, type-check, PII scan, and mutation testing run automatically after completion.
- **Plan files** — multi-stage YAML with stages and steps, like Ansible playbooks (`bernstein run plan.yaml`).
- **Cost tracking** — per-model spend, tokens, and duration (`bernstein cost`).
- **Live dashboards** — terminal TUI (`bernstein live`) and browser UI (`bernstein dashboard`).
- **Self-evolution** — analyze metrics, propose improvements, sandbox-test, and auto-apply what passes (`--evolve`).
- **CI autofix** — parse failing CI logs, create fix tasks, route to the right agent (`bernstein ci fix <url>`).
- **Circuit breaker** — halt agents that repeatedly violate purpose or crash.
- **Token growth monitor** — detect runaway token consumption and intervene automatically.
- **Cross-model verification** — route completed task diffs to a different model for review.
- **Audit trail** — HMAC-chained tamper-evident logs with Merkle seal verification.
- **Pluggy plugin system** — hook into any lifecycle event.
- **Multi-repo workspaces** — orchestrate across multiple git repositories as one workspace.
- **Cluster mode** — central server + remote worker nodes for distributed execution.
- **MCP server mode** — run Bernstein as an MCP tool server for other agents.
- **12 agent adapters** — Claude, Codex, Cursor, Gemini, Aider, Amp, Roo Code, Kiro, Kilo, OpenCode, Qwen, Goose, plus a generic catch-all.

## Monitoring and diagnostics

```bash
bernstein live          # interactive TUI dashboard (3 columns)
bernstein dashboard     # open web dashboard in browser
bernstein status        # task summary and agent health
bernstein ps            # running agent processes
bernstein cost          # spend breakdown by model and task
bernstein doctor        # pre-flight: adapters, API keys, ports
bernstein recap         # post-run: tasks, pass/fail, cost
bernstein retro         # detailed retrospective report
bernstein trace <ID>    # step-by-step agent decision trace
bernstein logs -f       # tail live agent output
```

Agents appear in Activity Monitor / `ps` as `bernstein: <role> [<session>]` — no more hunting for mystery Python processes.

## Plan files

For multi-stage projects, define stages and steps in a YAML plan file:

```bash
bernstein run plan.yaml
```

The plan skips manager decomposition and goes straight to execution. See [`templates/plan.yaml`](templates/plan.yaml) for the format and [`examples/plans/flask-api.yaml`](examples/plans/flask-api.yaml) for a working example.

## Observability

Prometheus metrics at `/metrics` — wire up Grafana, set alerts, monitor cost. OTLP telemetry initialization supports distributed tracing.

## Extensibility

Pluggy-based plugin system. Hook into any lifecycle event:

```python
from bernstein.plugins import hookimpl

class SlackNotifier:
    @hookimpl
    def on_task_completed(self, task_id, role, result_summary):
        slack.post(f"#{role} finished {task_id}: {result_summary}")
```

## GitHub App integration

Install a GitHub App on your repository to automatically convert GitHub events into Bernstein tasks. Issues become backlog items, PR review comments become fix tasks, and pushes trigger QA verification.

```bash
bernstein github setup       # print setup instructions
bernstein github test-webhook  # verify configuration
```

## Comparisons

- [Bernstein vs. GitHub Agent HQ](docs/compare/bernstein-vs-github-agent-hq.md) — open-source alternative to GitHub's multi-agent system
- [Full comparison index](docs/compare/README.md) — Conductor, Crystal, Stoneforge, single-agent baseline, and more
- [Benchmark data](docs/BENCHMARKS.md) — 1.78× faster, 23% lower cost vs. single-agent baseline

## Origin

Built during a 47-hour sprint: 12 AI agents on a single laptop, 737 tickets closed (15.7/hour), 826 commits. [Full write-up](docs/blog/swe-bench-orchestration-thesis.md). Every design decision here is a direct response to those findings.

## Roadmap

Bernstein's roadmap is public. Near-term work focuses on adoption and the governance moat; longer-term work on enterprise standards and distribution.

### Shipped

| Area | What | Status |
|------|------|--------|
| **Governance** | Lifecycle governance kernel — guarded state transitions, typed events | Done |
| **Governance** | Governed workflow mode — deterministic phases, hashable definitions | Done |
| **Governance** | Model routing policy — provider allow/deny lists | Done |
| **Governance** | Immutable HMAC-chained audit log — tamper-evident, daily rotation | Done |
| **Governance** | Execution WAL — hash-chained write-ahead log, crash recovery, determinism fingerprinting | Done |
| **Adoption** | CI autofix pipeline — `bernstein ci fix <url>` and `bernstein ci watch` | Done |
| **Adoption** | Comparative benchmark suite — orchestrated vs. single-agent proof | Done |
| **Adoption** | Agent run manifest — hashable workflow spec for SOC2 evidence | Done |
| **Adoption** | `bernstein demo` — zero-config first-run experience | Done |
| **Adoption** | `bernstein doctor` — pre-flight health check | Done |

### Now (P1)

| Area | What | Target |
|------|------|--------|
| **Enterprise** | SSO/SAML/OIDC auth for multi-tenant deployments | H2 2026 |
| **Governance** | Time-based model policy constraints ("deny expensive providers during peak hours") | H2 2026 |
| **Adoption** | Verified SWE-Bench eval publication | In progress |

### Next (P2)

| Area | What | Target |
|------|------|--------|
| **Enterprise** | Dynamic policy hot-reload without restart | 2026 |
| **Adoption** | JetBrains IDE extension | 2026 |
| **Governance** | Task-specific model constraints ("role=security must use opus-only") | 2026 |

---

**Don't babysit agents.** Set a goal, walk away, come back to working code.

What will your agents build first?
