---
title: What's New
description: >-
  User-facing summary of the major features that landed in Bernstein 1.9.x —
  OpenAI Agents SDK v2 adapter, pluggable sandbox backends, cloud artifact
  storage sinks, and progressive-disclosure skill packs.
---

# What's New in 1.9.x

Bernstein 1.9 collects four feature tracks that landed as tickets
`oai-001` through `oai-004`. This page summarises them in terms of what
changes for someone upgrading from 1.8.x. Full detail lives in the
dedicated architecture pages and the
[CHANGELOG](CHANGELOG.md).

## OpenAI Agents SDK v2 adapter (`openai_agents`) — oai-001

Bernstein now ships a first-class adapter for the
[OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/).
It wraps `agents.Agent` + `Runner.run_sync` in a CLI-spawnable
subprocess so the existing Bernstein spawner can manage lifecycle,
timeouts, rate-limit back-off, and cost tracking the same way it does
for every other coding agent.

```bash
pip install 'bernstein[openai]'
```

```yaml
# plan.yaml
steps:
  - title: "Add unit tests"
    role: qa
    cli: openai_agents
    model: gpt-5-mini
```

- Structured JSONL event stream (`start`, `tool_call`, `tool_result`,
  `usage`, `completion`).
- MCP bridging — Bernstein-managed MCP servers are forwarded into the
  SDK's `RunConfig` so tool calls show up in the central audit log.
- Rate-limit handling maps SDK exception classes onto
  `COST.rate_limit_cooldown_s`.
- Pricing rows for `gpt-5`, `gpt-5-mini`, and `o4` land alongside the
  adapter.

See the [adapter reference](adapters/openai-agents.md) and the
[decision guide](compare/openai-agents.md) for when to pick
`openai_agents` vs `codex` vs `claude`.

## Pluggable sandbox backends — oai-002

Every spawned agent now runs inside a `SandboxSession`. Four
first-party backends ship:

| Backend    | Extra             | Notes                                                                 |
| ---------- | ----------------- | --------------------------------------------------------------------- |
| `worktree` | core              | Local git worktree. Zero overhead. Default.                            |
| `docker`   | `bernstein[docker]` | Per-session container; cgroup + namespace isolation.                  |
| `e2b`      | `bernstein[e2b]`    | E2B Firecracker microVMs. Supports `SNAPSHOT`.                        |
| `modal`    | `bernstein[modal]`  | Modal serverless containers. Supports `SNAPSHOT` and optional `GPU`. |

```yaml
# plan.yaml
stages:
  - name: risky-execution
    sandbox:
      backend: docker
      options:
        image: python:3.13-slim
        memory_mb: 2048
    steps:
      - title: "Run untrusted code analysis"
        role: security
        cli: claude
```

```bash
bernstein agents sandbox-backends   # list every installed backend
```

Third parties register backends through the `bernstein.sandbox_backends`
entry-point group.

Full detail:
[architecture/sandbox.md](architecture/sandbox.md).

## Cloud artifact storage sinks — oai-003

`.sdd/` persistence (WAL, audit log, task outputs, cost ledger) now
decouples from the local filesystem via an async `ArtifactSink`
protocol. First-party sinks:

| Sink         | Extra               | Provider SDK                  |
| ------------ | ------------------- | ----------------------------- |
| `local_fs`   | core (always on)    | stdlib                        |
| `s3`         | `bernstein[s3]`    | `boto3`                       |
| `gcs`        | `bernstein[gcs]`   | `google-cloud-storage`        |
| `azure_blob` | `bernstein[azure]` | `azure-storage-blob`          |
| `r2`         | `bernstein[r2]`    | `boto3` (R2 is S3-compatible) |

The `BufferedSink` wrapper preserves the WAL crash-safety contract by
fsyncing the local write first and mirroring to the remote
asynchronously, so synchronous write latency stays bounded by local
disk.

Full detail:
[architecture/storage.md](architecture/storage.md).

## Progressive-disclosure skill packs — oai-004

Role prompts migrated from monolithic `templates/roles/<role>/system_prompt.md`
files to OpenAI Agents SDK-shaped **skill packs** under
`templates/skills/<role>/`. Every spawn's system prompt now receives
only a compact skill *index*; agents pull full bodies on demand
through the `load_skill` MCP tool.

Net effect: fewer tokens on every spawn, retry, and fork.

```bash
bernstein skills list             # compact table of every skill
bernstein skills show backend     # print SKILL.md body
bernstein skills show backend --reference python-conventions.md
```

Plugin authors can ship additional skill packs via the
`bernstein.skill_sources` entry-point group. 17 built-in role packs
(backend, qa, security, frontend, devops, architect, docs, retrieval,
ml-engineer, reviewer, manager, vp, prompt-engineer, visionary,
analyst, resolver, ci-fixer) are migrated — the legacy
`templates/roles/` tree remains on disk for backwards compat for two
more minor versions.

Full detail: [architecture/skills.md](architecture/skills.md).

## Installing the new extras

All four features are additive — `pip install bernstein` continues to
pull a minimal core. Combine extras to opt into just what you use:

```bash
# OpenAI Agents adapter + Docker sandbox + S3 artifact sink
pip install 'bernstein[openai,docker,s3]'

# Everything
pip install 'bernstein[openai,docker,e2b,modal,s3,gcs,azure,r2]'
```

See the [install section in the README](https://github.com/chernistry/bernstein#install)
for the full extras matrix.

## ACP native bridge — `bernstein acp serve`

Bernstein speaks [Agent Client Protocol](https://agentclientprotocol.org)
natively. Editors that ship ACP support can plug Bernstein in as their backend
with no per-IDE plumbing.

```bash
bernstein acp serve --stdio          # IDE embedding (Zed, etc.)
bernstein acp serve --http :8062     # remote / CI / debugging
```

Full detail: [reference/acp-bridge.md](reference/acp-bridge.md).

## Autofix CI daemon — `bernstein autofix`

A long-running daemon that watches Bernstein-opened PRs, pulls failing CI logs
via `gh run view --log-failed`, and dispatches a scoped repair run. Each
attempt is HMAC-audited and label-gated; the daemon stops after three
consecutive failures on the same PR.

```bash
bernstein autofix start              # start the daemon
bernstein autofix status             # show watched PRs and attempt counts
bernstein autofix attach             # tail the daemon log live
bernstein autofix stop               # graceful stop
```

## Credential vault — `bernstein connect`

API tokens now live in the OS keychain, not in `.env` files. `bernstein
connect <provider>` runs the OAuth / API-key flow for the named provider and
stores the result securely. Agents receive scoped credentials at spawn time
via `core/security/vault/`.

```bash
bernstein connect github             # OAuth flow for GitHub
bernstein connect openai             # store OpenAI API key
bernstein creds list                 # show all stored credentials
bernstein creds test github          # smoke-test stored credential
bernstein creds revoke github        # delete from keychain
```

This is now the recommended first step before `bernstein init` when you are
setting up Bernstein for the first time with external providers.

## Dev preview — `bernstein preview`

After an agent runs a dev server, `bernstein preview start` captures the bound
port, tunnels it, and returns a shareable HTTPS link with configurable expiry
and auth mode.

```bash
bernstein preview start              # expose the running dev server
bernstein preview list               # list active previews with URLs
bernstein preview status             # check tunnel health
bernstein preview stop               # tear down all tunnels
```

## Fleet dashboard — `bernstein fleet`

A cross-session view of all Bernstein instances running on the same host (or
reachable via the configured server URL). Useful for teams running parallel
sessions on a shared development machine or CI cluster.

```bash
bernstein fleet                      # TUI fleet view
bernstein fleet --web localhost:9000 # browser fleet dashboard
```

## MCP catalog client — `bernstein mcp catalog`

Browse and install MCP servers from the community catalog without leaving the
terminal. The catalog schema lives at
[`docs/reference/mcp-catalog-schema.json`](reference/mcp-catalog-schema.json).

```bash
bernstein mcp catalog browse         # paginated catalog listing
bernstein mcp catalog search pytest  # search by name/tag
bernstein mcp catalog install <name> # install and register a server
```

## Notification sinks — `bernstein notify`

Bernstein events (task complete, budget threshold, quality gate failure) can
now fan out to pluggable notification sinks — Slack, email, webhooks, and more.
Configure sinks in `.sdd/config.yaml` under `notifications:`.

```bash
bernstein notify test --sink slack   # send a test notification to a named sink
```

## Plan archival — `bernstein plan ls/show`

Completed plan runs are now archived and inspectable after the fact.

```bash
bernstein plan ls                    # list all plan runs with status and cost
bernstein plan show <id>             # show task breakdown for a specific run
```

## PR review responder — `bernstein review-responder`

A persistent daemon that monitors open PRs for new review comments and
auto-dispatches an agent to address each comment, committing a follow-up and
re-requesting review.

```bash
bernstein review-responder start     # start the daemon
bernstein review-responder status    # show watched PRs and response queue
bernstein review-responder tick      # manual single-pass (useful in CI)
```

## Review pipeline DSL — `bernstein review --pipeline`

Quality-review flows can now be expressed as YAML pipelines. Starter templates
live in `templates/review/*.yaml`.

```bash
bernstein review --pipeline review.yaml  # run the review pipeline
```

Example pipeline fragment:

```yaml
# review.yaml
phases:
  - name: lint
    adapter: ruff
  - name: type-check
    adapter: pyright
  - name: security
    adapter: bandit
    fail_fast: true
```

---

## Upgrade notes

- **No breaking changes.** Existing `plan.yaml` and `bernstein.yaml`
  files keep working unchanged. The new `sandbox:` block on a stage is
  entirely optional; when omitted, behaviour is byte-identical to
  1.8.x.
- **Default sandbox is still `worktree`.** You must opt in to Docker /
  E2B / Modal explicitly.
- **Default artifact sink is still `local_fs`.** Remote sinks need the
  matching extra plus explicit config.
- **Legacy `templates/roles/` still loads.** Skill packs are preferred
  when both are present for the same role.
