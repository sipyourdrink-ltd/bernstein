# Adapter Selection Guide

Bernstein supports 19 adapters for different CLI coding agents. This guide helps
you pick the right one for your use case.

All adapters implement the `CLIAdapter` interface (`adapters/base.py`): `spawn()`,
process monitoring via PID, log capture to `.sdd/runtime/<session>.log`, and
timeout watchdog with SIGTERM-then-SIGKILL cleanup.

Source of truth: `src/bernstein/adapters/registry.py`, individual adapter files.

---

## Comparison Matrix

| Adapter | Provider | Models | Reasoning | Cost Tier | Tool Use | Structured Output | MCP | Recommended Use Case |
|---------|----------|--------|-----------|-----------|----------|-------------------|-----|----------------------|
| `claude` | Anthropic | opus, sonnet, haiku | ★★★★★ (opus) / ★★★★ (sonnet) / ★★ (haiku) | $$–$$$ | Full (role-scoped) | JSON schema enforced | Yes | Primary workhorse — architecture, features, tests, docs |
| `codex` | OpenAI | o3, o4-mini, gpt-4o | ★★★★★ (o3/o4) / ★★★★ (gpt-4o) | $$–$$$ | Full | JSON (`--json`) | No | Provider diversity; OpenAI reasoning models |
| `gemini` | Google | gemini-2.5-pro, flash | ★★★★ (pro) / ★★★ (flash) | Free–$$$ | Full | JSON (`--output-format json`) | No | Free-tier usage; cost-effective medium tasks |
| `aider` | Multi | Any (Anthropic/OpenAI/Azure) | Inherited from model | $–$$$ | File editing | No | Commit-per-change workflows; focused file edits |
| `amp` | Sourcegraph | Anthropic + OpenAI models | ★★★★★ (opus/o3) | $$–$$$ | Full | No | Sourcegraph-integrated teams; codebase-aware context |
| `qwen` | Multi | qwen3-coder, qwen3.6-plus | ★★★ | Free–$$ | Full | No | Cost-sensitive; low-complexity tasks; free OpenRouter |
| `ollama` | Local | deepseek-r1, qwen2.5-coder, phi4 | ★★★ (r1:70b) / ★★ (7b) | Free | File editing (via Aider) | No | Air-gapped; privacy-sensitive; zero API cost |
| `cody` | Sourcegraph | Anthropic/OpenAI/Google (via SG) | Inherited from model | $$ | Chat only | No | Sourcegraph-integrated with codebase-level context |
| `cursor` | Cursor | Cursor's model routing | ★★★★ | $$ | Full | No | Teams with Cursor subscriptions |
| `goose` | Block | Anthropic models | ★★★★ | $$–$$$ | Full | No | Teams already using Block's Goose |
| `roo-code` | Multi | Anthropic + OpenAI | ★★★★ | $$–$$$ | Full | JSON (`--output-format json`) | No | VS Code extension users wanting headless CLI |
| `continue` | Multi | Anthropic/OpenAI/Google | Inherited from model | $–$$$ | Full | No | Teams with existing Continue.dev configurations |
| `opencode` | Multi | Any configured provider | Inherited from model | $–$$$ | Full | JSON (`--format json`) | No | Multi-provider setups; single CLI interface |
| `kiro` | AWS | AWS-managed models | ★★★ | $$ | Full | No | AWS-centric teams using AWS AI services |
| `kilo` | Stackblitz | Any (via provider routing) | Inherited from model | $–$$$ | Full | No | Web development; Stackblitz-integrated teams |
| `tabby` | Self-hosted | Server-configured model | Varies | Free | Agent tasks | No | Self-hosted; compliance-restricted; full model control |
| `iac` | N/A | N/A (Terraform/Pulumi) | N/A | N/A | IaC plan+apply | No | Infrastructure tasks — pair with LLM adapter for codegen |
| `generic` | Any | Pass-through | Depends on CLI | Varies | Depends on CLI | No | Unlisted CLIs; prototyping new adapters |
| `mock` | None | None (simulated) | N/A | Free | Simulated | Simulated | Unit and integration tests only |

