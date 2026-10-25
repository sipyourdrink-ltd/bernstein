# Cooperative task cancellation

`POST /tasks/{task_id}/cancel?stop_mode=request` with the existing cancellation
reason body requests a cooperative stop. Claimed or running tasks enter
`stop_req`; unstarted descendants cancel immediately. The default remains
`stop_mode=immediate`. The existing task-access checks apply to every mode.

A running orchestrator processes stop requests once per tick. It writes a shutdown
notice, waits 5 seconds, sends INT, waits 10 seconds, sends TERM, waits 15 seconds,
and sends KILL. KILL verification retries are bounded to three attempts, spaced
5 seconds apart. Grace periods count from observed stage changes; a slow tick can
extend the wall-clock duration. Independent local process-group leaders are
recorded at both spawn sites; unsafe groups are refused. Other runtimes use the
existing spawner stop path and liveness observation.

The claim remains held while a group is alive or termination is unverified.
After observed termination and cleanup, the orchestrator calls the existing
cancel route with `stop_mode=confirm`, transitions the task to `cancelled`, and
records one claim-release receipt. Rejected completion/failure/requeue attempts
must preserve the pending task index. Unrelated mailbox `suspended` tasks retain
their existing claims and transitions.

`GET /tasks/counts` includes `stop_req`. Runtime heartbeat state is written
atomically to `.sdd/runtime/stop_reaper.heartbeat.json`; repeated unchanged writes
are throttled. Refused and unverified stops remain pending and are logged.

A server without a running orchestrator leaves stop requests pending. The reaper
registry is in memory; durable restart recovery, harness lanes, tuning YAML,
UI colours and protocol maps remain follow-ups. Real process regressions cover
POSIX; Windows branches are tested with platform stubs only.

In-process runs keep their actual thread handle during cooperative shutdown.
A thread that has not exited remains `stop_req` and retains its claim even when
it ignores the shutdown notice. No forced Python-thread termination is implied.
Synthetic thread identifiers are not probed as OS PIDs or process groups;
external process resume is still observed through its actual process handle.
