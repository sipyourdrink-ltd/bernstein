<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.svg">
  <img alt="Bernstein" src="docs/assets/logo-light.svg" width="340">
</picture>

<br>

> *"To achieve great things, two things are needed: a plan and not quite enough time."* — Leonard Bernstein

### Orchestrate any AI coding agent. Any model. One command.

<img alt="Bernstein in action: parallel AI agents orchestrated in real time" src="docs/assets/in-action-small.gif" width="700">

[![CI](https://github.com/chernistry/bernstein/actions/workflows/ci.yml/badge.svg)](https://github.com/chernistry/bernstein/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/bernstein)](https://pypi.org/project/bernstein/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/github/license/chernistry/bernstein)](LICENSE)
[![MseeP.ai](https://img.shields.io/badge/MseeP.ai-verified-2496ed)](https://mseep.ai/app/chernistry-bernstein)

[Website](https://bernstein.run) &middot; [Documentation](https://bernstein.readthedocs.io/) &middot; [Getting Started](docs/getting-started/GETTING_STARTED.md) &middot; [Glossary](docs/reference/GLOSSARY.md) &middot; [Limitations](docs/reference/KNOWN_LIMITATIONS.md)

</div>

---

**What is this?** You tell it what you want built. It splits the work across several AI coding agents (Claude Code, Codex, Gemini CLI, and 28 more), runs the tests, and merges the code that actually passes. You come back to working code.

### Install and run

One line on macOS / Linux:

```bash
curl -fsSL https://bernstein.run/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://bernstein.run/install.ps1 | iex
```

Then point it at your project and set a goal:

```bash
cd your-project
bernstein init                          # creates a .sdd/ workspace
bernstein -g "Add JWT auth with refresh tokens, tests, and API docs"
```

What you see while it runs:

```
$ bernstein -g "Add JWT auth"
[manager] decomposed into 4 tasks
[agent-1] claude-sonnet: src/auth/middleware.py  (done, 2m 14s)
[agent-2] codex:         tests/test_auth.py      (done, 1m 58s)
[verify]  all gates pass. merging to main.
```

### Why it's different

Most agent orchestrators use an LLM to decide who does what. That's non-deterministic and burns tokens on scheduling instead of code. Bernstein does one LLM call to break down your goal, then the rest — running agents in parallel, isolating their git branches, running tests, routing retries — is plain Python. Every run is reproducible. Every step is logged and replayable.

No framework to learn. No vendor lock-in. Swap any agent, any model, any provider.

Other install options: `pipx install bernstein`, `pip install bernstein`, `uv tool install bernstein`, `brew`, `dnf copr`, `npx bernstein-orchestrator`. See [install options](#install).

## Supported agents

Bernstein auto-discovers installed CLI agents. Mix them in the same run. Cheap local models for boilerplate, heavier cloud models for architecture.

31 CLI agent adapters: 30 third-party wrappers plus a generic wrapper for anything with `--prompt`.

| Agent | Models | Install |
|-------|--------|---------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Opus 4, Sonnet 4.6, Haiku 4.5 | `npm install -g @anthropic-ai/claude-code` |
| [Codex CLI](https://github.com/openai/codex) | GPT-5, GPT-5 mini | `npm install -g @openai/codex` |
| [OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/) | GPT-5, GPT-5 mini, o4 | `pip install 'bernstein[openai]'` |
| [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) | Copilot-managed (GPT-5, Sonnet 4.6) | `npm install -g @github/copilot` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Gemini 2.5 Pro, Gemini Flash | `npm install -g @google/gemini-cli` |
| [Cursor](https://www.cursor.com) | Sonnet 4.6, Opus 4, GPT-5 | [Cursor app](https://www.cursor.com) |
| [Aider](https://aider.chat) | Any OpenAI/Anthropic-compatible | `pip install aider-chat` |
| [Amp](https://ampcode.com) | Amp-managed | `npm install -g @sourcegraph/amp` |
| [Cody](https://sourcegraph.com/cody) | Sourcegraph-hosted | `npm install -g @sourcegraph/cody` |
| [Continue](https://continue.dev) | Any OpenAI/Anthropic-compatible | `npm install -g @continuedev/cli` (binary: `cn`) |
| [Goose](https://block.github.io/goose/) | Any provider Goose supports | See [Goose docs](https://block.github.io/goose/) |
| [IaC](https://www.terraform.io/) (Terraform/Pulumi) | Any provider the base agent uses | Built-in |
| [Kilo](https://kilo.dev) | Kilo-hosted | See [Kilo docs](https://kilo.dev) |
| [Kiro](https://kiro.dev) | Kiro-hosted | See [Kiro docs](https://kiro.dev) |
| [Ollama](https://ollama.ai) + Aider | Local models (offline) | `brew install ollama` |
| [OpenCode](https://opencode.ai) | Any provider OpenCode supports | See [OpenCode docs](https://opencode.ai) |
| [Qwen](https://github.com/QwenLM/qwen-code) | Qwen Code models | `npm install -g @qwen-code/qwen-code` |
| [Cloudflare Agents](https://developers.cloudflare.com/agents/) | Workers AI models | `bernstein cloud login` |
| **Generic** | Any CLI with `--prompt` | Built-in |

Any adapter also works as the **internal scheduler LLM**. Run the entire stack without any specific provider:

```yaml
internal_llm_provider: gemini            # or qwen, ollama, codex, goose, ...
internal_llm_model: gemini-2.5-pro
```

> [!TIP]
> Run `bernstein --headless` for CI pipelines. No TUI, structured JSON output, non-zero exit on failure.

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

1. **Decompose**. The manager breaks your goal into tasks with roles, owned files, and completion signals.
2. **Spawn**. Agents start in isolated git worktrees, one per task. Main branch stays clean.
3. **Verify**. The janitor checks concrete signals: tests pass, files exist, lint clean, types correct.
4. **Merge**. Verified work lands in main. Failed tasks get retried or routed to a different model.

The orchestrator is a Python scheduler, not an LLM. Scheduling decisions are deterministic, auditable, and reproducible.

## Cloud execution (Cloudflare)

Bernstein can run agents on Cloudflare Workers instead of locally. The `bernstein cloud` CLI handles deployment and lifecycle.

- **Workers**. Agent execution on Cloudflare's edge, with Durable Workflows for multi-step tasks and automatic retry.
- **V8 sandbox isolation**. Each agent runs in its own isolate, no container overhead.
- **R2 workspace sync**. Local worktree state syncs to R2 object storage so cloud agents see the same files.
- **Workers AI** (experimental). Use Cloudflare-hosted models as the LLM provider, no external API keys required.
- **D1 analytics**. Task metrics and cost data stored in D1 for querying.
- **Vectorize**. Semantic cache backed by Cloudflare's vector database.
- **Browser rendering**. Headless Chrome on Workers for agents that need to inspect web output.
- **MCP remote transport**. Expose or consume MCP servers over Cloudflare's network.

```bash
bernstein cloud login      # authenticate with Bernstein Cloud
bernstein cloud deploy     # push agent workers
bernstein cloud run plan.yaml  # execute a plan on Cloudflare
```

A `bernstein cloud init` scaffold for `wrangler.toml` and bindings is planned.

## Capabilities

**Core orchestration**. Parallel execution, git worktree isolation, janitor verification, quality gates (lint, types, PII scan), cross-model code review, circuit breaker for misbehaving agents, token growth monitoring with auto-intervention.

**Intelligence**. Contextual bandit router for model/effort selection. Knowledge graph for codebase impact analysis. Semantic caching saves tokens on repeated patterns. Cost anomaly detection (burn-rate alerts). Behavior anomaly detection with Z-score flagging.

**Sandboxing**. Pluggable [`SandboxBackend`](docs/architecture/sandbox.md) protocol — run agents in local git worktrees (default), Docker containers, [E2B](https://e2b.dev) Firecracker microVMs, or [Modal](https://modal.com) serverless containers (with optional GPU). Plugin authors can register custom backends through the `bernstein.sandbox_backends` entry-point group. Inspect installed backends with `bernstein agents sandbox-backends`.

**Artifact storage**. `.sdd/` state can stream to pluggable [`ArtifactSink`](docs/architecture/storage.md) backends: local filesystem (default), S3, Google Cloud Storage, Azure Blob, or Cloudflare R2. `BufferedSink` keeps the WAL crash-safety contract by writing locally with fsync first and mirroring to the remote asynchronously.

**Skill packs**. Progressive-disclosure [skills](docs/architecture/skills.md) (OpenAI Agents SDK pattern): only a compact skill index ships in every spawn's system prompt, agents pull full bodies via the `load_skill` MCP tool on demand. 17 built-in role packs plus third-party `bernstein.skill_sources` entry-points.

**Controls**. HMAC-chained audit logs, policy engine, PII output gating, WAL-backed crash recovery (experimental multi-worker safety), OAuth 2.0 PKCE. SSO/SAML/OIDC support is in progress.

**Observability**. Prometheus `/metrics`, OTel exporter presets, Grafana dashboards. Per-model cost tracking (`bernstein cost`). Terminal TUI and web dashboard. Agent process visibility in `ps`.

**Ecosystem**. MCP server mode, A2A protocol support, GitHub App integration, pluggy-based plugin system, multi-repo workspaces, cluster mode for distributed execution, self-evolution via `--evolve` (experimental).

Full feature matrix: [FEATURE_MATRIX.md](docs/reference/FEATURE_MATRIX.md) &middot; Recent features: [What's New](docs/whats-new.md)

## What's new in v1.9

**ACP bridge** — `bernstein acp serve --stdio` exposes Bernstein to any editor that speaks the Agent Communication Protocol (Zed, etc.). No plugin code needed on the editor side.

**Autonomous CI repair** — `bernstein autofix` watches open Bernstein PRs and, when CI turns red, spawns a fixer agent automatically. Once green, it pushes the fix and re-requests review.

**Credential vault** — `bernstein connect <provider>` writes API keys to the OS keychain; `bernstein creds` lists and rotates them. Agents inherit scoped credentials without touching environment variables.

**Preview tunnels** — `bernstein preview start` boots a sandboxed dev server and prints a public URL. Useful for sharing a running branch with a reviewer without deploying to staging.

Full changelog: [docs/whats-new.md](docs/whats-new.md)

## Operator commands

Commands that eliminate the glue code most teams end up writing around their runs.

| Command | What it does |
|---------|--------------|
| `bernstein pr` | Auto-creates a GitHub PR from a completed session; body carries the janitor's gate results and token/USD cost breakdown. |
| `bernstein from-ticket <url>` | Imports a Linear / GitHub Issues / Jira ticket as a Bernstein task. Label-based role + scope inference. Supports `--dry-run` and `--run`. |
| `bernstein ticket import <url>` | Alias / group form of `from-ticket` for scripting. |
| `bernstein remote` | SSH sandbox backend. `remote test <host>`, `remote run <host> <path>`, `remote forget <host>`. ControlMaster socket reuse for fast repeat calls. |
| `bernstein hooks` | Lifecycle hooks for `pre_task`, `post_task`, `pre_merge`, `post_merge`, `pre_spawn`, `post_spawn` — shell scripts or pluggy `@hookimpl`s. `hooks list`, `hooks run <event>`, `hooks check`. |
| `bernstein chat serve --platform=telegram\|discord\|slack` | Drive runs from chat with `/run`, `/status`, `/approve`, `/reject`, `/switch`, `/stop`. |
| `bernstein approve-tool` / `bernstein reject-tool` | Interactive mid-run tool-call approval. `--latest`, `--id`, `--always`. |
| `bernstein tunnel start <port> [--provider auto\|cloudflared\|ngrok\|bore\|tailscale]` | One wrapper around four tunnel providers. Also `tunnel list`, `tunnel stop <name>\|--all`. ControlMaster-style process reuse. |
| `bernstein daemon install [--user\|--system] [--command="..."] [--env KEY=VAL]...` | Installs a systemd (Linux) or launchd (macOS) unit for auto-start. Also `daemon start/stop/restart/status/uninstall`. |
| `bernstein connect <provider>` / `bernstein creds` | Stores and rotates API credentials in the OS keychain. Agents inherit scoped keys per-run. |
| `bernstein autofix` | Daemon that monitors open Bernstein PRs; spawns a fixer agent when CI fails and pushes the repair automatically. |
| `bernstein preview start` | Starts a sandboxed dev server for the current branch and prints a shareable public tunnel URL. |

## How it compares

| Feature | Bernstein | CrewAI | AutoGen [^autogen] | LangGraph |
|---------|-----------|--------|---------|-----------|
| Orchestrator | Deterministic code | LLM-driven (+ code Flows) | LLM-driven | Graph + LLM |
| Works with | Any CLI agent (31 adapters) | Python SDK classes | Python agents | LangChain nodes |
| Git isolation | Worktrees per agent | No | No | No |
| Pluggable sandboxes | Worktree, Docker, E2B, Modal | No | No | No |
| Verification | Janitor + quality gates | Guardrails + Pydantic output | Termination conditions | Conditional edges |
| Cost tracking | Built-in | `usage_metrics` | `RequestUsage` | Via LangSmith |
| State model | File-based (.sdd/) | In-memory + SQLite checkpoint | In-memory | Checkpointer |
| Remote artifact sinks | S3, GCS, Azure Blob, R2 | No | No | No |
| Self-evolution | Built-in (experimental) | No | No | No |
| Declarative plans (YAML) | Yes | Yes (`agents.yaml`, `tasks.yaml`) | No | Partial (`langgraph.json`) |
| Model routing per task | Yes | Per-agent LLM | Per-agent `model_client` | Per-node (manual) |
| MCP support | Yes (client + server) | Yes | Yes (client + workbench) | Yes (client + server) |
| Agent-to-agent chat | Bulletin board | Yes (Crew process) | Yes (group chat) | Yes (supervisor, swarm) |
| Web UI | TUI + web dashboard | CrewAI AMP | AutoGen Studio | LangGraph Studio + LangSmith |
| Cloud hosted option | Yes (Cloudflare) | Yes (CrewAI AMP) | No | Yes (LangGraph Cloud) |
| Built-in RAG/retrieval | Yes (codebase FTS5 + BM25) | `crewai_tools` | `autogen_ext` retrievers | Via LangChain |

*Last verified: 2026-04-19. See [full comparison pages](docs/compare/README.md) for detailed feature matrices.*

The table above compares Bernstein against LLM-orchestration frameworks (they orchestrate LLM calls). The table below covers the closer category — other tools that orchestrate **CLI coding agents**:

| Feature | Bernstein | [ComposioHQ/agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator) | [emdash](https://github.com/generalaction/emdash) | [umputun/ralphex](https://github.com/umputun/ralphex) |
|---------|-----------|-----------|-----------|-----------|
| Shape | Python CLI + library + MCP server | TypeScript CLI + local dashboard | Electron desktop app | Go CLI |
| Primary language | Python | TypeScript | TypeScript | Go |
| Install | `pipx install bernstein` | `npm install -g @aoagents/ao` | `.dmg` / `.msi` / `.AppImage` | `go install` / single binary |
| Agent adapters | 31 | 3 (Claude Code, Codex, Aider) | 24 | 1 (Claude Code only) |
| Parallel multi-agent execution | Yes | Yes | Yes | No (single sequential session) |
| Git worktree per agent | Yes | Yes | Yes | Optional `--worktree` flag |
| MCP server mode (exposes self as MCP) | Yes (stdio + HTTP/SSE) | No | No | No |
| Coordinator | Deterministic Python scheduler | LLM-driven | Not documented | Linear plan executor |
| HMAC-chained audit replay | Yes | No | No | No |
| Cross-model verifier / quality gates | Yes (multi-stage) | No | No | Multi-phase review (Claude only) |
| Autonomous CI-fix / PR flow | Yes (`bernstein autofix`) | Yes | No | No |
| Visual dashboard | TUI + web | Web | Desktop app | Web (`--serve`) |
| Notification sinks | Telegram/Slack/Discord/Email/Webhook/Shell | No | No | Telegram / Email / Slack / Webhook |
| Backing | Solo OSS | Funded (Composio.dev) | YC W26 | Solo OSS |
| License | Apache 2.0 | MIT | Apache 2.0 | MIT |

Bernstein's wedge in this category: **Python-native, MCP-server-first, widest adapter coverage, true multi-agent parallelism**. If your stack is TypeScript and you want a product with a dashboard, Composio's `@aoagents/ao` is a better fit; if you want a polished desktop ADE, emdash is; if you only use Claude Code and want a single Go binary that walks a plan top-to-bottom, ralphex is. If you want a primitive that imports into Python, exposes itself over MCP to any client, runs many agents in parallel, and covers the full agent breadth (including Qwen, Goose, Ollama, OpenAI Agents SDK, Cloudflare Agents, and more) — Bernstein.

[^autogen]: AutoGen is in maintenance mode; successor is Microsoft Agent Framework 1.0.

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
bernstein skills list    # discoverable skill packs (progressive disclosure)
bernstein skills show <name>  # print a skill body with its references
```

```bash
bernstein fingerprint build --corpus-dir ~/oss-corpus  # build local similarity index
bernstein fingerprint check src/foo.py                 # check generated code against the index
```

## Install

| Method | Command |
|--------|---------|
| **One-liner (macOS / Linux)** | `curl -fsSL https://bernstein.run/install.sh \| sh` |
| **One-liner (Windows)** | `irm https://bernstein.run/install.ps1 \| iex` |
| **pip** | `pip install bernstein` |
| **pipx** | `pipx install bernstein` |
| **uv** | `uv tool install bernstein` |
| **Homebrew** | `brew tap chernistry/bernstein && brew install bernstein` |
| **Fedora / RHEL** | `sudo dnf copr enable alexchernysh/bernstein && sudo dnf install bernstein` |
| **npm** (wrapper) | `npx bernstein-orchestrator` |

The one-liner scripts check for Python 3.12+, bootstrap pipx when it's missing, fix PATH for the current session, and install (or upgrade) `bernstein`. They handle brew-managed macOS environments and the Windows `py -3` launcher fallback. Script sources: [install.sh](scripts/install.sh) · [install.ps1](scripts/install.ps1).

### Optional extras

Provider SDKs are optional so the base install stays lean. Pick what you need:

| Extra | Enables |
|-------|---------|
| `bernstein[openai]` | OpenAI Agents SDK v2 adapter (`openai_agents`) |
| `bernstein[docker]` | Docker sandbox backend |
| `bernstein[e2b]` | [E2B](https://e2b.dev) microVM sandbox backend (needs `E2B_API_KEY`) |
| `bernstein[modal]` | [Modal](https://modal.com) sandbox backend, optional GPU (needs `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`) |
| `bernstein[s3]` | S3 artifact sink (via `boto3`) |
| `bernstein[gcs]` | Google Cloud Storage artifact sink |
| `bernstein[azure]` | Azure Blob artifact sink |
| `bernstein[r2]` | Cloudflare R2 artifact sink (S3-compatible `boto3`) |
| `bernstein[grpc]` | gRPC bridge |
| `bernstein[k8s]` | Kubernetes integrations |

Combine extras with brackets, e.g. `pip install 'bernstein[openai,docker,s3]'`.

Editor extensions: [VS Marketplace](https://marketplace.visualstudio.com/items?itemName=alex-chernysh.bernstein) &middot; [Open VSX](https://open-vsx.org/extension/alex-chernysh/bernstein)

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and code style.

## Support

If Bernstein saves you time: [GitHub Sponsors](https://github.com/sponsors/chernistry)

Contact: [forte@bernstein.run](mailto:forte@bernstein.run)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=chernistry/bernstein&type=Date)](https://star-history.com/#chernistry/bernstein&Date)

## License

[Apache License 2.0](LICENSE)

---

Made with love by [Alex Chernysh](https://alexchernysh.com) &middot; [GitHub](https://github.com/chernistry) &middot; [bernstein.run](https://bernstein.run)

<!-- mcp-name: io.github.chernistry/bernstein -->
