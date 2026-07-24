# Adapter capability contract

Every CLI agent expresses the same orchestration concepts differently.
Resume is `--resume <id>` for one CLI, `--session-id <id>` for another, and
a subcommand `<cli> resume <id>` for a third. "Skip permission prompts" is a
flag here, an environment variable there, and an always-on default for CLIs
with no permission system. The event surface Bernstein observes is
stream-json for some, a plain-text signal grammar for others, and upstream
hooks for the rest.

To keep the orchestrator free of `if adapter == "X"` branches, each adapter
declares its strategy on three typed axes. The orchestrator dispatches off
the enum, so adding a new adapter is a contract-completion exercise rather
than a hunt-and-patch across the core.

The enums and the declaration matrix live in
[`src/bernstein/adapters/_contract.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/_contract.py).
Strategy is **declared**, not probed: Bernstein never runs the CLI at start
just to discover its capabilities.

## The three axes

### Resume strategy (`ResumeStrategy`)

How an adapter reattaches to a prior session for `bernstein resume`.

| Value | Meaning |
| --- | --- |
| `flag` | A single flag carries the session id, e.g. `--resume <id>`. |
| `flag-pair` | Two flags: one names the existing session, one mints a new one. |
| `subcommand` | A dedicated subcommand, e.g. `<cli> resume <id>`. |
| `unsupported` | No native resume; fall back to a fresh session with scratchpad reinjection. |

The legacy two-state view (`native` / `fallback-fresh`) consumed by
`bernstein resume` is derived from this axis: any value other than
`unsupported` maps to `native`. See `resume_capability` in `_contract.py`.

### Dangerous-mode strategy (`DangerousModeStrategy`)

How an adapter is told to skip interactive permission prompts so it can run
unattended.

| Value | Meaning |
| --- | --- |
| `cli-flag` | A flag, e.g. `--yolo` or `--permission-mode bypassPermissions`. |
| `env-var` | An environment variable the CLI reads at startup. |
| `always-on` | The CLI has no permission system; it is always non-interactive. |
| `unsupported` | The CLI cannot run unattended in dangerous mode. |

### Event channel (`EventChannel`)

The surface Bernstein reads for an adapter's lifecycle signals.

| Value | Meaning |
| --- | --- |
| `stream-json` | Newline-delimited JSON events from the upstream CLI. |
| `text-signals` | Plain stdout carrying the canonical `BERNSTEIN:<KIND>` grammar (see [stream_signals.md](stream_signals.md)). |
| `hooks` | The upstream SDK fires hooks/callbacks Bernstein registers against. |
| `poll-pty` | No structured channel; Bernstein polls a PTY or log for liveness. |
| `none` | No event channel; process-exit detection only. |

## Declaring a strategy

The canonical declaration is a row in `STRATEGY_MATRIX`, keyed by registry
name:

```python
STRATEGY_MATRIX = {
    "claude": AdapterStrategy(
        resume=ResumeStrategy.FLAG,
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
    ),
    # ...
}
```

An adapter MAY instead keep the declaration next to its implementation by
setting the class attribute `strategy_override` to an `AdapterStrategy`.
Read the resolved strategy through `CLIAdapter.strategy()`, never the raw
attribute: the resolver applies the inline override first, then the matrix
keyed by registry name, then the conservative `DEFAULT_ADAPTER_STRATEGY`
(no native resume, dangerous mode unsupported, text-signal channel).

## Conformance

Every shipped adapter must declare its strategy on each axis. The
conformance harness calls `assert_strategies_declared()`, which raises
`StrategyDeclarationError` listing any registry adapter missing a row in
`STRATEGY_MATRIX`. `bernstein adapters check` surfaces the per-adapter
strategy table (`strategy_conformance_table`) so operators can compare
adapters at a glance.

## Shipped adapter declarations

| Adapter | Resume | Dangerous mode | Event channel |
| --- | --- | --- | --- |
| `aichat` | unsupported | unsupported | text-signals |
| `aider` | unsupported | unsupported | text-signals |
| `amp` | unsupported | unsupported | text-signals |
| `auggie` | unsupported | unsupported | text-signals |
| `autohand` | unsupported | unsupported | text-signals |
| `charm` | unsupported | cli-flag | text-signals |
| `claude` | flag | cli-flag | stream-json |
| `cline` | unsupported | cli-flag | text-signals |
| `clm` | unsupported | unsupported | text-signals |
| `codebuff` | unsupported | unsupported | text-signals |
| `codex` | unsupported | cli-flag | text-signals |
| `cody` | unsupported | unsupported | text-signals |
| `composio` | unsupported | unsupported | hooks |
| `continue` | unsupported | unsupported | text-signals |
| `copilot` | unsupported | unsupported | text-signals |
| `cursor` | unsupported | cli-flag | stream-json |
| `devin_terminal` | unsupported | unsupported | poll-pty |
| `droid` | unsupported | unsupported | text-signals |
| `forge` | unsupported | unsupported | text-signals |
| `gemini` | unsupported | cli-flag | stream-json |
| `generic` | unsupported | unsupported | text-signals |
| `goose` | unsupported | unsupported | text-signals |
| `gptme` | unsupported | unsupported | text-signals |
| `hermes` | unsupported | unsupported | text-signals |
| `iac` | unsupported | unsupported | text-signals |
| `junie` | unsupported | unsupported | text-signals |
| `kilo` | unsupported | unsupported | text-signals |
| `kimi` | unsupported | cli-flag | text-signals |
| `kiro` | unsupported | unsupported | text-signals |
| `letta_code` | unsupported | cli-flag | text-signals |
| `mistral` | unsupported | unsupported | text-signals |
| `mock` | unsupported | unsupported | text-signals |
| `ollama` | unsupported | unsupported | text-signals |
| `open_interpreter` | unsupported | unsupported | text-signals |
| `openai_agents` | flag | always-on | hooks |
| `opencode` | unsupported | unsupported | text-signals |
| `openhands` | unsupported | unsupported | text-signals |
| `pi` | unsupported | unsupported | text-signals |
| `plandex` | unsupported | unsupported | text-signals |
| `q_dev` | unsupported | unsupported | text-signals |
| `qwen` | unsupported | unsupported | text-signals |
| `ralphex` | unsupported | unsupported | text-signals |
| `rovo` | unsupported | cli-flag | text-signals |

The matrix is the source of truth; this table is regenerated from
`strategy_conformance_table()`. Out of scope for this contract: runtime
capability discovery, and removing every adapter-specific conditional (some
output-formatting quirks remain as overrides).

## Sampling and endpoint parameters

Adapters that can honour per-spawn sampling and endpoint overrides declare
`AdapterCapability.SUPPORTS_SAMPLING_PARAMS` in their `plugin_info()`
capabilities. The overrides travel in the per-spawn `mcp_config` and, for
`openai_agents`, land in the runner manifest as optional fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `temperature` | float | Sampling temperature. Absent = provider default. |
| `top_p` | float | Nucleus sampling. Absent = provider default. |
| `top_k` | int | Top-k sampling, forwarded for OpenAI-compatible endpoints that accept it. Absent = provider default. |
| `base_url` | str | OpenAI-compatible endpoint URL. Absent = default endpoint. |
| `api_key_env` | str | NAME of the environment variable holding the API key for `base_url`. Never a literal key. |

All fields are optional; a manifest without them behaves exactly as before.
When `base_url` is set, the runner switches the SDK to the chat-completions
API (third-party OpenAI-compatible endpoints do not serve `/responses`) and
excludes the custom client from tracing so the endpoint's key is never sent
to api.openai.com.

`api_key_env` must match `^[A-Z][A-Z0-9_]*$` AND be a known LLM provider
key (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, and the
rest of the built-in allowlist in `openai_agents_runner.py`). Any other
name - including credential-shaped names of unrelated secrets such as
`GITHUB_TOKEN` - is rejected at startup with a `config_invalid` error
(exit code 2), the same failure path as a name whose variable is not set.
To use a provider key outside the built-in set, the operator sets
`BERNSTEIN_ALLOWED_API_KEY_ENVS` (comma-separated names) on the host; a
repo-carried config cannot set host environment variables, so the
override cannot be forged by the repo. The runner logs every effective
value in its `start` event; only the env var name is ever logged, never
the key itself.

The spawn path enforces the capability: requesting any of these keys for an
adapter that does not declare `SUPPORTS_SAMPLING_PARAMS` raises
`SamplingParamsRefusal` instead of silently dropping the parameters. See
`ensure_sampling_params_supported` in
[`src/bernstein/adapters/plugin_sdk.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/plugin_sdk.py).

