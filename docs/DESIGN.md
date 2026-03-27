# Bernstein — Design Document

## Problem

AI coding agents (Claude Code, Cursor, etc.) are powerful individually but hard to orchestrate as a team. Key issues from our 13-day competition sprint with 12 agents:

1. **Agent sleep** — after finishing a task, agents stop picking up new work
2. **Context loss** — long-running agents lose track of project state
3. **Coordination overhead** — 58% of commits were coordination, not code
4. **No automatic verification** — agents self-report "done" without proof
5. **Model mismatch** — simple tasks wasted expensive Opus tokens

## Solution: Short-lived, auto-spawned agents

Instead of keeping agents alive and hoping they stay productive, Bernstein:
- Spawns a fresh Claude Code session per task (or small batch of 1-3 tasks)
- Each session gets a focused system prompt, the task description, and relevant context
- Agent works, commits, reports result, exits
- Task server detects idle queues and spawns next agent automatically
- State lives in files (`.sdd/`), not in agent memory

## Architecture

```
bernstein CLI
    │
    ▼
Task Server (FastAPI, localhost:8052)
    │
    ├── GET  /next/{role}        → returns next task for role
    ├── POST /complete/{task_id} → marks done, triggers janitor
    ├── POST /task               → add new task (from manager or human)
    ├── GET  /status             → dashboard data
    └── POST /spawn              → request new agent (server spawns via CLI)
    │
    ▼
Agent Spawner
    │
    ├── Reads task metadata (scope, complexity, estimated_effort)
    ├── Selects model: Opus (complex/review) vs Sonnet (implementation)
    ├── Selects effort: max (architecture) vs high (coding) vs normal (docs)
    ├── Renders system prompt from role template + task details
    ├── Launches: claude --model X --effort Y -p "prompt" --dangerously-skip-permissions
    └── Monitors: heartbeat timeout → kill + respawn
```

## Task metadata schema

```yaml
id: "PROJ-042"
title: "Implement hybrid retrieval with BM25 fallback"
role: "retrieval"           # which specialist
priority: 1                 # 1=critical, 2=normal, 3=nice-to-have
scope: "medium"             # small/medium/large → affects model choice
complexity: "high"          # low/medium/high → affects effort level
estimated_minutes: 30       # time budget
depends_on: ["PROJ-040"]    # task dependencies
completion_signals:         # janitor auto-close criteria
  - type: "path_exists"
    path: "src/retrieval/bm25.py"
  - type: "test_passes"
    command: "pytest tests/test_bm25.py"
files:                      # file ownership (prevents conflicts)
  - "src/retrieval/bm25.py"
  - "tests/test_bm25.py"
```

## Model routing

| Task property | Model | Effort | Reasoning |
|--------------|-------|--------|-----------|
| scope=small, complexity=low | Sonnet | normal | Quick fixes, docs, formatting |
| scope=medium, complexity=medium | Sonnet | high | Feature implementation, tests |
| scope=large, complexity=high | Opus | high | Architecture, complex logic |
| role=manager | Opus | max | Planning, decomposition, review |
| role=qa, scope=any | Sonnet | high | Testing, validation |
| role=security | Opus | max | Security review needs deep reasoning |

## Role templates

Each role is a directory in `templates/roles/{role}/`:
```
templates/roles/backend/
├── system_prompt.md    # Role description, rules, style
├── task_prompt.md      # Per-task prompt template with {{TASK_TITLE}}, {{TASK_DESCRIPTION}}, {{FILES}}
└── config.yaml         # Default model, effort, max_tasks_per_session
```

## Scaling model

### Single cell (1 project)
```
1 manager (Opus, max) → plans, reviews, quality gates
3-6 workers (Sonnet/Opus) → implement, test, document
```

### Multi-cell (large project)
```
1 VP (Opus, max) → overall architecture, cross-cell coordination
N cells, each:
  1 manager (Opus) → cell-level planning
  3-4 workers → implementation
```

Cells communicate via shared bulletin board (`.sdd/agents/BULLETIN.jsonl`).

## Implementation plan

### Phase 1: Core (MVP)
- [ ] Task server with spawn endpoint
- [ ] Agent spawner (model routing, prompt rendering)
- [ ] CLI: `bernstein start`, `bernstein status`, `bernstein add-task`
- [ ] Role templates: manager, backend, qa
- [ ] Heartbeat monitoring + auto-respawn

### Phase 2: Intelligence
- [ ] Manager agent that decomposes goals into tasks
- [ ] Janitor signals for automatic task verification
- [ ] Budget tracking (tokens, time, cost)
- [ ] Run reports with retrospectives

### Phase 3: Scale
- [ ] Multi-cell coordination
- [ ] Git worktree isolation per agent
- [ ] Bulletin board for cross-agent communication
- [ ] Dashboard UI

## Key lessons from rag_challenge

1. **Pull > Push** — agents requesting tasks beats assigning tasks
2. **Short-lived > Long-lived** — fresh context beats accumulated drift
3. **File state > Agent memory** — survives crashes, human-readable
4. **Roles > Generic** — specialized prompts dramatically outperform "do everything"
5. **Verify > Trust** — janitor signals catch incomplete work
6. **Proxy metrics lie** — always validate against the real evaluation

