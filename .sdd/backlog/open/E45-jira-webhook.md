# E45 — Jira Webhook Integration

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Teams using Jira must manually translate Jira issues into Bernstein tasks and keep statuses in sync across both systems.

## Solution
- Create a Jira webhook receiver at `integrations/jira-webhook/`.
- Listen for `jira:issue_created` and `jira:issue_updated` webhook events.
- Map Jira issue fields (summary, description, priority, assignee) to Bernstein task fields.
- Sync Bernstein task status back to Jira using the Jira REST API v3 (transition issues to In Progress, Done).
- Authenticate with Jira via API token (basic auth) or OAuth 2.0.
- Filter by Jira project key or label to control which issues trigger Bernstein tasks.

## Acceptance
- [ ] Jira issues matching the configured filter create Bernstein tasks
- [ ] Bernstein task status changes are reflected in Jira issue transitions
- [ ] Webhook authenticates requests using Jira's webhook secret
- [ ] Non-matching Jira issues are ignored without errors
