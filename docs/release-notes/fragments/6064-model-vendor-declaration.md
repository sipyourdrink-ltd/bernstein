## `trace export` names the model vendor, not the CLI adapter

An exported Trust Record previously wrote the CLI adapter identifier
(`claude`, `codex`) into `model.provider`. Adapters now declare the vendor
of the models they front, and the orchestrator journals that declaration as
`model_provider`. An adapter that fronts several vendors, a gateway, or
nothing it can name declares no vendor; that hop journals no
`model_provider` key and export refuses it with the agent id, matching the
existing endpoint-routed-worker behaviour.
