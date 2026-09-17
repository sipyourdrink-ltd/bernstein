## Spawn prompts name the server this run actually started

Every curl example a spawned agent is handed -- task completion, bulletin posts,
and the agent-to-agent channel -- had `http://127.0.0.1:8052` written into it. A
run on a dynamically allocated port therefore told its agent to POST to a port
nothing was listening on, or to another run's server, which answers and rejects.
The agent cannot work this out for itself: it runs in a worktree whose `.sdd`
carries no port file. The prompt now renders the URL the spawner resolves --
`BERNSTEIN_SERVER_URL`, then the run's own `server.port`, then 8052 (#5964).
