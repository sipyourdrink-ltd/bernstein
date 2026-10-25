# Cooperative cancellation integration

`core/agents/stop_reaper.py` advances opt-in task stop requests once per
orchestrator tick. The task store owns durable `stop_req` state and held claims;
the reaper owns the in-memory escalation registry and observed worker shutdown.
The existing authenticated cancellation route exposes request/confirm modes;
immediate cancellation remains the default.

Spawn and resume record independently led local process groups. The reaper
writes a shutdown notice, applies the bounded escalation policy, observes worker
termination through the spawner, finalizes cleanup, then confirms cancellation.
A failed or refused observation keeps the task pending. Heartbeat state is an
atomic, throttled diagnostic projection, not a durable reaper registry.

`SUSPENDED` mailbox waits and `STOP_REQ` both hold claims, with separate existing
transition paths. Store mutations validate lifecycle edges before removing index
entries so rejected worker actions cannot hide pending tasks from subsequent
ticks. Validation emits no events and writes no persistence; the existing
transition function remains responsible for those effects.

See [the operator workflow](../operations/cooperative-cancellation.md). POSIX
process regressions use owned groups and the actual tick; Windows runtime support
is not established by the platform stubs.