### Where these values come from

Three sources feed the per-spawn slots above, in ascending precedence:

1. **Mode profile** - the resolved `ModeProfile` for the spawn carries
   optional `temperature`, `top_p`, `top_k`, and `max_tokens`. They are
   folded in only when the target adapter declares
   `SUPPORTS_SAMPLING_PARAMS`, so a default profile never breaks a spawn on
   an adapter that cannot honour sampling.
2. **`role_model_policy[<role>]`** - a role may set `base_url` and
   `api_key_env` in `bernstein.yaml` to target an OpenAI-compatible
   endpoint other than the default. `api_key_env` is the NAME of an
   environment variable, validated at parse time against the same
   fail-closed credential allowlist described above; a rejected name fails
   `bernstein.yaml` parsing rather than reaching a spawn.
3. **Explicit `mcp_config`** - an operator-set value always wins over the
   two derived sources; the merge only fills slots the operator left unset.

A run that sets none of these behaves exactly as before.

## Builtin tool source

By default the runner gives the agent the tools brokered by the Bernstein
MCP gateway (`tool_source: "gateway"`). Some runs execute with no MCP
gateway reachable - an isolated worktree with no bridge, a minimal offline
environment - and would otherwise have no sanctioned way to act on the
workdir at all.

Setting `tool_source: "builtin"` in the per-spawn `mcp_config` (which the
adapter forwards to the runner manifest) opts into a small set of builtins.
They split into two confinement tiers - do not conflate them:

| Tool | Confinement | Purpose |
| --- | --- | --- |
| `read_file(path)` | Workdir-confined (builtin layer) | Read a UTF-8 file relative to the run workdir. |
| `write_file(path, content)` | Workdir-confined (builtin layer) | Write UTF-8 text relative to the run workdir. |
| `list_dir(path)` | Workdir-confined (builtin layer) | List a directory relative to the run workdir. |
| `run_command(argv)` | Restricted process-exec primitive; filesystem confinement is the OS sandbox | Run a bare-name command inside the run workdir. |

`read_file`, `write_file`, and `list_dir` are workdir-confined **at the
builtin layer**: every path argument is resolved against the run workdir, and
absolute paths and `..` escapes are rejected on the real (symlink-resolved)
target.

`run_command` is **not** workdir-sandboxed. It runs a child process, and a
child process can read and write anywhere the runner process can reach, so its
TRUE filesystem confinement is the configured OS sandbox
(`docker`/`e2b`/`modal`), not the builtin. The builtin layer applies only
defense-in-depth: `argv` is a list run with `shell=False` (no shell string, no
interpolation), and `argv[0]` must be a bare command name - absolute paths,
path separators, and known shell/interpreters (`sh`, `bash`, `python`, `env`,
...) are rejected, and the survivor is resolved on `PATH` and run by its
resolved absolute path. Because true confinement is the OS sandbox,
`run_command` is exposed **only** when the run has an OS sandbox provider
configured, or the operator explicitly opts in with
`BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND=1`. Under the bare local/worktree path
with no opt-in, `run_command` is withheld and the three file tools remain
available.

The builtins never become the default; `gateway` stays the default and any
value other than `"builtin"` selects it. The builtins enforce these
properties, each covered by tests in
[`tests/unit/adapters/test_openai_agents_builtins.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/unit/adapters/test_openai_agents_builtins.py):

1. **Path confinement** (file tools). Every path argument is resolved against
   the run workdir. Absolute paths and `..` escapes are rejected: the real
   (symlink-resolved) target must stay inside the real workdir.
2. **Restricted exec** (`run_command`). `argv` is a list run with
   `shell=False` (no shell string, no interpolation), and `argv[0]` is a bare
   command name resolved on `PATH`; absolute paths, path separators, and
   shell/interpreters are rejected. Availability is gated on an OS sandbox
   provider or the explicit opt-in.
3. **Audited.** Every call emits a `tool_call` and a `tool_result` event to
   the runner's line-delimited JSON event stream, tagged
   `"tool_source": "builtin"`, with the tool name, arguments, and outcome -
   so a run with no MCP gateway stays auditable and replayable.

See `build_builtin_tools` in
[`src/bernstein/adapters/openai_agents_builtins.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/openai_agents_builtins.py).
