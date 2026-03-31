# Chaos Engineering Tests

This directory contains chaos tests to verify Bernstein's resilience to various failure modes.

## Chaos Scenarios

- **Server Restart Resilience:** Verifies that the `Orchestrator` can correctly handle brief outages of the task server and resume work seamlessly.
- **Agent Crash Recovery:** (In Progress) Verifies that the `Orchestrator` correctly detects process-level agent crashes and triggers the appropriate retry logic.

## Resilience Mechanisms Tested

- **HTTP Retry/Backoff:** Orchestrator's internal `httpx` client behavior.
- **State Persistence:** Task state persistence in `tasks.jsonl` during server restarts.
- **Incident Management:** Automatic pausing and alerting when failure rates exceed thresholds.