## CLI Adapter System

Bernstein is CLI-agnostic. Each coding agent CLI gets an adapter:

```
src/bernstein/adapters/
├── base.py       # CLIAdapter ABC: spawn(), is_alive(), kill()
├── claude.py     # Claude Code CLI
├── codex.py      # OpenAI Codex CLI
├── gemini.py     # Gemini CLI
├── kiro.py       # AWS Kiro
└── qwen.py       # Qwen CLI agent
```

Each adapter knows how to:
1. Format the prompt for that CLI's syntax
2. Pass model/effort parameters
3. Launch and monitor the process
4. Read output logs

A special `adapter-writer` agent role can study a new CLI's docs and generate the adapter code automatically.

## Self-Evolving Development

Bernstein develops itself:

```bash
# Bootstrap: human starts manager manually
claude --model opus -p "Read CLAUDE.md and docs/DESIGN.md. You are the manager.
Plan implementation of Phase 1 (task server + spawner + CLI).
Create tasks in .sdd/backlog/open/. Then use bernstein to spawn workers."
```

Once the task server is implemented, the manager can spawn workers through it. The system bootstraps from manual Claude invocation to self-orchestrated development.

## Cell Scaling

```
Small project (1 cell):
  Manager (Opus) → 3-5 Workers (Sonnet/Opus)
  Total: 4-6 agents, ~$5-15/hour

Medium project (2 cells):
  VP (Opus) → 2 Managers (Opus) → 6-10 Workers
  Total: 9-13 agents, ~$15-40/hour

Large project (N cells, enterprise):
  VP (Opus) → N Managers → N×5 Workers
  Each cell owns a subsystem (auth, API, ML, frontend, etc.)
  Inter-cell coordination via shared bulletin board
  Total: N×6 agents, scales linearly
```

Maximum per cell: 8 agents (1 manager + 7 workers). Beyond that, spawn a new cell.
Sweet spot: 5-6 per cell (1 manager + 4-5 workers).

The VP role only activates when >1 cell exists. For single-cell projects, the manager IS the top-level coordinator.

## Cost awareness

Every task tracks token usage. The manager sees cumulative cost and can:
- Switch expensive agents to cheaper models mid-sprint
- Defer low-priority tasks when budget is tight
- Report cost-per-task for retrospective analysis

---

# Self-Evolution Feedback Loop Architecture

## Overview

The self-evolution system enables Bernstein to monitor its own performance, identify improvement areas, and trigger automatic upgrades. This creates a closed-loop system that continuously improves without human intervention.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    SELF-EVOLUTION FEEDBACK LOOP                         │
└─────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
    │   METRICS    │────▶│   ANALYSIS   │────▶│   UPGRADE    │
    │  COLLECTION  │     │   ENGINE     │     │   DECISION   │
    └──────────────┘     └──────────────┘     └──────────────┘
           ▲                                         │
           │                                         ▼
           │                              ┌──────────────┐
           │                              │  EXECUTION   │
           │                              │   ENGINE     │
           │                              └──────────────┘
           │                                         │
           └─────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  STATE STORE    │
                    │  (.sdd/metrics) │
                    └─────────────────┘
```

## Component Details

### 1. Metrics Collection

The metrics collector gathers data from multiple sources:

```
┌─────────────────────────────────────────────────────────────┐
│                    METRICS COLLECTION                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   TASK      │  │   AGENT     │  │   COST      │         │
│  │   METRICS   │  │   METRICS   │  │   METRICS   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│         │                │                │                 │
│         ▼                ▼                ▼                 │
│  ┌─────────────────────────────────────────────────┐       │
│  │              METRICS AGGREGATOR                  │       │
│  └─────────────────────────────────────────────────┘       │
│                         │                                   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────┐       │
│  │           TIME-SERIES STORAGE (.sdd)            │       │
│  └─────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**Task Metrics:**
- `task_duration_seconds`: Time from spawn to completion
- `task_success_rate`: Percentage of tasks passing janitor verification
- `task_rework_rate`: Percentage requiring fix tasks
- `task_token_usage`: Total tokens consumed per task
- `task_cost_usd`: Dollar cost per task
- `files_modified`: Number of files changed
- `lines_added_deleted`: Code churn metrics

**Agent Metrics:**
- `agent_lifetime_seconds`: Session duration
- `agent_tasks_completed`: Tasks per session
- `agent_heartbeat_failures`: Times heartbeat was missed
- `agent_sleep_incidents`: Times agent stopped responding
- `agent_context_tokens`: Context window utilization

**Cost Metrics:**
- `cost_per_provider`: USD spent per LLM provider
- `cost_per_role`: USD spent per agent role
- `cost_per_task`: Average cost per completed task
- `free_tier_utilization`: Percentage of requests using free tiers
- `budget_remaining`: Remaining budget for billing period

**Quality Metrics:**
- `janitor_pass_rate`: First-pass verification success
- `human_approval_rate`: Percentage accepted without human review
- `rollback_rate`: Percentage of changes later reverted
- `test_pass_rate`: Automated test success rate

