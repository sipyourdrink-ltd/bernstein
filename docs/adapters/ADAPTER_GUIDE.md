# Adapter Selection Guide

Bernstein ships 37 CLI agent adapters in `src/bernstein/adapters/` (36 named
third-party wrappers plus a `generic` catch-all), along with support modules
(caching, conformance testing, environment isolation, plugin SDK, etc.).

All CLI agent adapters implement the `CLIAdapter` interface (`adapters/base.py`):
`spawn()`, process monitoring via PID, log capture to `.sdd/runtime/<session>.log`,
and timeout watchdog with SIGTERM-then-SIGKILL cleanup.

Source of truth: `src/bernstein/adapters/registry.py`, individual adapter files.

> **Quick pick**: Need the strongest results? → `claude` with `model: opus`.
> Free tier? → `gemini` or `qwen`. Air-gapped? → `ollama` or `tabby`.
> Multi-provider resilience? → combine `claude` + `codex` + `gemini`.

### Dual role: agents AND scheduler

Every adapter can serve two roles in Bernstein:

1. **As an agent** — spawned per-task to write code, run tests, commit changes
2. **As the internal scheduler LLM** — used by the orchestrator for task decomposition, cost estimation, and plan optimization

Set the scheduler model in `bernstein.yaml`:
```yaml
internal_llm_provider: gemini            # any adapter name
internal_llm_model: gemini-pro
```

This means you can run Bernstein with **zero Claude Code dependency** — use `qwen` or `gemini` for everything, or run fully air-gapped with `ollama`.

---

## Comparison Matrix

| Adapter | Provider | Models | Reasoning | Cost Tier | Tool Use | Structured Output | MCP | Recommended Use Case |
|---------|----------|--------|-----------|-----------|----------|-------------------|-----|----------------------|
| `claude` | Anthropic | opus, sonnet, haiku | ★★★★★ (opus) / ★★★★ (sonnet) / ★★ (haiku) | $$–$$$ | Full (role-scoped) | JSON schema enforced | Yes | Primary workhorse — architecture, features, tests, docs |
| `codex` | OpenAI | GPT-5, GPT-5 mini | ★★★★★ (GPT-5) / ★★★★ (mini) | $$–$$$ | Full | JSON (`--json`) | No | Provider diversity; OpenAI reasoning models |
| `openai_agents` | OpenAI (Agents SDK v2) | GPT-5, GPT-5 mini, o4 | ★★★★ | $–$$$ | Full (SDK tool protocol) | JSONL event stream | Yes (Bernstein-bridged) | OpenAI sandboxed execution with E2B / Modal / Docker |
| `gemini` | Google | Gemini Pro, Gemini Flash | ★★★★★ (Pro) / ★★★★ (Flash) | Free–$$$ | Full | JSON (`--output-format json`) | No | Free-tier usage; cost-effective medium tasks |
| `aider` | Multi | Any (Anthropic/OpenAI/Azure) | Inherited from model | $–$$$ | File editing | No | Commit-per-change workflows; focused file edits |
| `amp` | Sourcegraph | Anthropic + OpenAI models | ★★★★★ (opus/o3) | $$–$$$ | Full | No | Sourcegraph-integrated teams; codebase-aware context |
| `qwen` | Multi | qwen3-coder, qwen3.6-plus | ★★★ | Free–$$ | Full | No | Cost-sensitive; low-complexity tasks; free OpenRouter |
| `ollama` | Local | deepseek-r1, qwen2.5-coder, phi4 | ★★★ (r1:70b) / ★★ (7b) | Free | File editing (via Aider) | No | Air-gapped; privacy-sensitive; zero API cost |
| `cody` | Sourcegraph | Anthropic/OpenAI/Google (via SG) | Inherited from model | $$ | Chat only | No | Sourcegraph-integrated with codebase-level context |
| `cursor` | Cursor | Cursor's model routing | ★★★★ | $$ | Full | No | Teams with Cursor subscriptions |
| `goose` | Block | Anthropic models | ★★★★ | $$–$$$ | Full | No | Teams already using Block's Goose |
| `continue` | Multi | Anthropic/OpenAI/Google | Inherited from model | $–$$$ | Full | No | Teams with existing Continue.dev configurations |
| `opencode` | Multi | Any configured provider | Inherited from model | $–$$$ | Full | JSON (`--format json`) | No | Multi-provider setups; single CLI interface |
| `kiro` | AWS | AWS-managed models | ★★★ | $$ | Full | No | AWS-centric teams using AWS AI services |
| `kilo` | Stackblitz | Any (via provider routing) | Inherited from model | $–$$$ | Full | No | Web development; Stackblitz-integrated teams |
| `cloudflare` | Cloudflare | Workers AI models | ★★★ | Free–$$ | Full | No | Cloudflare Workers / Agents SDK users |
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

