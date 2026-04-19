# `openai_agents` vs. `claude` / `codex` / `gemini`

Bernstein ships 18 CLI agent adapters as of April 2026.  Four of them are
general-purpose executors you might reach for on a typical plan.yaml
step: `claude`, `codex`, `gemini`, and `openai_agents`.  This page
explains when to prefer each one.

*Last verified: 2026-04-19.*

---

## tl;dr

| You need... | Prefer |
|-------------|--------|
| Best reasoning + long-context planning | `claude` (Opus 4.7) |
| Mature MCP + Bernstein-native tooling | `claude` |
| Cheapest reasonable reasoning, fast iteration | `codex` (o4-mini or gpt-5.4-mini) |
| OpenAI Agents SDK v2 sandboxed execution with first-class tool-use | **`openai_agents`** |
| Large-context reads (>1M tokens) | `gemini` (Gemini 3.1 Pro) |

When both `codex` and `openai_agents` could work: pick `openai_agents`
if you care about the SDK's sandbox providers (E2B, Modal, Docker) or
the structured tool-call protocol.  Pick `codex` if you just want a
cheap OpenAI agent with Bernstein doing all the orchestration.

---

## What each adapter is

**`claude`** wraps the Claude Code CLI.  It is Bernstein's most
feature-complete adapter: agent definitions, subagent spawning,
CLAUDE.md injection, cache-control blocks, hooks, session persistence,
stream-JSON parsing.  Best general-purpose executor.

**`codex`** wraps the OpenAI Codex CLI (`codex exec --full-auto`).
Thin spawner: passes a prompt, reads JSON from a last-message file,
reports cost.  Cheap, predictable, no sandbox abstraction.

**`gemini`** wraps the Gemini CLI.  Distinct value prop: very large
context windows and first-party Google tooling.

**`openai_agents`** wraps the OpenAI Agents SDK v2.  It runs a Python
subprocess that constructs `agents.Agent(...)` + `Runner.run_sync(...)`
and emits structured events.  The SDK brings sandboxed execution,
first-class tool-use, and pluggable sandbox providers (unix_local,
docker, E2B, Modal, Daytona, Cloudflare, Vercel, Runloop, Blaxel).

---

## Feature comparison

| Feature | `claude` | `codex` | `gemini` | `openai_agents` |
|---------|----------|---------|----------|-----------------|
| **Vendor** | Anthropic | OpenAI | Google | OpenAI |
| **Transport** | Claude Code CLI | `codex` CLI | `gemini` CLI | `openai-agents` Python SDK |
| **Extra install** | `brew install claude` | `npm install -g @openai/codex` | `npm install -g @google/generative-ai-cli` | `pip install 'bernstein[openai]'` |
| **Structured output** | JSON schema enforced | `--json` | `--output-format json` | JSONL event stream |
| **MCP support** | First-class | No | No | Via runner manifest (Bernstein-managed servers) |
| **Sandboxing** | CLI permission model | Full-auto only | CLI permission model | Pluggable: unix_local / docker / e2b / modal |
| **Rate-limit detection** | Yes (probe + cached cooldown) | Yes (fast-exit probe) | Yes | Yes (SDK exception classes + fast-exit) |
| **Cache tiers** | Cache read / write | No | Implicit context caching | No explicit cache API |
| **Streaming** | Stream-JSON | Line-by-line | Line-by-line | JSONL events |
| **Tool-call granularity** | Per-tool hooks | Bundled in JSON output | Bundled | Per-event (tool_call / tool_result / usage) |
| **Pricing visibility** | Yes (cache-aware) | Yes (input/output) | Yes | Yes (gpt-5 / gpt-5-mini / o4 priced) |
| **When to pick it** | Anything complex, multi-turn, or MCP-heavy | Cheap, single-file fixes | Huge-context reads | OpenAI sandbox / SDK-native tool-use |

---

## Decision tree

**Is your task going to use Bernstein-managed MCP servers (bulletin
board, task server, custom MCP tools you've registered)?**

* **Yes, and you want first-class MCP handling** → `claude`.
* **Yes, and you're on OpenAI** → `openai_agents`.  The runner
  forwards Bernstein's existing MCP servers to the SDK; you get MCP
  bridging without giving up the SDK's sandbox features.
* **No** → any adapter works; pick by cost / reasoning quality.

**Do you need a specific sandbox (E2B, Modal, Docker)?**

* **Yes** → `openai_agents`.  No other Bernstein adapter exposes
  pluggable sandbox providers today.

**Cost-sensitive, quick fixes?**

* `codex` with `o4-mini` or `gpt-5.4-mini` is cheapest.
* `openai_agents` with `gpt-5-mini` is a close second and brings
  the SDK's tool-use protocol.
* `claude` with Haiku 4.5 is competitive when you're already
  doing most of the orchestration work in Claude.

**Best reasoning quality?**

* `claude` with Opus 4.7 still wins most reasoning benchmarks.
* `openai_agents` with `o4` competes on multi-step tool-use
  tasks where the SDK's sandbox feedback loop matters more than
  raw reasoning depth.

---

## When NOT to use `openai_agents`

* **You already have a Codex-based plan that works.**  Migrating
  for the sake of migrating burns cost without buying much.
* **You don't want the `openai-agents` Python dependency.**  It's
  optional for a reason — `bernstein[openai]` adds the SDK and its
  transitive deps (pydantic, httpx, etc.).
* **You need Claude's cache-control blocks or subagent handoffs.**
  The SDK has its own handoff model but it's not the same
  abstraction — if your plan.yaml already assumes Claude Code
  subagents, keep them there.
* **You're on a deterministic-only workflow.**  The SDK ships tool
  execution as part of the agent loop, so the boundary between
  "planner" and "executor" is softer than with `codex`.

---

## Migration cheatsheet

Coming from `codex`:

```yaml
# Before
cli: codex
model: gpt-5.4-mini
```

```yaml
# After
cli: openai_agents
model: gpt-5-mini
sandbox_provider: unix_local
```

The `sandbox_provider` field is optional and defaults to `unix_local`.
Model names roughly map: `gpt-5.4` → `gpt-5`, `gpt-5.4-mini` →
`gpt-5-mini`, `o4-mini` stays as-is (both adapters accept it).

Coming from `claude`:

Usually don't migrate — if you were using Claude Code's subagent
system, MCP integration, or `--append-system-prompt`, you'll give
those up.  Consider `openai_agents` only for tasks that specifically
benefit from the SDK's sandbox providers.