### 2. Analysis Engine

The analysis engine processes metrics to identify patterns and improvement opportunities:

```
┌─────────────────────────────────────────────────────────────┐
│                     ANALYSIS ENGINE                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────┐    ┌─────────────────┐                │
│  │  TREND DETECTOR │    │  ANOMALY DETECTOR│                │
│  │  (7-day trends) │    │  (outliers)      │                │
│  └─────────────────┘    └─────────────────┘                │
│           │                     │                           │
│           ▼                     ▼                           │
│  ┌─────────────────────────────────────────┐               │
│  │         ROOT CAUSE ANALYZER             │               │
│  │  - Correlation analysis                 │               │
│  │  - Bottleneck identification            │               │
│  │  - Cost driver analysis                 │               │
│  └─────────────────────────────────────────┘               │
│                         │                                   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────┐               │
│  │        IMPROVEMENT OPPORTUNITIES        │               │
│  │  - Model routing optimization           │               │
│  │  - Provider switching recommendations   │               │
│  │  - Policy adjustment suggestions        │               │
│  │  - Role template improvements           │               │
│  └─────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

**Analysis Algorithms:**

1. **Trend Detection**
   - Rolling average comparison (current vs 7-day baseline)
   - Linear regression for cost/performance trends
   - Change-point detection for sudden shifts

2. **Anomaly Detection**
   - Z-score based outlier detection (threshold: |z| > 2.5)
   - Isolation forest for multi-variate anomalies
   - Threshold-based alerts (e.g., cost spike > 50%)

3. **Correlation Analysis**
   - Pearson correlation between metrics
   - Identifies relationships (e.g., model choice → success rate)
   - Surfaces hidden dependencies

4. **Bottleneck Identification**
   - Queue depth analysis per role
   - Agent utilization rates
   - Task completion rate by complexity

### 3. Upgrade Decision Logic

The upgrade decision engine determines when and how to improve the system:

```
┌─────────────────────────────────────────────────────────────┐
│                  UPGRADE DECISION LOGIC                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────────────────────┐           │
│  │           TRIGGER CONDITIONS                  │           │
│  │  - Cost threshold exceeded                    │           │
│  │  - Success rate below target                  │           │
│  │  - Performance degradation detected           │           │
│  │  - New provider available                     │           │
│  │  - Scheduled review period                    │           │
│  └──────────────────────────────────────────────┘           │
│                         │                                    │
│                         ▼                                    │
│  ┌──────────────────────────────────────────────┐           │
│  │           UPGRADE CATEGORIES                  │           │
│  │                                               │           │
│  │  ┌─────────────┐  ┌─────────────┐            │           │
│  │  │   POLICY    │  │   ROUTING   │            │           │
│  │  │   UPDATE    │  │   RULES     │            │           │
│  │  └─────────────┘  └─────────────┘            │           │
│  │                                               │           │
│  │  ┌─────────────┐  ┌─────────────┐            │           │
│  │  │   MODEL     │  │   ROLE      │            │           │
│  │  │   ROUTING   │  │   TEMPLATES │            │           │
│  │  └─────────────┘  └─────────────┘            │           │
│  └──────────────────────────────────────────────┘           │
│                         │                                    │
│                         ▼                                    │
│  ┌──────────────────────────────────────────────┐           │
│  │           DECISION CRITERIA                   │           │
│  │  - Expected improvement > threshold           │           │
│  │  - Risk level acceptable                      │           │
│  │  - Cost of change < expected savings          │           │
│  │  - No conflicting upgrades pending            │           │
│  └──────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────┘
```

**Trigger Conditions:**

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Cost spike | >50% increase in 24h | Immediate review |
| Success rate drop | <80% for 10+ tasks | Model routing adjustment |
| Free tier available | New provider detected | Policy update |
| Budget threshold | >80% of monthly budget | Cost optimization |
| Scheduled review | Weekly/Monthly | Full system analysis |

**Upgrade Categories:**

1. **Policy Updates** (Low Risk)
   - Adjust provider switching thresholds
   - Modify batch sizes
   - Update rate limit configurations

2. **Routing Rules** (Medium Risk)
   - Change model selection criteria
   - Add/remove provider preferences
   - Adjust effort level mappings

3. **Model Routing** (Medium Risk)
   - Switch default models for roles
   - Update complexity thresholds
   - Add new model providers

4. **Role Templates** (High Risk)
   - Update system prompts
   - Modify task prompt templates
   - Change role configurations

### 4. Execution Engine

The execution engine applies upgrades safely:

```
┌─────────────────────────────────────────────────────────────┐
│                    EXECUTION ENGINE                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │  VALIDATE   │───▶│   APPLY     │───▶│   VERIFY    │     │
│  │   CHANGE    │    │   CHANGE    │    │   CHANGE    │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│         │                  │                  │              │
│         ▼                  ▼                  ▼              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │   ROLLBACK  │◀───│   MONITOR   │◀───│   ALERT     │     │
│  │   IF NEEDED │    │   RESULTS   │    │   IF FAIL   │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

