# E30 — Official Plugin Pack

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Users need integrations with common project management and communication tools but there are no official plugins to serve as references or production-ready connectors.

## Solution
- Create six official plugins, each as a minimal Python package: `bernstein-jira`, `bernstein-linear`, `bernstein-slack`, `bernstein-github`, `bernstein-gitlab`, `bernstein-pagerduty`.
- Each plugin includes: `pyproject.toml`, webhook handler module, configuration schema, and README with setup instructions.
- Webhook handlers follow a common interface: `handle_event(event_type, payload) -> Action`.
- Register all six in the plugin registry JSON.

## Acceptance
- [ ] Each of the six plugins is a valid installable Python package
- [ ] Each plugin implements the `handle_event` webhook handler interface
- [ ] Each plugin has a README documenting required environment variables and setup
- [ ] All six plugins are listed in the plugin registry
