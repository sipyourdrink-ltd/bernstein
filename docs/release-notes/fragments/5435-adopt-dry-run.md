## `bernstein adopt` detects the running agent without side effects

`bernstein adopt` now detects the agent session it runs under from the process
ancestry and home-directory markers, and reports the result in dry-run mode
before writing anything. Only interpreter-launched processes (`python`, `node`,
`bun`, `deno`, `ruby`, `uv`, `npx`) are matched on their script argument, so an
editor or pager that merely has an agent name in its argv is not mistaken for a
live session. (#5435)
