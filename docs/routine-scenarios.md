# Routine <-> Scenario Bridge

The Bernstein scenario library and Anthropic's Claude Code Routines complement
each other: scenarios are version-controlled YAML recipes for multi-agent work,
Routines are cloud-side triggers that fire prompts on schedules or GitHub
events. The bridge wires them together so a team lead can author one
scenario in the repo and an operator can stand up the matching Routine in
about five minutes.

The subcommands live under `bernstein schedule routine`. The older top-level
`bernstein routine <sub>` spelling remains registered as a deprecated alias for
the whole 3.x line — same subcommands, same flags, a warning on stderr — and is
unregistered in 4.0.0.

## Two directions

### Direction A - Scenario to Routine (export)

`bernstein schedule routine export <scenario-id> --repo owner/name --output ./out` writes:

```
out/
  prompt.md          paste into the Routine prompt field
  mcp-config.json    add as an MCP connector
  env.json           env vars the Routine session needs
  setup-guide.md     step-by-step instructions
  triggers.md        recommended trigger configurations
```

The prompt instructs the Routine session to call `bernstein_scenario`,
poll status, and summarise outcomes (including PR comments when a GitHub
trigger supplied a pull request number).

`bernstein schedule routine provision` is an interactive wrapper around the same
flow that also registers the trigger id once the operator pastes it back.

### Direction B - Routine to Scenario (invoke)

The MCP server exposes three tools:

| Tool | Purpose |
| --- | --- |
| `bernstein_scenario(action="list")` | List scenarios known to the local library. |
| `bernstein_scenario(action="run", scenario_id, context, pr_number, branch)` | Spawn one task per scenario template; returns the same pollable handle shape as `bernstein_run`. |
| `bernstein_scenario(action="status", orchestration_id=...)` | Aggregate status of a running scenario. |

A scenario holds reusable task templates. Each invocation creates a
separate executable task from each template; the template itself is not
the running task.

Each spawned task carries `metadata.scenario_id` and
`metadata.orchestration_id` so the status tool can group them.

## Provisioning flow

```
team lead          bernstein           Anthropic Routines
---------          ---------           -----------------
defines    ---->   exports    ---->   trigger fires on
scenario           prompt +           GitHub event
.yaml              mcp config         ----v-----------
                                          calls
                                          bernstein_scenario()
                                          ----v-----------
bernstein orchestrates parallel
multi-agent work in worktrees
```

Scenarios live in `templates/scenarios/` (packaged into the wheel under
`bernstein/_default_templates/scenarios/`) with an extended repository library
in `.bernstein/scenarios/`. The packaged wheel library ships **24** ready-to-use
templates covering:

- PR review (`pr-review-comprehensive`, `pr-review-security-focused`)
- AI governance and MCP security (`owasp-agentic-threat-review`, `eu-ai-act-iso42001-readiness`, `mcp-server-security-vetting`)
- Security and supply chain (`cve-zero-day-hotfix`, `dependency-audit`, `supply-chain-hardening`, `vendor-security-assessment`)
- Reliability, FinOps, and migrations (`zero-downtime-db-migration`, `disaster-recovery-failover-drill`, `incident-postmortem-pack`, `release-readiness-audit`, `deploy-verification`, `ai-token-and-cloud-cost-audit`, `nightly-maintenance`)
- Quality, docs, research, and compliance (`flaky-test-quarantine-fix`, `test-coverage-boost`, `issue-decomposition`, `customer-escalation-rca`, `docs-sync`, `rfc-drafting`, `market-scan`, `regulator-audit-evidence-pack`)

## Auto-provisioning

Trigger ids resolved through `bernstein schedule routine register --scenario <id>
--trigger-id <tid> --repo owner/name` are persisted under
`.sdd/routines/registry.json`. When the Routine webhook arrives at the
Bernstein server with the matching trigger id, the bridge looks up the
binding and invokes the scenario without operator intervention.

`bernstein schedule routine bindings` lists all registered bindings.

## See also

- `src/bernstein/core/planning/scenario_library.py` - scenario data model.
- `src/bernstein/core/planning/routine_provisioner.py` - Direction A export.
- `src/bernstein/core/planning/routine_bridge.py` - bidirectional bridge.
- `src/bernstein/mcp/routine_tools.py` - MCP tool registration.
- `src/bernstein/cli/commands/routine_cmd.py` - CLI surface.
