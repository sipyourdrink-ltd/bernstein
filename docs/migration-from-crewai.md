# Migration Guide: CrewAI → Bernstein

This guide walks you through migrating a CrewAI project to Bernstein. It covers
concept mapping, code-to-config conversions, and the practical steps to get your
first run working. Read it top-to-bottom the first time, then use the mapping
tables as a reference.

---

## Why teams migrate

CrewAI and Bernstein solve different problems. CrewAI orchestrates LLM agents in
Python code. Bernstein orchestrates CLI coding agents (Claude Code, Codex, Gemini
CLI, and others) with a deterministic Python control plane. If your goal is to
automate multi-step software development tasks — write code, run tests, create PRs
— Bernstein is a closer fit.

Key differences:

| Dimension | CrewAI | Bernstein |
|-----------|--------|-----------|
| Orchestrator | LLM manager agent | Deterministic Python scheduler |
| Agent type | LLM API calls inside Python | CLI agents (Claude Code, Codex, etc.) |
| State | In-process (lost on crash) | Files in `.sdd/` (survives restart) |
| Task parallelism | Controlled by manager LLM | Controlled by `max_agents` config |
| Cost of "thinking" | LLM tokens for every scheduling decision | Zero tokens for scheduling |
| Observability | Python logs | `.sdd/` files, REST API, TUI dashboard |
| Agent lifecycle | Persistent sessions | Short-lived (spawn per task batch, then exit) |

---

## Concept mapping

| CrewAI | Bernstein | Notes |
|--------|-----------|-------|
| `Agent(role=..., goal=..., backstory=...)` | Role template + CLI adapter | Bernstein separates *what* a role does (YAML/Markdown template) from *which agent executes it* (adapter: claude, codex, gemini…) |
| `Task(description=..., expected_output=...)` | Task file (`.sdd/backlog/open/<id>.yaml`) | Tasks are files, not Python objects. They survive crashes and can be retried. |
| `Crew(agents=[...], tasks=[...])` | Plan file (`plans/<name>.yaml`) | A plan describes stages and steps. The orchestrator executes it deterministically. |
| `Process.sequential` | Stage `depends_on` chain | Sequential order is expressed as dependency edges, not a process flag. |
| `Process.hierarchical` | Manager role + worker roles | A `manager` role decomposes high-level goals; workers execute the steps. |
| `@tool` decorated function | Quality gate or MCP server | Per-task validation runs as quality gates. External capabilities plug in via MCP. |
| `crew.kickoff()` | `bernstein run` | One command starts the orchestrator and spawns agents. |
| `Task.context=[task1]` | `depends_on: [task1-id]` | Dependency declared in the task file, not in Python. |
| Agent memory | `.sdd/` context files | Shared state is files on disk. Any agent can read them. |
| `CrewOutput` | Completed task file + metrics | Results land in `.sdd/backlog/done/` and `.sdd/metrics/`. |

---

## Architecture: before and after

### CrewAI: everything in Python

```python
from crewai import Agent, Task, Crew, Process

researcher = Agent(
    role='Senior Researcher',
    goal='Find and synthesize information on AI trends',
    backstory='Expert researcher with deep analytical skills',
    verbose=True,
)

writer = Agent(
    role='Content Writer',
    goal='Write clear, compelling articles',
    backstory='Experienced technical writer',
    verbose=True,
)

research_task = Task(
    description='Research the latest trends in multi-agent AI systems',
    expected_output='A 3-paragraph summary with key findings',
    agent=researcher,
)

write_task = Task(
    description='Write an article based on the research findings',
    expected_output='A 500-word article ready for publication',
    agent=writer,
    context=[research_task],
)

crew = Crew(
    agents=[researcher, writer],
    tasks=[research_task, write_task],
    process=Process.sequential,
    verbose=True,
)

result = crew.kickoff()
```

**Problems at scale**: The Python process must stay alive. If it crashes, all
state is lost. The manager LLM burns tokens on every scheduling decision. Long
sessions cause agent context drift.

### Bernstein: config files + deterministic scheduler

**Plan file** (`plans/ai-article.yaml`):
```yaml
id: ai-article
title: Research and write an AI trends article
stages:
  - id: research
    steps:
      - id: research-trends
        title: Research the latest trends in multi-agent AI systems
        role: docs
        goal: |
          Produce a 3-paragraph summary of key trends in multi-agent AI systems.
          Save findings to docs/research-notes.md.
        complexity: medium

  - id: writing
    depends_on: [research]
    steps:
      - id: write-article
        title: Write article from research notes
        role: docs
        goal: |
          Using docs/research-notes.md, write a 500-word article.
          Save to docs/ai-trends-article.md.
        complexity: medium
```

**Run it**:
```bash
bernstein run plans/ai-article.yaml
```

The orchestrator reads the plan, spawns a CLI agent for `research-trends`, waits
for it to complete, then spawns a CLI agent for `write-article`. No LLM tokens
spent on scheduling. If the process crashes mid-run, restart with:
```bash
bernstein run plans/ai-article.yaml  # resumes from last completed task
```

---

