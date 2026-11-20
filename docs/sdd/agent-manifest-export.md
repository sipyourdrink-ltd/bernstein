# Agent Manifest composition export boundary

`core/security/agent_manifest_export.py` owns declarations, upstream schema
validation, prompt hashing, custody signing, availability companion binding
and fresh per-hop storage. The SDK is imported only by the export operation;
`agent-manifest==0.12.0` is an optional, hash-locked dependency.

`AgentSpawner.agent_manifest_exporter` is an optional callback. It receives
the session ID and composed prompt before backend launch, including on
resume. It returns only existing artifact-reference dictionaries; no upstream
manifest type enters the scheduler. `AgentSession.agent_manifest_references`
holds those references until `Orchestrator._record_spawned_events` appends
hop-scoped `artifact_produced` events. The existing Trust Record exporter
projects them without adding another reference relation or authority claim.

The current binding is deliberately the spawner composition, not a claim
about the complete adapter runtime. Nine other upstream slots stay explicitly
unbound. Root times, IDs, version, classification, key type and unavailable
reasons are operator declarations. The signed availability companion binds
the complete signed manifest digest and the exact unbound list; it does not
change the upstream schema or verifier verdict.

[Operator contract and slot mapping](../operations/agent-manifest.md).