**Install:**
```bash
npm install -g @anthropic-ai/claude-code
```

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
| `opus` | `claude-opus-4-7` |
| `sonnet` | `claude-sonnet-4-6` |
| `haiku` | `claude-haiku-4-5-20251001` |

**Env vars:** `ANTHROPIC_API_KEY` (required).

**Best for:** Primary workhorse. Use for all task types. Opus for architecture/security, sonnet for features/tests, haiku for docs/formatting.

---

### codex (OpenAI Codex CLI)

**Install:**
```bash
npm install -g @openai/codex
```

**Unique features:**
- Full-auto mode (`--full-auto`)
- JSON output with `--json`
- Output written to a `.last-message.txt` file
- Tier detection from API key format (`sk-proj` = Pro, `sk-` = Plus, other = Free)

**Model mapping:** Direct pass-through of `model_config.model` (e.g., `gpt-5`, `gpt-5-mini`).

**Env vars:** `OPENAI_API_KEY` (required), `OPENAI_ORG_ID` (optional, triggers Enterprise tier), `OPENAI_BASE_URL` (optional).

**Best for:** Tasks that benefit from OpenAI's reasoning models. Good complement to Claude for provider diversity.

---

### gemini (Google Gemini CLI)

**Install:**
```bash
npm install -g @google/gemini-cli
```

**Unique features:**
- YOLO mode (`--yolo`) for autonomous execution
- JSON output format
- Tier detection: GCP project = Enterprise, `AIza` key prefix = Pro
- Supports both `GOOGLE_API_KEY` and `GEMINI_API_KEY`

**Model mapping:** Direct pass-through (e.g., `gemini-pro`, `gemini-flash`).

**Env vars:** `GOOGLE_API_KEY` or `GEMINI_API_KEY` (one required), `GOOGLE_CLOUD_PROJECT` (optional, Enterprise tier), `GOOGLE_APPLICATION_CREDENTIALS` (optional).

**Best for:** Free tier users (generous free quota). Cost-effective for medium-complexity tasks. Good as a tertiary provider for rate-limit resilience.

---

### aider

**Install:**
```bash
pip install aider-chat
# or
pipx install aider-chat
```

**Unique features:**
- Non-interactive mode via `--message` + `--yes`
- Auto-commits each change (clean worktree history)
- Larger repo map (`--map-tokens 2048`) for better codebase navigation
- No auto-lint (orchestrator handles linting)
- Multi-provider: works with Anthropic, OpenAI, and Azure models

**Model mapping:**
| Short name | Aider model ID |
|------------|---------------|
| `opus` | `anthropic/claude-opus-4-7` |
| `sonnet` | `anthropic/claude-sonnet-4-6` |
| `haiku` | `anthropic/claude-haiku-4-5-20251001` |
| `gpt-5` | `openai/gpt-5` |
| `gpt-5-mini` | `openai/gpt-5-mini` |

**Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY` (at least one).

**Best for:** Focused file-editing tasks. Particularly good when you want per-change commits in the worktree history.

---

### amp (Sourcegraph Amp)

**Install:**
```bash
npm install -g @sourcegraph/amp
```

**Unique features:**
- Headless mode (`--headless`)
- Supports both Anthropic and OpenAI models with provider-prefixed IDs

**Model mapping:**
| Short name | Amp model ID |
|------------|-------------|
| `opus` | `anthropic:claude-opus-4-7` |
| `sonnet` | `anthropic:claude-sonnet-4-6` |
| `gpt-5` | `openai:gpt-5` |
| `o3` | `openai:o3` |

**Env vars:** `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, plus optional `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN` for Sourcegraph integration.

