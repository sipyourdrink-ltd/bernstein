## A killed agent no longer blocks its own retry with a file lock

`spawner.kill()` marks a session dead itself, so the dead-agent path that
releases file locks never ran for a killed agent (idle recycling, heartbeat
and wall-clock timeouts, kill signals). Locks persisted by an earlier
orchestrator process named sessions the new process never tracked. In both
cases the retry of the task saw its own files listed as edited by another
agent and stopped, until the two-hour lock TTL expired. Each orchestrator tick
now releases every lock that no live session holds (#6536).
