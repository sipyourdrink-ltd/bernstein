# awesome-bernstein-plugins

> A curated list of plugins, adapters, role templates, and integrations for [Bernstein](https://github.com/chernistry/bernstein) — the multi-agent orchestration system.

Plugins extend Bernstein without touching core code. They hook into the task and agent lifecycle via [pluggy](https://pluggy.readthedocs.io/) — the same machinery used by pytest. A plugin is a plain Python class. No subclassing. No registration boilerplate.

**Install a plugin** in 30 seconds:

```bash
bernstein plugin install my-plugin-name
```

Or add it directly to `bernstein.yaml`:

```yaml
plugins:
  - my_package.hooks:MyPlugin
```

See [docs/plugin-sdk.md](../docs/plugin-sdk.md) for the full authoring guide.

---

## Contents

- [Notifiers](#notifiers)
- [Observability & Metrics](#observability--metrics)
- [Issue Tracker Sync](#issue-tracker-sync)
- [Quality Gates](#quality-gates)
- [Cost & Routing](#cost--routing)
- [Adapters](#adapters)
- [Role Templates](#role-templates)
- [Plan Templates](#plan-templates)
- [Utilities](#utilities)
- [Writing a Plugin](#writing-a-plugin)
- [Publishing a Plugin](#publishing-a-plugin)
- [Contributing to This List](#contributing-to-this-list)

---

## Notifiers

Send alerts when tasks complete, fail, or agents are spawned.

### Bundled (ships with Bernstein)

| Plugin | Description | Install |
|--------|-------------|---------|
| `LoggingPlugin` | Prints all lifecycle events to stdout. Useful as a starting template. | `examples.plugins.logging_plugin:LoggingPlugin` |
| `SlackNotifier` | Posts task failure and completion alerts to Slack via Incoming Webhook. Non-blocking (daemon thread). | `examples.plugins.slack_notifier:SlackNotifier` |
| `DiscordNotifier` | Posts color-coded embeds to a Discord channel via webhook. | `examples.plugins.discord_notifier:DiscordNotifier` |

**SlackNotifier** — configuration:

```bash
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx
```

```yaml
plugins:
  - examples.plugins.slack_notifier:SlackNotifier
```

**DiscordNotifier** — configuration:

```bash
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/CHANNEL_ID/TOKEN
```

### Community

> Submit your notifier plugin — see [Contributing to This List](#contributing-to-this-list).

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here)_ | _(description)_ | _(link)_ |

---

## Observability & Metrics

Record lifecycle events, feed dashboards, and build audit trails.

### Bundled

| Plugin | Description | Install |
|--------|-------------|---------|
| `MetricsPlugin` | Writes all lifecycle events to `.sdd/metrics/plugin_events.jsonl` as structured JSON. Foundation for custom dashboards. | `examples.plugins.metrics_plugin:MetricsPlugin` |

**MetricsPlugin** — configuration:

```bash
export BERNSTEIN_METRICS_DIR=/path/to/metrics   # default: .sdd/metrics
```

Sample output:

```json
{"ts": "2026-03-29T10:00:01+00:00", "event": "task_created", "task_id": "a468891b", "role": "backend", "title": "Implement auth middleware"}
{"ts": "2026-03-29T10:05:22+00:00", "event": "agent_spawned", "session_id": "s1a2b3c4", "role": "backend", "model": "claude-sonnet-4-6"}
{"ts": "2026-03-29T10:08:44+00:00", "event": "task_completed", "task_id": "a468891b", "role": "backend", "result_summary": "JWT auth middleware added"}
```

### Community

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here)_ | _(description)_ | _(link)_ |

---

## Issue Tracker Sync

Keep Jira, Linear, or GitHub Issues in sync with Bernstein task state.

### Bundled

| Plugin | Description | Install |
|--------|-------------|---------|
| `JiraPlugin` | Transitions Jira issues when tasks complete or fail. Links via `external_ref: "jira:PROJ-42"`. | `examples.plugins.jira_plugin:JiraPlugin` |
| `LinearPlugin` | Transitions Linear issues via GraphQL API. Links via `external_ref: "linear:ENG-42"`. | `examples.plugins.linear_plugin:LinearPlugin` |

**JiraPlugin** — configuration:

```bash
pip install bernstein-sdk[jira]
export JIRA_BASE_URL=https://your-org.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=<token>
```

Hook mapping:

| Bernstein event | Jira action |
|-----------------|-------------|
| `on_task_completed` | Transition issue → Done |
| `on_task_failed` | Transition issue → Done (failed tag) + add error comment |

**LinearPlugin** — configuration:

```bash
pip install bernstein-sdk
export LINEAR_API_KEY=lin_api_...
```

Hook mapping:

| Bernstein event | Linear action |
|-----------------|---------------|
| `on_task_completed` | Transition issue → Done |
| `on_task_failed` | Transition issue → Cancelled |

### Community

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here — GitHub Issues, Shortcut, Asana, etc.)_ | | |

---

## Quality Gates

Run automated checks after tasks complete. Log results, alert on failure, optionally block the orchestrator.

### Bundled

| Plugin | Description | Install |
|--------|-------------|---------|
| `SecurityScanGate` | Runs `bandit` (or any configurable command) after every task completes. Records results to `.sdd/metrics/custom_gates.jsonl`. | `examples.plugins.quality_gate_plugin:SecurityScanGate` |

**SecurityScanGate** — configuration:

```bash
# Default: bandit -r . -ll -q
# Override with any command:
export BERNSTEIN_SECURITY_CMD="semgrep --config=auto . --quiet"
# or
export BERNSTEIN_SECURITY_CMD="trivy fs . --exit-code 1 --severity HIGH,CRITICAL"
```

**Note**: The plugin gate is a *soft gate* — it records and alerts but does not block the orchestrator. For hard blocking, use `quality_gates:` in `bernstein.yaml`.

### Community

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here — coverage threshold, lint gate, type-check gate, etc.)_ | | |

---

## Cost & Routing

Monitor spend and influence model selection without touching core routing code.

### Bundled

| Plugin | Description | Install |
|--------|-------------|---------|
| `CostAwareRouter` | Tracks cumulative model spend. Writes routing hints to `.sdd/runtime/routing_hints.json`. Downgrades preferred model at 60% and 90% of daily budget. Protected roles (`manager`, `architect`, `security`) are never downgraded. | `examples.plugins.custom_router_plugin:CostAwareRouter` |

**CostAwareRouter** — configuration:

```bash
export BERNSTEIN_DAILY_BUDGET_USD=5.00   # default: 10.00
```

```yaml
plugins:
  - examples.plugins.custom_router_plugin:CostAwareRouter
```

Routing hints file (`.sdd/runtime/routing_hints.json`):

```json
{
  "preferred_model": "haiku",
  "budget_remaining_usd": 0.83,
  "override_roles": {
    "manager": "opus",
    "architect": "opus"
  }
}
```

### Community

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here — spend alerting, per-team budgets, model A/B testing, etc.)_ | | |

---

## Adapters

Adapters connect Bernstein to a specific CLI agent executable. Bernstein ships adapters for all major agents. Community adapters extend support to additional tools.

### Bundled adapters

| Adapter | Agent | Install |
|---------|-------|---------|
| `claude` | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm install -g @anthropic-ai/claude-code` |
| `codex` | [Codex CLI](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| `gemini` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `npm install -g @google/gemini-cli` |
| `aider` | [Aider](https://aider.chat) | `pip install aider-chat` |
| `amp` | [Amp](https://ampcode.com) | See [Amp docs](https://ampcode.com) |
| `cursor` | [Cursor](https://www.cursor.com) | [Cursor app](https://www.cursor.com) |
| `cody` | [Cody](https://sourcegraph.com/cody) | See [Cody docs](https://sourcegraph.com/cody) |
| `continue_dev` | [Continue.dev](https://continue.dev) | See [Continue docs](https://continue.dev) |
| `goose` | [Goose](https://block.github.io/goose/) | See [Goose docs](https://block.github.io/goose/) |
| `kilo` | [Kilo](https://kilo.dev) | See [Kilo docs](https://kilo.dev) |
| `kiro` | [Kiro](https://kiro.dev) | See [Kiro docs](https://kiro.dev) |
| `ollama` | [Ollama](https://ollama.ai) + Aider (local, offline) | `brew install ollama` |
| `opencode` | [OpenCode](https://opencode.ai) | See [OpenCode docs](https://opencode.ai) |
| `qwen` | [Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) | `pip install qwen-agent` |
| `roo_code` | [Roo Code](https://github.com/RooVetGit/Roo-Code) | See [Roo Code docs](https://github.com/RooVetGit/Roo-Code) |
| `tabby` | [Tabby](https://tabby.tabbyml.com) | See [Tabby docs](https://tabby.tabbyml.com) |
| `generic` | Any CLI agent via stdin/stdout | Built-in |

### Community adapters

| Adapter | Agent | Source |
|---------|-------|--------|
| _(your adapter here)_ | _(agent name)_ | _(link)_ |

See [docs/ADAPTER_GUIDE.md](../docs/ADAPTER_GUIDE.md) to write your own adapter.

---

## Role Templates

Role templates define the system prompt and behavior for a specific agent role. Bernstein ships templates for all standard roles.

### Bundled roles

| Role | Description |
|------|-------------|
| `manager` | Decomposes goals into tasks, coordinates work, resolves blockers |
| `vp` | Strategic oversight, priorities, cross-team decisions |
| `backend` | Server-side logic, APIs, databases, infrastructure |
| `frontend` | UI, components, accessibility, design systems |
| `qa` | Test writing, coverage analysis, regression hunting |
| `security` | Threat modeling, code review for vulnerabilities, SAST |
| `devops` | CI/CD, Docker, Kubernetes, deployment automation |
| `architect` | System design, ADRs, dependency decisions |
| `docs` | API reference, tutorials, READMEs, changelogs |
| `reviewer` | Code review, style enforcement, PR feedback |
| `ml-engineer` | Model training, feature pipelines, evaluation |
| `prompt-engineer` | Prompt design, chain-of-thought, evaluation harnesses |
| `retrieval` | RAG pipelines, embedding strategies, vector search |
| `visionary` | Long-horizon planning, roadmap, opportunity analysis |
| `analyst` | Data analysis, metrics, reporting, dashboards |
| `resolver` | Resolves merge conflicts, blocked tasks, cross-agent disagreements |
| `ci-fixer` | Diagnoses and repairs failing CI pipelines |

### Community roles

| Role | Description | Source |
|------|-------------|--------|
| _(your role here)_ | | |

Role templates live in `templates/roles/`. Each is a plain text system prompt. Drop your `.md` file in that directory and reference it with `role: your-role-name` in a plan step.

---

## Plan Templates

Plan templates are reusable `bernstein.yaml` structures for common project patterns.

### Bundled

| Template | Description |
|----------|-------------|
| `templates/plan.yaml` | Base template — stages, steps, roles, priorities |
| `templates/demo/bernstein.yaml` | Demo Flask app — 4 bug-fix tasks across 3 agents |

### Community

| Template | Description | Source |
|----------|-------------|--------|
| _(your template here — SaaS launch, API migration, monolith split, etc.)_ | | |

---

## Utilities

Miscellaneous plugins that don't fit the above categories.

### Bundled

| Plugin | Description | Install |
|--------|-------------|---------|
| `AdapterPlugin` | Base class for building adapter-style plugins that wrap external CLI tools. | `examples.plugins.adapter_plugin` |
| `TriggerPlugin` | Emits lifecycle events to external trigger systems (webhooks, queues). | `examples.plugins.trigger_plugin` |
| `ReporterPlugin` | Aggregates and formats lifecycle events into structured reports. | `examples.plugins.reporter_plugin` |

### Community

| Plugin | Description | Source |
|--------|-------------|--------|
| _(your plugin here)_ | | |

---

## Writing a Plugin

A plugin is a Python class decorated with `@hookimpl`. No base class. No registration. Just decorate the methods you want to handle.

```python
from bernstein.plugins import hookimpl

class MyPlugin:
    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        # Called whenever a task transitions to done
        print(f"Task {task_id} done: {result_summary}")
```

### Available hooks

| Hook | When fired | Parameters |
|------|-----------|------------|
| `on_task_created` | Task added to task server | `task_id`, `role`, `title` |
| `on_task_completed` | Task transitions to `done` | `task_id`, `role`, `result_summary` |
| `on_task_failed` | Task transitions to `failed` | `task_id`, `role`, `error` |
| `on_agent_spawned` | Agent session started | `session_id`, `role`, `model` |
| `on_agent_reaped` | Agent session collected by janitor | `session_id`, `role`, `outcome` |
| `on_evolve_proposal` | Evolution proposal verdict issued | `proposal_id`, `title`, `verdict` |

### Rules

- **Decorate with `@hookimpl`** — unmarked methods are ignored.
- **Keyword arguments only** — omit parameters you don't need.
- **Return `None`** — return values are discarded.
- **Don't block** — hooks run in the orchestrator's main loop. Offload slow I/O to a background thread.

Exceptions raised inside a hook are caught and logged at `WARNING` level — they never crash the orchestrator.

### Loading a plugin

**Option A — per-project (`bernstein.yaml`)**:

```yaml
plugins:
  - my_package.hooks:MyPlugin
```

**Option B — distributable (entry points)**:

```toml
# pyproject.toml
[project.entry-points."bernstein.plugins"]
my-plugin = "my_package.hooks:MyPlugin"
```

Entry-point plugins auto-load when installed alongside Bernstein. No `bernstein.yaml` change required.

Full guide: [docs/plugin-sdk.md](../docs/plugin-sdk.md)

---

## Publishing a Plugin

1. **Create your plugin directory**:

```
my-bernstein-plugin/
├── pyproject.toml
├── manifest.yaml        # required for marketplace
├── README.md
└── src/
    └── my_bernstein_plugin/
        ├── __init__.py
        └── hooks.py
```

2. **Write `manifest.yaml`**:

```yaml
name: my-plugin
version: 1.0.0
description: One-line description of what this plugin does
author: Your Name
plugin_types:
  - hook          # one of: quality_gate, adapter, role_template, plan_template, hook, mcp_server
compatibility:
  min_bernstein_version: "0.1.0"
  python_version: ">=3.12"
```

3. **Publish to the marketplace**:

```bash
bernstein plugin publish ./my-bernstein-plugin/
```

4. **Add to this list** — open a pull request with your plugin added to the appropriate section above. Include: name, one-line description, source link.

---

## Contributing to This List

This is a community-maintained list. To add your plugin:

1. It must be publicly available (GitHub, PyPI, or npm).
2. It must implement at least one Bernstein hook correctly.
3. It must include a README with installation instructions and at least one usage example.
4. Open a pull request adding it to the relevant section above.

For questions, open an issue on the [Bernstein repository](https://github.com/chernistry/bernstein).
