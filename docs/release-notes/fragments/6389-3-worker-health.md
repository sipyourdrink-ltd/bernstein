## Worker health telemetry

Worker heartbeats accept optional disk, memory and mesh latency readings.
The registry uses them for direct node selection, task stealing and
`bernstein worker` pulls. Orchestrator claims by task ID remain open in
[#7853](https://github.com/sipyourdrink-ltd/bernstein/issues/7853).