**Reasoning key:** ★★★★★ Exceptional (frontier reasoning) · ★★★★ Strong · ★★★ Good · ★★ Basic · ★ Minimal  
**Cost tier key:** Free = no API cost · $ = <$0.01/task · $$ = $0.01–$0.10/task · $$$ = $0.10+/task  
Actual costs depend on task complexity, token usage, and provider pricing.

---

## Detailed Adapter Profiles

### claude (Anthropic Claude Code)

The primary adapter. Deepest integration with Bernstein.

**Unique features:**
- Role-scoped tool allowlists (qa agents get read-only tools, docs agents get write-only, etc.)
- Structured output via `--json-schema` enforcing `{status, summary, files_changed, exit_reason}`
- Automatic fallback model chain: opus -> sonnet -> haiku
- Effort-to-max-turns mapping: max=100, high=50, medium=30, low=15
- Rate limit probing: real API call to detect account-level limits before spawning
- `--permission-mode bypassPermissions` for autonomous execution
- `--agents` flag for per-task subagent definitions
- `--append-system-prompt` for orchestration context injection
- MCP config injection from `~/.claude/mcp.json` + project overrides
- Stream-JSON output format for real-time parsing

**Model mapping:**
| Short name | Claude model ID |
|------------|----------------|
| `opus` | `claude-opus-4-6` |
| `sonnet` | `claude-sonnet-4-6` |
| `haiku` | `claude-haiku-4-5-20251001` |

**Env vars:** `ANTHROPIC_API_KEY` (required).

**Best for:** Primary workhorse. Use for all task types. Opus for architecture/security, sonnet for features/tests, haiku for docs/formatting.

---

### codex (OpenAI Codex CLI)

**Unique features:**
- Full-auto mode (`--full-auto`)
- JSON output with `--json`
- Output written to a `.last-message.txt` file
- Tier detection from API key format (`sk-proj` = Pro, `sk-` = Plus, other = Free)

**Model mapping:** Direct pass-through of `model_config.model` (e.g., `o3`, `gpt-4o`).

**Env vars:** `OPENAI_API_KEY` (required), `OPENAI_ORG_ID` (optional, triggers Enterprise tier), `OPENAI_BASE_URL` (optional).

**Best for:** Tasks that benefit from OpenAI's reasoning models. Good complement to Claude for provider diversity.

---

### gemini (Google Gemini CLI)

**Unique features:**
- YOLO mode (`--yolo`) for autonomous execution
- JSON output format
- Tier detection: GCP project = Enterprise, `AIza` key prefix = Pro
- Supports both `GOOGLE_API_KEY` and `GEMINI_API_KEY`

**Model mapping:** Direct pass-through (e.g., `gemini-2.5-pro`, `gemini-2.5-flash`).

**Env vars:** `GOOGLE_API_KEY` or `GEMINI_API_KEY` (one required), `GOOGLE_CLOUD_PROJECT` (optional, Enterprise tier), `GOOGLE_APPLICATION_CREDENTIALS` (optional).

**Best for:** Free tier users (generous free quota). Cost-effective for medium-complexity tasks. Good as a tertiary provider for rate-limit resilience.

---

### aider

**Unique features:**
- Non-interactive mode via `--message` + `--yes`
- Auto-commits each change (clean worktree history)
- Larger repo map (`--map-tokens 2048`) for better codebase navigation
- No auto-lint (orchestrator handles linting)
- Multi-provider: works with Anthropic, OpenAI, and Azure models

**Model mapping:**
| Short name | Aider model ID |
|------------|---------------|
| `opus` | `anthropic/claude-opus-4-6` |
| `sonnet` | `anthropic/claude-sonnet-4-6` |
| `haiku` | `anthropic/claude-haiku-4-5-20251001` |
| `gpt-4o` | `openai/gpt-4o` |
| `gpt-4.1` | `openai/gpt-4.1` |

**Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` (at least one).

**Best for:** Focused file-editing tasks. Particularly good when you want per-change commits in the worktree history.

---

### amp (Sourcegraph Amp)

**Unique features:**
- Headless mode (`--headless`)
- Supports both Anthropic and OpenAI models with provider-prefixed IDs

**Model mapping:**
| Short name | Amp model ID |
|------------|-------------|
| `opus` | `anthropic:claude-opus-4-6` |
| `sonnet` | `anthropic:claude-sonnet-4-6` |
| `gpt-5.4` | `openai:gpt-5.4` |
| `o3` | `openai:o3` |

**Env vars:** `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, plus optional `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN` for Sourcegraph integration.

**Best for:** Teams already using Sourcegraph for code search. Codebase-aware context from Sourcegraph indexing.

---

### qwen

**Unique features:**
- OpenAI-compatible endpoint routing with multiple free/cheap providers
- Auto-detects provider from env vars: OpenRouter (paid/free), Together, Oxen, G4F
- Maps Bernstein tier names to native Qwen models (opus -> qwen3.6-plus, haiku -> qwen3-coder-plus)
- Optional Tavily web search integration

**Provider tiers:**
| Provider | Tier | RPM | TPM |
|----------|------|-----|-----|
| OpenRouter (paid) | Pro | 200 | 20,000 |
| OpenRouter (free) | Free | 20 | 2,000 |
| Together | Plus | 60 | 6,000 |
| Oxen | Pro | 100 | 10,000 |
| G4F | Free | 10 | 1,000 |

**Env vars:** Provider-specific (see `LLMSettings`).

**Best for:** Cost-sensitive deployments. Free-tier usage through OpenRouter or G4F. Good for low-complexity tasks where you want to avoid Anthropic/OpenAI costs.

---

### ollama (Local LLMs)

**Unique features:**
- Zero cloud API cost
- Uses Aider as the coding frontend with Ollama as the LLM backend
- Works in air-gapped and privacy-sensitive environments
- Supports all Ollama-compatible models

**Model mapping:**
| Short name | Ollama model |
|------------|-------------|
| `opus` | `deepseek-r1:70b` |
| `sonnet` | `qwen2.5-coder:32b` |
| `haiku` | `qwen2.5-coder:7b` |
| `codellama` | `codellama` |
| `deepseek-r1` | `deepseek-r1` |
| `phi4` | `phi4` |

**Env vars:** None required. `OLLAMA_BASE_URL` (optional, default `http://localhost:11434`).

**Prerequisites:** `ollama` running locally + `aider-chat` installed + model pulled (`ollama pull qwen2.5-coder:7b`).

**Best for:** Air-gapped environments, privacy-sensitive code, cost-zero experimentation, local development without API keys.

---

### cody (Sourcegraph Cody)

**Model mapping:** Uses `provider::version::model` format (e.g., `anthropic::2024-10-22::claude-sonnet-4-5`).

**Env vars:** `SRC_ACCESS_TOKEN`, `SRC_ENDPOINT` (default: `https://sourcegraph.com`).

**Best for:** Sourcegraph-integrated workflows with codebase-level context.

---

### cursor

**Unique features:**
- Session isolation via separate `--user-data-dir` per agent
- MCP config injection via `--add-mcp`
- Auth via OAuth session in `~/.cursor/` (no env vars needed)

**Best for:** Teams with Cursor subscriptions who want to leverage Cursor's model routing.

---

### goose (Block)

**Env vars:** Depends on configured model provider.

**Best for:** Teams already using Block's Goose for autonomous task execution.

---

### roo-code

**Unique features:**
- JSON structured output via `--output-format json`
- Task passed via `--task` flag

**Best for:** VS Code extension users wanting headless CLI execution.

---

### continue

**Unique features:**
- Config-driven model and context setup via `~/.continue/config.yaml`
- MCP managed via config file (not runtime injection)

