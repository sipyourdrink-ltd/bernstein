# Observability Overview

Audience: SREs wiring Bernstein into an existing observability stack.

## Overview

Bernstein emits four classes of signal:

- **Metrics** - Prometheus-format counters, gauges, and histograms exposed at
  `/metrics`. Native Datadog APM and OpenTelemetry/OTLP export are also
  available out of the box.
- **Traces** - task and agent execution spans via OpenTelemetry, with built-in
  presets for Jaeger, Grafana Tempo, Datadog, Zipkin, and console.
- **Events** - structured JSON events on a Server-Sent-Events bus
  (`/events`), per-task progress streams, and per-agent log streams.
- **Anomaly signals** - `core/observability/behavior_anomaly.py` runs both
  post-completion and real-time detectors over agent state and emits
  signals from `LOG` (informational) all the way to `KILL_AGENT` (terminate
  the agent on the next tick).

Everything in this doc is shipped today and exposed via documented
endpoints. Wiring it into Prometheus + Grafana takes one scrape config plus
one dashboard import; adding Datadog or OTLP export is one config block.

## Endpoints catalogue

All HTTP. Authentication rules follow the standard middleware
([Security and identity](security-and-identity.md)) - `/metrics` is
gated by viewer permissions when an auth backend is configured.

### Prometheus / Grafana / SLOs

| Endpoint                       | Purpose                                                                 | Code                                                       |
| ------------------------------ | ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| `GET /metrics`                 | Prometheus exposition (text format)                                     | `core/routes/status_lifecycle.py:251-265`                  |
| `GET /grafana/dashboard`       | Pre-built Grafana dashboard JSON. Query `?datasource=Prometheus`        | `core/routes/grafana.py:18-30`                             |
| `GET /slo`                     | SLO dashboard summary                                                   | `core/routes/slo.py:36-41`                                 |
| `GET /slo/budget`              | Error budget detail (consumed / remaining / burn rate / actions)        | `core/routes/slo.py:44-63`                                 |
| `GET /slo/burndown`            | Burn-down chart data with linear projection of breach date              | `core/routes/slo.py:66-94`                                 |
| `POST /slo/reset`              | Reset SLO tracker (admin only)                                          | `core/routes/slo.py:97-...`                                |

### Observability detail

These power the in-product dashboard and are equally consumable by
external tools.

| Endpoint                                                | Returns                                                                    |
| ------------------------------------------------------- | -------------------------------------------------------------------------- |
| `GET /observability/agents`                             | Per-agent runtime: heartbeat, stall profile, log summary                   |
| `GET /observability/effectiveness`                      | Effectiveness score per role/model                                         |
| `GET /observability/recommendations`                    | Improvement recommendations from `RecommendationEngine`                    |
| `GET /observability/budget`                             | Cost budget snapshot (current spend, projection, headroom)                 |
| `GET /observability/deps`                               | Dependency validator findings                                              |
| `GET /observability/token-histogram`                    | Token-usage histogram bucketed by complexity                               |
| `GET /observability/queue-depth`                        | Queue-depth time series; `?limit=100`                                      |
| `GET /observability/timeline`                           | Run timeline (start/end of tasks, gates, escalations)                      |
| `GET /observability/incidents`                          | Open incident list                                                         |
| `GET /observability/token-breakdown`                    | Per-session token attribution + optimisation opportunities                 |
| `GET /observability/incident-timeline/{incident_id}`    | Reconstructed timeline for a single incident                               |
| `GET /recap`                                            | Recap of recent runs                                                       |
| `GET /changelog`                                        | Auto-generated changelog (`?days=30`)                                      |

All defined in `core/routes/observability.py`.

### Costs (12 sub-endpoints in `core/routes/costs.py`)

