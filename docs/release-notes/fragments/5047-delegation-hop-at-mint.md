## Delegation receipts are recorded for every spawned agent, and a run that cannot record one does not spawn

A run now mints one parentless run-root identity at start and records its id in
the run manifest. Every top-level agent is minted under that root, and a nested
spawn under its spawning agent, so each mint records one delegation hop carrying
the agent's task and file scope on the receipt. `bernstein delegation verify
<run>` reads the root id from the manifest, anchors the chain on it, and returns
a populated chain with its hop count instead of "no receipts" -- offline, from
the receipts and the manifest alone, without reading the identity store.

The write is fail closed at the spawn: a hop that cannot be written raises
`DelegationWriteError`, the agent does not start, and a `spawn.refused_unreceipted`
audit event records the run, the session and the reason. Other identity failures
keep their previous behaviour and still do not block a spawn. (#5047)
