## Prompt sources follow the selected task server

Role task templates and the progress-report/team-awareness partials use
`{{SERVER_URL}}` rather than a fixed loopback endpoint. These reusable files
have no automatic agent prompt consumer today; registry seeding stores the
partials unchanged. The manager's raw task-API skill reference now tells the
agent to use the base URL from its system prompt, without exposing an
unrendered placeholder. Existing system-prompt URL resolution is unchanged.
