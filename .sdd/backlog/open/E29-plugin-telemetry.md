# E29 — Plugin Telemetry

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Plugin authors have no visibility into how their plugins are being used, making it hard to prioritize improvements or identify common failure modes.

## Solution
- Define an opt-in telemetry interface that plugins can implement by exposing a `telemetry_enabled = True` flag.
- Track three aggregate counters: install count, run count, error count.
- Send anonymous telemetry events to a simple API endpoint (POST with plugin name, event type, timestamp).
- Add a `GET /registry/stats` endpoint that returns aggregated stats per plugin.
- Respect a global `BERNSTEIN_NO_TELEMETRY=1` environment variable to disable all reporting.

## Acceptance
- [ ] Plugins with `telemetry_enabled = True` report install/run/error events
- [ ] Plugins without the flag or with it set to False send no telemetry
- [ ] `BERNSTEIN_NO_TELEMETRY=1` disables all telemetry regardless of plugin settings
- [ ] Aggregate stats endpoint returns correct counts per plugin