## Migration steps

### Step 1: Map your agents to roles

List every CrewAI `Agent` you use. Map each to a Bernstein role. Bernstein
ships with these built-in roles:

| CrewAI agent role | Bernstein role |
|-------------------|----------------|
| Developer / Engineer | `backend` or `frontend` |
| QA / Tester | `qa` |
| Architect / Tech Lead | `architect` |
| Security Reviewer | `security` |
| DevOps / SRE | `devops` |
| Technical Writer / Docs | `docs` |
| Researcher / Analyst | `analyst` |
| Manager / Planner | `manager` |

If you need a custom role, add a file to `templates/roles/<role-name>.md`:

```markdown
# Role: data-engineer

You are a data engineer. Your responsibilities:
- Design and implement data pipelines
- Write tests for all transforms
- Document schema changes

## Standards
- All pipelines must be idempotent
- Schema migrations require a rollback plan
```

Then reference it in tasks:
```yaml
role: data-engineer
```

### Step 2: Convert tasks to YAML files

Every CrewAI `Task` becomes a YAML file in `.sdd/backlog/open/` or a step in a
plan file.

**CrewAI task:**
```python
Task(
    description="Implement a rate-limiting middleware for the FastAPI app",
    expected_output="Working middleware with tests, added to app/middleware/",
    agent=backend_dev,
)
```

**Bernstein task file** (`.sdd/backlog/open/rate-limiting.yaml`):
```yaml
id: rate-limiting
title: Implement rate-limiting middleware
role: backend
priority: p1
complexity: medium
goal: |
  Implement rate-limiting middleware for the FastAPI app.
  - Add to app/middleware/rate_limiter.py
  - Write unit tests in tests/test_rate_limiter.py
  - Register middleware in app/main.py
  Expected: tests pass, no linting errors.
```

Or as a step in a plan:
```yaml
- id: rate-limiting
  title: Implement rate-limiting middleware
  role: backend
  priority: p1
  complexity: medium
  goal: |
    Implement rate-limiting middleware for the FastAPI app.
    Add to app/middleware/rate_limiter.py with tests.
```

**Key difference**: The `goal` field is what the CLI agent reads. Write it like
a ticket — specific, actionable, with clear acceptance criteria.

### Step 3: Replace Process.sequential with stage dependencies

**CrewAI sequential process:**
```python
crew = Crew(
    agents=[researcher, developer, qa],
    tasks=[research_task, implement_task, test_task],
    process=Process.sequential,
)
```

**Bernstein plan with stage dependencies:**
```yaml
stages:
  - id: research
    steps:
      - id: research-requirements
        role: analyst
        goal: "Document the feature requirements in docs/requirements.md"

  - id: implementation
    depends_on: [research]
    steps:
      - id: implement-feature
        role: backend
        goal: "Implement the feature described in docs/requirements.md"

  - id: testing
    depends_on: [implementation]
    steps:
      - id: write-tests
        role: qa
        goal: "Write integration tests for the feature. All tests must pass."
```

`depends_on` guarantees the stage doesn't start until all steps in the
referenced stages are marked done.

### Step 4: Replace Process.hierarchical with manager + worker pattern

**CrewAI hierarchical process:**
```python
crew = Crew(
    agents=[manager, dev1, dev2, qa],
    tasks=[complex_task],
    process=Process.hierarchical,
    manager_llm=claude_opus,
)
```

**Bernstein equivalent:** Let the `manager` role decompose the goal. Add a
high-level task:

```yaml
- id: plan-feature
  title: "Decompose the payment integration into tasks"
  role: manager
  goal: |
    Create a detailed implementation plan for payment processing.
    Break it into concrete tasks: schema migrations, API endpoints,
    webhook handling, tests. Write the plan to docs/payment-plan.md.
  complexity: high
```

Then create worker tasks that depend on it:
```yaml
- id: payment-schema
  title: "Create payment database schema"
  role: backend
  depends_on: [plan-feature]
  goal: "Implement the database schema from docs/payment-plan.md"

- id: payment-api
  title: "Implement payment API endpoints"
  role: backend
  depends_on: [payment-schema]
  goal: "Build the API endpoints described in docs/payment-plan.md"
```

Or use `bernstein run --goal "Integrate Stripe payments"` and let Bernstein's
planner decompose it automatically.

### Step 5: Replace @tool with quality gates or MCP

**CrewAI tool:**
```python
from crewai_tools import FileReadTool, SerperDevTool

@tool("Run test suite")
def run_tests(test_path: str) -> str:
    """Run pytest and return results."""
    result = subprocess.run(["pytest", test_path], capture_output=True)
    return result.stdout.decode()

agent = Agent(tools=[run_tests, FileReadTool(), SerperDevTool()])
```

**Bernstein approach — quality gates** for post-completion verification:
```yaml
# bernstein.yaml
quality_gates:
  lint: true
  type_check: true
  tests: true           # runs pytest automatically after every task
  security_scan: false
```

**Bernstein approach — MCP servers** for capabilities during execution:
```yaml
# bernstein.yaml
mcp:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  - name: brave-search
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: "${BRAVE_API_KEY}"
```

