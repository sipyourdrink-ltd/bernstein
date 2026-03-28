# 615 — OpenTelemetry Traces

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein emits no structured telemetry data. 89% of production agent teams require observability. Without OpenTelemetry support, users cannot integrate Bernstein with their existing monitoring stacks (Langfuse, LangSmith, Datadog, Grafana).

## Design

Add OpenTelemetry trace emission for all orchestration events. Define spans for: orchestration run (root span), task assignment, agent spawn, model call, tool invocation, CI check, and task completion. Attach attributes: model name, token counts, cost, agent role, task ID. Use the OpenTelemetry Python SDK with configurable exporters (OTLP, console, Jaeger). Default to disabled; enable via `--trace` flag or `BERNSTEIN_OTEL_ENDPOINT` environment variable. Ensure zero overhead when tracing is disabled. Provide example configurations for Langfuse, LangSmith, and Datadog integration in docs.

## Files to modify

- `src/bernstein/core/telemetry.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `pyproject.toml` (add opentelemetry-api, opentelemetry-sdk)
- `docs/observability.md` (new)
- `tests/unit/test_telemetry.py` (new)

## Completion signal

- `bernstein run --trace` emits OTEL spans to configured endpoint
- Spans visible in Jaeger or similar OTEL-compatible backend
- Zero overhead when tracing is disabled (verified by benchmark)
