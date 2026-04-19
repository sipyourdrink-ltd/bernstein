# `openai_agents` adapter

Bernstein's adapter for [OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/).
Wraps the SDK's `Agent` + `Runner` in a CLI-spawnable subprocess so the
existing Bernstein spawner can manage lifecycle, timeouts, rate-limit
back-off, and cost tracking the same way it does for every other coding
agent.

Ticket: [oai-001](../../.sdd/backlog/open/oai-001-feat-openai-agents-sdk-adapter.yaml).

---

## Installation

The SDK is an optional dependency.  Install it with:

```bash
pip install 'bernstein[openai]'
```

or with uv:

```bash
uv add 'bernstein[openai]'
```

The adapter module itself loads without the SDK — `bernstein agents` will
list the adapter either way, but `spawn()` will fail with a clear error
until the extra is installed.

---

## Configuration

### Credentials

The adapter inherits four OpenAI env vars through Bernstein's credential
scoping:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | API key (required) |
| `OPENAI_BASE_URL` | Custom endpoint (optional — proxies, Azure) |
| `OPENAI_ORGANIZATION` | Organization ID (optional — Enterprise tier detection) |
| `OPENAI_PROJECT` | Project ID (optional — per-project billing) |

Register per-agent scope in `.sdd/config/credential_scopes.yaml`:

```yaml
enabled: true
known_keys:
  - OPENAI_API_KEY
  - OPENAI_BASE_URL
  - OPENAI_ORGANIZATION
  - OPENAI_PROJECT
roles:
  backend:
    - OPENAI_API_KEY
    - OPENAI_PROJECT
```

### Supported models

The runner accepts any OpenAI model ID the SDK recognises.  The default
supported set has pricing rows in `src/bernstein/core/cost/cost.py`:

| Model | Input $/1M | Output $/1M | Default for |
|-------|-----------:|------------:|-------------|
| `gpt-5` | 2.50 | 15.00 | High-quality executors |
| `gpt-5-mini` | 0.50 | 2.50 | Adapter default |
| `o4` | 3.00 | 12.00 | Reasoning tasks |

Any other model works — Bernstein will fall back to the generic
`_model_cost` default if pricing is missing, and SonarCloud's cost
panels will flag the gap.

---

## Usage in plan.yaml

```yaml
stages:
  - name: implement
    steps:
      - title: "Add unit tests"
        role: qa
        cli: openai_agents
        model: gpt-5-mini
        effort: medium
        sandbox_provider: unix_local   # unix_local | docker | e2b | modal
```

Sandbox provider selection is adapter-internal for now.  Once
[oai-002](../../.sdd/backlog/open/) ships the pluggable
`SandboxBackend` abstraction, this choice will be promoted to a
top-level Bernstein setting.

---

## How the adapter works

```
bernstein spawner
    │
    ▼
python -m bernstein.adapters.openai_agents_runner --manifest <path>
    │
    ▼
agents.Agent(...) + agents.Runner.run_sync(...)
    │
    ▼
structured JSON events on stdout
    │
    ▼
Bernstein log tail + cost tracker
```

The runner script emits line-delimited JSON so the spawner can mix OpenAI
Agents events into the same log stream as Claude Code, Codex, etc.:

```jsonl
{"type": "start", "session_id": "oai-abc", "model": "gpt-5-mini"}
{"type": "tool_call", "name": "file_read", "args": {"path": "src/foo.py"}}
{"type": "tool_result", "name": "file_read", "output": "..."}
{"type": "usage", "input_tokens": 1234, "output_tokens": 567, "tool_calls": 3}
{"type": "completion", "status": "done", "summary": "Added 4 tests"}
```

---

## MCP bridging

MCP servers that Bernstein already manages — `bernstein` bridge,
user-configured servers — are passed through to the OpenAI Agents
runner via the manifest's `mcp_servers` key.  The runner forwards them
to `RunConfig` so the Agent can call into them **without** letting the
SDK spawn its own MCP child processes.  This avoids duplicate
connections, duplicate cost accounting, and ensures every tool call
still shows up in Bernstein's central audit log.

---

## Cost tracking

The runner emits a `usage` event before `completion`:

```json
{"type": "usage", "input_tokens": 1234, "output_tokens": 567, "tool_calls": 3}
```

Bernstein's cost tracker reads these events from
`.sdd/runtime/<session>.log` and records them in
`.sdd/runtime/cost/` using the `gpt-5` / `gpt-5-mini` / `o4` pricing rows.

---

## Rate-limit handling

The runner inspects exceptions raised from `Runner.run_sync` for the
usual OpenAI rate-limit signals (429, `RateLimitError`, `insufficient_quota`)
and exits with code `4`.  The adapter's `_probe_fast_exit` maps that
code onto Bernstein's existing back-off (`COST.rate_limit_cooldown_s`).

---

## Known gaps (tracked separately)

* **oai-002** — promote sandbox provider selection to Bernstein's outer
  `SandboxBackend` once the abstraction lands.
* **oai-003** — capture per-tool latency breakdown from the SDK's event
  stream (currently only total tool-call count is recorded).