**Execution Flow:**

1. **Validation**
   - Syntax check for YAML/JSON policy changes
   - Dry-run simulation for routing changes
   - Backward compatibility verification

2. **Application**
   - Atomic file writes with rollback capability
   - Version control integration (git commit per change)
   - Notification to running agents

3. **Verification**
   - Immediate metric check (did things improve?)
   - A/B comparison with baseline
   - Rollback trigger if degradation detected

4. **Monitoring**
   - Watch key metrics for 24h post-change
   - Alert on unexpected side effects
   - Log all changes for audit trail

## Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│                      DATA FLOW                               │
└─────────────────────────────────────────────────────────────┘

Task Completion
       │
       ▼
┌──────────────┐
│  Janitor     │───▶ Pass/Fail + Metrics
└──────────────┘
       │
       ▼
┌──────────────┐
│  Metrics     │───▶ Append to .sdd/metrics/tasks.jsonl
│  Collector   │
└──────────────┘
       │
       ▼
┌──────────────┐
│  Analysis    │───▶ Run every N tasks or T minutes
│  Scheduler   │
└──────────────┘
       │
       ▼
┌──────────────┐
│  Analysis    │───▶ Identify patterns
│  Engine      │
└──────────────┘
       │
       ▼
┌──────────────┐
│  Upgrade     │───▶ Decide on changes
│  Decision    │
└──────────────┘
       │
       ▼
┌──────────────┐
│  Execution   │───▶ Apply + Verify
│  Engine      │
└──────────────┘
       │
       ▼
┌──────────────┐
│  Git Commit  │───▶ Track changes
│  + Notify    │
└──────────────┘
```

## State Storage

All state lives in `.sdd/` directory:

```
.sdd/
├── metrics/
│   ├── tasks.jsonl          # Per-task metrics (append-only)
│   ├── agents.jsonl         # Per-agent session metrics
│   ├── costs.jsonl          # Cost tracking per provider
│   └── quality.jsonl        # Quality metrics (janitor, tests)
├── analysis/
│   ├── trends.json          # 7-day rolling trends
│   ├── anomalies.json       # Detected anomalies
│   └── opportunities.json   # Improvement suggestions
├── upgrades/
│   ├── pending.json         # Upgrades awaiting approval
│   ├── applied.json         # Recently applied upgrades
│   └── history.jsonl        # Full upgrade history
└── config/
    ├── policies.yaml        # Active policies
    ├── routing.yaml         # Model routing rules
    └── providers.yaml       # Provider configurations
```

## Metrics Schema

**Task Metrics Record:**
```json
{
  "timestamp": "2026-03-22T10:30:00Z",
  "task_id": "PROJ-042",
  "role": "backend",
  "model": "sonnet",
  "provider": "openrouter",
  "duration_seconds": 180,
  "tokens_prompt": 2500,
  "tokens_completion": 1200,
  "cost_usd": 0.0045,
  "janitor_passed": true,
  "files_modified": 3,
  "lines_added": 45,
  "lines_deleted": 12
}
```

**Provider Cost Record:**
```json
{
  "timestamp": "2026-03-22T10:30:00Z",
  "provider": "openrouter",
  "model": "sonnet",
  "tier": "paid",
  "tokens_in": 2500,
  "tokens_out": 1200,
  "cost_usd": 0.0045,
  "rate_limit_remaining": 950,
  "free_tier_remaining": 0
}
```

## Upgrade Approval Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| Auto | Apply immediately | Low-risk policy tweaks |
| Human | Require approval | High-risk template changes |
| Hybrid | Auto if confidence >90%, else human | Most upgrades |

---

# API Tier Optimization Router

## Overview

The router intelligently distributes LLM work across multiple providers based on cost, rate limits, task complexity, and free tier availability. It maximizes free tier usage while maintaining quality and reliability.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    API TIER OPTIMIZATION ROUTER                  │
└─────────────────────────────────────────────────────────────────┘

                         ┌──────────────┐
                         │   TASK       │
                         │   REQUEST    │
                         └──────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│                    ROUTING DECISION                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   COST      │  │   RATE      │  │   TASK      │         │
│  │   OPTIMIZER │  │   LIMIT     │  │   COMPLEXITY│         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│         │                │                │                 │
│         ▼                ▼                ▼                 │
│  ┌─────────────────────────────────────────────────┐       │
│  │              PROVIDER SCORER                     │       │
│  │  - Calculate score for each available provider  │       │
│  │  - Consider cost, speed, quality, availability  │       │
│  └─────────────────────────────────────────────────┘       │
│                         │                                   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────┐       │
│  │           PROVIDER SELECTION                    │       │
│  │  - Primary provider (highest score)             │       │
│  │  - Fallback chain (ordered by score)            │       │
│  └─────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│                    EXECUTION LAYER                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │  PROVIDER   │───▶│  PROVIDER   │───▶│  PROVIDER   │     │
│  │  OPENROUTER │    │  OXEN       │    │  TOGETHER   │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │  PROVIDER   │───▶│  PROVIDER   │───▶│  PROVIDER   │     │
│  │  G4F        │    │  OPENAI     │    │  CUSTOM     │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
└─────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│                    RESULT HANDLING                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │   SUCCESS   │    │   RETRY     │    │   FAIL      │     │
│  │   + LOG     │    │   FALLBACK  │    │   + ALERT   │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

## Provider Configuration

Each provider is configured with:

```yaml
providers:
  openrouter:
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY_PAID"
    models:
      - id: "anthropic/claude-3-opus"
        name: "Opus"
        tier: "paid"
        cost_per_million_input: 15.0
        cost_per_million_output: 75.0
        rate_limit_requests_per_minute: 60
        rate_limit_tokens_per_minute: 100000
      - id: "anthropic/claude-3-sonnet"
        name: "Sonnet"
        tier: "paid"
        cost_per_million_input: 3.0
        cost_per_million_output: 15.0
        rate_limit_requests_per_minute: 100
        rate_limit_tokens_per_minute: 200000

  openrouter_free:
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY_FREE"
    models:
      - id: "meta-llama/llama-3-70b-instruct"
        name: "Llama 3 70B"
        tier: "free"
        cost_per_million_input: 0.0
        cost_per_million_output: 0.0
        rate_limit_requests_per_minute: 20
        rate_limit_tokens_per_minute: 50000
        daily_free_limit: 100000  # tokens per day

  oxen:
    base_url: "https://hub.oxen.ai/api"
    api_key_env: "OXEN_API_KEY"
    models:
      - id: "stepfun/step-3.5-flash:free"
        name: "Step 3.5 Flash"
        tier: "free"
        cost_per_million_input: 0.0
        cost_per_million_output: 0.0
        rate_limit_requests_per_minute: 30
        rate_limit_tokens_per_minute: 100000

  together:
    base_url: "https://api.together.xyz/v1"
    api_key_env: "TOGETHERAI_USER_KEY"
    models:
      - id: "meta-llama/Llama-3-70b-chat-hf"
        name: "Llama 3 70B"
        tier: "free"
        cost_per_million_input: 0.0
        cost_per_million_output: 0.0
        rate_limit_requests_per_minute: 30
        rate_limit_tokens_per_minute: 100000

  g4f:
    base_url: "https://g4f.space/v1"
    api_key_env: "G4F_API_KEY"
    models:
      - id: "gpt-4"
        name: "GPT-4"
        tier: "free"
        cost_per_million_input: 0.0
        cost_per_million_output: 0.0
        rate_limit_requests_per_minute: 10
        rate_limit_tokens_per_minute: 30000
