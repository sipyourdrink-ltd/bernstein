# Migration Guide: LangGraph → Bernstein

This guide walks you through migrating a LangGraph project to Bernstein. It
covers concept mapping, code-to-config conversions, and the practical steps to
get running. Read it top-to-bottom the first time; use the mapping tables as
a reference thereafter.

---

## Why teams migrate

LangGraph is designed for building stateful, multi-step LLM applications embedded
in Python code — chatbots, reasoning pipelines, retrieval systems. Bernstein
is designed for orchestrating CLI coding agents that write and modify real code.

If you're using LangGraph to coordinate agents that read files, write code, run
tests, and create PRs, Bernstein is a closer fit. The file-based state model,
worktree isolation, and janitor verification are built specifically for software
development workflows.

| Dimension | LangGraph | Bernstein |
|-----------|-----------|-----------|
| Primary use case | Stateful LLM app workflows | Multi-agent software development |
| Orchestration | Graph runtime (nodes + edges) | Deterministic Python scheduler |
| State | Checkpoint store (in-process or Redis) | Files in `.sdd/` (git-friendly) |
| Agent type | Functions / LLM API calls | CLI agents (Claude Code, Codex, Gemini…) |
| Parallelism | Configured at graph level | Configured via `max_agents` |
| Observability | LangSmith tracing | `.sdd/` files, REST API, TUI dashboard |
| Cost model | LLM tokens for every node | Zero tokens for scheduling |
| Resumability | Checkpoint-based | File-based (always resumable after crash) |

---

## Concept mapping

| LangGraph | Bernstein | Notes |
|-----------|-----------|-------|
| `StateGraph` | Plan file (`plans/<name>.yaml`) | The plan defines the workflow. The orchestrator is the runtime. |
| `Node` (graph node function) | Task step | A step is a unit of work assigned to a role. It runs as a short-lived CLI agent. |
| `Edge` (unconditional) | No dependency field → parallel execution | Steps without `depends_on` run in parallel. |
| `Edge` (from A to B) | `depends_on: [A]` | Sequential execution expressed as task dependencies. |
| `ConditionalEdge` | Quality gate failure routing | Quality gates block completion and route to fix tasks. |
| `State` (TypedDict) | Files in `.sdd/` | Shared state between steps is files on disk. |
| `Checkpoint` (persistence) | `.sdd/backlog/` + task server | State survives crashes. Restart picks up where it left off. |
| `ToolNode` | Quality gate or MCP server | Post-completion verification via gates; agent capabilities via MCP. |
| `graph.compile().invoke()` | `bernstein run plans/<name>.yaml` | One command to run a defined workflow. |
| `graph.compile().stream()` | `bernstein dashboard` | Live TUI shows agent status in real time. |
| `MemorySaver` | `.sdd/` (automatic) | No configuration needed — everything persists to files automatically. |
| `add_messages` reducer | Bulletin board (`POST /bulletin`) | Cross-agent communication via an append-only board, not in-memory reducers. |

---

## Architecture: before and after

### LangGraph: graph-defined workflow in Python

```python
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from typing import TypedDict, Annotated
import operator

class DevState(TypedDict):
    task: str
    plan: str
    code: str
    tests: str
    review: str
    passed: bool

def plan_node(state: DevState) -> DevState:
    """Use LLM to create implementation plan."""
    plan = llm.invoke(f"Create a plan for: {state['task']}")
    return {"plan": plan.content}

def code_node(state: DevState) -> DevState:
    """Use LLM to write code based on plan."""
    code = llm.invoke(f"Implement this plan: {state['plan']}")
    return {"code": code.content}

def test_node(state: DevState) -> DevState:
    """Use LLM to write tests."""
    tests = llm.invoke(f"Write tests for: {state['code']}")
    return {"tests": tests.content, "passed": True}

def review_node(state: DevState) -> DevState:
    """Use LLM to review the code."""
    review = llm.invoke(f"Review: {state['code']}")
    return {"review": review.content}

def should_fix(state: DevState) -> str:
    if not state["passed"]:
        return "fix"
    return "review"

builder = StateGraph(DevState)
builder.add_node("plan", plan_node)
builder.add_node("code", code_node)
builder.add_node("test", test_node)
builder.add_node("review", review_node)

builder.set_entry_point("plan")
builder.add_edge("plan", "code")
builder.add_edge("code", "test")
builder.add_conditional_edges("test", should_fix, {"fix": "code", "review": "review"})
builder.add_edge("review", END)

graph = builder.compile(checkpointer=MemorySaver())
result = graph.invoke({"task": "Add user authentication to the API"})
```

