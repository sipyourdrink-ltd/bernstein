## `--port` reaches the agents, not just the server

`bernstein run --port N` started the server on N but never told the agents, so
the completion instructions in every prompt, and the Claude adapter's own hook
URLs, still named `127.0.0.1:8052`. On a machine running more than one workspace
that is a different run's server: it answered 401, and the log scanner turned
that into a failed task whose work had already merged. The orchestrator now
publishes its own base URL into the agent environment, the task-server
resolution falls back to the run's recorded port before the historical default,
and the Claude adapter derives its hook URL from the same value instead of a
parameter default nothing passed (#2808).