Quality gates verify output. MCP servers provide capabilities to the agent while
it works. The distinction matters: quality gates run after the agent finishes,
MCP servers are available during execution.

### Step 6: Configure bernstein.yaml

The `bernstein.yaml` file is the equivalent of the Python script that instantiates
your Crew. Put it in the root of your project:

```yaml
# bernstein.yaml
cli: auto          # auto-detect installed agents (claude, codex, gemini, etc.)
max_agents: 3      # run up to 3 agents in parallel

model_policy:
  default: sonnet    # default model for all roles
  roles:
    architect: opus  # escalate to opus for high-stakes decisions
    qa: haiku        # haiku is sufficient for test writing

quality_gates:
  lint: true
  tests: true
  type_check: true

budget:
  max_usd: 10.00   # stop if total cost exceeds $10
```

### Step 7: Run and verify

```bash
# From an existing CrewAI-style task file
bernstein run plans/my-workflow.yaml

# Or from a high-level goal (Bernstein plans it automatically)
bernstein run --goal "Refactor the authentication module to use JWTs"

# Monitor progress
bernstein status          # snapshot
bernstein dashboard       # live TUI
```

---

## Pattern conversions

### CrewAI: task with output file

```python
Task(
    description="Write a database schema for users",
    expected_output="schema.sql file in db/migrations/",
    output_file="db/migrations/001_users.sql",
    agent=developer,
)
```

**Bernstein:**
```yaml
id: users-schema
role: backend
goal: |
  Write a PostgreSQL schema migration for the users table.
  Create db/migrations/001_users.sql.
  Include: id (uuid), email (unique), created_at, updated_at.
verification:
  - type: file_exists
    path: db/migrations/001_users.sql
```

### CrewAI: multi-task with shared context

```python
task2 = Task(
    description="Write tests for the schema",
    context=[task1],  # receives task1's output
    agent=qa,
)
```

**Bernstein:**
```yaml
- id: test-schema
  role: qa
  depends_on: [users-schema]  # task2 starts after task1 completes
  goal: |
    Write tests for db/migrations/001_users.sql.
    Verify all constraints are tested.
```

Context flows through files. When `users-schema` completes, the QA agent reads
the actual file. No in-process object passing needed.

### CrewAI: parallel agents

```python
# CrewAI doesn't parallelize by default — you work around it
crew = Crew(agents=[dev1, dev2], tasks=[task1, task2])
```

**Bernstein:** Tasks without `depends_on` run in parallel automatically, up to
`max_agents`. No special configuration needed:

```yaml
stages:
  - id: parallel-work
    steps:
      - id: frontend-component   # no depends_on → runs in parallel
        role: frontend
        goal: "Build the user profile card component"

      - id: backend-endpoint     # no depends_on → runs in parallel
        role: backend
        goal: "Implement the /api/users/:id endpoint"

      - id: write-tests          # no depends_on → runs in parallel
        role: qa
        goal: "Write unit tests for the profile service"
```

All three tasks above start at the same time (subject to `max_agents`).

### CrewAI: conditional logic

```python
def route_task(output):
    if "error" in output.raw.lower():
        return "fix_task"
    return "review_task"

# CrewAI's router pattern
```

**Bernstein:** Quality gates handle the common case. If tests fail, the task is
marked failed and requeued automatically. For custom routing:

```yaml
# bernstein.yaml
quality_gates:
  tests: true          # blocks completion if tests fail
  on_gate_failure: requeue  # requeue for fix instead of abandoning
```

---

## Troubleshooting

### Tasks not being picked up

```bash
bernstein status
# Check that tasks show status: open
# Check that bernstein is running: bernstein status | grep orchestrator
```

If tasks are stuck in `open` state and no agent is claimed them, check:
1. `bernstein.yaml` exists in the project root
2. At least one CLI agent is installed (`bernstein agents`)
3. The agent is logged in (e.g., `claude /login` for Claude Code)

### Wrong agent selected

Force a specific adapter in `bernstein.yaml`:
```yaml
cli: claude  # instead of: auto
```

Or set per-task:
```yaml
adapter: codex  # override for this task only
```

### Task keeps failing quality gates

Check the gate output:
```bash
cat .sdd/backlog/failed/<task-id>.yaml
# Look at the verification_results field
```

Then check the specific gate logs:
```bash
ls .sdd/runtime/logs/
```

### Costs are higher than expected

```bash
bernstein status  # shows per-task cost summary
```

Reduce costs by routing lighter tasks to cheaper models:
```yaml
model_policy:
  roles:
    qa: haiku       # test writing doesn't need opus
    docs: haiku     # documentation tasks are low-stakes
```

---

## What to read next

- [Plans reference](plans.md) — full YAML schema for plan files
- [Configuration reference](CONFIG.md) — all bernstein.yaml options
- [Role templates](../templates/roles/) — built-in roles and how to extend them
- [Quality gates](ARCHITECTURE.md#quality-gates) — configuring verification
- [Plugin SDK](plugin-sdk.md) — extend Bernstein with custom hooks
