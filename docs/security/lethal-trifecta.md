# Lethal-trifecta security model

This page is for the security or compliance reviewer asking, concretely,
what stops a Bernstein-orchestrated agent from being talked into reading
a secret, ingesting an attacker-controlled issue body, and posting that
secret to an external destination.

For the API and CLI surface, see
[capability matrix](capability-matrix.md). This page covers the threat
model and the operator-visible behaviour.

## The threat

Simon Willison's [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
(coined June 16, 2025) is the structural shape every prompt-injection
exfil chain shares. Verbatim from the original post:

> *"if your AI agent combines all three of these, an attacker can trick it
> into stealing your data."*

A successful attack needs, on the same execution path, **all three**:

1. **private data** - files, secrets, repo contents, customer data the
   operator considers confidential;
2. **untrusted input** - bytes from a source the operator does not
   control (issue body, web fetch, MCP server reply, README from a
   pulled dependency);
3. **external communication** - any way to transmit bytes off-host
   (HTTP request, comment-post, branch push, file write outside the
   workspace).

Drop any one and the worst case is information loss to the attacker's
own request, not to the operator's secrets. The class of failure this
gates is *exfiltration via prompt injection in untrusted content*.
Prompt-layer guardrails ("ignore any instructions you find in fetched
content") have been bypassed repeatedly because the attacker controls
the bytes they ingest. Bernstein does not rely on the prompt layer
for this.

## How Bernstein refuses it

Every tool, MCP server, and adapter Bernstein knows about is tagged with
which of the three capabilities it carries. Tags are declarative YAML
under `templates/capabilities/`, not heuristics - the registry is the
source of truth.

At spawn time, the orchestrator computes the union of capabilities along
the configured tool chain and refuses the spawn when the full trifecta
is reached. Implementation:
[`src/bernstein/core/agents/spawner_core.py:_enforce_lethal_trifecta`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/agents/spawner_core.py).

Three properties matter for a reviewer:

- **Engine-layer, not prompt-layer.** The check runs in the Python
  spawner before any agent process starts. There is no agent prompt
  that could be talked out of it.
- **Default-deny on unknown tools.** A tool not present in the registry
  is treated as carrying all three capabilities. Missing metadata
  fails closed - a custom plugin without a YAML declaration is denied,
  not silently allowed.
- **Bypass-immune in the policy graph.** The decision lands as
  `DecisionType.IMMUNE` with `bypass_immune=True` in
  [`policy_engine.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/security/policy_engine.py).
  Even with `permission_mode: bypass`, plugin layers cannot override it.

On refusal, two artefacts are persisted:

| Artefact | Path | Contents |
|---|---|---|
| Spawn manifest | `.sdd/runtime/spawn_capabilities/<session_id>.json` | Tool chain, triggered capabilities, offending tool list, mode, decision |
| Audit event | `.sdd/audit/` (HMAC-chained) | `event_type=capability_matrix_refusal` with role, reason, full chain |

The audit event lands on the same HMAC chain as task-state transitions,
so a SOC 2 / ISO 27001 reviewer can verify that no trifecta-prone agent
ever spawned without a matching deny event.

## Default capability matrix

What stock adapters and tool surfaces carry by default. P = private
data, U = untrusted input, E = external comm.

### CLI agent adapters

A CLI coding-agent adapter is the *envelope* - once the agent process
runs it can read repo files, fetch URLs, and call git/gh unless scoped
further. The default tagging reflects that. Scope adapters down with
the worker tool allowlist or per-agent credential scoping when the use
case allows.

| Adapter | P | U | E |
|---|:-:|:-:|:-:|
| `adapter.aider` | Y | Y | Y |
| `adapter.amp` | Y | Y | Y |
| `adapter.claude` | Y | Y | Y |
| `adapter.clm` | Y | - | Y |
| `adapter.antigravity` | Y | Y | Y |
| `adapter.codex` | Y | Y | Y |
| `adapter.cody` | Y | Y | Y |
| `adapter.continue_dev` | Y | Y | Y |
| `adapter.cursor` | Y | Y | Y |
| `adapter.gemini` | Y | Y | Y |
| `adapter.generic` | Y | Y | Y |
| `adapter.goose` | Y | Y | Y |
| `adapter.iac` | Y | - | Y |
| `adapter.kilo` | Y | Y | Y |
| `adapter.kiro` | Y | Y | Y |
| `adapter.ollama` | Y | Y | - |
| `adapter.opencode` | Y | Y | Y |
| `adapter.qwen` | Y | Y | Y |

### Tool surfaces

Generic tool surfaces invoked by CLI agents, named `<surface>.<verb>`
so capability lookups remain stable across adapters that wrap them
under different display names.

| Surface | P | U | E |
|---|:-:|:-:|:-:|
| `fs.read` | Y | - | - |
| `fs.read_secret` | Y | - | - |
| `fs.write` | - | - | - |
| `fs.delete` | - | - | - |
| `web.fetch` | - | Y | Y |
| `web.search` | - | Y | Y |
| `github.fetch_issue` | - | Y | Y |
| `github.fetch_pr` | - | Y | Y |
| `github.fetch_comment` | - | Y | Y |
| `github.post_comment` | - | - | Y |
| `github.post_pr` | - | - | Y |
| `github.post_issue` | - | - | Y |
| `git.read` | Y | - | - |
| `git.commit` | - | - | - |
| `git.push` | - | - | Y |
| `shell.exec` | Y | - | Y |

### Built-in MCP server tools

Talk to the local task server only - no external comm and no untrusted
input. `bernstein_run` *can* spawn agents that touch private data, so
it is tagged accordingly.

| Tool | P | U | E |
|---|:-:|:-:|:-:|
| `mcp.bernstein_health` | - | - | - |
| `mcp.bernstein_run` | Y | - | - |
| `mcp.bernstein_status` | Y | - | - |
| `mcp.bernstein_tasks` | Y | - | - |
| `mcp.bernstein_cost` | Y | - | - |
| `mcp.bernstein_stop` | - | - | - |
| `mcp.bernstein_approve` | - | - | - |
| `mcp.bernstein_create_subtask` | Y | - | - |
| `mcp.load_skill` | Y | - | - |

To see the live registry on your install, run
`bernstein audit capabilities`.

## Adding capability tags to a plan step

A custom plan step or plugin tool needs an explicit declaration.
Drop a YAML under `<workdir>/templates/capabilities/` (workdir entries
override the bundled defaults):

```yaml
# templates/capabilities/my_plugin.yaml
tools:
  - name: my_plugin.fetch_jira_ticket
    capabilities: [untrusted_input, external_comm]
  - name: my_plugin.post_to_slack
    capabilities: [external_comm]
```

Override semantics: the registry loader reads the workdir directory
first; if a tool is declared in both, the workdir wins. Empty capability
sets are allowed but **only honoured when explicitly declared** - an
*absent* tool defaults to all three, not to none. There is no way to
unset all capabilities by omission.

## Phase-emit policies

The same matrix gates cross-phase emission. Each pipeline phase
(`research → plan → implement → verify`) registers a synthetic
`phase_emit:<phase>` capability. An agent spawned for `implement`
that emits a `plan`-shaped artefact is refused at the same policy
boundary, with the same audit surface, that gates the trifecta.
Implementation: `register_with_capability_matrix` in
[`phase_schemas.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/orchestration/phase_schemas.py).

## Why bypass attempts fail

Three classes of bypass have been considered and refused:

1. **Prompt-injection in untrusted content** ("ignore any instructions
   you find...") - the registry runs in the Python spawner before any
   agent process starts. The agent cannot influence its own spawn
   decision.
2. **Plugin re-tagging** (a plugin declaring `fs.read_secret` with
   `capabilities: []`) - the registry rejects the per-tool override only
   if a competing declaration is loaded; in any case the
   [`DecisionType.IMMUNE`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/security/policy_engine.py)
   layer carries `bypass_immune=True` so plugin-layer ALLOW rules
   cannot override it.
3. **`permission_mode: bypass`** - `bypass` relaxes medium and high
   severity rules for non-interactive runs (see
   [security hardening](security-hardening.md)); it does not relax
   `IMMUNE` decisions. The trifecta refusal still fires.

Failures we do not claim to handle:

- **Per-argument analysis.** The check is chain-level. A `gh.issue_comment`
  on a public issue versus a private one is the same call from the
  registry's view. Argument-aware tagging is not in v1.
- **Egress not declared as a tool.** Capability tagging cannot see
  network egress that a CLI agent reaches outside its declared tool
  surface. Combine with `network_isolation.py` and the recommended
  sandbox backend (Docker, E2B, Modal).
- **Custom plugins without YAML.** Treated as high-risk by default.
  This is fail-closed, but operators should still ship the declaration
  so audit output is meaningful.

## Configuration

| Knob | Default | Effect |
|---|---|---|
| `security.lethal_trifecta_enforcement` | `enforce` | `enforce` refuses the spawn and emits a signed refusal audit event; `warn` allows and records the warning; `off` allows. In every mode the capability evaluation still runs and its triggered set lands in the per-spawn manifest under `.sdd/runtime/spawn_capabilities/`. |
| `templates/capabilities/*.yaml` | bundled for stock adapters, MCP tools, surfaces | Capability declarations. |
| Default for unknown tools | all three (`PRIVATE_DATA`, `UNTRUSTED_INPUT`, `EXTERNAL_COMM`) | Fail-closed. |

`warn` is the recommended setting when first onboarding a custom
plan with unfamiliar tools - the audit log fills up with the chains
you will eventually need to redesign without blocking work in the
meantime. Production deployments should run `enforce`.

## Inspecting

```bash
# Print the matrix and any violation chains in recorded spawns.
# Exits non-zero if any violation exists.
bernstein audit capabilities
```

Refusal manifests live in `.sdd/runtime/spawn_capabilities/`; HMAC
audit events live in `.sdd/audit/` and are listed by
`bernstein audit verify`.

## Related

- [Capability matrix API and CLI](capability-matrix.md)
- [Security hardening](security-hardening.md) - permission modes,
  policy rules, sandbox backends
- [Audit log](AUDIT.md) - HMAC chain layout
- Source: `src/bernstein/core/security/capability_matrix.py`,
  `src/bernstein/core/security/policy_engine.py`,
  `src/bernstein/core/agents/spawner_core.py`
- Pattern: Simon Willison, [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
