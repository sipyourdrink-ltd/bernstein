# Task server URLs in agent prompts

The spawn renderers supply `SERVER_URL` using `BERNSTEIN_SERVER_URL`, then
the run's `.sdd/runtime/server.port`, then the default loopback port 8052.
The manager system prompt and shared completion contract use that resolved
URL for their task-server commands.

Role `task_prompt.md` files also use `{{SERVER_URL}}` for failure reporting,
bulletins and task creation. They are read by template compression tooling;
the runtime spawner currently loads `system_prompt.md`, not `task_prompt.md`.
Custom callers rendering task prompts with
`bernstein.templates.renderer.render_template` must supply `SERVER_URL`
alongside the task context.

The `progress-report.md` and `team-awareness.md` partials under
`templates/prompts/` use the same double-brace syntax. Custom callers must
supply `SERVER_URL` and the relevant `TASK_ID`, `SESSION_ID`, `AGENT_ID` and
`BULLETIN_SUMMARY` values. These partials have no dedicated runtime renderer;
seeding the prompt registry copies their content without rendering it.

Skill references are loaded verbatim. The manager's `task-api.md` therefore
instructs the agent to use the base URL in its system prompt and set the
shell variable `SERVER_URL` for its curl examples. It contains no URL
template placeholder requiring an additional rendering pass.
