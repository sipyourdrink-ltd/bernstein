# Getting Started with Bernstein

Bernstein orchestrates short-lived CLI coding agents (Claude Code, Codex, Cursor, Gemini CLI, etc.)
around a central task server.  One command starts the whole orchestra.

---

## Install

Requires Python 3.12+ and at least one AI coding agent (Claude Code, Codex, Cursor, or Gemini CLI).

```bash
# Recommended (fastest)
pip install bernstein

# macOS / Linux (Homebrew)
brew tap chernistry/tap && brew install bernstein

# Isolated install (pipx)
pipx install bernstein

# Rust-based installer (uv)
uv tool install bernstein

# Verify
bernstein --version
```

> **One-liner**: `pip install bernstein && bernstein -g "add tests"` — works on any platform with Python 3.12+.

### Development install

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
uv run python scripts/run_tests.py -x
```

---

## Quick start

### 1. Initialise a workspace

Run this once inside your project directory:

```bash
cd my-project
bernstein init
```

This creates `.sdd/` — a lightweight file-based state directory:

```
.sdd/
  backlog/open/      task files waiting to be claimed
  backlog/closed/    completed and failed task records
  agents/            per-agent state & heartbeat files
  runtime/           PID files and log files
  knowledge/         project notes injected as context
  decisions/         architecture decision records
  config.yaml        server port, model defaults, worker limits
```

### 2. Start orchestration

Pass a plain-English goal inline:

```bash
bernstein -g "Build a legal RAG system with hybrid retrieval and typed answers"
```

Or point at a YAML seed file (Bernstein looks for `bernstein.yaml` automatically):

```bash
bernstein
```

Bernstein will:
1. Start the task server on `localhost:8052`
2. Inject an initial manager task
3. Spawn a manager agent (Claude Code, Opus, max effort)
4. The manager decomposes the goal and spawns specialist workers automatically

### 3. Monitor progress

**Process visibility** — agents appear in Activity Monitor / `ps` as `bernstein: <role> [<session>]`:

```bash
bernstein ps                        # table of running agents
bernstein status                    # task summary + provider health/quota when available
```

**TUI dashboard** — `bernstein` blocks in a live terminal dashboard by default.
Press `Ctrl+C` to exit (agents keep running).

```bash
bernstein live                      # attach dashboard to running session
```

**Web dashboard** — real-time browser UI:

```bash
bernstein dashboard                 # opens http://localhost:8052/dashboard
```

**Prometheus metrics** — available at `/metrics` for Grafana/alerting.

**HTTP API** — query the task server directly:

```bash
curl http://127.0.0.1:8052/status   # dashboard summary
curl http://127.0.0.1:8052/tasks    # all tasks
curl http://127.0.0.1:8052/metrics  # Prometheus format
```

### 3b. Validate adapters and inspect cache

Before a long run, smoke-test the adapters you expect Bernstein to use:

```bash
bernstein test-adapter codex
bernstein test-adapter gemini
```

Response-cache entries are safe by default: only verified entries can auto-complete future tasks. Inspect or clear them with:

```bash
bernstein cache list
bernstein cache inspect TASK-123
bernstein cache clear --unverified --yes
```

### 4. Add a task manually

Inject a task while the server is running via the HTTP API:

```bash
curl -s -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add BM25 fallback to retriever",
    "role": "backend",
    "description": "Implement a BM25 sparse index alongside the dense vector index.",
    "priority": 1,
    "scope": "medium",
    "complexity": "medium"
  }'
