## Trigger rule governance for Slack Events API and GitHub `workflow_run`

`POST /webhooks/slack/events` and GitHub `workflow_run/completed` deliveries on
`POST /webhooks/github` now pass through `.sdd/config/triggers.yaml` governance
via `TriggerManager.gate_direct_task` before creating tasks (Refs #4545).

- Matching `source: slack` and `source: github_workflow_run` rules enforce
  their filters (`channels`, `workflow_names`, `conclusion`, etc.), cooldown,
  and deduplication conditions, recording fires under the matched trigger
  source (`slack` or `github_workflow_run`) only after task creation succeeds.
- When `.sdd/config/triggers.yaml` is absent or contains no matching rule for
  the delivery, both endpoints remain transparent pass-throughs with unchanged
  responses and task payloads.
- A corrupt or unreadable `.sdd/config/triggers.yaml` fails closed and
  suppresses task creation instead of bypassing trigger governance.
