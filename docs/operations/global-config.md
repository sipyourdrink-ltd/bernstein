# Global config CLI (`bernstein config`)

`bernstein config` reads and writes the per-operator global config file at
`~/.bernstein/config.yaml`. This is a different layer from
[`bernstein.yaml`, `.sdd/config.yaml`, and the tunable defaults documented in
the configuration reference](CONFIG.md): those are per-project files checked
into (or living next to) a repo, while `~/.bernstein/config.yaml` holds an
operator's cross-project defaults (default CLI adapter, budget, effort,
concurrency) that apply unless a project overrides them.

## How to use it

```
bernstein config set <key> <value>     # write to ~/.bernstein/config.yaml
bernstein config get <key>             # show the effective value + its source
bernstein config list                  # show every known key, value, and source
bernstein config diff                  # diff project bernstein.yaml against built-in defaults
bernstein config validate              # validate project model policy / providers
bernstein config conflicts             # show settings where sources disagree
bernstein config view-mode <mode>      # set dashboard detail level
```

### `bernstein config set KEY VALUE`

Writes `KEY` into `~/.bernstein/config.yaml`. Numeric-looking values are
coerced to `int`/`float`; `null`/`none` (case-insensitive) is stored as
`None`; everything else is stored as a string.

```
bernstein config set cli codex
bernstein config set budget 25
bernstein config set max_agents 4
```

### `bernstein config get KEY`

Prints the effective value for `KEY` and which layer it came from, plus the
full resolution chain.

```
bernstein config get cli
# cli = 'codex'  (source: global)
# resolution: session -> project -> context -> global -> default
```

Flags: `--project-dir DIR` (default `.`) — the project root used to load
`.sdd/config.yaml` for the precedence check.

### `bernstein config list`

Same resolution, applied to every key in the built-in defaults table
(`cli`, `budget`, `max_agents`, `effort`, `model`), rendered as a table of
key / value / source / full resolution chain. Same `--project-dir` flag as
`get`.

### `bernstein config diff`

Different scope from the commands above: it reads `bernstein.yaml` (or
`bernstein.yml`) in the current directory — the **project seed**, not the
global config file — and prints every key that differs from Bernstein's
built-in defaults (added / changed / removed). Empty output means the
project seed matches defaults exactly, or no seed file is present.

### `bernstein config validate`

Validates the project's model policy and provider configuration: loads
`bernstein.yaml` (from the current directory or a parent), loads
`.sdd/config/providers.yaml` and `.sdd/config/model_policy.yaml` (or the
model policy embedded in `bernstein.yaml`) into a `TierAwareRouter`, and
runs `router.validate_policy()`. Exits non-zero and lists each issue when
validation fails (e.g. allow/deny conflicts, a preferred provider excluded
by its own allow list, or a tier with no available provider once policy
constraints apply). On success, prints a provider summary table (tier,
region, health, policy-allowed, residency).

### `bernstein config conflicts`

Resolves every config key across all sources and reports two kinds of
problems, using `--project-dir` the same way as `get`/`list`:

- **Setting conflicts** — the same key has different values across sources
  (e.g. `.sdd/config.yaml` and `~/.bernstein/config.yaml` disagree).
- **Policy violations** — a source sets a key it isn't allowed to set
  (`check_source_policies`).

Prints "No setting conflicts or policy violations detected." when clean.

### `bernstein config view-mode {novice|standard|expert}`

Sets the dashboard/CLI detail level for the current project, persisted to
`.sdd/config.yaml`. `novice` trims output to the essentials; `expert` shows
full detail.

## Precedence

`bernstein config get`/`list`/`conflicts` resolve a key across, from
highest to lowest precedence:

1. **seed** — the run-seed (`bernstein.yaml`) value actually enforced by the
   orchestrator, when present.
2. **session** — environment overrides (`BERNSTEIN_CLI`, `BERNSTEIN_BUDGET`,
   `BERNSTEIN_MAX_AGENTS`, `BERNSTEIN_EFFORT`, `BERNSTEIN_MODEL`) or
   caller-provided session overrides.
3. **project** — `<project>/.sdd/config.yaml`.
4. **context** — the active operating context, when one is selected (see
   `bernstein ctx` in [`CONFIG.md`](CONFIG.md#operating-contexts-bernstein-ctx)).
5. **global** — `~/.bernstein/config.yaml`, the file `bernstein config set`
   writes to.
6. **default** — built-in defaults (`cli=claude`, `budget=None`,
   `max_agents=6`, `effort=max`, `model=None`).

Only `bernstein config set`/`get`/`list`/`conflicts`/`view-mode` operate on
this precedence chain. `bernstein config diff` is a separate, narrower tool
that only compares the project seed file to built-in defaults.

## Known config keys

| Key | Default | Meaning |
|---|---|---|
| `cli` | `claude` | Default CLI adapter (`claude`, `codex`, `gemini`, `qwen`, ...). |
| `budget` | `null` | Default spending cap in USD (`null` = unlimited). |
| `max_agents` | `6` | Default max concurrent agents. |
| `effort` | `max` | Default effort level (`max`/`medium`/`low`). |
| `model` | `null` | Default model override (`null` = adapter default). |

## Source

- `src/bernstein/cli/commands/workspace_cmd.py` — `config_group` and all
  subcommands.
- `src/bernstein/core/config/home.py` — `BernsteinHome`, `resolve_config`,
  `resolve_config_bundle`, `explain_conflicts`, `check_source_policies`.
- `src/bernstein/cli/config_diff_cli.py` — `bernstein config diff`.

## Related

- [Bernstein Configuration Reference](CONFIG.md) — `bernstein.yaml`,
  `.sdd/config.yaml`, environment variables, and tunable defaults.