```

## Routing Decision Algorithm

```
┌─────────────────────────────────────────────────────────────┐
│                 ROUTING DECISION ALGORITHM                   │
└─────────────────────────────────────────────────────────────┘

Input: Task (role, complexity, estimated_tokens)
       Provider State (rate limits, costs, availability)

1. FILTER ELIGIBLE PROVIDERS
   ├─ Remove providers below rate limit
   ├─ Remove providers exceeding free tier daily limit
   ├─ Remove providers not supporting required model tier
   └─ Result: Eligible provider set E

2. SCORE EACH PROVIDER
   For each provider p in E:
   
   score(p) = w_cost × cost_score(p)
            + w_speed × speed_score(p)
            + w_quality × quality_score(p)
            + w_reliability × reliability_score(p)
            + w_free_tier × free_tier_bonus(p)
   
   Where:
   - cost_score: Lower cost = higher score
   - speed_score: Historical response time
   - quality_score: Success rate, task completion quality
   - reliability_score: Uptime, rate limit adherence
   - free_tier_bonus: Extra points for free tier usage

3. SELECT PRIMARY + FALLBACKS
   ├─ Primary: Provider with highest score
   ├─ Fallback 1: Provider with 2nd highest score
   ├─ Fallback 2: Provider with 3rd highest score
   └─ Return: [Primary, Fallback1, Fallback2]

4. EXECUTE WITH FALLBACK CHAIN
   Try primary provider
   If fails (rate limit, error, timeout):
     Try fallback 1
     If fails:
       Try fallback 2
       If fails:
         Return error with all failure reasons
```

## Scoring Formula

```
score(p) = 
    0.30 × cost_score(p)           # 30% weight on cost
  + 0.20 × speed_score(p)          # 20% weight on speed
  + 0.25 × quality_score(p)        # 25% weight on quality
  + 0.15 × reliability_score(p)    # 15% weight on reliability
  + 0.10 × free_tier_bonus(p)      # 10% bonus for free tiers

cost_score(p) = 1 - (estimated_cost(p) / max_cost_across_providers)
speed_score(p) = 1 - (avg_latency(p) / max_latency_across_providers)
quality_score(p) = success_rate(p) × quality_rating(p)
reliability_score(p) = uptime(p) × (1 - rate_limit_hit_rate(p))
free_tier_bonus(p) = 1.0 if tier == "free" else 0.0
```

## Fallback Strategy

```
┌─────────────────────────────────────────────────────────────┐
│                    FALLBACK STRATEGY                         │
└─────────────────────────────────────────────────────────────┘

Primary Provider Fails
       │
       ▼