```

### 5. Check logs

Log files are written to `.sdd/runtime/`:

```bash
tail -f .sdd/runtime/server.log
tail -f .sdd/runtime/spawner.log
```

### 6. Stop everything

```bash
bernstein stop
```

Sends a graceful shutdown signal to the task server, waits up to 10 s for
in-flight agents to finish, then kills the server and spawner processes.

```bash
# Shorter timeout if you need to stop quickly
bernstein stop --timeout 3
```

---

## Seed file format

`bernstein.yaml` lets you pre-define goals and initial tasks:

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

  - title: "Write integration tests"
    role: qa
    priority: 2
    scope: medium
    complexity: medium
    depends_on: ["TSK-001", "TSK-002"]

# Optional: pin specific roles to registered providers/models.
# Provider names must match your provider config (for example .sdd/config/providers.yaml).
role_model_policy:
  backend:
    provider: claude_standard
    model: sonnet
    effort: high
  reviewer:
    provider: codex_standard
    model: o3
    effort: high
  docs:
    provider: gemini_cheap
    model: gemini-2.5-flash
    effort: high
```

---

## Multi-repo workspaces

Bernstein can orchestrate work across multiple git repositories as a single
workspace.  Add a `workspace:` section to `bernstein.yaml`:

```yaml
goal: "Build the microservices platform"

workspace:
  repos:
    - name: backend
      path: ./services/backend
      url: git@github.com:org/backend.git
      branch: main
    - name: frontend
      path: ./services/frontend
      url: git@github.com:org/frontend.git
    - name: shared
      path: ./libs/shared-types
      url: git@github.com:org/shared-types.git
```

Each repo entry supports:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | yes | -- | Short identifier used in task routing |
| `path` | yes | -- | Relative or absolute path to the repo |
| `url` | no | `null` | Git clone URL (used by `workspace clone`) |
| `branch` | no | `main` | Default branch |

When a task includes `"repo": "backend"`, the spawner automatically sets the
agent's working directory to the backend repo path.

### Workspace CLI

```bash
# Show status of all repos (branch, clean/dirty, ahead/behind)
bernstein workspace

# Clone any repos that don't exist locally
bernstein workspace clone

# Validate that all repos exist and are valid git repos
bernstein workspace validate
```

### Workspace API

```bash
# Get workspace status via the task server
curl http://127.0.0.1:8052/workspace
```

### Task routing to repos

When creating a task, set the `repo` field to target a specific repository:

```bash
curl -s -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix API endpoint",
    "role": "backend",
    "description": "Fix the /users endpoint.",
    "repo": "backend"
  }'
```

The agent will be spawned with its working directory set to the backend repo.

---

## Configuration

`.sdd/config.yaml` (created by `bernstein init`):

| Key | Default | Description |
|-----|---------|-------------|
| `server_port` | `8052` | Task server port |
| `max_workers` | `6` | Max simultaneous worker agents |
| `default_model` | `sonnet` | Default model for workers |
| `default_effort` | `high` | Default effort level |

---

## Storage backends

By default Bernstein stores task state in memory with JSONL persistence.
For production or multi-node deployments, configure a PostgreSQL or
PostgreSQL + Redis backend.

### Configuration via `bernstein.yaml`

Add a `storage:` section to your seed file:

```yaml
# Default — in-memory with JSONL persistence (no external deps)
storage:
  backend: memory

# PostgreSQL — production-grade, single-node
storage:
  backend: postgres
  database_url: postgresql://user:pass@localhost/bernstein

# PostgreSQL + Redis distributed locking — multi-node deployments
storage:
  backend: redis
  database_url: postgresql://user:pass@localhost/bernstein
  redis_url: redis://localhost:6379
```

### Configuration via environment variables

| Variable | Default | Description |
|---|---|---|
| `BERNSTEIN_STORAGE_BACKEND` | `memory` | `memory`, `postgres`, or `redis` |
| `BERNSTEIN_DATABASE_URL` | — | PostgreSQL DSN (required for postgres/redis backends) |
| `BERNSTEIN_REDIS_URL` | — | Redis URL (required for redis backend) |

Environment variables override seed file settings.

### Checking connectivity

```bash
bernstein doctor
```

The `doctor` command checks storage backend connectivity when a non-memory
backend is configured.

---

## How agents are selected

