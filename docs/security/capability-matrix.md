# Tool capability matrix and the lethal-trifecta check

Every tool / MCP server / adapter call Bernstein knows about is
classified along three axes: **accesses-private-data**,
**consumes-untrusted-input**, **can-externally-communicate**. Before
each agent spawn (and at policy time for each tool call), Bernstein
evaluates the union of capabilities on the active execution path and
**fails closed when all three are present**. That combination is the
"lethal trifecta" Simon Willison calls the worst-case prompt-injection
shape, and refusing it is a structural rule, not a guardrail.

## Why it exists

The existing layered policy (network isolation, DLP, command allow-list)
helps when configured correctly, but it is per-axis. There was no
declarative tagging that said "this tool can read secrets", and no
orchestration-time check for the chain `read-secrets +
fetch-untrusted-issue-body + post-public-comment`. The capability
matrix is that tagging plus that check.

## How capabilities are declared

Capabilities ship in three aggregate YAML files under
`templates/capabilities/`: `adapters.yaml` (CLI agent adapters),
`mcp_tools.yaml` (built-in Bernstein MCP tools), and `surfaces.yaml`
(generic tool surfaces). Each file is a `tools:` list keyed by `name`:

```yaml
# templates/capabilities/adapters.yaml
tools:
  - name: adapter.claude
    capabilities: [private_data, untrusted_input, external_comm]
```

`Capability` is one of:

| Value | Meaning |
|---|---|
| `PRIVATE_DATA` | The tool can read data the operator considers private (secrets, repo source, customer data). |
| `UNTRUSTED_INPUT` | The tool ingests bytes from a source the operator does not control (issue body, web fetch, MCP server reply). |
| `EXTERNAL_COMM` | The tool can transmit bytes to a destination outside the local sandbox (HTTP request, file write outside the repo, comment-posting). |

The default for unknown tools is **all three** (high-risk). That is
deliberate: missing metadata fails closed.

## How to use it

The matrix is consulted automatically. To check what the runtime
sees:

```bash
# Print the matrix and any agent configs currently violating the rule.
# Exits non-zero if any violation exists.
bernstein audit capabilities
```

Sample output:

```
Tool                             PRIVATE_DATA  UNTRUSTED  EXTERNAL_COMM  Source
gh.issue_comment                 yes           no         yes            declared
web_fetch                        no            yes        yes            declared
read_file                        yes           no         no             declared
mcp.cocoindex.search             yes           no         no             declared

Active violations:
  agent[reviewer-1]: gh.issue_comment + web_fetch + read_file = LETHAL_TRIFECTA
```

Programmatically:

```python
from bernstein.core.security.capability_matrix import (
    CapabilityRegistry, Capability,
)

registry = CapabilityRegistry.load_default()
decision = registry.evaluate_chain([
    "read_file",
    "web_fetch",
    "gh.issue_comment",
])
assert decision.kind == "DENY-trifecta"
print(decision.reason)
```

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `security.lethal_trifecta_enforcement` | `enforce` | `enforce` (refuse spawn + signed refusal audit event), `warn` (allow, warning recorded), `off` (allow). Every mode still records the evaluated chain in the per-spawn manifest under `.sdd/runtime/spawn_capabilities/`. |
| `templates/capabilities/*.yaml` | three files: `adapters.yaml` (19 adapters), `mcp_tools.yaml`, `surfaces.yaml` | Capability declarations. |
| Default for unknown tools | `frozenset(Capability)` (all three) | Fail-closed. |

The decision lands as a new `DecisionType.IMMUNE` layer in the policy
engine (`core/security/policy_engine.py`), evaluated **before** any
`ALLOW` rule. Plugins cannot override it.

Audit log records every denied chain with reason `lethal_trifecta`
plus the offending tool list.

## Limitations

- Chain-level only. Per-argument analysis (`gh.issue_comment` on a
  public issue vs a private issue) is not in v1.
- No auto-discovery of capabilities by inspecting tool source. Each
  surface needs an explicit YAML.
- Runtime monitoring of *undeclared* network egress is the existing
  `network_isolation.py` job, not this module's.
- Capability tagging covers what we ship. Custom plugins that add
  new tools without YAML are treated as high-risk by default.

## Related

- [Lethal-trifecta security model](lethal-trifecta.md) - threat model,
  default capability table, audit behaviour
- Source: `src/bernstein/core/security/capability_matrix.py`
- Policy engine: `src/bernstein/core/security/policy_engine.py`
- Capability declarations: `templates/capabilities/`
- CLI: `bernstein audit capabilities`
- Pattern: [Lethal-trifecta threat model](https://github.com/nibzard/awesome-agentic-patterns/blob/main/patterns/lethal-trifecta-threat-model.md)
