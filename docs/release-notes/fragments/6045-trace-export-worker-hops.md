## `trace export` now emits one record per worker hop, plus an aggregate

A run that spawned more than one worker previously exported only a single
record, even though the journal holds one `agent_spawned` event per worker.
The emitter now splits such a run into one Trust Record per hop (`exec_id`
set to that spawn event's `agent_id`, in spawn order) and folds them into
the existing run-level aggregate. A hop's `model` comes from its own
`agent_spawned` event and a hop with no `tool_call` evidence omits
`tool_transcript` rather than reporting a false zero. The aggregate carries
`tool_transcript` only when every member does. Journals with no
`agent_spawned` event keep the prior single-record behaviour.