| Endpoint                        | Purpose                                                                            |
| ------------------------------- | ---------------------------------------------------------------------------------- |
| `GET /events/cost`              | SSE stream of cost events                                                          |
| `GET /costs/current`            | Snapshot of in-flight spend                                                        |
| `GET /costs/alerts`             | Active cost alerts                                                                 |
| `GET /costs/history`            | Historical cost time-series                                                        |
| `GET /costs/{run_id}`           | Per-run cost breakdown                                                             |
| `GET /costs/export`             | CSV export                                                                         |
| `GET /costs/forecast`           | Forecast remaining spend from planned backlog                                      |
| `GET /costs/compare`            | Compare runs                                                                       |
| `GET /costs/cache-stats`        | Prompt-cache hit-rate (`prompt_cache.py`)                                          |
| `GET /costs/model-comparison`   | Cost-per-model summary                                                             |
| `GET /costs/token-efficiency`   | Tokens-per-line-changed efficiency                                                 |
| `GET /costs/by-tag`             | Cost grouped by task tag                                                           |
| `GET /costs/token-breakdown`    | Per-session token splits (input/output/cache)                                      |
| `GET /costs/efficiency`         | Composite efficiency score                                                         |

### Provider latency

| Endpoint                                         | Purpose                                                            | Code                                          |
| ------------------------------------------------ | ------------------------------------------------------------------ | --------------------------------------------- |
| `GET /metrics/provider-latency`                  | Current p50/p95/p99 per provider+model with degradation flag       | `core/routes/provider_latency.py:27-52`       |
| `GET /metrics/provider-latency/history`          | Raw samples for charting; `?provider`, `?model`, `?hours` (1-168)  | `core/routes/provider_latency.py:55-...`      |

A provider+model is flagged `degraded: true` when its p99 exceeds 2x the
7-day baseline (`provider_latency.py:42-44`).

### Custom metrics + predictive alerts

| Endpoint                              | Purpose                                                            | Code                                 |
| ------------------------------------- | ------------------------------------------------------------------ | ------------------------------------ |
| `GET /metrics/custom`                 | Evaluate user-defined metric formulas                              | `core/routes/custom_metrics.py`      |
| `GET /metrics/custom/schema`          | List configured custom metric definitions                          | `core/routes/custom_metrics.py`      |
| `GET /metrics/predictions`            | Predictive alert horizon (which SLO is about to breach)            | `core/routes/predictive.py:62`       |

Custom metrics are configured under `metrics:` in `bernstein.yaml`, each
with a `formula`, `unit`, and `description` (`custom_metrics.py:32-39`);
the evaluator gets tick-level variables (`tasks_spawned`, `errors`,
`active_agents`, etc.) plus cumulative totals
(`custom_metrics.py:44-77`).

### Health and readiness

| Endpoint                                | Purpose                                                          | Code                                          |
| --------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------- |
| `GET /health`                           | Component-level liveness with `degraded` rollup                  | `core/routes/status_lifecycle.py:43-67`       |
| `GET /health/ready` (`/ready`)          | Readiness probe (200/503) for load balancers                     | `core/routes/status_lifecycle.py:70-83`       |
| `GET /health/live` (`/alive`)           | Liveness probe                                                   | `core/routes/status_lifecycle.py:86-95`       |
| `GET /health/deps`                      | Status of upstream dependencies (LLM, git, task store)           | (same module)                                 |
| `GET /cache-stats`                      | Internal cache hit-rate                                          | (same module)                                 |

## Prometheus + Grafana wiring

Minimal scrape config:

```yaml
scrape_configs:
  - job_name: bernstein
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ['bernstein-host:8052']
    bearer_token: ${BERNSTEIN_AUTH_TOKEN}    # required when auth is on
    scrape_interval: 15s
    scrape_timeout: 10s
```

Import the bundled dashboard:

```bash
curl -H "Authorization: Bearer $BERNSTEIN_AUTH_TOKEN" \
     http://bernstein-host:8052/grafana/dashboard?datasource=Prometheus \
     -o bernstein-dashboard.json
# Then in Grafana UI: Dashboards → Import → upload JSON, pick the
# Prometheus datasource that matches `?datasource=` above.
```

The dashboard ships as `Content-Disposition: attachment` so the response
is safe to save directly. The `?datasource=` query parameter rewrites the
datasource references in the JSON
(`grafana.py:18-30`); pass the exact name of your Grafana datasource.

## Datadog APM

`core/observability/apm_integration.py` (header at lines 1-44) supports
both `ddtrace`-agent and direct-OTLP-ingest paths.

Environment variables (`apm_integration.py:29-37`):

