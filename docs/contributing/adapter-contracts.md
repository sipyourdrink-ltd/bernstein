# Adapter contracts

Bernstein wraps upstream coding-agent CLIs through a per-CLI adapter
under [`src/bernstein/adapters/`](../../src/bernstein/adapters/). Each
adapter passes a small set of flags / subcommands that the upstream CLI
must keep advertising. When a release drops or renames one of those
flags the adapter breaks silently - the spawn still runs but emits
wrong-shape output.

The **adapter contract** captures the always-passed surface so the
[`Adapter contract drift`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/.github/workflows/adapter-contract-drift.yml)
workflow can detect drift three times a day and on every PR that
touches `src/bernstein/adapters/` or `tests/contract/`. Drift is a
**hard fail** - there is no batched auto-PR. The workflow opens (or
refreshes) a tracking issue and fails CI.

Refs: [#1291](https://github.com/sipyourdrink-ltd/bernstein/issues/1291).

## File layout

```
tests/contract/contracts/<adapter>.yaml   # one per shipped adapter
src/bernstein/adapters/_contract.py       # loader + checker
src/bernstein/cli/commands/adapters_contract_cmd.py
                                          # `bernstein adapters contract-check`
tests/unit/test_adapter_contract_check.py # checker unit tests
.github/workflows/adapter-contract-drift.yml
```

## Contract schema

```yaml
adapter: claude              # registry key
binary:  claude              # name of the executable on $PATH
install:
  method: npm                # npm | pipx | curl | cargo
  spec:   "@anthropic-ai/claude-code@latest"
auth:
  # ``<binary> --help`` is expected to work without auth. Set
  # required_for_help: true only when the CLI prompts for credentials
  # on plain --help (extremely rare).
  required_for_help: false

  # Model-list requires auth on most CLIs. Set this true if the
  # workflow should skip the model check when secret_env is absent
  # rather than try and fail.
  required_for_models: false

  # The env var the CI workflow may inject so the model check runs.
  # When the variable is unset, model coverage degrades to help-only.
  secret_env: "ANTHROPIC_API_KEY"

# Tokens that MUST appear in the help output. Case-insensitive.
required_flags:
  - "--model"
  - "--output-format"

# Subcommand names that MUST appear on a token boundary in the help
# output. Used when the flags live under a subcommand (see help_command).
required_subcommands: []

# Optional. When the adapter passes flags only visible via
# ``<binary> <sub> --help`` (codex, opencode, plandex, goose, q_dev),
# override the help command here.
help_command: ["claude", "--help"]

# Optional model-presence check. Only runs when secret_env is set in CI.
expected_models:
  command: ["claude", "models", "list"]
  required_present: []
```

## Adding a new adapter contract

1. **Read the adapter source.** Open
   `src/bernstein/adapters/<adapter>.py` and identify which flags and
   subcommands the adapter passes to the CLI on **every** invocation.
   Flags wrapped in `if <opt>:` blocks are conditional - leave them
   out. Be conservative: a too-strict contract creates false drift.

2. **Inspect the upstream help.** Install the CLI locally and run
   `<binary> --help` (or `<binary> <sub> --help` when the relevant
   flags live under a subcommand). Confirm every entry you plan to put
   in `required_flags` / `required_subcommands` is visible.

3. **Pick the install method.** Free-tier, no auth required for `--help`.
   * `npm` and `pipx` packages auto-install in CI.
   * `curl` and `cargo` adapters fall back to "help-only" coverage in
     CI (the workflow notes this; the local binary check still runs
     when an operator pre-installs the CLI).

4. **Write the YAML.** Save it as `tests/contract/contracts/<name>.yaml`.

5. **Add the adapter to the matrix.** Edit
   `.github/workflows/adapter-contract-drift.yml` and add
   `- <name>` to `jobs.check.strategy.matrix.adapter`.

6. **Run locally.**

   ```bash
   bernstein adapters contract-check <name> --json
   ```

   Exit code `0` means the contract holds. Exit code `2` means the
   local CLI is missing a required token; re-check step 1 (the
   contract may be over-strict) or step 2 (the upstream CLI may have
   genuinely drifted, in which case the adapter needs updating first).

7. **Add a unit test** if the adapter exposes a new failure mode the
   existing parametrised test in
   `tests/unit/test_adapter_contract_check.py` doesn't cover.

## Optional secrets

The workflow consults the following organisation/repository secrets
when running the optional model-presence check. **None of them are
required** - adapters that need a secret simply degrade to help-only
coverage when it is absent.

| Secret                             | Used by                |
|------------------------------------|------------------------|
| `ADAPTER_CONTRACT_ANTHROPIC_API_KEY` | claude, crush, goose |
| `ADAPTER_CONTRACT_OPENAI_API_KEY`    | codex, aichat, aider, gptme |
| `ADAPTER_CONTRACT_GEMINI_API_KEY`    | gemini               |

Add a new secret only when the matching contract sets
`expected_models.required_present` and you want the workflow to verify
the listed model IDs are still served.

## What the contract is NOT

* **Not a snapshot of `--help`.** The text format upstream CLIs use
  for help output shifts frequently; a byte-level diff produces noise
  that drowns the rare real regression.
* **Not a full integration test.** The check confirms the surface the
  adapter relies on still exists. It does not verify the CLI actually
  produces correct output - that is the job of the adapter unit
  suite under [`tests/unit/adapters/`](../../tests/unit/adapters/).
* **Not auto-fixable.** When drift is detected the workflow opens a
  tracking issue; an operator updates the adapter (or the contract)
  in a follow-up PR.
