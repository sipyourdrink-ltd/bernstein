# Bernstein Architecture

## Overview

Bernstein is a multi-agent orchestration platform for CLI coding agents. It orchestrates short-lived agents (1-3 tasks each, then exit) with state living in files (`.sdd/`), not in agent memory.

## Core Components

```
┌─────────────────────────────────────────────────────────────────┐
│                     Bernstein Orchestrator                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Task       │  │   Agent      │  │   Quality    │          │
│  │   Server     │  │   Spawner    │  │   Gates      │          │
│  │  (FastAPI)   │  │              │  │              │          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│         │                 │                 │                   │
│         └─────────────────┼─────────────────┘                   │
│                           │                                     │
│                  ┌────────▼────────┐                            │
│                  │   Orchestrator  │                            │
│                  │   (Tick Loop)   │                            │
│                  └────────┬────────┘                            │
│                           │                                     │
│         ┌─────────────────┼─────────────────┐                   │
│         │                 │                 │                   │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────▼───────┐          │
│  │   Router     │  │   Janitor    │  │   Metrics    │          │
│  │              │  │              │  │  Collector   │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
       ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
       │   Claude    │ │   Codex     │ │   Gemini    │
       │   Code      │ │   CLI       │ │   CLI       │
       └─────────────┘ └─────────────┘ └─────────────┘
```

## Component Descriptions

### Task Server (FastAPI)
- Central coordination point for all agents
- REST API for task CRUD operations
- State persisted to `.sdd/runtime/tasks.jsonl`
- Endpoints: `/tasks`, `/tasks/{id}/complete`, `/tasks/{id}/fail`, `/status`

### Agent Spawner
- Spawns CLI agents for task batches
- Supports multiple adapters (Claude, Codex, Gemini, etc.)
- Manages agent lifecycle (spawn, monitor, reap)
- Worktree isolation per agent

### Quality Gates
- Automated validation after task completion
- Gates: lint, type_check, tests, security_scan, coverage_delta
- Configurable in `.bernstein/quality_gates.yaml`
- Blocking or non-blocking modes

### Orchestrator (Tick Loop)
- Main orchestration loop
- Fetches tasks, batches by role, spawns agents
- Monitors agent heartbeats
- Handles task completion/failure

### Router
- Routes tasks to appropriate model and effort level
- Tier awareness (free/standard/premium)
- Cost optimization
- Skill profile-based routing

### Janitor
- Verifies task completion via concrete signals
- Checks files exist, tests pass, lint clean
- Moves tasks to closed/done or closed/failed

### Metrics Collector
- Collects performance metrics
- Token usage, cost, completion times
- Persists to `.sdd/metrics/*.jsonl`
- Exposes via `/metrics` endpoint

## Data Flow

```
1. User creates task → Task Server
2. Orchestrator fetches open tasks
3. Router assigns model/effort
4. Spawner launches agent in worktree
5. Agent completes task → Git commit
6. Janitor verifies completion
7. Quality gates run
8. Metrics recorded
9. Task marked done/failed
```

## File Structure

```
.sdd/
├── backlog/
│   ├── open/       # YAML task specs waiting to be picked up
│   ├── claimed/    # Tasks currently being worked
│   └── closed/     # Completed/cancelled tasks
├── runtime/
│   ├── pids/       # PID metadata JSON files
│   ├── signals/    # Agent signal files (WAKEUP, SHUTDOWN)
│   └── logs/       # Agent runtime logs
├── metrics/
│   ├── tasks.jsonl     # Task metrics
│   ├── costs_*.json    # Cost data
│   └── quality_scores.jsonl
└── archive/
    └── tasks.jsonl     # Historical task data
```

## Agent Lifecycle

```
spawn → working → heartbeat → complete/fail → reap
           │
           └─→ stall detection → kill if stuck
```

## Key Design Decisions

1. **Short-lived agents**: No persistent processes, fresh spawn per task
2. **File-based state**: `.sdd/` is git-friendly, inspectable, recoverable
3. **Deterministic orchestrator**: Scheduling is code, not LLM
4. **Agent-agnostic**: Works with any CLI agent
5. **Git worktree isolation**: Main branch never dirty
