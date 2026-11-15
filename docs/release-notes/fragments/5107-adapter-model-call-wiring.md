## Adapter invocations, agent spawns, and cost rows wired into ModelCallLedger

- `CLIAdapter` now declares `parameter_schema`, `parameter_schema_version`,
  `model_version`, `validate_parameters()`, `model_version_for()`, and
  `invoke_recorded()` so adapters validate resolved call parameters against their
  versioned schema before running.
- `ModelCallLedger` adds `begin_call()`, `finish_call()`, `finalize_call()`,
  `invoke_adapter()`, and `replay_adapter()`, updating in-memory records by id so
  append-only `running` and terminal `succeeded`/`failed` rows resolve cleanly to a
  single current record per call.
- `AgentSpawner`, `AgentSession.model_call_id`, `reap_completed_agent()`, and
  `agent_lifecycle` record spawned agent invocations into
  `.sdd/runtime/model_calls.jsonl` and pass `model_call_id` into
  `price_model_usage()`.
- `bernstein cost model-call invoke|replay` execute registered adapters implementing
  direct `CLIAdapter.invoke()` (while continuing to fail closed for spawn-only or
  unregistered adapters), and add hidden `list` and `show` subcommands. Refs #5107.
