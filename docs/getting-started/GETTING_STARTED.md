# Getting Started with Bernstein

Bernstein orchestrates short-lived CLI coding agents around a central task server. One command starts the whole orchestra.

---

## Prerequisites

- **Python 3.12+** (macOS, Linux, Windows)
- **At least one CLI coding agent** installed and authenticated. Bernstein supports 37 adapters out of the box:

| Agent | Install |
|-------|---------|
| [Aider](https://aider.chat) | `pip install aider-chat` |
| [Amp](https://ampcode.com) | `brew install amp` |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm install -g @anthropic-ai/claude-code` |
| [Cloudflare Agents](https://developers.cloudflare.com/agents/) | `bernstein cloud login` |
| [Codex CLI](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| [Cody](https://sourcegraph.com/cody) | Install Cody CLI |
| [Continue](https://continue.dev) | VS Code / JetBrains extension |
| [Cursor](https://www.cursor.com) | [Cursor app](https://www.cursor.com) |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `npm install -g @google/gemini-cli` |
| [Goose](https://block.github.io/goose/) | Install Goose CLI |
| [Kilo](https://kilo.dev) | `npm install -g kilo` |
| [Kiro](https://kiro.dev) | Install Kiro CLI |
| [Ollama](https://ollama.com) | `brew install ollama` |
| [OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/) | `pip install 'bernstein[openai]'` |
| [OpenCode](https://opencode.ai) | Install OpenCode CLI |
| [Qwen](https://github.com/QwenLM/Qwen-Agent) | `npm install -g qwen-code` |
| Generic | Any CLI agent via `generic` adapter |
| IaC | Infrastructure-as-code adapter |
| [Droid](https://docs.factory.ai/) (Factory AI) | `curl -fsSL https://app.factory.ai/cli \| sh` |
| [GitHub Copilot](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli) | `npm install -g @github/copilot` |
| [Hermes](https://hermes-agent.nousresearch.com/docs/) (Nous Research) | `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \| bash` |
| [Crush](https://github.com/charmbracelet/crush) (Charm) | `npm install -g @charmland/crush` |
| [Auggie](https://docs.augmentcode.com/cli/overview) (Augment Code) | `npm install -g @augmentcode/auggie` |
| [Kimi](https://www.kimi.com/code/) (Moonshot) | `uv tool install kimi-cli` |
| [Rovo Dev](https://support.atlassian.com/rovo/) (Atlassian) | `acli rovodev auth login` |
| [Cline](https://docs.cline.bot/cline-cli/overview) | `npm install -g cline` |
| [Codebuff](https://www.codebuff.com/docs/help/quick-start) | `npm install -g codebuff` |
| [Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) | `npm install -g @mariozechner/pi-coding-agent` |
| [Mistral Vibe](https://github.com/mistralai/mistral-vibe) | `curl -LsSf https://mistral.ai/vibe/install.sh \| bash` |
| [Autohand](https://autohand.ai/code/) | `npm install -g autohand-cli` |
| [Forge](https://forgecode.dev/docs/) | `curl -fsSL https://forgecode.dev/cli \| sh` |
| [OpenHands](https://github.com/OpenHands/OpenHands) | `uv tool install openhands --python 3.12` |
| [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter) | `pip install open-interpreter` |
| [gptme](https://github.com/gptme/gptme) | `pipx install gptme` |
| [Plandex](https://github.com/plandex-ai/plandex) | `curl -sL https://plandex.ai/install.sh \| bash` |
| [AIChat](https://github.com/sigoden/aichat) | `cargo install aichat` |
| [Letta Code](https://github.com/letta-ai/letta-code) | `npm install -g @letta-ai/letta-code` |

No agent yet? Run `bernstein demo` for a zero-config walkthrough.

---

## Install

```bash
# Fastest (Rust-based)
uv tool install bernstein

# Or any of these
pip install bernstein
pipx install bernstein
brew tap chernistry/bernstein && brew install bernstein

# Fedora / RHEL
sudo dnf copr enable alexchernysh/bernstein
sudo dnf install bernstein

# npm wrapper (requires Python 3.12+)
npx bernstein-orchestrator

# Verify
bernstein --version
```

**Editor extensions:** search "Bernstein" in VS Code or Cursor, or run `code --install-extension alex-chernysh.bernstein`.

### Development install

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
uv run python scripts/run_tests.py -x
```

---

## Connect credentials (optional but recommended)

If you are using external providers (GitHub, OpenAI, Anthropic, etc.), store
their API keys in the OS keychain before initialising your workspace. This
keeps tokens out of `.env` files and shell history:

```bash
bernstein connect github             # OAuth flow for GitHub
bernstein connect openai             # prompts for API key, stores in keychain
bernstein creds list                 # confirm what is stored
```

Skip this step if you are using only local agents (e.g. Ollama).

---

## Initialise a workspace

Run this once inside your project directory:

```bash
cd my-project
bernstein init
```

This creates `.sdd/` — a lightweight, file-based state directory. Nothing hidden, nothing magical:

```
.sdd/
├── backlog/
│   ├── open/       # YAML task specs waiting to be claimed
│   ├── claimed/    # Tasks currently being worked by an agent
│   ├── done/       # Completed tasks (automated sync)
│   └── closed/     # Completed tasks (manual scripts)
├── runtime/
│   ├── pids/       # PID metadata JSON files (for bernstein ps)
│   ├── signals/    # Agent signal files: WAKEUP, SHUTDOWN, HEARTBEAT
│   └── logs/       # Agent and server runtime logs
├── metrics/
│   ├── tasks.jsonl     # Per-task timing and outcome data
│   ├── costs_*.json    # Cost tracking by model
│   └── quality_scores.jsonl  # Quality gate results
├── traces/         # Step-by-step agent decision traces
├── memory/         # Cross-session lessons and memory state
├── agents/         # Agent catalog (agency + custom sources)
├── caching/        # Prompt cache artifacts
└── config.yaml     # Server port, model defaults, worker limits
```

The `.sdd/` directory is your single source of truth. Back it up, inspect it, recover from it. No databases, no hidden state.

---

## First run — three paths

### Path 1: Inline goal (fastest)

Pass a plain-English goal directly on the command line:

```bash
bernstein -g "Build a legal RAG system with hybrid retrieval and typed answers"
```

Bernstein starts a manager agent, decomposes the goal into tasks, spawns worker agents in parallel, and verifies each result before marking it done.

### Path 2: Seed file (bernstein.yaml)

Pre-define goals, tasks, and role policies in a YAML file:

```bash
bernstein              # auto-discovers bernstein.yaml
```

```yaml
goal: "Build a legal RAG system with hybrid retrieval and typed answers"

tasks:
  - title: "Implement vector store"
    role: backend
    priority: 1
    scope: medium
    complexity: medium

  - title: "Add BM25 sparse index"
    role: backend
    priority: 2
    scope: small
    complexity: low
    depends_on: ["TSK-001"]
```

Full seed file reference at [`templates/bernstein.yaml`](https://github.com/chernistry/bernstein/blob/main/templates/bernstein.yaml).

### Path 3: Plan file (multi-stage projects)

For projects with known stages, write a plan file with stages and steps — like an Ansible playbook:

```bash
bernstein run plan.yaml
```

Plan files skip the manager decomposition step and go straight to execution. See [`templates/plan.yaml`](https://github.com/chernistry/bernstein/blob/main/templates/plan.yaml) for the format.

---

## Monitoring

### TUI dashboard

`bernstein` blocks in a live terminal dashboard by default. Attach to a running session:

```bash
bernstein live                      # attach to running session
bernstein live --classic            # legacy 3-column view
```

### Web dashboard

Open a real-time browser UI:

```bash
bernstein dashboard                 # opens http://localhost:8052/dashboard
```

### Process and task status

```bash
bernstein status                    # task counts by status and role
bernstein ps                        # table of running agents (PID, role, model)
bernstein cost                      # spend breakdown by model and task
bernstein plan                      # show task backlog as a table
bernstein plan --export             # export backlog to JSON
```

### Cost tracking

```bash
bernstein cost                      # human-readable spend summary
bernstein cost --json               # machine-readable JSON output
bernstein cost --share              # generate shareable cost report link
```

### Logs

```bash
bernstein logs                      # tail recent agent output
bernstein logs tail -f              # follow mode (like tail -f)
bernstein logs tail -a claude       # filter by agent name
```

---

## Stopping

```bash
bernstein stop                      # graceful shutdown (agents save work first)
bernstein stop --timeout 3          # shorter timeout (default: 10s)
bernstein stop --force              # hard kill (kill immediately)
bernstein checkpoint                # snapshot session progress for later resume
bernstein checkpoint --goal         # include current goal in snapshot
bernstein wrap-up                   # end session with structured summary and learnings
bernstein wrap-up --stop            # wrap-up and stop in one command
```

Graceful stop sends a `SHUTDOWN` signal via `.sdd/runtime/signals/`. Agents finish their current subtask, write their state, and exit. Use `--force` only when agents are stuck.

---

## Diagnostics

### Debug bundle

```bash
bernstein debug                     # generate a diagnostic zip for bug reports
```

Collects logs, config (secrets redacted), and runtime state into a zip file suitable for attaching to GitHub issues.

### Pre-flight health check

```bash
bernstein doctor                    # check: adapters, API keys, ports, .sdd/ integrity
bernstein doctor --fix              # auto-repair issues where possible
bernstein doctor --json             # machine-readable output
```

Doctor checks:
- Python version (must be 3.12+)
- Installed CLI agents and their login status
- Server port availability
- `.sdd/` directory structure and stale locks
- MCP server reachability
- Storage backend connectivity (if postgres/redis configured)

### Post-run summary

```bash
bernstein recap                     # tasks, pass/fail, cost, duration
bernstein recap --as-json           # JSON output for automation
```

### Retrospective

```bash
bernstein retro                     # full retrospective from all completed tasks
bernstein retro --since 24          # last 24 hours only
bernstein retro --print             # print to stdout and write file
bernstein retro -o custom-report.md # custom output path
```

### Decision traces

```bash
bernstein trace <task_id>           # step-by-step agent decision trace
bernstein trace <task_id> --as-json # JSON output
bernstein replay <run_id>           # deterministically re-run from a recorded trace
bernstein replay <run_id> --limit 5 # limit replay depth
```

### Git diff per task

```bash
bernstein diff <task_id>            # git diff for what an agent changed
bernstein diff <task_id> --stat     # diffstat summary only
```

---

## Common patterns

### Parallel agents by role

Assign different roles to different tasks. Bernstein fans out to specialists:

```yaml
tasks:
  - title: "Implement auth middleware"
    role: backend
    priority: 1
  - title: "Write integration tests"
    role: qa
    priority: 2
  - title: "Update API documentation"
    role: docs
    priority: 2
```

The manager decomposes and assigns backend, qa, and docs agents in parallel.

### Cost budgets

Cap spend to avoid surprise bills:

```bash
bernstein -g "Refactor the monolith" --budget 5.00   # stop after $5
bernstein --evolve --budget 2.00                      # evolve mode with $2 cap
```

When the budget hits, Bernstein stops spawning new agents and wraps up.

### Headless mode

Run without the live dashboard — useful for CI pipelines and overnight runs:

```bash
bernstein --headless -g "Refactor the auth module"
bernstein --headless   # auto-discovers bernstein.yaml
```

Output goes to `.sdd/runtime/` logs instead of the terminal.

### Dry-run mode

Preview the task plan without spawning agents or writing state:

```bash
bernstein --dry-run -g "Build a REST API with auth"
bernstein --dry-run   # preview plan from bernstein.yaml
```

Shows tasks, roles, priorities, and dependencies the manager would generate. Nothing written to disk.

### Plan mode (human approval before execution)

```bash
bernstein --plan-only               # generate plan, wait for approval
bernstein --from-plan saved_plan.yaml  # execute a previously saved plan
```

Tasks stay frozen until you approve them via `POST /plans/{id}/approve`.

### Multi-repo workspaces

Orchestrate across multiple git repositories:

```yaml
# bernstein.yaml
goal: "Build the microservices platform"
workspace:
  repos:
    - name: backend
      path: ./services/backend
    - name: frontend
      path: ./services/frontend
```

```bash
bernstein workspace         # show status of all repos
bernstein workspace clone   # clone missing repos
bernstein workspace validate # check workspace health
```

### Self-evolution

Bernstein can improve itself. Leave it running and it analyses its own metrics, proposes changes, sandboxes them, and auto-applies what passes:

```bash
bernstein --evolve                          # run indefinitely
bernstein --evolve --max-cycles 10          # stop after 10 cycles
bernstein --evolve --budget 5.00            # stop after $5 spent
bernstein --evolve --interval 120           # 2-minute cycles (default: 5 min)
bernstein --evolve --headless               # unattended overnight
```

Low-risk proposals (L0/L1) apply automatically. Higher-risk ones (L2+) save to `.sdd/evolution/deferred.jsonl` for human review:

```bash
bernstein evolve review       # list pending proposals
bernstein evolve approve <ID> # approve one
bernstein evolve run          # run the evolution loop manually
```

---

## Troubleshooting

### "No agents detected"

```bash
bernstein doctor                    # check which agents are installed
bernstein agents discover           # auto-detect installed CLI agents
```

Make sure at least one agent is installed and you've run its login/auth flow (e.g. `claude login`, `codex login`).

### Agents spawn but exit immediately

Check the agent logs:

```bash
bernstein logs tail -f              # follow all agent output
bernstein logs tail -a claude       # filter by agent
tail -f .sdd/runtime/logs/*.log     # raw log files
```

Common causes: missing API key, expired auth token, or the agent's CLI returned an error on the prompt.

### Tasks stuck in "claimed"

An agent likely crashed or was killed. Run janitor cleanup:

```bash
bernstein stop && bernstein         # restart with fresh state
bernstein doctor --fix              # clear stale locks
```

Tasks in `claimed/` that never completed will show up in the next `bernstein recap`.

### "Port 8052 already in use"

A previous Bernstein session is still running:

```bash
bernstein stop --force              # kill it
# Or find the PID:
cat .sdd/runtime/pids/server.json
kill $(cat .sdd/runtime/pids/server.json/pid)
```

### Cost is higher than expected

```bash
bernstein cost                      # see spend by model
bernstein cost --json | python -m json.tool  # detailed breakdown
```

Reduce cost by:
- Setting a budget: `--budget 5.00`
- Using cheaper models for simple tasks via `role_model_policy`
- Enabling plan mode to review tasks before spawning
- Mixing models: cheap agents for docs/tests, heavy models for architecture

### Quality gates failing

```bash
bernstein logs -f                   # see what the agent produced
bernstein diff <task_id>            # inspect the changes
```

Quality gates check lint, type-check, and tests. If your project has no tests, the test gate may pass trivially — or fail if the test runner can't find tests. Add tests to get real signal.

### SWE-Bench or benchmark numbers don't match

SWE-Bench results in `benchmarks/swe_bench/results/` are currently **mock preview artifacts** — not verified eval runs. To publish real numbers:

```bash
uv run python benchmarks/swe_bench/run.py eval --scenarios bernstein-sonnet --limit 50
```

The 1.78× speedup headline comes from the simulation harness in `benchmarks/run_benchmark.py` — it models scheduling, not real agent execution. Treat it as a capacity planning estimate.

---

## Learn more

- Project site: [bernstein.run](https://bernstein.run)
- Source: [github.com/chernistry/bernstein](https://github.com/chernistry/bernstein)
- PyPI: [pypi.org/project/bernstein](https://pypi.org/project/bernstein/)
- Author: [Alex Chernysh](https://alexchernysh.com) ([@chernistry](https://github.com/chernistry))