| Variable                | Default          | Notes                                                            |
| ----------------------- | ---------------- | ---------------------------------------------------------------- |
| `DD_API_KEY`            | required         | Direct ingest path. `DATADOG_API_KEY` is a recognised alias.     |
| `DD_SITE`               | `datadoghq.com`  | Use `datadoghq.eu` for EU residency.                             |
| `DD_SERVICE`            | `bernstein`      | Service name in the Datadog UI.                                  |
| `DD_ENV`                | `production`     | Environment tag.                                                 |
| `DD_VERSION`            | none             | Free-form version string.                                        |
| `DD_AGENT_HOST`         | `localhost`      | When using ddtrace-agent path.                                   |
| `DD_TRACE_AGENT_PORT`   | `8126`           | When using ddtrace-agent path.                                   |

Setup is one of:

```python
from bernstein.core.observability.apm_integration import (
    auto_configure_apm, configure_datadog
)

# Auto-pick whatever is available based on env vars
auto_configure_apm()

# Or be explicit
configure_datadog()
```

What gets sent: task and agent spans (start/end, status,
role, model, complexity), gate execution spans, cascade-router
escalations, and exception traces. Logs are not piped directly - use the
Datadog Agent's log file collection on `.sdd/runtime/*.log` if you need
log correlation.

New Relic is also supported (`apm_integration.py:39-44`); set
`NEW_RELIC_LICENSE_KEY` (or `NEWRELIC_API_KEY`) and call
`configure_newrelic()`.

## OpenTelemetry / OTLP

`core/observability/telemetry.py` provides the canonical OTel surface:
tracer, meter, span context manager, and named presets for common
backends (`telemetry.py:1-22`).

```python
from bernstein.core.observability.telemetry import (
    init_telemetry_from_preset, init_telemetry, start_span
)

# Built-in presets (telemetry.py:86-100): "jaeger", "grafana", "datadog",
# "zipkin", "console", and more. Each preset hardcodes endpoint, protocol,
# and TLS expectations.
init_telemetry_from_preset("jaeger")

# Or supply a custom OTLP endpoint
init_telemetry("http://my-collector:4317", protocol="grpc")

# Spans are produced from anywhere in the runtime
with start_span("task.run", {"task.id": task_id}):
    ...
```

The default protocol is OTLP/gRPC (`telemetry.py:50,
DEFAULT_PROTOCOL`); switch to HTTP/protobuf via the `protocol` argument
or via the preset's `_HTTP_PROTOBUF` (`telemetry.py:53`). The default
service name is `bernstein` (`telemetry.py:58`).

Service maps work out of the box because every span carries
`service.name` as a resource attribute; no extra config required for
Tempo or Jaeger to associate them.

### Signed journal projection (verifiable spans)

Ordinary OTLP spans carry random ids that any tool can emit and none can
verify. `core/observability/otel_projection.py` makes the GenAI export a
*deterministic projection* of the run event journal instead:

- span ids are derived from journal entry hashes (`span_id = H("otel.span",
  entry_hash)[:8 bytes]`), so two replays of the same run export a
  byte-identical trace,
- every span carries `bernstein.journal.entry_hash` pinning the exact
  journal row it projects,
- journal events map onto the OTel GenAI operation layers (`invoke_workflow`
  root, `invoke_agent`, `execute_tool`, `chat`),
- the whole span set is signed with the install-identity Ed25519 key, so a
  tampered span breaks the entry-hash binding or the signature.

The projection is written to the local
`.sdd/runs/<run_id>/projection.otel.json` store even when no OTLP endpoint is
set (`BERNSTEIN_OTEL_ENDPOINT` stays opt-in). The GenAI convention attributes
are still Development-stage; they live behind the stability flag
(`--no-genai-stability` to omit them) and never affect the ids or the
entry-hash binding, so an attribute rename cannot corrupt the journal-anchored
truth.

```bash
# Project a run's journal into a signed OTel span set (local JSONL store).
bernstein trace project <run_id>

# Recompute span ids from the journal and verify the signature.
bernstein trace verify-projection <run_id>
```

Each emit appends an `otel.projection` event to the HMAC audit chain
recording the run id, the journal head, the derived trace id, the span count,
and the sha256 of the signed span set, so an operator can confirm from the
chain alone that a trace faithfully projects the journal.

## SLO tracking

`core/observability/slo.py` defines targets, computes status, and tracks
error-budget burn-down (`slo.py:1-13`). Default targets ship at:

- Task success rate ≥ 90 %
- Merge success rate ≥ 95 %
- P95 task duration < 30 minutes

Each target has `current` value, `target` threshold, `warning_threshold`,
and a 1-hour rolling `window_seconds` (`slo.py:80-89`). Status is one of
`green` / `yellow` / `red` (`slo.py:64-69`).

Burn-rate snapshots (`slo.py:35-61`) are kept in a 120-sample ring buffer
(~1 hour at 30s intervals). When the rolling burn rate would deplete the
error budget, `ErrorBudgetAction` (`slo.py:72-77`) signals which
remediation policy to apply: `REDUCE_AGENTS`, `UPGRADE_MODEL`, or
`INCREASE_REVIEW`.

To define a custom SLO, instantiate `SLOTarget` and add it to the
tracker; expose it via `GET /slo`. To act on breaches outside the
in-process actions, watch `GET /slo/budget` for `is_depleted: true`
(`slo.py:...` and `routes/slo.py:55-62`).

## Behavior anomaly detection

Source: `core/observability/behavior_anomaly.py`. Covers two distinct
detection modes (`behavior_anomaly.py:1-20`):

1. **Post-completion** - `BehaviorAnomalyDetector` analyses metrics from
   `.sdd/metrics/tasks.jsonl` after a task finishes and emits
   `AnomalySignal` values.
2. **Real-time** - `RealtimeBehaviorMonitor` tracks in-flight session
   state on every progress update and fires immediately on suspicious
   activity. On `KILL_AGENT` severity it writes a structured kill-signal
   file (`.sdd/runtime/{session_id}.kill`) so the orchestrator
   terminates the agent on the next tick - the same mechanism the
   circuit breaker uses.

Real-time detection dimensions (`behavior_anomaly.py:11-19`):

| Dimension                    | Trigger pattern                                                          | Severity     |
| ---------------------------- | ------------------------------------------------------------------------ | ------------ |
| Suspicious file access       | `*.key`, `*.pem`, `id_rsa`, `.env`, `*credentials*`, `*/.aws/*`, `*/.ssh/*`, `.git/config`, `/etc/{passwd,shadow}`, `/proc/*` (`:46-77`) | `KILL_AGENT` |
| Dangerous command execution  | `curl`, `wget`, `nc`, `bash -i`, `python -c`, `sudo`, `chmod 777`, `cat /etc/{passwd,shadow}`, etc. (`:95-120`)                          | `KILL_AGENT` |
| Suspicious network endpoints | Cloud metadata IPs, C2 callbacks, internal SSRF targets in progress messages | `KILL_AGENT` |
| Output-size explosion        | Cumulative stdout exceeds configured limit                                                                                              | `KILL_AGENT` |
| File-change velocity         | Statistical outlier vs learned per-agent baseline                                                                                       | `LOG`        |

Allow-list patterns counter false positives: `*.env.example`,
`*.env.template`, `*.env.sample`, `tests/*`, `test/*`, `docs/*`
(`:80-87`).

How to interpret alerts:

- **`LOG`** - informational. Often noise during refactors that touch
  many files at once.
- **`WARN`** - investigate the agent log; the agent is misbehaving but
  not necessarily compromised.