**Problems at scale**: Every node is an LLM API call — all reasoning, planning,
and code generation happens inside the graph. When code is written, it only
exists in the Python string `state['code']`; there's no actual file on disk until
you write it explicitly. Tests run against an LLM-generated string, not an actual
file system. Resuming after a crash requires the checkpoint store to be intact.

### Bernstein: config files + actual CLI agents writing real code

**Plan file** (`plans/auth-feature.yaml`):
```yaml
id: auth-feature
title: Add user authentication to the API
stages:
  - id: planning
    steps:
      - id: create-plan
        title: Create authentication implementation plan
        role: architect
        goal: |
          Design the JWT authentication system for this FastAPI app.
          Write the plan to docs/auth-design.md covering:
          - Token structure and signing
          - Login/logout endpoints
          - Middleware for protected routes
          - Test strategy
        complexity: medium

  - id: implementation
    depends_on: [planning]
    steps:
      - id: auth-models
        title: Create auth database models
        role: backend
        goal: |
          Implement the User model per docs/auth-design.md.
          Create app/models/user.py with SQLAlchemy model.
          Write tests in tests/test_models.py.
        complexity: medium

      - id: auth-endpoints
        title: Implement auth API endpoints
        role: backend
        depends_on: [auth-models]
        goal: |
          Implement /auth/login and /auth/logout endpoints per docs/auth-design.md.
          All tests must pass. Type hints required.
        complexity: high

  - id: quality
    depends_on: [implementation]
    steps:
      - id: security-review
        title: Security review of auth implementation
        role: security
        goal: |
          Review the auth implementation in app/models/user.py and app/routers/auth.py.
          Check for: SQL injection, token expiry, password hashing, secrets in code.
          Write findings to docs/security-review.md.
        complexity: medium
```

**Run it:**
```bash
bernstein run plans/auth-feature.yaml
bernstein dashboard  # live progress view
```

A real CLI agent (Claude Code, Codex, etc.) runs in an isolated git worktree.
It reads the codebase, writes actual files, runs the test suite. The janitor
verifies that tests pass before marking the task done. If the process crashes,
restart the same command — it resumes from the last completed task.

---

## Migration steps

### Step 1: Map graph nodes to task steps

Every LangGraph node becomes a Bernstein task step. The key conversion: instead
of a Python function that calls an LLM, you write a YAML step with a `goal`
field. A CLI agent reads the goal and does the work in a real git worktree.

**LangGraph node:**
```python
def generate_api_endpoint(state: State) -> State:
    code = llm.invoke(
        f"Write a FastAPI endpoint for: {state['spec']}\n"
        f"Requirements: {state['requirements']}"
    )
    return {"endpoint_code": code.content}
```

**Bernstein step:**
```yaml
- id: implement-api-endpoint
  title: Implement user profile API endpoint
  role: backend
  goal: |
    Implement GET /api/users/:id endpoint.
    - Read the spec in docs/api-spec.md
    - Add the route to app/routers/users.py
    - Write tests in tests/test_users.py
    - All tests must pass, no type errors
  complexity: medium
```

The difference: the LangGraph node holds code in a Python string. The Bernstein
step writes code to actual files that you can inspect, test, and commit.

### Step 2: Convert edges to dependencies

**Unconditional edges** (A must complete before B):

LangGraph:
```python
builder.add_edge("plan", "implement")
builder.add_edge("implement", "test")
```

Bernstein:
```yaml
steps:
  - id: plan
    role: architect
    goal: "..."

  - id: implement
    role: backend
    depends_on: [plan]      # waits for plan to complete
    goal: "..."

  - id: test
    role: qa
    depends_on: [implement]  # waits for implement to complete
    goal: "..."
```

**Parallel nodes** (no dependency between them):

LangGraph:
```python
builder.add_node("lint_code", lint_fn)
builder.add_node("check_types", types_fn)
builder.add_node("run_tests", tests_fn)
# These all fan out from a parent node
```

Bernstein — steps without `depends_on` run in parallel automatically:
```yaml
steps:
  - id: lint-code
    role: qa
    goal: "Run ruff on the codebase and fix all warnings."

  - id: check-types
    role: backend
    goal: "Run pyright in strict mode and fix all type errors."

  - id: run-tests
    role: qa
    goal: "Ensure all tests pass. Fix any failures found."
```

All three start at the same time (subject to `max_agents`).

### Step 3: Replace conditional edges with quality gates

LangGraph uses conditional edges to route to fix nodes when verification fails:

```python
def should_fix(state: State) -> str:
    """Route to 'fix' node if tests failed, otherwise to 'review'."""
    if state["tests_passed"]:
        return "review"
    return "fix"

builder.add_conditional_edges(
    "test",
    should_fix,
    {"fix": "implement", "review": "review"}
)
```

Bernstein handles this with quality gates. When a gate fails, the task is
requeued for a fix:

```yaml
# bernstein.yaml
quality_gates:
  tests: true           # task cannot complete if tests fail
  lint: true
  type_check: true
  on_gate_failure: requeue  # failed task goes back to open queue
```

The agent that claimed the task gets a new attempt. If it fails repeatedly, the
task is quarantined and you're notified.

For custom routing logic, use task verification signals:
```yaml
# Task file
verification:
  - type: tests_pass
    command: "uv run pytest tests/ -q"
  - type: file_exists
    path: "src/feature.py"
  - type: file_contains
    path: "src/feature.py"
    contains: "def process_"
```

### Step 4: Replace State with `.sdd/` files

LangGraph's `State` TypedDict holds the workflow's shared data in memory:

```python
class PipelineState(TypedDict):
    spec: str
    plan: str
    code: dict[str, str]  # filename → content
    test_results: str
    review_notes: str
```

In Bernstein, state lives in files. Agents read and write files directly. Pass
context between steps by having earlier steps write to known file paths:

```yaml
- id: write-spec
  role: analyst
  goal: |
    Write the feature specification to docs/spec.md.
    Include: problem statement, API contract, acceptance criteria.

- id: implement
  role: backend
  depends_on: [write-spec]
  goal: |
    Read docs/spec.md and implement the feature.
    Write code to src/feature.py.
```

The `implement` step reads `docs/spec.md` — no in-memory passing required.
Files survive process restarts, are inspectable with any text editor, and live
in git history.

For truly ephemeral coordination (agent-to-agent messages mid-run), use the
bulletin board:
```bash
# From within a task's goal description, agents can reference the bulletin API:
# POST http://127.0.0.1:8052/bulletin
# GET  http://127.0.0.1:8052/bulletin?since=<timestamp>
```

### Step 5: Replace MemorySaver with nothing

LangGraph requires explicit checkpoint configuration:

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

# In-process memory (lost on restart)
checkpointer = MemorySaver()

# Persistent SQLite
checkpointer = SqliteSaver.from_conn_string(":memory:")

graph = builder.compile(checkpointer=checkpointer)
```

Bernstein persists state automatically. There is no checkpoint API to configure.
Everything lands in `.sdd/`:

```
.sdd/
├── backlog/
│   ├── open/          ← tasks waiting to run
│   ├── claimed/       ← tasks currently running
│   ├── done/          ← completed tasks with results
│   └── failed/        ← failed tasks with error details
├── runtime/
│   ├── tasks.jsonl    ← recovery checkpoint
│   └── logs/          ← per-agent logs
└── metrics/
    ├── tasks.jsonl    ← per-task timing, cost, token usage
    └── agents.jsonl   ← per-agent session metrics
```

If the process crashes, restart `bernstein run` — it reads `.sdd/` and resumes.

### Step 6: Replace ToolNode with quality gates or MCP

LangGraph's `ToolNode` wraps tools available to agents during graph execution:

```python
from langgraph.prebuilt import ToolNode

tools = [
    TavilySearchResults(max_results=3),
    read_file_tool,
    write_file_tool,
    run_pytest_tool,
]

tool_node = ToolNode(tools)
builder.add_node("tools", tool_node)
builder.add_edge("agent", "tools")
```

In Bernstein, tools split into two categories:

**Capabilities during execution → MCP servers** (available to the CLI agent
while it's working):
```yaml
# bernstein.yaml
mcp:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  - name: search
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: "${BRAVE_API_KEY}"
```

**Verification after completion → quality gates** (run after the agent finishes,
before the task is marked done):
```yaml
# bernstein.yaml
quality_gates:
  tests: true           # pytest must pass
  lint: true            # ruff must pass
  type_check: true      # pyright must pass
```

### Step 7: Write bernstein.yaml

```yaml
# bernstein.yaml
cli: auto              # auto-detect claude, codex, gemini, etc.
max_agents: 4          # parallel agents

model_policy:
  default: sonnet
  roles:
    architect: opus     # complex design decisions
    security: opus      # security reviews deserve careful attention
    qa: haiku           # test writing is mechanical

quality_gates:
  lint: true
  type_check: true
  tests: true

budget:
  max_usd: 20.00
```

### Step 8: Run your first plan

```bash
# Run a defined plan
bernstein run plans/your-workflow.yaml

# Or let Bernstein plan from a high-level goal
bernstein run --goal "Refactor the database layer to use async SQLAlchemy"

# Watch live progress
bernstein dashboard

# Check task status
bernstein status
```

---

## Pattern conversions

### LangGraph: fan-out / fan-in (parallel → aggregate)

```python
# LangGraph fan-out to parallel nodes, then aggregate
builder.add_node("fetch_data", fetch_fn)
builder.add_node("analyze_a", analyze_a_fn)
builder.add_node("analyze_b", analyze_b_fn)
builder.add_node("aggregate", aggregate_fn)