┌──────────────────┐
│ Check Error Type │
└──────────────────┘
       │
       ├─ Rate Limit Exceeded
       │      │
       │      ▼
       │  Wait for rate limit reset (if < 60s)
       │  Then retry primary
       │
       ├─ API Error (5xx)
       │      │
       │      ▼
       │  Immediate fallback to next provider
       │
       ├─ Timeout (> 30s)
       │      │
       │      ▼
       │  Immediate fallback + mark provider as slow
       │
       └─ Authentication Error
              │
              ▼
          Alert admin, skip provider, use fallback

Fallback Chain Exhausted
       │
       ▼
┌──────────────────┐
│ Queue Task for   │
│ Retry Later      │
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ Alert Admin      │
│ (Critical Issue) │
└──────────────────┘
```

## Cost Tracking

```
┌─────────────────────────────────────────────────────────────┐
│                    COST TRACKING                             │
└─────────────────────────────────────────────────────────────┘

Per-Request Tracking:
┌─────────────────────────────────────────────────────────────┐
│ {                                                           │
│   "timestamp": "2026-03-22T10:30:00Z",                     │
│   "provider": "openrouter",                                │
│   "model": "sonnet",                                       │
│   "task_id": "PROJ-042",                                   │
│   "role": "backend",                                       │
│   "tokens_in": 2500,                                       │
│   "tokens_out": 1200,                                      │
│   "cost_usd": 0.0045,                                      │
│   "tier": "paid",                                          │
│   "latency_ms": 1250                                       │
│ }                                                           │
└─────────────────────────────────────────────────────────────┘

Aggregated Reports:
┌─────────────────────────────────────────────────────────────┐
│ Daily Cost Summary:                                         │
│ ├─ OpenRouter (paid): $12.50                                │
│ ├─ OpenRouter (free): $0.00 (saved ~$8.00)                  │
│ ├─ Oxen (free): $0.00 (saved ~$5.00)                        │
│ ├─ Together (free): $0.00 (saved ~$3.00)                    │
│ └─ Total: $12.50 (saved ~$16.00 vs all-paid)               │
└─────────────────────────────────────────────────────────────┘

Budget Alerts:
┌─────────────────────────────────────────────────────────────┐
│ Budget: $100/month                                          │
│ Current: $75.50 (75.5%)                                     │
│ Daily Average: $3.50                                        │
│ Projected Month-End: $112.00 (OVER BUDGET)                  │
│ Recommendation: Increase free tier usage by 20%             │
└─────────────────────────────────────────────────────────────┘
```

## Rate Limit Management

```
┌─────────────────────────────────────────────────────────────┐
│                  RATE LIMIT MANAGEMENT                       │
└─────────────────────────────────────────────────────────────┘

Provider Rate Limits:
┌─────────────────────────────────────────────────────────────┐
│ Provider: openrouter_free                                   │
│ ├─ Requests/minute: 20 (current: 15)                        │
│ ├─ Tokens/minute: 50000 (current: 32000)                    │
│ ├─ Daily free limit: 100000 tokens (used: 45000)            │
│ └─ Status: AVAILABLE                                        │
└─────────────────────────────────────────────────────────────┘

Rate Limit Strategies:
1. Token Bucket Algorithm
   - Track tokens consumed per window
   - Refill at fixed rate
   - Reject when bucket empty

2. Sliding Window
   - Track requests in last N seconds
   - Smooths out burst traffic

3. Predictive Throttling
   - Estimate token needs for queued tasks
   - Reserve capacity for high-priority tasks
   - Defer low-priority when capacity low
```

## Free Tier Maximization

```
┌─────────────────────────────────────────────────────────────┐
│                 FREE TIER MAXIMIZATION                       │
└─────────────────────────────────────────────────────────────┘

Strategy:
1. Route simple tasks to free tiers first
   - Docs, formatting, simple fixes → Free models
   - Complex reasoning → Paid models (when needed)

2. Batch small tasks together
   - Combine 2-3 small tasks into one request
   - Maximize value per free tier token

3. Time-based routing
   - Use free tiers during off-peak hours
   - Paid tiers for urgent/critical tasks

4. Model capability matching
   - Match task requirements to model capabilities
   - Don't waste Opus on Sonnet-worthy tasks
   - Don't use paid when free tier suffices