- **`KILL_AGENT`** - the runtime has already written a kill signal. The
  orchestrator will terminate the agent on its next tick and the
  agent's branch is preserved for human review under
  `.sdd/quarantine/{session_id}.json` (see [Circuit breakers](#circuit-breakers)
  below).

To export anomaly events to your stack, watch
`/observability/incidents` and `/observability/incident-timeline/{id}`,
and tail `.sdd/metrics/kill_audit.jsonl` for the same events with full
forensic context.

## Circuit breakers

Two distinct breakers, both producing structured signals.

**Agent-level circuit breaker** (`core/observability/circuit_breaker.py`):

Real-time breaker that auto-terminates misbehaving agents
(`circuit_breaker.py:1-15`). Triggers:

- **Scope violation** - agent edited files outside the task's
  `owned_files`.
- **Budget violation** - agent exceeded a per-session token limit.

On trigger:

1. Writes a structured `.sdd/runtime/{session_id}.kill` JSON file.
2. Appends to `.sdd/metrics/kill_audit.jsonl`
   (`circuit_breaker.py:41-72`).
3. Writes `.sdd/quarantine/{session_id}.json` so the agent's git branch
   is preserved for review (`:75-...`).

The orchestrator picks up `.kill` files via `check_kill_signals()` in
the `agent_lifecycle` module on the next tick.

**Service-level cascading-failure circuit breaker**
(`core/observability/cascading_failure_circuit_breaker.py`):

Three-state breaker per service (CLOSED / OPEN / HALF_OPEN -
`cascading_failure_circuit_breaker.py:39-44`). Wraps calls to upstream
services (LLM providers, task server, git) with independent failure and
latency thresholds. Configuration
(`cascading_failure_circuit_breaker.py:53-69`):

| Field                    | Default | Meaning                                                  |
| ------------------------ | ------- | -------------------------------------------------------- |
| `failure_threshold`      | 5       | Consecutive failures before opening.                     |
| `recovery_timeout_s`     | 30.0    | Seconds in OPEN before probing.                          |
| `half_open_max_calls`    | 3       | Probe call limit in HALF_OPEN.                           |
| `latency_threshold_ms`   | none    | If set, slow successes count as failures.                |

How to reset:

- Agent-level: human review of the quarantined branch and resolution via
  the approvals UI (`/approvals/queue/{id}/resolve`). The kill signal
  is consumed by the orchestrator and is not user-replayable.
- Service-level: breakers transition CLOSED automatically after a
  successful HALF_OPEN probe set. To force a reset, restart the service
  or call the breaker registry's reset method during incident response.

Provider-specific circuit breakers
(`core/observability/provider_circuit_breaker.py`) extend the same
state machine with per-provider rate-limit awareness; they feed into
`CascadeFallbackManager` to drive cross-adapter failover (see
[Quality pipeline](../architecture/quality-pipeline.md) and
[Model routing](../architecture/model-routing.md)).

## Code pointers

| Concern                                | File                                                                    |
| -------------------------------------- | ----------------------------------------------------------------------- |
| Prometheus counters/gauges             | `src/bernstein/core/prometheus.py`, `core/observability/prometheus.py`  |
| `/metrics` route                       | `src/bernstein/core/routes/status_lifecycle.py:251-265`                 |
| Grafana dashboard generator            | `src/bernstein/core/grafana_dashboard.py`, `core/routes/grafana.py`     |
| OpenTelemetry tracer + presets         | `src/bernstein/core/observability/telemetry.py`                         |
| Datadog / New Relic APM                | `src/bernstein/core/observability/apm_integration.py`                   |
| OTLP exporters                         | `src/bernstein/core/observability/apm_export.py`, `metric_export.py`    |
| SLO tracker                            | `src/bernstein/core/observability/slo.py`                               |
| SLO routes                             | `src/bernstein/core/routes/slo.py`                                      |
| Behavior anomaly detection             | `src/bernstein/core/observability/behavior_anomaly.py`                  |
| Agent-level circuit breaker            | `src/bernstein/core/observability/circuit_breaker.py`                   |
| Cascading-failure circuit breaker      | `src/bernstein/core/observability/cascading_failure_circuit_breaker.py` |
| Provider circuit breaker               | `src/bernstein/core/observability/provider_circuit_breaker.py`          |
| Provider latency tracker               | `src/bernstein/core/observability/provider_latency.py`, `routes/provider_latency.py` |
| Custom metrics evaluator               | `src/bernstein/core/custom_metrics.py`, `routes/custom_metrics.py`      |
| Predictive alerts                      | `src/bernstein/core/observability/predictive_alerts.py`, `routes/predictive.py` |
| Observability detail routes            | `src/bernstein/core/routes/observability.py`                            |
| Cost routes (12 endpoints)             | `src/bernstein/core/routes/costs.py`                                    |
| Incident store + timeline              | `src/bernstein/core/observability/incident.py`, `incident_timeline.py`  |
| Behavior anomaly source data           | `.sdd/metrics/tasks.jsonl`                                              |
| Quality gate metrics                   | `.sdd/metrics/quality_gates.jsonl`                                      |
| Cascade chain metrics                  | `.sdd/metrics/cascade_chains.jsonl`                                     |
| Kill audit log                         | `.sdd/metrics/kill_audit.jsonl`                                         |