| Task property | Model | Effort |
|--------------|-------|--------|
| `role=manager` | opus | max |
| `role=security` | opus | max |
| `scope=large, complexity=high` | opus | high |
| `complexity=medium` (any scope) | sonnet | high |
| `complexity=low` (any scope) | sonnet | normal |

---

## Task lifecycle

```
open → claimed → in_progress → done
                              ↘ failed
                 blocked (dependency not met)
```

When a task moves to `done`, the janitor checks completion signals
(file exists, test passes, etc.) before finalising.

---

## Headless mode

Run without the live dashboard — useful for overnight runs or CI pipelines:

```bash
bernstein --headless -g "Refactor the auth module"
bernstein --headless   # auto-discovers bernstein.yaml
```

Output is written to `.sdd/runtime/` logs instead of the terminal.

---

## Dry-run mode

Preview the task plan that Bernstein would create without actually spawning any
agents or writing state:

```bash
bernstein --dry-run -g "Build a REST API with auth"
bernstein --dry-run   # preview plan from bernstein.yaml
```

The output shows the tasks, roles, priorities, and dependencies that the manager
agent would generate.  Nothing is written to disk and no agents are spawned.
Useful for validating a goal or seed file before committing to a full run.

---

## Continuous self-improvement (evolve mode)

`--evolve` runs a continuous loop where Bernstein analyses its own metrics,
proposes improvements, validates them in a sandbox, and applies the safe ones
automatically.

```bash
# Run indefinitely
bernstein --evolve

# Stop after 10 cycles
bernstein --evolve --max-cycles 10

# Stop after spending $5
bernstein --evolve --budget 5

# Run cycles every 2 minutes instead of the default 5
bernstein --evolve --interval 120
```

| Flag | Default | Description |
|------|---------|-------------|
| `--evolve` / `-e` | — | Enable continuous self-improvement mode |
| `--max-cycles N` | `0` (unlimited) | Stop after N evolve cycles |
| `--budget N` | `0` (unlimited) | Stop after $N spent |
| `--interval N` | `300` | Seconds between cycles |

Combine `--headless` with `--evolve` for unattended overnight runs:

```bash
bernstein --evolve --max-cycles 20 --budget 10 --headless
```

Low-risk proposals (L0/L1) are applied automatically.  Higher-risk proposals
(L2+) are saved to `.sdd/evolution/deferred.jsonl` for human review — see
`bernstein evolve review` below.

---

## Managing evolution proposals

The `bernstein evolve` subcommand lets you inspect and approve pending
proposals.

### List proposals awaiting review

```bash
bernstein evolve review
```

### Approve a proposal

```bash
bernstein evolve approve <PROPOSAL_ID>
```

### Run the autoresearch loop manually

```bash
bernstein evolve run
```

---

## Zero-to-running demo

`bernstein demo` creates a temporary Flask starter project, seeds three backlog
tasks, and runs agents to complete them — all in one command.  Good for a first
look or to verify your adapter is wired up correctly.

```bash
# Run the full demo (auto-detects adapter)
bernstein demo

# Preview the plan without spawning any agents
bernstein demo --dry-run

# Use a specific adapter
bernstein demo --adapter codex

# Cap the run time at 60 seconds
bernstein demo --timeout 60
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | `false` | Show the demo plan without spawning any agents |
| `--adapter NAME` | auto-detect | CLI adapter to use (`claude`, `codex`, `cursor`, `gemini`, `qwen`) |
| `--timeout N` | `120` | Maximum seconds to wait for tasks to complete |

The demo creates a throwaway directory under `$TMPDIR`.  Nothing is written to
your current project.

---

## Creative evolution pipeline (ideate)

`bernstein ideate` runs a two-stage pipeline: a *visionary* stage that generates
bold feature proposals and an *analyst* stage that evaluates them.  Approved
proposals are converted into backlog tasks automatically.

Agent-driven generation of proposals and verdicts is a future feature; today
you supply pre-written JSON files.

```bash
# Convert pre-written proposals and verdicts into tasks
bernstein ideate --proposals ideas.json --verdicts evals.json

