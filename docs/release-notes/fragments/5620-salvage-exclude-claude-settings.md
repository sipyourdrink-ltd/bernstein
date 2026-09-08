## Exclude Claude settings.local.json from salvage staging

The Claude CLI adapter creates `<worktree>/.claude/settings.local.json` before
process launch to declare permissions. In target repositories that do not ignore
`.claude/`, salvage worktree's `git add -A` staged this orchestrator-written
configuration file into `[WIP]` commits, polluting run histories and integration
diffs. `_derive_local_exclude_entries` and `RUN_EXCLUDE_ENTRIES` now include
`/.claude/settings.local.json` alongside `.claude/mcp.json` and
`.claude/scheduled_tasks.json`, ensuring orchestrator adapter settings are never
staged into target repository deliverables (#5620).
