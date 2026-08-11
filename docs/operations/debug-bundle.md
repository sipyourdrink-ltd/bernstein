# Debug bundle

`bernstein debug bundle` collects a self-contained, redacted ZIP — config,
recent traces, metrics, and log tails — that an operator can attach to a bug
report without manually gathering files or hand-checking them for secrets.

## CLI

```bash
bernstein debug bundle                              # bundle the most recent run
bernstein debug bundle --task <task-id>              # filter traces/metrics to one task
bernstein debug bundle --run <run-id>                # filter to one run
bernstein debug bundle --out mybundle.zip            # explicit output path
bernstein debug bundle --manifest-only               # print the manifest, write no ZIP
bernstein debug bundle --include-source-snippets 5    # also include the 5 most-recently-changed src/ files
```

| Flag | Default | Meaning |
|---|---|---|
| `--task TEXT` | none | Task id to filter traces/metrics by. |
| `--run TEXT` | none | Run id to filter traces/metrics by. |
| `--last / --no-last` | `--last` | Use the most recent run. |
| `--out`, `-o PATH` | timestamped file in CWD | Destination ZIP path. |
| `--manifest-only` | off | Print the manifest JSON to stdout instead of writing a ZIP. |
| `--include-source-snippets N` | `0` (off) | Include the N most-recently-changed `src/` files. |

## Bundle contents

Every text artefact is passed through the secret-redaction pipeline
(`core/security/redactor.py`) before being written:

| Path in ZIP | Contents |
|---|---|
| `manifest.json` | Bernstein version, Python version, OS, install method, the selection used (task/run/last), file count, redaction count. |
| `bernstein.yaml` | Redacted copy of the project config. |
| `doctor.json` | Output of `bernstein doctor --json`. |
| `traces/` | Recent `.sdd/traces/` entries for the selected task/run. |
| `metrics/` | Recent `.sdd/metrics/` entries for the same window. |
| `logs/` | Last 200 lines of each `.sdd/runtime/*.log`. |
| `source/` (optional) | The N most-recently-changed git-tracked files under `src/`, only when `--include-source-snippets` is set. |

## Redaction

Text artefacts go through `bernstein.core.security.redactor`, which:

- Blanks API keys, tokens, secrets, passwords, bearer headers, JWTs, SSH
  keys, and URL-embedded credentials.
- Collapses absolute paths under `$HOME` to `~`.
- Strips the values of environment variables whose names contain
  `KEY`/`TOKEN`/`SECRET`/`PASSWORD` from `NAME=value` / `NAME: value`
  text dumps.

The manifest records `redactions_applied`, a total count of substitutions
made across every text artefact in the bundle, so the operator sending the
bundle can see at a glance how many patterns were caught.

## Deprecated: `bernstein debug-bundle`

A separate, older entry point, `bernstein debug-bundle`, still exists
(`cli/commands/debug_cmd.py`). It runs an interactive confirmation prompt by
default and uses its own bundle builder
(`core/observability/debug_bundle.py`), which shares the same redaction
pattern set but has a smaller flag surface:

```bash
bernstein debug-bundle                # prompts for confirmation
bernstein debug-bundle --yes          # skip the prompt
bernstein debug-bundle --output FILE  # explicit output path
bernstein debug-bundle --extended     # include full (untruncated) logs
```

It does not support task/run filtering or `--include-source-snippets`.
`bernstein debug bundle` (the group command above) is the actively developed
path. The flat `debug-bundle` spelling prints a deprecation warning on stderr
and is removed in v4.0.0 (#3138).

Migrating is not a plain rename, because the two are different builders:

| `debug-bundle` | `debug bundle` |
|---|---|
| `--output FILE` | `--out FILE` |
| `--yes` | not needed; there is no confirmation prompt |
| `--extended` | no equivalent |

The deprecation warning names those three differences, so a script that
migrates blind does not lose a flag silently.

### The ZIP is not interchangeable either

The two builders lay their archives out differently, so any tooling that reads
entries out of the ZIP by path has to be updated as well as the command name.
Nothing in the CLI can detect that for you: swapping the command succeeds and
produces a valid bundle with different member paths.

| `debug-bundle` (legacy) | `debug bundle` (current) |
|---|---|
| Everything under a `bernstein-debug-<timestamp>/` top-level directory | Everything at the archive root |
| no manifest member | `manifest.json` |
| `config/bernstein.yaml` | `bernstein.yaml` |
| `diagnostics/*` (git status, worktree list, disk space) | `doctor.json` |
| `state/tasks.jsonl`, `state/archive_tail.jsonl`, `state/runtime_summary.json` | `traces/*`, `metrics/*` |
| `logs/*` | `logs/*` |
| `bernstein_version.txt`, `platform.txt`, `README.md` | folded into `manifest.json` |
| — | `source/*` with `--include-source-snippets N` |

Both report a redaction count. The legacy builder prints it into the archive's
`README.md`; the current path writes it into `manifest.json` as
`redactions_applied`.

## Source

- `src/bernstein/cli/debug_bundle.py` — `bernstein debug bundle`, manifest schema, selection logic, ZIP assembly
- `src/bernstein/cli/commands/debug_cmd.py` — legacy `bernstein debug-bundle` alias
- `src/bernstein/core/observability/debug_bundle.py` — legacy bundle builder and shared redaction pattern set
- `src/bernstein/core/security/redactor.py` — text redaction wrapper used by the current bundle path