# Dry-run: show what would be created without writing tasks
bernstein ideate --proposals ideas.json --verdicts evals.json --dry-run

# Raise the approval bar (default 7.0)
bernstein ideate --proposals ideas.json --verdicts evals.json --threshold 8

# Run against a different project directory
bernstein ideate --proposals ideas.json --verdicts evals.json --dir ../other-project
```

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | `false` | Show proposals without creating backlog tasks |
| `--proposals FILE` | — | JSON file with pre-written visionary proposals |
| `--verdicts FILE` | — | JSON file with pre-written analyst verdicts |
| `--threshold N` | `7.0` | Minimum analyst score (0–10) to approve a proposal |
| `--dir DIR` | `.` | Project root directory (parent of `.sdd/`) |

See `templates/roles/visionary/` and `templates/roles/analyst/` for the expected
JSON formats.

---

## Retrospective report

`bernstein retro` reads completed and failed tasks from the archive and writes a
markdown retrospective report.

```bash
# Report on all recorded tasks
bernstein retro

# Last 24 hours only
bernstein retro --since 24

# Print to stdout as well as writing the file
bernstein retro --print

# Write to a custom file instead of .sdd/runtime/retrospective.md
bernstein retro -o report.md
```

| Option | Default | Description |
|--------|---------|-------------|
| `--since HOURS` | all time | Include only tasks completed in the last N hours |
| `-o / --output FILE` | `.sdd/runtime/retrospective.md` | Write report to FILE instead of the default path |
| `--print` | `false` | Print the report to stdout in addition to writing it |

---

## Agent spend

```bash
bernstein cost
```

Shows cost, tokens, and duration per model from `.sdd/metrics/`.

---

## Benchmark suite

Run the golden benchmark suite to measure Bernstein's capabilities:

```bash
# Run all tiers (smoke, capability, stretch)
bernstein benchmark run

# Run only the smoke tier (fast sanity check)
bernstein benchmark run --tier smoke

# Run only stretch benchmarks
bernstein benchmark run --tier stretch
```

Results are saved to `.sdd/benchmarks/YYYY-MM-DD.jsonl` by default.

| Tier | Purpose |
|------|---------|
| `smoke` | Fast sanity checks — should always pass |
| `capability` | Core feature validation |
| `stretch` | Aspirational targets, expected to fail until the system matures |

---

## Task API reference

The task server runs on `http://127.0.0.1:8052`.  All request and response bodies are JSON.

### POST /tasks — create a task

**Required fields**

| Field | Type | Description |
|-------|------|-------------|
| `title` | `string` | Short name shown in the dashboard |
| `description` | `string` | Full instructions given to the agent |
| `role` | `string` | Role tag used for agent selection (e.g. `backend`, `qa`, `security`, `manager`) |

