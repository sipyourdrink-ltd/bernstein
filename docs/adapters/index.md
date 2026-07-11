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
- `cloudflare` - Cloudflare Agents SDK via wrangler.
- `openai_agents` - in-process OpenAI Agents SDK v2 (requires the
  `[openai]` extra).

### Other supported CLIs

See `bernstein integrations list` for the full enumerated set. The
registry currently surfaces ~40 adapters; this page lists categories
rather than re-listing each entry so the index does not drift.

## Adding a new adapter

1. Add a `<name>.py` module under `src/bernstein/adapters/` implementing
   `CLIAdapter`.
2. Register the class in `src/bernstein/adapters/registry.py`.
3. Declare the adapter's resume / dangerous-mode / event-channel strategy
   in `STRATEGY_MATRIX` (see
   [capability_contract.md](./capability_contract.md)); the conformance
   harness fails when a registered adapter has no declaration.
4. Add an entry to `src/bernstein/adapters/use_cases.py` so the new
   adapter shows up in `bernstein integrations list` with a meaningful
   headline.
5. Cover the adapter with a conformance test under `tests/contract/`.

The contract for new adapters lives in
[ADAPTER_GUIDE.md](./ADAPTER_GUIDE.md). The typed strategy axes are
documented in [capability_contract.md](./capability_contract.md).
