## RunActor refuses terminal-status transitions instead of silently rewinding

The `RunActor` state reducer previously applied `session_started` unconditionally, so a
late or replayed event could move a run that had already reached `done` or `failed` back to
`running`. It now refuses any transition that would leave or move between terminal statuses
(`running` → `running`, `done`/`failed` → `running`, and `done`/`failed` → `done`/`failed`),
leaving the run in its terminal state and journaling the refusal as a governance event.
Terminal status is now a durable guarantee for operators and subscribers regardless of what
late events arrive (#5088).