**Optional fields**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `priority` | `int` | `2` | Lower number = higher priority; `1` is urgent, `3` is low |
| `scope` | `string` | `"medium"` | Size hint: `"small"`, `"medium"`, `"large"` |
| `complexity` | `string` | `"medium"` | Difficulty hint: `"low"`, `"medium"`, `"high"` — used by the router to pick model |
| `estimated_minutes` | `int` | `30` | Rough time budget; informational only |
| `depends_on` | `list[string]` | `[]` | Task IDs that must be `done` before this task becomes claimable |
| `owned_files` | `list[string]` | `[]` | File paths the agent is expected to own / modify |
| `cell_id` | `string \| null` | `null` | Optional cell grouping identifier |
| `repo` | `string \| null` | `null` | Target repo in a multi-repo workspace (spawns agent in that repo's directory) |
| `task_type` | `string` | `"standard"` | One of `"standard"`, `"upgrade"` |
| `model` | `string \| null` | `null` | Override model selection: `"opus"`, `"sonnet"`, `"haiku"` |
| `effort` | `string \| null` | `null` | Override effort selection: `"max"`, `"high"`, `"medium"`, `"low"` |
| `completion_signals` | `list[object]` | `[]` | Conditions the janitor checks before marking a task done (see below) |

**`completion_signals` object**

Each entry has exactly two fields:

| Field | Type | Description |
|-------|------|-------------|
| `type` | `string` | One of: `path_exists`, `glob_exists`, `test_passes`, `file_contains`, `llm_review`, `llm_judge` |
| `value` | `string` | The path, glob pattern, shell command, or prompt depending on `type` |

Example — require a file to exist and tests to pass before the task is finalised:

```json
{
  "title": "Implement vector store",
  "role": "backend",
  "description": "Write src/store.py with a FAISS-backed vector store.",
  "completion_signals": [
    {"type": "path_exists", "value": "src/store.py"},
    {"type": "test_passes", "value": "uv run pytest tests/test_store.py -x -q"}
  ]
}
```

---

### POST /tasks/{id}/complete — mark a task done

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `result_summary` | `string` | yes | Short summary of what was done; stored in the archive |

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "Implemented FAISS store in src/store.py, all tests pass."}'
```

---

### POST /tasks/{id}/fail — mark a task failed

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | yes | Why the task failed; stored in the archive |

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/fail \
  -H "Content-Type: application/json" \
  -d '{"reason": "FAISS not available in the sandbox environment."}'
```

---

### POST /tasks/{id}/cancel — cancel a task

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | `string` | yes | Why the task was cancelled; stored in the archive |

Cannot cancel a task that is already in a terminal state (`done`, `failed`, `cancelled`).

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/TASK_ID/cancel \
  -H "Content-Type: application/json" \
  -d '{"reason": "Requirements changed — no longer needed."}'
```

---

### Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tasks` | List all tasks; optional `?status=open\|in_progress\|done\|failed` filter |
| `GET` | `/tasks/next/{role}` | Claim the highest-priority open task for the given role |
| `POST` | `/tasks/{id}/claim` | Claim a specific task by ID |
| `POST` | `/tasks/claim-batch` | Claim multiple tasks by ID in one call |
| `GET` | `/status` | Dashboard summary (counts by status and role) |
| `GET` | `/bulletin` | Recent bulletin board messages from agents |

---

## CLI reference

Run `bernstein --help` for the full option list. For a feature-by-feature
documentation coverage status, see [`docs/FEATURE_MATRIX.md`](FEATURE_MATRIX.md).

### Core orchestration

| Command | Description |
|---------|-------------|
| `bernstein [-g GOAL] [--evolve] [--headless]` | Start orchestration; shows live TUI dashboard unless `--headless` |
| `bernstein --dry-run [-g GOAL]` | Preview the task plan without spawning any agents |
| `bernstein --plan-only` | Generate plan only, do not execute |
| `bernstein --from-plan FILE` | Execute a previously saved plan |
| `bernstein init` | Initialise a `.sdd/` workspace in the current directory |
| `bernstein stop [--timeout N] [--force]` | Gracefully stop all agents and the task server |
| `bernstein demo [--dry-run] [--real] [--adapter NAME]` | Zero-to-running demo (`--real` uses live agents instead of mocks) |
| `bernstein quickstart [--keep] [--adapter]` | Zero-config demo: 3 tasks on a Flask TODO API |

### Monitoring & observability

| Command | Description |
|---------|-------------|
| `bernstein status [--json]` | Task summary, active agents, cost estimate |
| `bernstein ps [--json-output]` | Show running agent processes (PID, role, model, runtime) |
| `bernstein doctor [--json] [--fix]` | Pre-flight health check; `--fix` auto-repairs issues |
| `bernstein live [--classic]` | Attach the live TUI dashboard to a running session |
| `bernstein dashboard [--port N]` | Open real-time web dashboard in browser |
| `bernstein cost [--json] [--share]` | Show agent spend: cost, tokens, and duration per model |
| `bernstein trace TASK_ID [--as-json]` | Show step-by-step agent decision trace |
| `bernstein replay RUN_ID [--limit N]` | Re-run from a recorded trace |
| `bernstein logs [-f] [-a AGENT] [-n LINES]` | Tail agent log output |
| `bernstein recap [--as-json]` | Post-run summary: tasks, pass/fail, cost |
| `bernstein diff TASK_ID [--stat]` | Git diff for what an agent changed for a task |
| `bernstein retro [--since HOURS] [-o FILE] [--print]` | Generate a retrospective report from task history |

