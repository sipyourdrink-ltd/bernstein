## Spawn prompts and the manager's prompt name the server this run started

A spawned agent's completion POST, its bulletin and channel curls, and the
manager's task-creation curls all had `http://127.0.0.1:8052` written into
them. A run on a dynamically allocated port therefore told its agents to POST
to a port nothing was listening on, or to another run's server, which answers
and rejects. The agent cannot work this out for itself: it runs in a worktree
whose `.sdd` carries no port file.

The completion contract include and the manager's system prompt now take
`{{SERVER_URL}}`, and both prompt renderers fill it with the URL the spawner
resolves: `BERNSTEIN_SERVER_URL`, then the run's own `server.port`, then 8052.
A rendered prompt names exactly one server (#5964).

Role `task_prompt.md` files, two prompt partials and the manager's task-API
skill reference still carry the literal; they are tracked in #6299.
