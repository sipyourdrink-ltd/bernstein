## `trace export` now accepts orchestrator-written runs

`bernstein trace export` refused every run the orchestrator wrote because the
emitter and the recorder had been specified independently: the emitter read
`model_id` / `model_provider` / `gate_config`, while the orchestrator journaled
`model` / `provider` and no gate configuration. Hand-built journals and the
committed vectors carried the emitter's names, so no test caught the mismatch.

The orchestrator now journals the resolved facts additively under the emitter's
names: `model_id` and `model_provider` on `agent_spawned` (the existing `model`
and `provider` keys stay), and the resolved gate configuration as `gate_config`
once at run start. A missing fact still refuses, and an endpoint-routed worker
with no provider still refuses rather than inventing a vendor name.
