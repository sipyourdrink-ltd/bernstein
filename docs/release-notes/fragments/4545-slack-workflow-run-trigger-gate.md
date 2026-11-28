## Trigger rule governance for Slack Events API and GitHub `workflow_run`

`POST /webhooks/slack/events` and GitHub `workflow_run/completed` deliveries on
`POST /webhooks/github` now pass through `.sdd/config/triggers.yaml` governance
via `TriggerManager.gate_direct_task` before creating tasks (Refs #4545).

- Matching `source: slack` and `source: github_workflow_run` rules enforce
  their filters (`channels`, `mention_required`, `workflow_names`,
  `conclusion`, etc.), tenant-scoped cooldown, and per-delivery deduplication
  (`event_id` / `(team_id, channel, slack_ts)` for Slack; `(repo,
  workflow_run.id, run_attempt)` for GitHub `workflow_run`), recording allowed
  (`produced=True`) and confirmed suppressed (`produced=False`) outcomes under
  the matched trigger source (`slack` or `github_workflow_run`).
- When `.sdd/config/triggers.yaml` is absent or contains no matching rule for
  the delivery, both endpoints remain transparent pass-throughs with unchanged
  responses and task payloads.
- A corrupt, unreadable, or quarantined `.sdd/config/triggers.yaml` (as well
  as a retry of an unconfirmed deduplication reservation) fails closed with
  HTTP `503` (keeping Slack `url_verification` at `200`) so webhooks retry
  instead of dropping events; a failed `store.create` releases its reservation
  so the next retry creates the task.