Free Tier Savings Tracker:
┌─────────────────────────────────────────────────────────────┐
│ Month: March 2026                                           │
│ ├─ Total requests: 1,250                                    │
│ ├─ Free tier requests: 875 (70%)                            │
│ ├─ Paid tier requests: 375 (30%)                            │
│ ├─ Estimated paid cost avoided: $125.00                     │
│ └─ Actual cost: $45.00                                      │
│ └─ Savings: 73.5%                                           │
└─────────────────────────────────────────────────────────────┘
```

---

# Tier Optimization Policy Engine

## Overview

The policy engine defines rules for provider switching, request batching, and free tier usage. Policies are configurable via YAML/JSON and evaluated at runtime for each routing decision.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    POLICY ENGINE                             │
└─────────────────────────────────────────────────────────────┘

┌──────────────┐
│  TASK        │
│  REQUEST     │
└──────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│                    POLICY LOADER                             │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │   YAML      │    │   JSON      │    │   DEFAULT   │     │
│  │   FILES     │    │   FILES     │    │   POLICIES  │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│         │                │                │                 │
│         └────────────────┴────────────────┘                 │
│                         │                                   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────┐       │
│  │           POLICY VALIDATION                      │       │
│  │  - Schema validation                             │       │
│  │  - Conflict detection                            │       │
│  │  - Priority ordering                             │       │
│  └─────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│                    POLICY STORE                              │
│  (In-memory cache of validated policies)                     │
└─────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│                    POLICY EVALUATOR                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  For each routing decision:                                 │
│  1. Gather context (task, provider state, budget)           │
│  2. Match applicable policies                               │
│  3. Evaluate conditions                                     │
│  4. Apply actions from highest priority matching policy     │
│  5. Return routing decision                                 │
└─────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│                    ROUTING DECISION                          │
│  { provider, model, fallback_chain, batch_size }            │
└─────────────────────────────────────────────────────────────┘
```

## Policy Schema

```yaml
# policies.yaml
policies:
  - id: "free-tier-first"
    name: "Prefer Free Tiers"
    description: "Route non-critical tasks to free tier providers"
    priority: 100  # Higher = evaluated first
    enabled: true
    
    conditions:
      - field: "task.complexity"
        operator: "in"
        value: ["low", "medium"]
      - field: "task.role"
        operator: "not_equals"
        value: "manager"
      - field: "task.role"
        operator: "not_equals"
        value: "security"
      - field: "provider.tier"
        operator: "equals"
        value: "free"
      - field: "provider.daily_remaining"
        operator: "greater_than"
        value: 10000
    
    actions:
      - type: "set_provider"
        value: "oxen"
      - type: "add_fallback"
        value: ["together", "g4f", "openrouter_free"]
      - type: "set_max_tokens"
        value: 50000

  - id: "complex-task-premium"
    name: "Premium Models for Complex Tasks"
    description: "Use high-quality models for complex/architectural work"
    priority: 90
    enabled: true
    
    conditions:
      - field: "task.complexity"
        operator: "equals"
        value: "high"
      - field: "task.scope"
        operator: "equals"
        value: "large"
    
    actions:
      - type: "set_provider"
        value: "openrouter"
      - type: "set_model"
        value: "anthropic/claude-3-opus"
      - type: "set_effort"
        value: "max"

  - id: "budget-conservation"
    name: "Budget Conservation Mode"
    description: "Switch to free tiers when budget is running low"
    priority: 80
    enabled: true
    
    conditions:
      - field: "budget.percent_used"
        operator: "greater_than"
        value: 80
      - field: "task.priority"
        operator: "greater_than"
        value: 1  # Not critical
    
    actions:
      - type: "require_free_tier"
        value: true
      - type: "set_max_cost_per_task"
        value: 0.01

  - id: "rate-limit-avoidance"
    name: "Rate Limit Avoidance"
    description: "Switch providers when approaching rate limits"
    priority: 95
    enabled: true
    
    conditions:
      - field: "provider.rate_limit_remaining_percent"
        operator: "less_than"
        value: 20
    
    actions:
      - type: "switch_provider"
        value: "next_best_available"
      - type: "add_cooldown"
        value: 60  # seconds before retrying

  - id: "batch-small-tasks"
    name: "Batch Small Tasks"
    description: "Combine small tasks into batches for efficiency"
    priority: 70
    enabled: true
    
    conditions:
      - field: "task.estimated_minutes"
        operator: "less_than"
        value: 15
      - field: "queue.similar_tasks_count"
        operator: "greater_than"
        value: 2
    
    actions:
      - type: "set_batch_size"
        value: 3
      - type: "set_batch_timeout"
        value: 300  # wait up to 5 minutes for batch

  - id: "urgent-task-priority"
    name: "Urgent Task Priority"
    description: "Critical tasks get fastest available provider"
    priority: 99
    enabled: true
    
    conditions:
      - field: "task.priority"
        operator: "equals"
        value: 1
    
    actions:
      - type: "select_fastest_provider"
        value: true
      - type: "skip_free_tier_check"
        value: true
```

## Policy Condition Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `equals` | Exact match | `field: "task.role", value: "backend"` |
| `not_equals` | Not equal | `field: "task.role", value: "manager"` |
| `in` | In list | `field: "task.complexity", value: ["low", "medium"]` |
| `not_in` | Not in list | `field: "provider.tier", value: ["paid"]` |
| `greater_than` | Numeric comparison | `field: "budget.percent_used", value: 80` |
| `less_than` | Numeric comparison | `field: "task.estimated_minutes", value: 30` |
| `contains` | String contains | `field: "task.title", value: "fix"` |
| `regex` | Regex match | `field: "task.id", value: "^SEC-.*"` |
| `always` | Always true | Used for default policies |

## Policy Actions