### Task management

| Command | Description |
|---------|-------------|
| `bernstein plan [--export] [--status]` | Show task backlog as a table, or export to JSON |
| `bernstein add-task TITLE [-d DESC] [--role R]` | Inject a task into the running server |
| `bernstein cancel TASK_ID [-r REASON]` | Cancel a running or queued task |
| `bernstein list-tasks [--status-filter] [--role]` | List tasks with optional filters |
| `bernstein sync [--port N] [--dir D]` | Sync `.sdd/backlog/open/` with the task server |
| `bernstein review` | Trigger an immediate manager queue review |
| `bernstein approve TASK_ID` | Approve a pending task review |
| `bernstein reject TASK_ID` | Reject a pending task review |
| `bernstein pending` | List tasks waiting for approval review |

### Agent management

| Command | Description |
|---------|-------------|
| `bernstein agents sync [--force]` | Force-refresh all agent catalogs |
| `bernstein agents list [--source local\|agency\|all]` | List available agents from loaded catalogs |
| `bernstein agents validate` | Validate all agent catalog files and report issues |
| `bernstein agents showcase` | Show agent capabilities showcase |
| `bernstein agents match QUERY` | Find the best agent for a task description |
| `bernstein agents discover` | Discover CLI agents installed on the system |

### Evolution & ideation

| Command | Description |
|---------|-------------|
| `bernstein evolve run [--window N] [--max-proposals N]` | Run the autoresearch evolution loop |
| `bernstein evolve review` | List evolution proposals awaiting human review |
| `bernstein evolve approve PROPOSAL_ID` | Approve a specific evolution proposal |
| `bernstein evolve status` | Show evolution pipeline status |
| `bernstein evolve export` | Export evolution proposals |
| `bernstein ideate [--count N] [--focus AREA]` | Generate improvement ideas for the project |

### CI/CD & GitHub

| Command | Description |
|---------|-------------|
| `bernstein ci fix <run-url>` | Parse a failing CI run, create a fix task |
| `bernstein ci watch <repo> [--interval N]` | Continuous monitoring, auto-fix on failure |
| `bernstein github setup` | Configure GitHub App integration |
| `bernstein github test-webhook` | Verify webhook configuration |

**Built-in CI pipeline** — the repository ships a production-grade GitHub Actions pipeline:

- **Quality gates**: ruff lint + format, pyright type check, pytest with Codecov (85% project / 70% patch), spelling (`typos`), dead code (`vulture`), workflow lint (`actionlint`), wheel size check (<10MB)
- **Security**: CodeQL, Semgrep SAST, license compliance (`pilosus`), Dependabot alerts + auto-merge for patch/minor updates
- **AI review**: three-tier PR review — GitHub Models (zero-key), Gemini CLI (optional), Bernstein self-review (deep)
- **DX automation**: PR auto-labeling by file path, PR size warnings, stale issue cleanup, Release Drafter for changelogs
- **Notifications**: Telegram bot notifications on CI completion
- **Concurrency**: all workflows cancel in-progress runs on new pushes to the same ref

### Governance & audit

| Command | Description |
|---------|-------------|
| `bernstein audit show [--limit N]` | Show recent audit log events |
| `bernstein audit seal` | Create Merkle seal of audit log |
| `bernstein audit verify` | Verify Merkle proof integrity |
| `bernstein audit verify-hmac` | Validate HMAC chain integrity |
| `bernstein audit query [--event-type E] [--since S]` | Search audit log |
| `bernstein verify --wal-integrity` | Verify WAL hash chain |
| `bernstein verify --determinism` | Check execution fingerprint reproducibility |
| `bernstein verify --memory-audit` | Detect memory leaks in agent processes |
| `bernstein verify --formal` | Formal verification mode (experimental) |
| `bernstein manifest list` | List all run manifests |
| `bernstein manifest show <run-id>` | Display run manifest |
| `bernstein manifest diff <a> <b>` | Compare two run configurations |