builder.add_edge("fetch_data", "analyze_a")
builder.add_edge("fetch_data", "analyze_b")
builder.add_edge("analyze_a", "aggregate")
builder.add_edge("analyze_b", "aggregate")
```

**Bernstein plan using stages:**
```yaml
stages:
  - id: fetch
    steps:
      - id: fetch-data
        role: backend
        goal: "Fetch and save the dataset to data/raw.csv"

  - id: analyze
    depends_on: [fetch]   # both analysis tasks start after fetch completes
    steps:
      - id: analyze-performance
        role: analyst
        goal: "Analyze performance metrics in data/raw.csv. Write findings to data/perf-analysis.md"

      - id: analyze-errors
        role: analyst
        goal: "Analyze error patterns in data/raw.csv. Write findings to data/error-analysis.md"

  - id: aggregate
    depends_on: [analyze]  # waits for both analysis tasks
    steps:
      - id: combine-findings
        role: docs
        goal: "Read data/perf-analysis.md and data/error-analysis.md. Write combined report to docs/report.md"
```

### LangGraph: subgraph (nested workflow)

```python
# LangGraph subgraph
sub_builder = StateGraph(SubState)
# ... define sub nodes ...
sub_graph = sub_builder.compile()

main_builder.add_node("run_sub_workflow", sub_graph)
```

**Bernstein nested stages:**
```yaml
stages:
  - id: main-work
    steps:
      - id: primary-task
        role: backend
        goal: "..."

  - id: sub-workflow
    depends_on: [main-work]
    steps:
      - id: sub-task-1
        role: qa
        goal: "..."
      - id: sub-task-2
        role: security
        goal: "..."
        depends_on: [sub-task-1]

  - id: finalize
    depends_on: [sub-workflow]
    steps:
      - id: merge-results
        role: architect
        goal: "..."
```

### LangGraph: human-in-the-loop (interrupt)

```python
from langgraph.types import interrupt

def review_node(state: State) -> State:
    human_feedback = interrupt({"code": state["code"]})
    return {"approved": human_feedback["approved"]}

graph = builder.compile(interrupt_before=["review_node"])
```

**Bernstein approval gates:**
```yaml
# bernstein.yaml
approval:
  enabled: true
  require_before:
    - merge        # require human approval before merging any PR
  slack_webhook: "${SLACK_WEBHOOK_URL}"  # optional notification
```

Or mark specific tasks as requiring approval:
```yaml
- id: deploy-to-production
  role: devops
  requires_approval: true   # pauses until a human approves
  goal: "Deploy the release to production using the deploy script."
```

---

## Troubleshooting

### Graph state is empty / context not flowing

In LangGraph, state is passed between nodes automatically. In Bernstein, state
flows via files. If a downstream task can't find context, check that the upstream
task wrote the expected file:

```bash
# Check what the upstream task produced
cat .sdd/backlog/done/<upstream-task-id>.yaml
ls -la <expected-output-path>
```

If the file doesn't exist, the upstream task's `goal` field may not have been
specific enough about where to write output.

### Conditional routing not working as expected

Bernstein doesn't have conditional edges — the router is deterministic. If you
need conditional behavior:

1. Use quality gates to automatically fail/retry tasks that don't meet criteria.
2. Use task `verification` blocks for custom pass/fail conditions.
3. For complex branching, use a `manager` role task to decompose the next steps
   based on what completed tasks produced.

### Checkpoint not resuming

Bernstein always resumes automatically. If it appears not to:

```bash
bernstein status          # see current task states
ls .sdd/backlog/open/     # confirm open tasks exist
ls .sdd/backlog/claimed/  # confirm no tasks are stuck claimed
```

Stuck claimed tasks (agent died without completing): they time out and return to
open after the heartbeat deadline passes.

### Performance is slower than LangGraph

LangGraph runs everything in-process. Bernstein spawns CLI agent processes.
Each spawn takes 5-30 seconds depending on the agent. To optimize:

1. Increase `max_agents` to run more tasks in parallel.
2. Group related tasks in the same stage so they batch together.
3. For very lightweight tasks, consider using `complexity: trivial` to route
   to a faster model.

---

## What to read next

- [Plans reference](plans.md) — full YAML schema for plan files
- [Configuration reference](CONFIG.md) — all bernstein.yaml options
- [Architecture](ARCHITECTURE.md) — how the orchestrator works internally
- [Plugin SDK](plugin-sdk.md) — extend Bernstein with custom hooks
- [Quality gates](ARCHITECTURE.md#quality-gates) — configuring verification