| Action Type | Description | Parameters |
|-------------|-------------|------------|
| `set_provider` | Force specific provider | `value: "oxen"` |
| `set_model` | Force specific model | `value: "anthropic/claude-3-opus"` |
| `set_effort` | Set effort level | `value: "max"` |
| `add_fallback` | Add fallback providers | `value: ["together", "g4f"]` |
| `set_max_tokens` | Limit token usage | `value: 50000` |
| `set_max_cost` | Limit cost per task | `value: 0.01` |
| `require_free_tier` | Only use free tiers | `value: true` |
| `switch_provider` | Switch to alternative | `value: "next_best_available"` |
| `add_cooldown` | Add cooldown period | `value: 60` (seconds) |
| `set_batch_size` | Set batch size | `value: 3` |
| `set_batch_timeout` | Set batch wait time | `value: 300` (seconds) |
| `select_fastest_provider` | Use fastest provider | `value: true` |
| `skip_free_tier_check` | Bypass free tier check | `value: true` |

## Policy Evaluation Flow

```
┌─────────────────────────────────────────────────────────────┐
│                 POLICY EVALUATION FLOW                       │
└─────────────────────────────────────────────────────────────┘

Task arrives for routing
       │
       ▼
┌─────────────────────────────────────────┐
│ 1. GATHER CONTEXT                       │
│    - Task metadata (role, complexity)   │
│    - Provider state (rate limits, cost) │
│    - Budget status                      │
│    - Queue state                        │
└─────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 2. SORT POLICIES BY PRIORITY            │
│    [99, 95, 90, 80, 70, 100]            │
│    → [100, 99, 95, 90, 80, 70]          │
└─────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 3. EVALUATE EACH POLICY                 │
│    For each policy (highest first):     │
│      - Check all conditions             │
│      - If ALL conditions match:         │
│        - Apply actions                  │
│        - Mark as matched                │
│        - Continue to next policy        │
└─────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 4. MERGE ACTIONS                        │
│    - Later policies can override        │
│    - Conflicts resolved by priority     │
│    - Build final routing decision       │
└─────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│ 5. RETURN ROUTING DECISION              │
│    {                                    │
│      provider: "oxen",                  │
│      model: "stepfun/step-3.5-flash",   │
│      fallback: ["together", "g4f"],     │
│      max_tokens: 50000                  │
│    }                                    │
└─────────────────────────────────────────┘
```

## Policy Hot-Reload

Policies can be updated without restarting:

```
┌─────────────────────────────────────────────────────────────┐
│                    POLICY HOT-RELOAD                         │
└─────────────────────────────────────────────────────────────┘

1. File Watcher detects policy file change
       │
       ▼
2. Load new policy file
       │
       ▼
3. Validate against schema
       │
       ├─ Valid → Apply new policies
       │
       └─ Invalid → Log error, keep old policies
       │
       ▼
4. Notify routing engine of update
       │
       ▼
5. New routing decisions use updated policies
```

## Policy Conflict Resolution

When multiple policies match with conflicting actions:

```
┌─────────────────────────────────────────────────────────────┐
│                 CONFLICT RESOLUTION                          │
└─────────────────────────────────────────────────────────────┘

Policy A (priority 90): set_provider = "openrouter"
Policy B (priority 95): set_provider = "oxen"

Resolution:
- Higher priority wins (Policy B)
- Final: provider = "oxen"

Policy C (priority 80): add_fallback = ["together"]
Policy D (priority 70): add_fallback = ["g4f"]

Resolution:
- Non-conflicting actions are merged
- Final: fallback = ["together", "g4f"]
```

## Policy Testing

Policies should be tested before deployment:

```yaml
# policy_tests.yaml
tests:
  - name: "Free tier routing for simple tasks"
    input:
      task:
        role: "backend"
        complexity: "low"
        estimated_minutes: 15
      budget:
        percent_used: 50
      providers:
        - id: "oxen"
          tier: "free"
          daily_remaining: 50000
    
    expected:
      provider: "oxen"
      tier: "free"

  - name: "Premium routing for complex tasks"
    input:
      task:
        role: "manager"
        complexity: "high"
        scope: "large"
      budget:
        percent_used: 30
    
    expected:
      provider: "openrouter"
      model: "anthropic/claude-3-opus"
      effort: "max"

  - name: "Budget conservation mode"
    input:
      task:
        role: "backend"
        complexity: "medium"
        priority: 2
      budget:
        percent_used: 85
    
    expected:
      require_free_tier: true
      max_cost: 0.01
```

---

## Implementation Notes

### Files to Create

1. **src/bernstein/core/policy.py** - Policy engine implementation
2. **src/bernstein/core/router_tiered.py** - Tier optimization router
3. **src/bernstein/core/metrics.py** - Metrics collection and analysis
4. **src/bernstein/core/self_evolution.py** - Self-evolution feedback loop

### Configuration Files

1. **bernstein_policies.yaml** - Policy definitions
2. **.sdd/config/providers.yaml** - Provider configurations
3. **.sdd/metrics/** - Metrics storage directory

### Integration Points

1. **router.py** - Integrate tier optimization into existing router
2. **spawner.py** - Use policy engine for provider selection
3. **server.py** - Expose metrics and policy endpoints
4. **llm.py** - Support multiple providers with fallback
