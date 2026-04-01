# Bernstein Design

This document describes the current architecture of Bernstein as implemented in the codebase today, with explicit boundaries for partial features.

---

## Core design principles

- Short-lived workers: agents are spawned for focused work and then exit.
- File-first state: runtime state is persisted under `.sdd/`.
- Deterministic orchestration: scheduling and lifecycle decisions are code-driven.
- Verification before closure: task completion passes through janitor/quality logic.
- Multi-adapter runtime: Bernstein is CLI-agent agnostic via adapter interfaces.

---

## High-level architecture

```text
CLI (src/bernstein/cli/)
  -> Task server (src/bernstein/core/server.py, FastAPI)
    -> Route modules (src/bernstein/core/routes/)
      -> Store + lifecycle + orchestration (core/)
        -> Adapter-based process spawning (adapters/)
```

Primary orchestration modules:

- `src/bernstein/core/orchestrator.py` (facade)
- `src/bernstein/core/tick_pipeline.py`
- `src/bernstein/core/task_lifecycle.py`
- `src/bernstein/core/agent_lifecycle.py`

Key runtime subsystems:

- Routing/cost: `router.py`, `cascade_router.py`, `cost.py`, `cost_history.py`, `cost_anomaly.py`
- Reliability: `heartbeat.py`, `completion_budget.py`, `failure_aware_retry.py`, `loop_detector.py`
- Verification: `janitor.py`, `quality_gates.py`, `approval.py`, `reviewer.py`
- Context and memory: `spawn_prompt.py`, `context.py`, `lessons.py`, `knowledge_base.py`, `rag.py`

---

## API surface (current)

The task server composes router modules from `src/bernstein/core/routes/`, including:

- `tasks.py`
- `status.py`
- `agents.py`
- `costs.py`
- `dashboard.py`
- `quality.py`
- `plans.py`
- `graduation.py`
- `webhooks.py`
- `slack.py`
- `auth.py`
- `observability.py`

Notable implemented endpoint groups:

- Task CRUD, claims, completion/fail, dependencies graph
- Agent heartbeats and process/session inspection
- Cluster node registration/heartbeat/status/task-steal primitives
- Status/events/metrics (including Prometheus-compatible metrics endpoint)
- Cost and quality reporting endpoints
- Trigger/webhook ingestion routes

---

## Trigger architecture

Trigger orchestration is implemented and centered on:

- `src/bernstein/core/trigger_manager.py`
- `src/bernstein/core/models.py` (`TriggerEvent`, trigger config models)

Current source adapters:

- `src/bernstein/core/trigger_sources/github.py`
- `src/bernstein/core/trigger_sources/slack.py`
- `src/bernstein/core/trigger_sources/file_watch.py`
- `src/bernstein/core/trigger_sources/webhook.py`

Configuration source:

- `.sdd/config/triggers.yaml`

Boundary: trigger infrastructure is real and usable, but project-specific rule libraries and operational runbooks are still evolving.

---

## Cluster and remote execution

Implemented pieces:

- Worker CLI: `src/bernstein/cli/worker_cmd.py`
- Cluster data model/policy: `src/bernstein/core/cluster.py`
- Cluster API routes in `src/bernstein/core/routes/tasks.py`

Boundary:

- Distributed operation works as an advanced deployment pattern.
- It is not presented as a fully managed autoscaling platform.

---

## Plugins and extensibility

Plugin system is pluggy-based and implemented under:

- `src/bernstein/plugins/hookspecs.py`
- `src/bernstein/plugins/manager.py`

Current hooks include task/agent/evolution lifecycle callbacks.

Boundary:

- Hook surface is stable for common extensions.
- Advanced plugin packaging/marketplace workflows are still light on guardrails.

---

## Observability and telemetry

Implemented:

- Status/event streaming routes
- Prometheus metrics export
- Cost and quality metrics files under `.sdd/metrics/`
- Observability route module for heartbeat/stall insights
- OTLP telemetry configuration hooks in core models/bootstrap path

Boundary:

- Prometheus and OTLP are real integrations.
- Turnkey production dashboards/alert packs are not bundled.

---

## Evolution and planning

Implemented:

- Evolution package (`src/bernstein/evolution/`)
- Plan execution and approval modules (`planner.py`, `plan_approval.py`, plan routes)
- Retrospective/reporting command path (`retro`)

Boundary:

- End-to-end autonomous self-evolution exists with safety controls, but should be treated as operator-supervised in production settings.

---

## `.sdd/` state model (current)

Common active paths:

- `.sdd/backlog/open|claimed|closed/`
- `.sdd/runtime/`
- `.sdd/metrics/`
- `.sdd/traces/`
- `.sdd/memory/`
- `.sdd/caching/`
- `.sdd/agents/`

Exact files vary by enabled features and run mode.

---

## Non-goals for this document

- This file is not a roadmap backlog.
- This file is not a generated protocol matrix.
- This file is not a per-command CLI reference (see `GETTING_STARTED.md` and `bernstein --help`).
