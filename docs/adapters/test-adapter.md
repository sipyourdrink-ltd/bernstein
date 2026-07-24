# Adapter smoke test (`bernstein test-adapter`)

`bernstein test-adapter` spawns a single headless run of one CLI adapter
against a one-off task, waits for it to exit, and prints the outcome. It is
the manual, on-demand check an operator runs after installing or upgrading
an adapter binary — a quick "does this adapter even work" probe, distinct
from the automated nightly [adapter conformance canary](conformance-canary.md)
that CI runs against every primary adapter.

## How to use it

```
bernstein test-adapter --adapter <name> --task "<prompt>" [--model MODEL] [--timeout SECONDS]
```

```
bernstein test-adapter --adapter gemini --task "Create a file named ok.txt containing OK"
bernstein test-adapter --adapter codex --task "List the files in this directory" --timeout 60
```

| Flag | Default | Meaning |
|---|---|---|
| `--adapter` | required | Adapter registry name (e.g. `gemini`, `codex`, `claude`). |
| `--task` | required | Prompt/task text for the adapter to execute. |
| `--model` | adapter-specific default | Model to use for the smoke run. Falls back to a built-in per-adapter default (e.g. `sonnet` for `claude`/`aider`/`amp`/`cursor`, `gpt-5.4-mini` for `codex`/`opencode`, `gemini-3-flash` for `gemini`) when omitted. |
| `--timeout` | `120` | Seconds to wait for the process to exit before killing it. |

## What it does

1. Resolves the adapter from the registry (`bernstein.adapters.registry.get_adapter`).
2. Creates a throwaway worktree at `.sdd/worktrees/test-<adapter>-<unix-ts>/`.
3. Spawns the adapter directly (`adapter.spawn(...)`, no task server or
   orchestrator involved) with the given prompt and model.
4. Waits up to `--timeout` seconds for the process to exit; on timeout it
   kills the process and reports `timed out`.
5. Prints the exit code and the last 40 lines of the adapter's log file.
6. Runs a heuristic check: if the prompt mentions a file or path, checks
   whether that path now exists in the worktree and prints a
   `✓`/`✗` line.
7. Always tears down the throwaway worktree afterward, even on error.

This runs entirely locally against the installed CLI binary — it does not
require `bernstein serve` or a running task server, and it does not touch
the project's real working tree (the run happens in its own worktree under
`.sdd/worktrees/`, deleted on exit).

## When to use it

- After installing or upgrading an adapter's upstream CLI binary, to catch
  a broken PATH entry, missing auth, or an incompatible CLI flag before
  relying on it in a real run.
- To validate MCP/protocol support for an adapter before a production run
  (see `reference/KNOWN_LIMITATIONS.md`).
- As a faster, local alternative to waiting for the nightly conformance
  canary when debugging one adapter interactively.

## Limitations

- Single adapter, single task, single process — it is not a suite. For
  breadth across many adapters on a schedule, see the
  [conformance canary](conformance-canary.md) or `bernstein adapters check`.
- The "expected file exists" check is a regex heuristic over the prompt
  text; it does not validate the file's contents.
- A `running` result (adapter did not return a waitable process handle) is
  possible for adapters whose `spawn()` does not expose a standard process
  handle; this is reported as a warning rather than a hard failure.

## Source

- `src/bernstein/cli/commands/adapter_cmd.py` — `test_adapter` command.
- `src/bernstein/adapters/registry.py` — `get_adapter`.
- `src/bernstein/adapters/base.py` — `CLIAdapter.cancel_timeout`.