### Benchmarks & evaluation

| Command | Description |
|---------|-------------|
| `bernstein benchmark run [--tier T]` | Run tiered golden benchmark suite |
| `bernstein benchmark compare` | Orchestrated vs. single-agent comparison |
| `bernstein benchmark swe-bench` | Run SWE-bench harness |
| `bernstein eval run` | Run evaluation suite with multiplicative scoring |
| `bernstein eval report` | Generate evaluation report |
| `bernstein eval failures` | Show evaluation failures |

### Advanced features

| Command | Description |
|---------|-------------|
| `bernstein chaos agent-kill\|rate-limit\|file-remove` | Fault injection for resilience testing |
| `bernstein chaos status` | Show chaos experiment status |
| `bernstein chaos slo` | Check SLO compliance under chaos |
| `bernstein gateway start [--upstream U]` | Start MCP gateway proxy |
| `bernstein gateway replay <run-id>` | Replay recorded MCP tool calls |
| `bernstein workflow validate FILE` | Validate a workflow YAML |
| `bernstein workflow list` | List workflow DSL files |
| `bernstein workflow show NAME` | Show workflow details |
| `bernstein mcp [--transport T] [--port N]` | Run Bernstein as an MCP tool server |
| `bernstein watch [DIR] [--glob G]` | Monitor directory, re-run on file changes |
| `bernstein listen [--dry-run]` | Voice command session (offline STT, experimental) |
| `bernstein checkpoint [--goal G]` | Snapshot session progress |
| `bernstein wrap-up [--stop]` | End session with a structured wrap-up brief |

### Scenario playbooks and rolling roadmaps

- Put reusable scenario recipes in `.bernstein/scenarios/**/*.yaml`.
- Put roadmap plans in `.sdd/roadmaps/open/*.yaml` with ordered `scenarios` and `wave_size`.
- On each orchestrator tick, Bernstein emits only the next wave of tickets into `.sdd/backlog/open/` and persists roadmap cursor state in `.sdd/runtime/roadmaps/`.
- This keeps large roadmap stubs as planning artifacts while execution still runs on concrete tickets.

### Configuration & workspace

| Command | Description |
|---------|-------------|
| `bernstein workspace` | Show multi-repo workspace status |
| `bernstein workspace clone` | Clone all missing repos |
| `bernstein workspace validate` | Check workspace health |
| `bernstein config set KEY VALUE` | Set a global config value |
| `bernstein config get KEY` | Show effective config value |
| `bernstein config list` | List all config keys and values |
| `bernstein config validate` | Validate project configuration |

### Utilities

| Command | Description |
|---------|-------------|
| `bernstein plugins` | List discovered plugins and their hooks |
| `bernstein install-hooks [--force]` | Install git hooks for automated checks |
| `bernstein completions [--shell S]` | Generate shell completion scripts |
| `bernstein self-update [--check] [--rollback]` | Upgrade Bernstein from PyPI |
| `bernstein worker --server URL` | Join a cluster as a worker node |
| `bernstein quarantine list` | List quarantined tasks |
| `bernstein quarantine clear` | Clear all quarantined tasks |
| `bernstein help-all` | Comprehensive help for all commands |

---

## Next steps

- See `docs/DESIGN.md` for the full architecture
- See `docs/FEATURE_MATRIX.md` for documentation coverage status
- See `docs/plugin-sdk.md` for the plugin SDK
- See `docs/compatibility.md` for MCP/A2A/ACP protocol support
- Add role templates in `templates/roles/` to customise agent prompts
- Review `.sdd/runtime/server.log` if anything behaves unexpectedly
