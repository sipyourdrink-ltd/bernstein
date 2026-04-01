# Feature Matrix

This matrix tracks shipped capabilities and their documentation coverage with explicit "Shipped / Partial / Roadmap" wording so contributors do not re-implement existing systems.

Last audited: 2026-04-01

Legend:

- `Shipped`: implemented and usable now
- `Partial`: implemented with scope boundaries
- `Roadmap`: not complete enough to present as a production feature

---

## Core orchestration

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Goal-based run (`-g`) | Shipped | Full | Main entry flow |
| Seed-file run (`bernstein.yaml`) | Shipped | Full | Auto-discovery supported |
| Plan-file execution (`stages`/`steps`) | Shipped | Brief | Covered at high level |
| Retry + escalation plumbing | Shipped | Brief | In lifecycle docs, needs deeper policy examples |
| Completion verification (janitor + signals) | Shipped | Full | API + getting started coverage |
| Process-aware stop/drain | Shipped | Brief | Recently expanded behavior |
| Multi-cell orchestration | Partial | Brief | Implemented, limited user-level docs |

## State and persistence

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| File-based state in `.sdd/` | Shipped | Full | Primary operating model |
| Metrics/trace persistence | Shipped | Brief | Paths documented; schema docs still light |
| Lessons/memory persistence | Partial | Brief | Stored and injected, still evolving UX |
| Storage backend options (`memory/postgres/redis`) | Shipped | Full | Config + doctor coverage |

## Observability

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| `/status` and task API | Shipped | Full | Core API documented |
| Prometheus `/metrics` | Partial | Brief | Endpoint is real; dashboards are user-defined |
| OTLP telemetry initialization | Partial | Brief | Wiring exists; deployment guidance still shallow |
| Retrospective reporting (`retro`) | Shipped | Full | CLI coverage present |
| Cost analysis (`cost`, history/anomaly hooks) | Partial | Brief | Core functionality exists, advanced interpretation docs pending |

## Safety and governance

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Quality gates integration | Shipped | Brief | Present in run flow |
| Rule enforcement (`.bernstein/rules.yaml`) | Shipped | Brief | Enforcement behavior documented in AGENTS/architecture material |
| Log redaction safeguards | Shipped | Brief | Safety feature is active but under-documented |
| Audit and verification commands | Partial | Brief | Command surface exists; deep runbooks pending |

## Ecosystem and integrations

| Capability | Runtime status | Docs status | Notes |
|---|---|---|---|
| Agent catalog/discovery | Shipped | Partial | Core commands documented; examples can improve |
| GitHub App and CI fix flows | Shipped | Partial | Setup docs exist; parity pass ongoing |
| Trigger sources (`github`, `slack`, `file_watch`, `webhook`) | Partial | Brief | Source adapters exist; authoring docs need detail |
| Plugin hooks (pluggy) | Shipped | Brief | SDK docs exist, should be expanded |
| Cluster/worker primitives | Partial | Brief | Implemented building blocks; not a turnkey managed cluster story |

---

## Highest-priority doc gaps

1. Deep examples for retry/escalation and fallback behavior.
2. Explicit observability guide for Prometheus + OTLP deployment patterns.
3. End-to-end docs for trigger rule authoring and source configuration.
4. Clear operator runbooks for audit/verification command families.
