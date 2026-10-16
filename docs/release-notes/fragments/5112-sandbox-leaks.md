## `bernstein sandbox leaks` lists sandbox sessions that outlived their task

A per-spawn sandbox session whose exec never resolved, or whose backend
`destroy` raised, stayed up with nothing pointing at it. The spawner now
records the spawn's task ids on the `sandbox.session_create` audit event, and
`bernstein sandbox leaks` lists every session created without a matching
destroy whose owning tasks are all recorded terminal in the task log. Sessions
whose task is still running or unknown are reported with the reason they were
left out. The command is read-only (#5112).