**Best for:** Teams with existing Continue.dev configurations.

---

### opencode

**Unique features:**
- Multi-provider support (OpenAI, Anthropic, Google, OpenRouter, xAI)
- JSON output format
- Auth via `opencode auth login` or env vars

**Best for:** Multi-provider setups wanting a single CLI interface.

---

### kiro (AWS)

**Unique features:**
- Non-interactive chat mode with `--trust-all-tools`
- Model selection controlled by Kiro settings (no per-run flag)
- AWS auth integration (`AWS_PROFILE`, `AWS_REGION`)

**Best for:** AWS-centric teams using AWS-managed AI services.

---

### kilo (Stackblitz)

**Unique features:**
- ACP/MCP protocol support
- MCP config injection via `--mcp` flag
- Auto-approve mode (`--yes`)

**Best for:** Web development workflows, Stackblitz-integrated teams.

---

### tabby (Self-hosted)

**Unique features:**
- Requires a running Tabby server (`tabby serve`)
- Model selection is server-side (not per-invocation)
- Zero cloud dependency when using local models

**Env vars:** `TABBY_SERVER_URL` (default: `http://127.0.0.1:8080`).

**Best for:** Self-hosted, air-gapped, or compliance-restricted environments where you control the entire model stack.

---

### iac (Infrastructure as Code)

**Unique features:**
- Not an LLM adapter. Runs Terraform or Pulumi plan+apply sequences.
- Enforces dry-run safety: plan/preview always runs before apply.
- Auto-detects available IaC tool (Terraform first, then Pulumi).

**Best for:** Infrastructure tasks in the orchestration pipeline. Pair with an LLM adapter for generating the IaC code, then use `iac` for applying it.

---

### generic

**Unique features:**
- Wraps any CLI command with configurable flags
- Constructor args: `cli_command`, `prompt_flag`, `model_flag`, `extra_args`, `display_name`
- Used as fallback when `cli: generic` is set in config

**Best for:** Integrating unlisted CLIs. Prototype adapter for new tools before writing a dedicated adapter.

---

### mock (Testing only)

Simulates agent behavior for unit and integration tests. Not for production use.

---

## Adapter Selection Decision Tree

1. **Do you need zero cloud cost?**
   - Yes, and have GPU -> `ollama`
   - Yes, and have Tabby server -> `tabby`
   - Yes, and want free tier API -> `gemini` or `qwen` (with free OpenRouter)

2. **Do you need the strongest reasoning?**
   - Claude Opus -> `claude` with `model: opus`
   - OpenAI o3 -> `codex` or `amp`
   - Want to compare both -> use `TierAwareRouter` with multiple providers

3. **Do you need structured output?**
   - `claude` (JSON schema enforced), `codex` (JSON), `gemini` (JSON), `roo-code` (JSON), `opencode` (JSON)

4. **Do you need MCP support?**
   - `claude` (deepest), `cursor`, `kilo`

5. **Do you need air-gapped / self-hosted?**
   - `ollama` (local Ollama), `tabby` (self-hosted server)

6. **Do you need multi-provider diversity?**
   - Primary: `claude`, Secondary: `codex` or `gemini`, Tertiary: `qwen`
   - The `TierAwareRouter` handles failover, cost balancing, and rate-limit avoidance across providers automatically.

---

## Registering Custom Adapters

Two approaches:

**1. Entry point plugin:**
```python
# In your package's pyproject.toml:
[project.entry-points."bernstein.adapters"]
my_agent = "my_package.adapter:MyAdapter"
```
The adapter is discovered automatically on first use via `importlib.metadata.entry_points`.

**2. Runtime registration:**
```python
from bernstein.adapters.registry import register_adapter
from my_package import MyAdapter

register_adapter("my-agent", MyAdapter)
```

Your adapter must subclass `CLIAdapter` and implement `spawn()` returning a `SpawnResult` with `pid` and `log_path`.