**Best for:** Teams already using Sourcegraph for code search. Codebase-aware context from Sourcegraph indexing.

---

### qwen

**Install:** No separate CLI install required — Qwen uses OpenAI-compatible APIs via env vars.
Optionally install the web search extension:
```bash
pip install tavily-python  # for web search support
```

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

**Install:**
```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh
# Pull a model
ollama pull qwen2.5-coder:7b      # fast, low VRAM
ollama pull qwen2.5-coder:32b     # best quality
ollama pull deepseek-r1:70b       # strongest reasoning (requires 40+ GB VRAM)
# Install aider as the coding frontend
pip install aider-chat
```

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

**Install:**
```bash
npm install -g @sourcegraph/cody
```

**Model mapping:** Uses `provider::version::model` format (e.g., `anthropic::2025-05-14::claude-sonnet-4-6`).

**Env vars:** `SRC_ACCESS_TOKEN` (required), `SRC_ENDPOINT` (default: `https://sourcegraph.com`).

**Best for:** Sourcegraph-integrated workflows with codebase-level context. Cody's indexing gives agents repo-wide semantic search without manual context injection.

---

### cursor

**Install:** Download from [cursor.com](https://cursor.com). The `cursor` CLI is bundled with the desktop app.

**Unique features:**
- Session isolation via separate `--user-data-dir` per agent
- MCP config injection via `--add-mcp`
- Auth via OAuth session in `~/.cursor/` (no env vars needed)

**Best for:** Teams with Cursor subscriptions who want to use Cursor's model routing and built-in context features without managing API keys per agent.

---

### goose (Block)

**Install:**
```bash
curl -fsSL https://github.com/block/goose/releases/latest/download/install.sh | bash
# or via Homebrew
brew install block/tap/goose
```

**Unique features:**
- Session mode (`--session`) for stateful multi-turn execution
- Provider configured via `~/.config/goose/config.yaml`
- Supports extensions/plugins via goose's built-in extension system

**Env vars:** Depends on configured model provider (e.g., `ANTHROPIC_API_KEY` for Claude models).

**Best for:** Teams already using Block's Goose for autonomous task execution. Goose's extension ecosystem works within Bernstein-orchestrated runs.

---

### openai_agents (OpenAI Agents SDK v2)

**Install:**
```bash
pip install 'bernstein[openai]'
```

**Unique features:**
- Wraps the OpenAI Agents SDK v2 (`agents.Agent` + `Runner.run_sync`) in a subprocess
- Structured JSONL event stream: `start`, `tool_call`, `tool_result`, `usage`, `completion`
- Pluggable sandbox providers exposed through the SDK: `unix_local`, `docker`, `e2b`, `modal`
- Rate-limit detection via SDK exception classes mapped to Bernstein's back-off
- MCP bridging: Bernstein-managed MCP servers are forwarded through the runner manifest; the SDK never spawns its own MCP children
- Cost tracking from emitted `usage` events (`gpt-5`, `gpt-5-mini`, `o4` pricing rows)

**Env vars:** `OPENAI_API_KEY` (required), plus optional `OPENAI_BASE_URL`, `OPENAI_ORGANIZATION`, `OPENAI_PROJECT`.

**Best for:** OpenAI plans that benefit from SDK-native tool-use, sandboxed execution (E2B / Modal), or where the Agents SDK event protocol is a better fit than the `codex` CLI. See the [dedicated `openai_agents` doc](openai-agents.md) and the [decision guide](../compare/openai-agents.md) for when to pick `openai_agents` vs `codex` vs `claude`.

---

### continue

**Install:**
```bash
npm install -g @continuedev/continue
```

**Unique features:**
- Config-driven model and context setup via `~/.continue/config.yaml`
- MCP managed via config file (not runtime injection)
- Supports all major providers via config: Anthropic, OpenAI, Google, Ollama, and more

**Env vars:** Provider-specific keys as configured in `~/.continue/config.yaml`.

**Best for:** Teams with existing Continue.dev configurations. Bernstein reuses your current model setup without duplicating API key management.

---

### opencode

**Install:**
```bash
curl -fsSL https://opencode.ai/install | bash
# or via npm
npm install -g opencode-ai
```

**Unique features:**
- Multi-provider support (OpenAI, Anthropic, Google, OpenRouter, xAI)
- JSON output format via `--format json`
- Auth via `opencode auth login` or env vars

**Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY` depending on configured provider.

**Best for:** Multi-provider setups wanting a single CLI interface. OpenCode normalizes provider differences so you can switch backends by changing one config value.

---

### kiro (AWS)

**Install:** Download from [kiro.dev](https://kiro.dev). The `kiro` CLI is bundled with the desktop app.

**Unique features:**
- Non-interactive chat mode with `--trust-all-tools`
- Model selection controlled by Kiro settings (no per-run flag)
- AWS auth integration (`AWS_PROFILE`, `AWS_REGION`)

**Env vars:** `AWS_PROFILE` (optional), `AWS_REGION` (optional), `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (if not using a profile).

**Best for:** AWS-centric teams using AWS-managed AI services. Kiro's AWS Bedrock integration means billing goes through your existing AWS account.

---

### kilo (Stackblitz)

**Install:**
```bash
npm install -g kilocode
```

**Unique features:**
- ACP/MCP protocol support
- MCP config injection via `--mcp` flag
- Auto-approve mode (`--yes`)
- Provider routing via Stackblitz's model infrastructure

**Best for:** Web development workflows and Stackblitz-integrated teams. Kilo's ACP support means it can participate in Bernstein's agent-to-agent communication protocols.

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

### cloudflare_agents (Cloudflare Agents SDK)

**Install:**
```bash
npm install -g wrangler
wrangler login
```

**Unique features:**
- Spawns agents via `npx wrangler dev` with Cloudflare Workers
- Task prompt, model, and session passed as Worker `--var` flags
- Environment filtered to Cloudflare-specific keys only (credential isolation)
- Requires `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`

**Best for:** Teams running agent infrastructure on Cloudflare Workers. Development and testing with wrangler dev server.

---

### codex_cloudflare (Codex on Cloudflare Sandboxes)

Runs OpenAI Codex inside Cloudflare sandboxes for isolated, scalable execution.

**Unique features:**
- Full container sandbox with configurable memory (default 512 MiB), CPU, and network access
- Workspace synced via R2 (same bucket as other Cloudflare bridges)
- Automatic cleanup on timeout or error
- Polls sandbox status every 5 seconds until completion

**Configuration:** `CodexSandboxConfig` with `cloudflare_account_id`, `cloudflare_api_token`, `openai_api_key`, `sandbox_image`, `max_execution_minutes`, `memory_mb`, `cpu_cores`, `network_access`, `r2_bucket`.

**Best for:** Running Codex agents in isolated environments where you need container-level security and R2-based workspace sync. See the [Cloudflare Adapters guide](cloudflare-adapters.md) for full details.

---

### droid (Factory AI)

**Install:** `curl -fsSL https://app.factory.ai/cli | sh`

**Env vars:** `FACTORY_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Best for:** Teams on Factory AI's managed runtime who want Bernstein to orchestrate parallel `droid` sessions.

---

### copilot (GitHub Copilot)

**Install:** `npm install -g @github/copilot`

**Env vars:** `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_COPILOT_TOKEN`.

**Best for:** GitHub-Copilot-subscribed teams who want to reuse existing GitHub auth.

---

### hermes (Nous Research)

**Install:** `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash`

**Env vars:** `HERMES_API_KEY`, `NOUS_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Best for:** Teams running Nous Research's Hermes open-weight models.

---

### charm (Crush)

**Install:** `npm install -g @charmland/crush`

**Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`.

**Best for:** Terminal-first workflows; pairs naturally with other Charm tooling.

---

### auggie (Augment Code)

**Install:** `npm install -g @augmentcode/auggie`

**Env vars:** `AUGMENT_API_KEY`, `AUGMENT_TOKEN`.

**Best for:** Monorepos using Augment's context engine for repo-scale retrieval.

---

### kimi (Moonshot)

**Install:** `uv tool install kimi-cli`

**Env vars:** `KIMI_API_KEY`, `MOONSHOT_API_KEY`.

**Best for:** Long-context tasks that benefit from Kimi K2's extended window.

---

### rovo (Atlassian Rovo Dev)

**Install:** `acli rovodev auth login` (Atlassian CLI).

**Env vars:** `ATLASSIAN_API_TOKEN`, `ACLI_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Best for:** Atlassian-integrated teams who want Jira/Confluence context inside agent runs.

---

### cline

**Install:** `npm install -g cline`

**Env vars:** `CLINE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.

**Best for:** Cline users in VS Code who want the same agent behavior under Bernstein.

---

### codebuff

**Install:** `npm install -g codebuff`

**Env vars:** `CODEBUFF_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Best for:** Multi-file refactors that benefit from Codebuff's buffered-diff workflow.

---

### pi

**Install:** `npm install -g @mariozechner/pi-coding-agent`

**Env vars:** `PI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.

**Best for:** Scripted pipelines that want a small, low-ceremony CLI wrapper.

---

### mistral (Mistral Vibe)

**Install:** `curl -LsSf https://mistral.ai/vibe/install.sh | bash`

**Env vars:** `MISTRAL_API_KEY`.

**Best for:** Teams standardized on Mistral (Codestral, Mistral Large) for code generation.

---

### autohand

**Install:** `npm install -g autohand-cli`

**Env vars:** `AUTOHAND_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

**Best for:** Workflows that need chained tool calls inside a single agent run.

---

### forge (forgecode.dev)

**Install:** `curl -fsSL https://forgecode.dev/cli | sh`

**Env vars:** `FORGE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.

**Best for:** Teams on Forge's agent runtime who want Bernstein to manage parallel sessions.

---

### openhands (OpenHands)

**Install:** `uv tool install openhands --python 3.12` (Python 3.12+ required).

**Env vars:** `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL` (OpenHands-native), plus `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (LiteLLM provider keys).

**Invocation:** `openhands --headless --override-with-envs -t '<task>'`. The `--override-with-envs` flag is mandatory — without it OpenHands ignores env vars and reads persisted config from `~/.openhands/agent_settings.json`.

**Best for:** Teams who want OpenHands' autonomous multi-step loop (plan + edit + execute) as a single Bernstein agent. Bernstein wraps the whole loop and only sees the final exit code; OpenHands' own sub-agent steps are not visible to Bernstein's accounting.

---

### open_interpreter (Open Interpreter)

**Install:** `pip install open-interpreter`

**Env vars:** `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (Open Interpreter uses LiteLLM).

**Invocation:** `interpreter -y --model <model> '<prompt>'`. The `-y` (auto-run) flag is mandatory — without it the subprocess hangs forever on the per-code-block confirmation prompt.

**Best for:** Tasks that benefit from Open Interpreter's local code-execution loop. Bernstein's worktree isolation handles the host-level sandbox concern.

---

### gptme

**Install:** `pipx install gptme`

**Env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`.

**Invocation:** `gptme -n -m <model> '<prompt>'`. The `-n` (`--non-interactive`) flag implies `--no-confirm` and exits when the prompt is complete.

**Best for:** Lightweight terminal coding sessions. gptme is a general-purpose agent (code + shell + browser tools); Bernstein invokes it for coding tasks and leaves the browser tooling unused.

---

### plandex (Plandex)

**Install:** `curl -sL https://plandex.ai/install.sh | bash`

**Env vars:** `PLANDEX_API_KEY`, `PLANDEX_ENV`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`.

**Invocation:** `plandex tell '<prompt>' --apply --auto-exec --skip-menu --stop`. The full flag combo is required to bypass Plandex's interactive REPL — `--auto-exec` skips per-command approval, `--apply` applies pending changes, `--skip-menu` avoids the post-response menu, `--stop` exits after one response.

**Server requirement:** Plandex uses a client-server architecture. The CLI must reach Plandex Cloud or a self-hosted server (default `http://localhost:8099`). When no server is reachable, `plandex` exits early with a connection error and Bernstein surfaces it via the standard early-exit fast-fail path.

**Best for:** Teams on Plandex's plan-first workflow who want Bernstein to drive the full plan-and-execute loop as one agent.

---

### aichat

**Install:** `cargo install aichat` (or `brew install aichat`).

**Env vars:** `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`.

**Invocation:** `aichat -m <model> -- '<prompt>'`. Prompt is positional; `--` terminates flags so prompts beginning with `-` are not misparsed.

**Best for:** Lightweight tasks where a thin LLM CLI (no built-in repo navigation) is enough. AIChat does not replace coding-specific agents; use it for cost-sensitive simple tasks or as a fallback provider.

---

### letta_code (Letta Code)

**Install:** `npm install -g @letta-ai/letta-code`

**Env vars:** `LETTA_API_KEY`, `LETTA_BASE_URL`, plus `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` for the underlying model.

**Invocation:** `letta --yolo -p '<prompt>'`. The `-p` flag is the documented one-off prompt mode; `--yolo` bypasses most permission prompts.

**Caveats:** Letta Code's signature feature is cross-task memory via Letta Cloud. **Bernstein wraps Letta as a leaf-node one-shot agent** — Bernstein does not coordinate Letta's memory across tasks. Cross-task memory still works in Letta's own backend; it's just opaque to Bernstein's accounting and routing.

**Best for:** Teams running Letta Cloud who want one-shot Letta sessions inside a larger Bernstein plan.

---

### mock (Testing only)

Simulates agent behavior for unit and integration tests. Not for production use.

---

## Support Modules

In addition to the 37 CLI agent adapters above, the adapter package includes
support modules that provide cross-cutting infrastructure:

| Module | Purpose |
|--------|---------|
| `caching_adapter` | Prompt prefix deduplication and response reuse wrapper |
| `claude_agents` | Per-task Claude Code subagent definitions for `--agents` flag |
| `claude_exit_codes` | Maps Claude Code exit codes to Bernstein lifecycle enums |
| `claude_stream_parser` | Parses Claude Code `--output-format stream-json` events |
| `conformance` | Golden-transcript replay and adapter conformance validation |
| `env_isolation` | Environment variable filtering to prevent credential leakage |
| `manager` | Spawns the internal Python ManagerAgent |
| `plugin_sdk` | Base classes and utilities for third-party adapter plugins |
| `registry` | Adapter discovery and registration (entry-point and runtime) |
| `skills_injector` | Injects per-task Claude Code skills into worktrees before spawn |

---

## Adapter Selection Decision Tree

1. **Do you need zero cloud cost?**
   - Yes, and have GPU -> `ollama`
   - Yes, and want free tier API -> `gemini` or `qwen` (with free OpenRouter)

2. **Do you need the strongest reasoning?**
   - Claude Opus -> `claude` with `model: opus`
   - OpenAI GPT-5 -> `codex`, `openai_agents`, or `amp`
   - Want to compare both -> use `TierAwareRouter` with multiple providers

3. **Do you need structured output?**
   - `claude` (JSON schema enforced), `codex` (JSON), `gemini` (JSON), `openai_agents` (JSONL events), `opencode` (JSON)

4. **Do you need MCP support?**
   - `claude` (deepest), `openai_agents` (Bernstein-bridged), `cursor`, `kilo`

5. **Do you need pluggable sandbox execution (Docker / E2B / Modal)?**
   - `openai_agents` today; more adapters follow the outer `SandboxBackend`
     abstraction as phase 2 of the sandbox roadmap lands.

6. **Do you need air-gapped / self-hosted?**
   - `ollama` (local Ollama via Aider front-end)

7. **Do you need multi-provider diversity?**
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
