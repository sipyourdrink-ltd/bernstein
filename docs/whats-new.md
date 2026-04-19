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
