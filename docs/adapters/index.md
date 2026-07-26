# Integrations index

Bernstein ships ready-made adapters for the CLI coding agents under
`src/bernstein/adapters/`. This page lists every wired-in integration
with a one-line use case so you do not have to grep the source tree.

The same data is available from the CLI:

```bash
bernstein integrations list                # one line per adapter
bernstein integrations list --details      # per-adapter block with config knob
bernstein integrations list --installed    # only adapters whose binary is on $PATH
bernstein integrations list --json         # stable JSON for CI dashboards
```

Per-adapter copy lives in
[`src/bernstein/adapters/use_cases.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/use_cases.py).
That module is the single source of truth - this page links to it so we
do not maintain two copies of the same list.

## Selecting an adapter

Set the active adapter through the `cli:` knob in `bernstein.yaml`:

```yaml
cli: claude            # or any other registry key listed below
```

Use `bernstein adapters check <name>` to verify conformance for a
single adapter, and `bernstein adapters list` for a richer view that
includes source paths and conformance verdicts.

## Categories

The current registry covers four broad categories. Names below match
the registry keys you pass via `cli:`.

### Mainstream coding agents

These are the most exercised adapters in the test matrix.

- `claude` - Anthropic Claude Code CLI.
- `codex` - OpenAI Codex CLI.
- `cursor` - Cursor Agent CLI.
- `aider` - Aider pair-programming CLI.
- `gemini` - Google Gemini CLI (enterprise / API-key lane).
- `agy` - Antigravity CLI, successor for the non-enterprise Gemini path
  (see [`agy.md`](agy.md) for the lane split).
- `copilot` - GitHub Copilot CLI.
- `goose` - Block Goose.

### Local and offline

For air-gap or BYO-model scenarios.

- `ollama` - drives Aider against an Ollama or OpenAI-compatible server.
- `gptme` - local-first coding agent with shell tools.
- `mock` - test stub, no API keys or network.
- `generic` - wrap any coding agent CLI by command string.

### Specialised adapters

- `iac` - infrastructure-as-code (Terraform / Pulumi) with plan-before-apply.
- `clm` - sovereign LLM gateway over mTLS for regulated deployments.
- `openai_agents` - in-process OpenAI Agents SDK v2 (requires the
  `[openai]` extra).
- `computer_use` - fronts a third-party autonomous browser / computer-use
  agent; each action is anchored into a signed, replayable lineage chain and
  each task runs in an isolated browser profile (see
  [`computer_use.md`](computer_use.md)).
- `pydantic_ai` - Pydantic AI `clai` in one-shot mode. Built from a
  declarative capability profile rather than a hand-written module (see
  [`capability_profiles.md`](capability_profiles.md)).

### Other supported CLIs

See `bernstein integrations list` for the full enumerated set. This
page lists categories rather than re-listing each entry so the index
does not drift.

## Adding a new adapter

Most CLI agents fit the declarative path: declare a capability profile
and the factory builds the adapter, with no new module to write. See
[capability_profiles.md](./capability_profiles.md) for the schema and
the gates a profile must clear.

1. Add an `AdapterCapabilityProfile` to `_PROFILE_LIST` in
   `src/bernstein/adapters/capability_profile.py`.
2. Add the contract YAML under `tests/contract/contracts/<name>.yaml`
   pinning the always-passed CLI surface.
3. Declare the adapter's resume / dangerous-mode / event-channel strategy
   in `STRATEGY_MATRIX` (see
   [capability_contract.md](./capability_contract.md)); the conformance
   harness fails when a registered adapter has no declaration.
4. Add an entry to `src/bernstein/adapters/use_cases.py` so the new
   adapter shows up in `bernstein integrations list` with a meaningful
   headline.

An agent whose invocation does not fit the always-passed-surface shape
(conditional flags, a non-subprocess SDK, a bespoke event channel) still
gets a hand-written module instead:

1. Add a `<name>.py` module under `src/bernstein/adapters/` implementing
   `CLIAdapter`.
2. Register the class in `src/bernstein/adapters/registry.py` and add the
   module to the `adapters-independent` contract in `.importlinter`.
3. Follow steps 2 to 4 above, and cover the adapter with a conformance
   test under `tests/contract/`.

Existing hand-written adapters keep working unchanged; adopting a profile
for one is optional and does not change how it spawns.

The contract for new adapters lives in
[ADAPTER_GUIDE.md](./ADAPTER_GUIDE.md). The typed strategy axes are
documented in [capability_contract.md](./capability_contract.md).


## Install matrix

Install commands for the wired-in agents. Bernstein auto-discovers
installed CLI agents; mix them in the same run, and check what is
available locally with `bernstein integrations list --installed`.

| Agent | Install |
|-------|---------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `npm install -g @anthropic-ai/claude-code` |
| [Codex CLI](https://github.com/openai/codex) | `npm install -g @openai/codex` |
| [OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/) | `pip install 'bernstein[openai]'` |
| [GitHub Copilot CLI](https://docs.github.com/en/copilot/github-copilot-in-the-cli) | `npm install -g @github/copilot` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `npm install -g @google/gemini-cli` |
| [Antigravity CLI](agy.md) (`agy`) | Upstream installer, then `agy install` |
| [Cursor](https://www.cursor.com) | [Cursor app](https://www.cursor.com) |
| [Devin Terminal](https://devin.ai) (Cognition) | `curl -fsSL https://cli.devin.ai/install.sh \| bash` then `devin auth login` |
| [Aider](https://aider.chat) | `pip install aider-chat` |
| [Amp](https://ampcode.com) | `npm install -g @sourcegraph/amp` |
| [CLM gateway](clm.md) (sovereign / on-prem LLM) | `pip install aider-chat`, then set `CLM_ENDPOINT` / `CLM_TOKEN` |
| [Cody](https://sourcegraph.com/cody) | `npm install -g @sourcegraph/cody` |
| [Continue](https://continue.dev) | `npm install -g @continuedev/cli` (binary: `cn`) |
| [Goose](https://block.github.io/goose/) | See [Goose docs](https://block.github.io/goose/) |
| [IaC](https://www.terraform.io/) (Terraform/Pulumi) | Built-in |
| [Junie](https://junie.jetbrains.com) | `curl -fsSL https://junie.jetbrains.com/install.sh \| bash` |
| [Kilo](https://kilocode.ai) | See [Kilo docs](https://kilocode.ai) |
| [Kiro](https://kiro.dev) | See [Kiro docs](https://kiro.dev) |
| [AWS Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line.html) | `brew install --cask amazon-q` then `q login` |
| [Ollama](https://ollama.ai) + Aider | `brew install ollama` |
| [OpenCode](https://opencode.ai) | See [OpenCode docs](https://opencode.ai) |
| [Qwen](https://github.com/QwenLM/qwen-code) | `npm install -g @qwen-code/qwen-code` |
| [OpenHands](https://github.com/OpenHands/OpenHands) | `uv tool install openhands --python 3.12` |
| [Open Interpreter](https://github.com/OpenInterpreter/open-interpreter) | `pip install open-interpreter` |
| [gptme](https://github.com/gptme/gptme) | `pipx install gptme` |
| [Plandex](https://github.com/plandex-ai/plandex) | `curl -sL https://plandex.ai/install.sh \| bash` |
| [AIChat](https://github.com/sigoden/aichat) | `cargo install aichat` |
| [Letta Code](https://github.com/letta-ai/letta-code) | `npm install -g @letta-ai/letta-code` |
| **Generic** | Built-in |

Any adapter also works as the internal scheduler LLM
(`internal_llm_provider` / `internal_llm_model` in `bernstein.yaml`; see
[configuration](../operations/CONFIG.md)). Local runtimes (Ollama,
LM Studio, MLX servers) plug in as a first-class worker tier via a
`local_endpoints` profile certified with `bernstein doctor --endpoint <url>`;
see [local endpoints](../reference/local-endpoints.md).
