# MCP tool tiers (context-budget knob)

Bernstein exposes its orchestration layer as MCP tools so any MCP client
(Cursor, Claude Code, Cline, Windsurf, and others) can drive multi-agent
work. Every advertised tool costs context tokens on every turn, whether or
not the agent calls it. Tool tiers let an operator cap that budget with a
single knob, trading capability for context.

## The three tiers

Tiers are named and cumulative: `core` is a subset of `standard`, which is a
subset of `all`. Selecting a tier advertises that tier's tools and only that
tier's tools. Out-of-tier tools are neither listed in `tools/list` nor
callable.

| Tier | Budget | Tools advertised | Use when |
|------|--------|------------------|----------|
| `core` | smallest | `bernstein_health`, `bernstein_run`, `bernstein_status`, `bernstein_tasks`, `bernstein_task_handle` | Cost-sensitive runs or small-context adapters; you only need to start and observe a run. |
| `standard` (default) | medium | core plus `bernstein_cost`, `bernstein_stop`, `bernstein_approve`, `bernstein_create_subtask`, `bernstein_claim`, `bernstein_update`, `bernstein_post_artifact`, `bernstein_context`, `load_skill` | The typical run: mutation, approval, the pull-worker claim/update verbs, artifact posting, the signed context capsule, and skill loading. |
| `all` | largest | standard plus the scenario bridge (`bernstein_scenarios`, `bernstein_scenario`, `bernstein_scenario_status`) and `verify_chain` | Power-user setups that drive scenario libraries or audit lineage. |

The exact membership is declared once in
`src/bernstein/core/protocols/mcp/tool_tiers.py` (`TOOL_TIERS`). Adding a new
tool sets its tier at that declaration; there is no separate runtime
registry to keep in sync.

A tool with no `TOOL_TIERS` entry falls back to the `all` tier, so omitting
the declaration removes the tool from `tools/list` at the default `standard`
tier. That omission is a test failure, not a silent drop: a coverage test
compares every tool the MCP server registers against `TOOL_TIERS` and fails
on the first undeclared name.

## Selecting a tier

Resolution order, first match wins:

1. `--mcp-tier <tier>` flag on `bernstein mcp` (applies to that server process).
2. `BERNSTEIN_MCP_TOOL_TIER` environment variable.
3. The `standard` default.

```bash
# Run the MCP server with the smallest tool surface.
bernstein mcp --mcp-tier core

# Or set it once for the shell via the environment.
export BERNSTEIN_MCP_TOOL_TIER=core
bernstein mcp
```

An unknown tier value is rejected with a clear error rather than silently
falling back, so a typo never quietly changes the exposed surface.

## Auditing before you switch

Use `bernstein mcp tools` to see exactly what each tier would advertise
before changing the knob:

```bash
# Audit every tier.
bernstein mcp tools

# Inspect a single tier.
bernstein mcp tools --tier core

# Machine-readable output for scripts.
bernstein mcp tools --tier all --json-output
```

## Budget-vs-capability trade

- Dropping from `all` to `standard` removes the scenario bridge and the
  lineage verifier. Pick `standard` for everyday orchestration where you do
  not invoke scenario recipes from the agent.
- Dropping from `standard` to `core` additionally removes cost reporting,
  graceful stop, approval, subtask creation, and skill loading. Pick `core`
  for the leanest surface on small-context adapters or when only the
  start/observe loop matters.
- The fewer tools advertised, the fewer tokens spent describing them on
  every turn. The trade is direct: smaller tier, smaller budget, fewer
  capabilities reachable without switching.

## Out of scope

- Per-user tier policy. The tier is a single global setting per process.
- Dynamic tier promotion during a run. The tier is fixed when the server
  starts.
