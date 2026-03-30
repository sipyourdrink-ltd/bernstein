# E44 — Linear Webhook Integration

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Teams using Linear must manually create Bernstein tasks from Linear issues, leading to duplicated work and status drift between systems.

## Solution
- Create a Linear webhook receiver at `integrations/linear-webhook/`.
- Listen for `issue.created` and `issue.updated` events via HTTP POST.
- Auto-create Bernstein tasks from Linear issues that have the `bernstein` label.
- Map Linear issue fields (title, description, priority) to Bernstein task fields.
- Implement bidirectional status sync: update Linear issue status when Bernstein task completes (In Progress, Done).
- Use Linear's GraphQL API for status updates back to Linear.

## Acceptance
- [ ] Linear issues with the `bernstein` label auto-create Bernstein tasks
- [ ] Bernstein task completion updates the Linear issue status
- [ ] Webhook validates Linear's webhook signature
- [ ] Issues without the `bernstein` label are ignored
