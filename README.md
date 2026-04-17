<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
  <img alt="Bernstein" src="docs/assets/logo-light.svg" width="340">
</picture>

<br>

> *"To achieve great things, two things are needed: a plan and not quite enough time."* — Leonard Bernstein

### Orchestrate any AI coding agent. Any model. One command.

<img alt="Bernstein in action — parallel AI agents orchestrated in real time" src="docs/assets/in-action.gif" width="700">

[![CI](https://github.com/chernistry/bernstein/actions/workflows/ci.yml/badge.svg)](https://github.com/chernistry/bernstein/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/chernistry/bernstein?style=social)](https://github.com/chernistry/bernstein/stargazers)
[![PyPI](https://img.shields.io/pypi/v/bernstein)](https://pypi.org/project/bernstein/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/bernstein?left_color=GREY)](https://pepy.tech/projects/bernstein)
[![npm](https://img.shields.io/npm/v/bernstein-orchestrator)](https://www.npmjs.com/package/bernstein-orchestrator)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/github/license/chernistry/bernstein)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-1.0%2C%201.1-blue)](docs/compatibility.md)
[![A2A Compatible](https://img.shields.io/badge/A2A-0.2%2C%200.3-blue)](docs/compatibility.md)
[![Share on X](https://img.shields.io/badge/share-on%20X-black?logo=x&logoColor=white)](https://x.com/intent/tweet?text=Bernstein%20%E2%80%94%20orchestrate%20parallel%20AI%20coding%20agents.%20Any%20model.%20One%20command.&url=https%3A%2F%2Fgithub.com%2Fchernistry%2Fbernstein&hashtags=ai,opensource,devtools)
[![SaaSHub](https://img.shields.io/badge/SaaSHub-Approved-brightgreen)](https://www.saashub.com/bernstein?utm_source=badge&utm_campaign=badge&utm_content=bernstein&badge_variant=color&badge_kind=approved)
[![Built with Bernstein](https://img.shields.io/badge/Built%20with-Bernstein%20%F0%9F%8E%BC-blue)](https://github.com/chernistry/bernstein)

[Documentation](https://bernstein.readthedocs.io/) &middot; [Getting Started](docs/GETTING_STARTED.md) &middot; [Glossary](docs/GLOSSARY.md) &middot; [Limitations](docs/KNOWN_LIMITATIONS.md)

#### Wall of fame

> *"lol, good luck, keep vibecoding shit that you have no idea about xD"* — [PeaceFirePL](https://www.reddit.com/r/coolgithubprojects/comments/1sc7pxn/comment/oel89qf/), Reddit

</div>

---

Bernstein takes a goal, breaks it into tasks, assigns them to AI coding agents running in parallel, verifies the output, and merges the results. You come back to working code, passing tests, and a clean git history.

No framework to learn. No vendor lock-in. Agents are interchangeable workers — swap any agent, any model, any provider. The orchestrator itself is deterministic Python code. Zero LLM tokens on scheduling.

```bash
pip install bernstein
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

Also available via `pipx`, `uv tool install`, `brew`, `dnf copr`, and `npx bernstein-orchestrator`. See [install options](#install).

## Supported agents

Bernstein auto-discovers installed CLI agents. Mix them in the same run — cheap local models for boilerplate, heavy cloud models for architecture.

| Agent | Models | Install |
|-------|--------|---------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | opus 4.6, sonnet 4.6, haiku 4.5 | `npm install -g @anthropic-ai/claude-code` |
| [Codex CLI](https://github.com/openai/codex) | gpt-5.4, gpt-5.4-mini | `npm install -g @openai/codex` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | gemini-3.1-pro, gemini-3-flash | `npm install -g @google/gemini-cli` |
| [Cursor](https://www.cursor.com) | sonnet 4.6, opus 4.6, gpt-5.4 | [Cursor app](https://www.cursor.com) |
| [Aider](https://aider.chat) | Any OpenAI/Anthropic-compatible | `pip install aider-chat` |
| [Ollama](https://ollama.ai) + Aider | Local models (offline) | `brew install ollama` |
| [Cloudflare Agents](https://developers.cloudflare.com/agents/) | Workers AI models | `bernstein cloud init` |
| [Codex on Cloudflare](https://developers.cloudflare.com/agents/) | gpt-5.4 via CF gateway | `bernstein cloud init` |
| [Amp](https://ampcode.com), [Cody](https://sourcegraph.com/cody), [Continue.dev](https://continue.dev), [Goose](https://block.github.io/goose/), [IaC](https://www.terraform.io/) (Terraform/Pulumi), [Kilo](https://kilo.dev), [Kiro](https://kiro.dev), [OpenCode](https://opencode.ai), [Qwen](https://github.com/QwenLM/Qwen-Agent), [Roo Code](https://github.com/RooVetGit/Roo-Code), [Tabby](https://tabby.tabbyml.com) | Various | See docs |
| **Generic** | Any CLI with `--prompt` | Built-in |

Any adapter also works as the **internal scheduler LLM** — run the entire stack without any specific provider:

```yaml
internal_llm_provider: gemini            # or qwen, ollama, codex, goose, ...
internal_llm_model: gemini-3.1-pro-preview
```

> [!TIP]
> Run `bernstein --headless` for CI pipelines — no TUI, structured JSON output, non-zero exit on failure.

## Quick start

```bash
cd your-project
bernstein init                    # creates .sdd/ workspace + bernstein.yaml
bernstein -g "Add rate limiting"  # agents spawn, work in parallel, verify, exit
bernstein live                    # watch progress in the TUI dashboard
bernstein stop                    # graceful shutdown with drain
```

For multi-stage projects, define a YAML plan:

```bash
bernstein run plan.yaml           # skips LLM planning, goes straight to execution
bernstein run --dry-run plan.yaml # preview tasks and estimated cost
```

## How it works

1. **Decompose** — the manager breaks your goal into tasks with roles, owned files, and completion signals
2. **Spawn** — agents start in isolated git worktrees, one per task. Main branch stays clean.
3. **Verify** — the janitor checks concrete signals: tests pass, files exist, lint clean, types correct
4. **Merge** — verified work lands in main. Failed tasks get retried or routed to a different model.

The orchestrator is a Python scheduler, not an LLM. Scheduling decisions are deterministic, auditable, and reproducible.

## Cloud execution (Cloudflare)

Bernstein can run agents on Cloudflare Workers instead of locally. The `bernstein cloud` CLI handles deployment and lifecycle.

- **Workers** — agent execution on Cloudflare's edge, with Durable Workflows for multi-step tasks and automatic retry
- **V8 sandbox isolation** — each agent runs in its own isolate, no container overhead
- **R2 workspace sync** — local worktree state syncs to R2 object storage so cloud agents see the same files
- **Workers AI** — use Cloudflare-hosted models as the LLM provider (no external API keys required)
- **D1 analytics** — task metrics and cost data stored in D1 for querying
- **Vectorize** — semantic cache backed by Cloudflare's vector database
- **Browser rendering** — headless Chrome on Workers for agents that need to inspect web output
- **MCP remote transport** — expose or consume MCP servers over Cloudflare's network

```bash
bernstein cloud init       # scaffold wrangler.toml + bindings
bernstein cloud deploy     # push agent workers
bernstein cloud run plan.yaml  # execute a plan on Cloudflare
```

## Capabilities

**Core orchestration** — parallel execution, git worktree isolation, janitor verification, quality gates (lint + types + PII scan), cross-model code review, circuit breaker for misbehaving agents, token growth monitoring with auto-intervention.

**Intelligence** — contextual bandit router learns optimal model/effort pairs over time. Knowledge graph for codebase impact analysis. Semantic caching saves tokens on repeated patterns. Cost anomaly detection with Z-score flagging.

**Enterprise** — HMAC-chained tamper-evident audit logs. Policy limits with fail-open defaults and multi-tenant isolation. PII output gating. OAuth 2.0 PKCE. SSO/SAML/OIDC auth. WAL crash recovery — no silent data loss.

**Observability** — Prometheus `/metrics`, OTel exporter presets, Grafana dashboards. Per-model cost tracking (`bernstein cost`). Terminal TUI and web dashboard. Agent process visibility in `ps`.

**Ecosystem** — MCP server mode, A2A protocol support, GitHub App integration, pluggy-based plugin system, multi-repo workspaces, cluster mode for distributed execution, self-evolution via `--evolve`.

Full feature matrix: [FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md)

## How it compares

| Feature | Bernstein | CrewAI | AutoGen | LangGraph |
|---------|-----------|--------|---------|-----------|
| Orchestrator | Deterministic code | LLM-driven | LLM-driven | Graph + LLM |
| Works with | Any CLI agent (20 adapters) | Python SDK classes | Python agents | LangChain nodes |
| Git isolation | Worktrees per agent | No | No | No |
| Verification | Janitor + quality gates | No | No | Conditional edges |
| Cost tracking | Built-in | No | No | No |
| State model | File-based (.sdd/) | In-memory | In-memory | Checkpointer |
| Self-evolution | Built-in | No | No | No |
| Declarative plans (YAML) | Yes | Partial | No | Yes |
| Model routing per task | Yes | No | No | Manual |
| MCP support | Yes | No | No | No |
| Agent-to-agent chat | No | Yes | Yes | No |
| Web UI | No | Yes | Yes | Partial |
| Cloud hosted option | Yes (Cloudflare) | Yes | No | Yes |
| Built-in RAG/retrieval | No | Yes | Yes | Yes |

*Last verified: 2026-04-14. See [full comparison pages](docs/compare/README.md) for detailed feature matrices.*

## Monitoring

```bash
bernstein live       # TUI dashboard
bernstein dashboard  # web dashboard
bernstein status     # task summary
bernstein ps         # running agents
bernstein cost       # spend by model/task
bernstein doctor     # pre-flight checks
bernstein recap      # post-run summary
bernstein trace <ID> # agent decision trace
bernstein run-changelog --hours 48  # changelog from agent-produced diffs
bernstein explain <cmd>  # detailed help with examples
bernstein dry-run    # preview tasks without executing
bernstein dep-impact # API breakage + downstream caller impact
bernstein aliases    # show command shortcuts
bernstein config-path    # show config file locations
bernstein init-wizard    # interactive project setup
bernstein debug-bundle   # collect logs, config, and state for bug reports
```

```bash
bernstein fingerprint build --corpus-dir ~/oss-corpus  # build local similarity index
bernstein fingerprint check src/foo.py                 # check generated code against the index
```

## Install

| Method | Command |
|--------|---------|
| **pip** | `pip install bernstein` |
| **pipx** | `pipx install bernstein` |
| **uv** | `uv tool install bernstein` |
| **Homebrew** | `brew tap chernistry/bernstein && brew install bernstein` |
| **Fedora / RHEL** | `sudo dnf copr enable alexchernysh/bernstein && sudo dnf install bernstein` |
| **npm** (wrapper) | `npx bernstein-orchestrator` |

Editor extensions: [VS Marketplace](https://marketplace.visualstudio.com/items?itemName=alex-chernysh.bernstein) &middot; [Open VSX](https://open-vsx.org/extension/alex-chernysh/bernstein)

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and code style.

## Support

If Bernstein saves you time: [GitHub Sponsors](https://github.com/sponsors/chernistry) &middot; [Open Collective](https://opencollective.com/bernstein)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=chernistry/bernstein&type=Date)](https://star-history.com/#chernistry/bernstein&Date)

## License

[Apache License 2.0](LICENSE)

---

<!-- mcp-name: io.github.chernistry/bernstein -->
